"""Take-profit close flow for Snowball."""

from __future__ import annotations

from dataclasses import dataclass

from core import Metadata, Tick

from snowball.config import SnowballConfig
from snowball.enums import CloseReason, EntryRole
from snowball.events import SnowballEvent
from snowball.models.grid import Layer, Slot
from snowball.models.state import Cycle
from snowball.services.flows.event_factory import SnowballEventFactory
from snowball.services.market_pricing import SnowballMarketPricing


@dataclass(frozen=True, slots=True)
class SnowballTakeProfitCloseService:
    """Close entries whose take-profit was hit."""

    config: SnowballConfig
    pricing: SnowballMarketPricing
    event_factory: SnowballEventFactory

    def process_counter_take_profits(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Close counter or layer-initial entries whose take-profit was hit."""
        events: list[SnowballEvent] = []
        while True:
            candidate = self._next_counter_take_profit_candidate(cycle=cycle, tick=tick)
            if candidate is None:
                break
            layer, slot = candidate
            entry = slot.filled_entry
            retracement_count = layer.retracement_count(slot)
            role = cycle.grid.role_for(layer, slot)
            if entry is None:
                break
            exit_price = self.pricing.exit_side_price(cycle.direction, tick)
            refillable_counter = (
                role == EntryRole.COUNTER
                and retracement_count <= self.config.grid.max_refillable_counter_retracement
            )
            close_reason = (
                CloseReason.LAYER_INITIAL_TAKE_PROFIT
                if role == EntryRole.LAYER_INITIAL
                else CloseReason.COUNTER_TAKE_PROFIT
            )
            slot.request_close(
                planned_at=tick.timestamp,
                planned_exit_price=exit_price,
                close_reason=close_reason,
                refillable=refillable_counter,
            )
            realized = self.pricing.realized_pl(
                direction=cycle.direction,
                entry=entry,
                exit_price=exit_price,
            )
            events.append(
                self.event_factory.close_event(
                    cycle=cycle,
                    entry=entry,
                    price=exit_price,
                    close_reason=close_reason,
                    metadata=Metadata.of(realized_pl=str(realized)),
                )
            )
        if events:
            cycle.refresh_status()
        return events

    def process_cycle_take_profit(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Close the cycle head when the cycle take-profit is hit."""
        layer = cycle.grid.first_layer
        slot = layer.r0
        entry = slot.filled_entry
        if entry is None or not self.pricing.take_profit_hit(
            direction=cycle.direction,
            entry=entry,
            tick=tick,
        ):
            return []
        if cycle.counter_entries():
            return []

        exit_price = self.pricing.exit_side_price(cycle.direction, tick)
        slot.request_close(
            planned_at=tick.timestamp,
            planned_exit_price=exit_price,
            close_reason=CloseReason.TAKE_PROFIT,
            refillable=False,
        )
        realized = self.pricing.realized_pl(
            direction=cycle.direction,
            entry=entry,
            exit_price=exit_price,
        )
        cycle.refresh_status()
        return [
            self.event_factory.close_event(
                cycle=cycle,
                entry=entry,
                price=exit_price,
                close_reason=CloseReason.TAKE_PROFIT,
                metadata=Metadata.of(realized_pl=str(realized)),
            )
        ]

    def _next_counter_take_profit_candidate(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> tuple[Layer, Slot] | None:
        head = cycle.head()
        for layer in cycle.grid.reversed_layers():
            live_count = layer.query.live_entry_count()
            for slot in layer.reversed_slots():
                entry = slot.filled_entry
                if entry is None or entry is head:
                    continue
                if cycle.grid.role_for(layer, slot) == EntryRole.LAYER_INITIAL and live_count > 1:
                    continue
                if self.pricing.take_profit_hit(
                    direction=cycle.direction,
                    entry=entry,
                    tick=tick,
                ):
                    return layer, slot
        return None
