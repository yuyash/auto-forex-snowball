"""Protection and shrink flows for Snowball."""

from __future__ import annotations

from dataclasses import dataclass

from core import Metadata, Percent, Pips, Tick

from snowball.config import SnowballConfig
from snowball.enums import CloseReason
from snowball.events import SnowballEvent, SnowballStatusEvent, SnowballStopEvent
from snowball.models.entries import FilledEntry
from snowball.models.grid import Layer, Slot
from snowball.models.state import Cycle, SnowballState
from snowball.services.accounting import AccountSnapshot, SnowballAccounting
from snowball.services.flows.event_factory import SnowballEventFactory
from snowball.services.market_pricing import SnowballMarketPricing
from snowball.services.selectors.grid import SnowballGridSelector


@dataclass(frozen=True, slots=True)
class SnowballProtectionService:
    """Apply Snowball margin protection rules."""

    config: SnowballConfig
    pricing: SnowballMarketPricing
    accounting: SnowballAccounting
    grid_selector: SnowballGridSelector
    event_factory: SnowballEventFactory

    def handle_emergency(self, *, margin_ratio: Percent) -> SnowballStopEvent | None:
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
            slot.request_close(
                planned_at=tick.timestamp,
                planned_exit_price=exit_price,
                close_reason=CloseReason.SHRINK,
                refillable=False,
            )
            cycle.refresh_status()
            events.append(
                self.event_factory.close_event(
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

    def _shrink_target(
        self,
        *,
        state: SnowballState,
        tick: Tick,
    ) -> tuple[Cycle, Layer, Slot, FilledEntry] | None:
        pip_size = tick.instrument.pip_size
        candidates: list[tuple[Pips, Cycle, Layer, Slot, FilledEntry]] = []
        for cycle in state.iter_active_cycles():
            entry = self.grid_selector.shrink_front_entry(cycle)
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
