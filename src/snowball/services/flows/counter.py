"""Counter-entry opening flow for Snowball."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

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
from snowball.services.selectors.grid import SnowballGridSelector


@dataclass(frozen=True, slots=True)
class SnowballCounterService:
    """Open counter and next-layer entries when grid distance rules are met."""

    config: SnowballConfig
    calculator: SnowballCalculator
    pricing: SnowballMarketPricing
    entry_service: SnowballEntryService
    grid_selector: SnowballGridSelector
    event_factory: SnowballEventFactory
    distance_rule: CounterDistanceRule = field(init=False)
    counter_opening: CounterSlotOpening = field(init=False)
    next_layer_opening: NextLayerOpening = field(init=False)

    def __post_init__(self) -> None:
        distance_rule = CounterDistanceRule(pricing=self.pricing)
        object.__setattr__(self, "distance_rule", distance_rule)
        object.__setattr__(
            self,
            "counter_opening",
            CounterSlotOpening(
                calculator=self.calculator,
                pricing=self.pricing,
                entry_service=self.entry_service,
                grid_selector=self.grid_selector,
                event_factory=self.event_factory,
            ),
        )
        object.__setattr__(
            self,
            "next_layer_opening",
            NextLayerOpening(
                config=self.config,
                calculator=self.calculator,
                pricing=self.pricing,
                entry_service=self.entry_service,
                event_factory=self.event_factory,
            ),
        )

    def process_counter_adds(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Open at most one counter or next-layer entry for this cycle on the tick."""
        event = self.next_counter_or_layer_event(cycle=cycle, tick=tick)
        if event is None:
            return []
        return [event]

    def next_counter_or_layer_event(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> SnowballEvent | None:
        head = self.grid_selector.effective_head(cycle)
        if head is None:
            return None
        current_entry_price = self.pricing.entry_side_price(cycle.direction, tick)
        if not self.distance_rule.is_losing_reference(
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
            return self.counter_opening.try_open(
                cycle=cycle,
                tick=tick,
                layer=layer,
                slot=slot,
                current_entry_price=current_entry_price,
            )
        if cycle.grid.layer_count >= self.config.grid.max_layers:
            return None
        return self.next_layer_opening.try_open(
            cycle=cycle,
            tick=tick,
            current_entry_price=current_entry_price,
        )


@dataclass(frozen=True, slots=True)
class CounterDistanceRule:
    """Evaluate counter-entry distance and direction rules."""

    pricing: SnowballMarketPricing

    def is_losing_reference(
        self,
        *,
        direction: PositionSide,
        reference: FilledEntry,
        current_entry_price: Money,
    ) -> bool:
        """Return whether current price is adverse to the reference entry."""
        if direction == PositionSide.LONG:
            return current_entry_price < reference.filled_entry_price
        return current_entry_price > reference.filled_entry_price

    def adverse_pips(
        self,
        *,
        direction: PositionSide,
        reference_price: Money,
        current_entry_price: Money,
        pip_size: Decimal,
    ) -> Decimal:
        """Return adverse distance in pips."""
        return self.pricing.adverse_pips(
            direction=direction,
            reference_price=reference_price,
            current_entry_price=current_entry_price,
            pip_size=pip_size,
        )


@dataclass(frozen=True, slots=True)
class CounterSlotOpening:
    """Open a counter entry inside the current layer."""

    calculator: SnowballCalculator
    pricing: SnowballMarketPricing
    entry_service: SnowballEntryService
    grid_selector: SnowballGridSelector
    event_factory: SnowballEventFactory

    def try_open(
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
        entry_id = cycle.next_entry_id(layer=layer, slot=slot)
        entry = self.entry_service.create_counter_entry(
            entry_id=entry_id,
            tick=tick,
            direction=cycle.direction,
            grid=cycle.grid,
            layer=layer,
            slot=slot,
            weighted_average_head=cycle.head(),
        )
        slot.place_entry(entry, expected_entry_id=entry_id)
        cycle.refresh_status()
        return self.event_factory.open_event(
            cycle=cycle,
            entry=entry,
            metadata=Metadata.of(
                expected_interval_pips=str(interval),
                actual_interval_pips=str(adverse),
            ),
        )


@dataclass(frozen=True, slots=True)
class NextLayerEntryPrice:
    """Project the planned entry price for the first slot of a new layer."""

    def project(
        self,
        *,
        direction: PositionSide,
        reference: Money,
        interval_pips: Decimal,
        pip_size: Decimal,
    ) -> Money:
        interval_amount = interval_pips * pip_size
        if direction == PositionSide.LONG:
            return Money.of(reference.amount - interval_amount, reference.currency)
        return Money.of(reference.amount + interval_amount, reference.currency)


@dataclass(frozen=True, slots=True)
class NextLayerOpening:
    """Open the first counter slot of the next layer."""

    config: SnowballConfig
    calculator: SnowballCalculator
    pricing: SnowballMarketPricing
    entry_service: SnowballEntryService
    event_factory: SnowballEventFactory
    entry_price: NextLayerEntryPrice = field(default_factory=NextLayerEntryPrice)

    def try_open(
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

        planned_entry_price = self.entry_price.project(
            direction=cycle.direction,
            reference=reference,
            interval_pips=interval,
            pip_size=pip_size,
        )
        previous_layer = cycle.grid.current_layer
        layer = cycle.grid.add_layer(
            base_units=self.config.sizing.layer_base_units(cycle.grid.layer_count + 1),
            max_retracements=self.config.grid.max_retracements_per_layer,
        )
        slot = layer.r0
        entry_id = cycle.next_entry_id(layer=layer, slot=slot)
        requested_entry = self.entry_service.create_layer_initial_entry(
            entry_id=entry_id,
            tick=tick,
            direction=cycle.direction,
            previous_layer=previous_layer,
            layer=layer,
            slot=slot,
            entry_price=planned_entry_price,
        )
        slot.place_entry(requested_entry, expected_entry_id=entry_id)
        cycle.refresh_status()
        return self.event_factory.open_event(
            cycle=cycle,
            entry=requested_entry,
            metadata=Metadata.of(
                expected_interval_pips=str(interval),
                actual_interval_pips=str(adverse),
            ),
        )
