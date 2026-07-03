"""Close, stop-loss, and protection flows for Snowball."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Metadata, Money, Tick

from snowball.config import SnowballConfig
from snowball.enums import CloseReason, CounterTakeProfitMode, EntryRole
from snowball.events import (
    SnowballCloseEvent,
    SnowballEvent,
    SnowballStatusEvent,
    SnowballStopEvent,
)
from snowball.models.entries import FilledEntry
from snowball.models.grid import Layer, Slot
from snowball.models.state import Cycle, SnowballState
from snowball.services.accounting import AccountSnapshot, SnowballAccounting
from snowball.services.pricing import SnowballPricing


@dataclass(frozen=True, slots=True)
class SnowballCloseService:
    """Apply close-side Snowball transitions and emit close events."""

    config: SnowballConfig
    pricing: SnowballPricing
    accounting: SnowballAccounting

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
            slot.close_for_take_profit(
                closed_at=tick.timestamp,
                refillable=refillable_counter,
            )
            realized = self.pricing.realized_pl(
                direction=cycle.direction,
                entry=entry,
                exit_price=exit_price,
            )
            if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
                self.pricing.sync_weighted_average_take_profits(layer)
            close_reason = (
                CloseReason.LAYER_INITIAL_TAKE_PROFIT
                if role == EntryRole.LAYER_INITIAL
                else CloseReason.COUNTER_TAKE_PROFIT
            )
            events.append(
                self._close_event(
                    cycle=cycle,
                    entry=entry,
                    price=exit_price,
                    close_reason=close_reason,
                    metadata=Metadata.of(realized_pl=str(realized)),
                )
            )
            cycle.refresh_status()
        return events

    def process_cycle_take_profit(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Close the cycle head when the cycle take-profit is hit."""
        layer = cycle.grid.layers[0]
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
        slot.close_for_take_profit(closed_at=tick.timestamp, refillable=False)
        realized = self.pricing.realized_pl(
            direction=cycle.direction,
            entry=entry,
            exit_price=exit_price,
        )
        cycle.refresh_status()
        return [
            self._close_event(
                cycle=cycle,
                entry=entry,
                price=exit_price,
                close_reason=CloseReason.TAKE_PROFIT,
                metadata=Metadata.of(realized_pl=str(realized)),
            )
        ]

    def process_stop_losses(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Close entries whose stop-loss was hit."""
        if not self.config.stop_loss.enabled:
            return []
        pip_size = tick.instrument.pip_size
        events: list[SnowballEvent] = []
        for layer in list(reversed(cycle.grid.layers)):
            highest = layer.highest_live_slot()
            for slot in list(reversed(layer.slots)):
                entry = slot.filled_entry
                if entry is None or not self.pricing.stop_loss_hit(
                    direction=cycle.direction,
                    entry=entry,
                    tick=tick,
                ):
                    continue
                if self._stop_loss_temporarily_protected(layer=layer, slot=slot, highest=highest):
                    continue
                requested_stop_loss_exit_price = entry.planned_stop_loss_price
                if requested_stop_loss_exit_price is None:
                    continue
                slot.request_stop_loss(
                    requested_at=tick.timestamp,
                    requested_stop_loss_exit_price=requested_stop_loss_exit_price,
                )
                exit_price = self.pricing.exit_side_price(cycle.direction, tick)
                realized = self.pricing.realized_pl(
                    direction=cycle.direction,
                    entry=entry,
                    exit_price=exit_price,
                )
                rebuild_trigger_price = (
                    self.pricing.rebuild_trigger_price(
                        direction=cycle.direction,
                        original_entry_price=entry.filled_entry_price,
                        stop_loss_exit_price=exit_price,
                        config=self.config,
                        pip_size=pip_size,
                    )
                    if self.config.rebuild.enabled
                    else None
                )
                slot.fill_stop_loss(
                    filled_at=tick.timestamp,
                    filled_stop_loss_exit_price=exit_price,
                    rebuildable=self.config.rebuild.enabled,
                    planned_rebuild_trigger_price=rebuild_trigger_price,
                )
                events.append(
                    self._close_event(
                        cycle=cycle,
                        entry=entry,
                        price=exit_price,
                        close_reason=CloseReason.STOP_LOSS,
                        metadata=Metadata.of(realized_pl=str(realized)),
                    )
                )
        cycle.refresh_status()
        return events

    def handle_emergency(self, *, margin_ratio: Decimal) -> SnowballStopEvent | None:
        """Return an emergency stop event when protection threshold is exceeded."""
        protection = self.config.protection
        if not protection.emergency_enabled or margin_ratio < protection.emergency_margin_percent:
            return None
        return SnowballStopEvent(
            message="Snowball emergency stop",
            metadata=Metadata.of(
                margin_ratio=str(margin_ratio),
                threshold=str(protection.emergency_margin_percent),
            ),
        )

    def handle_shrink(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        account: AccountSnapshot,
    ) -> list[SnowballEvent]:
        """Shrink positions until margin ratio falls below the target."""
        protection = self.config.protection
        if (
            not protection.shrink_enabled
            or account.margin_ratio < protection.shrink_start_margin_percent
        ):
            return []

        events: list[SnowballEvent] = [
            SnowballStatusEvent(
                message="Snowball shrink entered",
                metadata=Metadata.of(margin_ratio=str(account.margin_ratio)),
            )
        ]
        current_account = account
        while current_account.margin_ratio >= protection.shrink_target_margin_percent:
            target = self._shrink_target(state=state, tick=tick)
            if target is None:
                events.append(
                    SnowballStopEvent(
                        message="Snowball shrink exhausted",
                        metadata=Metadata.of(margin_ratio=str(current_account.margin_ratio)),
                    )
                )
                return events
            cycle, layer, slot, entry = target
            exit_price = self.pricing.exit_side_price(cycle.direction, tick)
            realized = self.pricing.realized_pl(
                direction=cycle.direction,
                entry=entry,
                exit_price=exit_price,
            )
            layer_number = cycle.grid.layer_number(layer)
            slot.close_for_take_profit(closed_at=tick.timestamp, refillable=False)
            cycle.refresh_status()
            events.append(
                self._close_event(
                    cycle=cycle,
                    entry=entry,
                    price=exit_price,
                    close_reason=CloseReason.SHRINK,
                    metadata=Metadata.of(
                        realized_pl=str(realized),
                        margin_ratio=str(current_account.margin_ratio),
                        layer_number=layer_number,
                    ),
                )
            )
            current_account = self.accounting.evaluate(
                state=state,
                tick=tick,
                config=self.config,
            )

        return events

    def _next_counter_take_profit_candidate(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> tuple[Layer, Slot] | None:
        head = cycle.head()
        for layer in reversed(cycle.grid.layers):
            live_count = len(layer.live_entries())
            for slot in reversed(layer.slots):
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

    def _stop_loss_temporarily_protected(
        self,
        *,
        layer: Layer,
        slot: Slot,
        highest: Slot | None,
    ) -> bool:
        if not self.config.stop_loss.protect_highest_retracement.enabled:
            return False
        if highest is None or highest.filled_entry is None or slot.filled_entry is None:
            return False
        protected_from = self.config.stop_loss.protect_highest_retracement.from_retracement
        if layer.retracement_count(slot) < protected_from:
            return False
        return highest is slot

    def _shrink_target(
        self,
        *,
        state: SnowballState,
        tick: Tick,
    ) -> tuple[Cycle, Layer, Slot, FilledEntry] | None:
        pip_size = tick.instrument.pip_size
        candidates: list[tuple[Decimal, Cycle, Layer, Slot, FilledEntry]] = []
        for cycle in state.active_cycles():
            entry = cycle.grid.shrink_front_entry()
            if entry is None or not self.pricing.can_close_on_tick(entry=entry, tick=tick):
                continue
            found = cycle.grid.find_entry_slot(entry)
            if found is None:
                continue
            layer, slot = found
            candidates.append(
                (
                    self.pricing.unrealized_loss_pips(
                        direction=cycle.direction,
                        entry=entry,
                        tick=tick,
                        pip_size=pip_size,
                    ),
                    cycle,
                    layer,
                    slot,
                    entry,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        _loss, cycle, layer, slot, entry = candidates[0]
        return cycle, layer, slot, entry

    def _close_event(
        self,
        *,
        cycle: Cycle,
        entry: FilledEntry,
        price: Money,
        close_reason: CloseReason,
        metadata: Metadata | None = None,
    ) -> SnowballCloseEvent:
        return SnowballCloseEvent(
            cycle_id=entry.entry_id.cycle_id,
            direction=cycle.direction,
            entry=entry,
            price=price,
            close_reason=close_reason,
            metadata=metadata or Metadata(),
        )
