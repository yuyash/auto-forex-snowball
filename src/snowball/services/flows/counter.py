"""Counter-entry opening flow for Snowball."""

from __future__ import annotations

from dataclasses import dataclass

from core import Metadata, Money, PositionSide, Tick

from snowball.config import SnowballConfig
from snowball.events import SnowballEvent, SnowballOpenEvent
from snowball.models.entries import FilledEntry
from snowball.models.grid import Layer, Slot
from snowball.models.state import Cycle
from snowball.services.calculators import SnowballCalculator
from snowball.services.flows.entry import SnowballEntryService
from snowball.services.flows.event_factory import SnowballEventFactory
from snowball.services.market_pricing import SnowballMarketPricing
from snowball.services.policies.take_profit import SnowballTakeProfitPlanner
from snowball.services.selectors.grid import SnowballGridSelector


@dataclass(frozen=True, slots=True)
class SnowballCounterService:
    """Open counter and next-layer entries when grid distance rules are met."""

    config: SnowballConfig
    calculator: SnowballCalculator
    pricing: SnowballMarketPricing
    entry_service: SnowballEntryService
    grid_selector: SnowballGridSelector
    take_profit_planner: SnowballTakeProfitPlanner
    event_factory: SnowballEventFactory

    def process_counter_adds(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Open as many counter or next-layer entries as the current tick allows."""
        events: list[SnowballEvent] = []
        max_adds = self.config.grid.max_layers * (self.config.grid.max_retracements_per_layer + 1)
        for _ in range(max_adds):
            event = self._try_add_one_counter_or_layer(
                cycle=cycle,
                tick=tick,
            )
            if event is None:
                break
            events.append(event)
        return events

    def _try_add_one_counter_or_layer(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> SnowballEvent | None:
        head = self.grid_selector.effective_head(cycle)
        if head is None:
            return None
        current_entry_price = self.pricing.entry_side_price(cycle.direction, tick)
        if not self._is_losing_reference(
            direction=cycle.direction,
            reference=head,
            current_entry_price=current_entry_price,
        ):
            return None

        layer = cycle.grid.current_layer
        slot = self.grid_selector.next_available_counter_slot(
            layer=layer,
            max_refillable_retracement=self.config.grid.max_refillable_counter_retracement,
        )
        if slot is not None:
            return self._try_open_counter(
                cycle=cycle,
                tick=tick,
                layer=layer,
                slot=slot,
                current_entry_price=current_entry_price,
            )
        if len(cycle.grid.layers) >= self.config.grid.max_layers:
            return None
        return self._try_open_next_layer(
            cycle=cycle,
            tick=tick,
            current_entry_price=current_entry_price,
        )

    def _try_open_counter(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        layer: Layer,
        slot: Slot,
        current_entry_price: Money,
    ) -> SnowballOpenEvent | None:
        reference = self.grid_selector.counter_reference_price(
            layer=layer, retracement_count=layer.retracement_count(slot)
        )
        if reference is None:
            return None
        pip_size = tick.instrument.pip_size
        adverse = self.pricing.adverse_pips(
            direction=cycle.direction,
            reference_price=reference,
            current_entry_price=current_entry_price,
            pip_size=pip_size,
        )
        interval = self.calculator.counter_interval_pips(layer.retracement_count(slot))
        if adverse < interval:
            return None
        entry = self.entry_service.create_counter_entry(
            entry_id=cycle.next_entry_id(layer=layer, slot=slot),
            tick=tick,
            direction=cycle.direction,
            grid=cycle.grid,
            layer=layer,
            slot=slot,
            weighted_average_head=cycle.head(),
        )
        slot.place_entry(entry)
        self.take_profit_planner.sync_weighted_average_take_profits(layer)
        cycle.refresh_status()
        return self.event_factory.open_event(
            cycle=cycle,
            entry=entry,
            metadata=Metadata.of(
                expected_interval_pips=str(interval),
                actual_interval_pips=str(adverse),
            ),
        )

    def _try_open_next_layer(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        current_entry_price: Money,
    ) -> SnowballOpenEvent | None:
        tail = cycle.grid.tail_present_slot()
        if tail is None:
            return None
        tail_layer, tail_slot = tail
        reference = tail_slot.reference_entry_price()
        if reference is None:
            return None
        pip_size = tick.instrument.pip_size
        next_step = min(
            tail_layer.retracement_count(tail_slot) + 1,
            self.config.grid.max_retracements_per_layer,
        )
        adverse = self.pricing.adverse_pips(
            direction=cycle.direction,
            reference_price=reference,
            current_entry_price=current_entry_price,
            pip_size=pip_size,
        )
        interval = self.calculator.counter_interval_pips(next_step)
        if adverse < interval:
            return None

        previous_layer = cycle.grid.current_layer
        layer = cycle.grid.add_layer(
            base_units=self.config.sizing.layer_base_units(len(cycle.grid.layers) + 1),
            max_retracements=self.config.grid.max_retracements_per_layer,
        )
        slot = layer.r0
        requested_entry = self.entry_service.create_layer_initial_entry(
            entry_id=cycle.next_entry_id(layer=layer, slot=slot),
            tick=tick,
            direction=cycle.direction,
            previous_layer=previous_layer,
            layer=layer,
            slot=slot,
        )
        slot.place_entry(requested_entry)
        cycle.refresh_status()
        return self.event_factory.open_event(
            cycle=cycle,
            entry=requested_entry,
            metadata=Metadata.of(
                expected_interval_pips=str(interval),
                actual_interval_pips=str(adverse),
            ),
        )

    def _is_losing_reference(
        self,
        *,
        direction: PositionSide,
        reference: FilledEntry,
        current_entry_price: Money,
    ) -> bool:
        if direction == PositionSide.LONG:
            return current_entry_price < reference.filled_entry_price
        return current_entry_price > reference.filled_entry_price
