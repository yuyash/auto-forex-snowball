"""Stop-loss close flow for Snowball."""

from __future__ import annotations

from dataclasses import dataclass

from core import Metadata, Tick

from snowball.config import SnowballConfig
from snowball.enums import CloseReason
from snowball.events import SnowballEvent
from snowball.models.state import Cycle
from snowball.services.flows.event_factory import SnowballEventFactory
from snowball.services.market_pricing import SnowballMarketPricing
from snowball.services.policies.stop_loss import SnowballStopLossPlanner


@dataclass(frozen=True, slots=True)
class SnowballStopLossCloseService:
    """Close entries whose stop-loss was hit."""

    config: SnowballConfig
    pricing: SnowballMarketPricing
    stop_loss_planner: SnowballStopLossPlanner
    event_factory: SnowballEventFactory

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
                if self.stop_loss_planner.stop_loss_temporarily_protected(
                    layer=layer,
                    slot=slot,
                    highest=highest,
                ):
                    continue
                planned_stop_loss_price = entry.planned_stop_loss_price
                if planned_stop_loss_price is None:
                    continue
                slot.request_stop_loss(
                    planned_at=tick.timestamp,
                    planned_stop_loss_price=planned_stop_loss_price,
                )
                exit_price = self.pricing.exit_side_price(cycle.direction, tick)
                realized = self.pricing.realized_pl(
                    direction=cycle.direction,
                    entry=entry,
                    exit_price=exit_price,
                )
                planned_rebuild_price = (
                    self.stop_loss_planner.planned_rebuild_price(
                        direction=cycle.direction,
                        original_entry_price=entry.filled_entry_price,
                        planned_stop_loss_price=planned_stop_loss_price,
                        stop_loss_exit_price=exit_price,
                        pip_size=pip_size,
                    )
                    if self.config.rebuild.enabled
                    else None
                )
                events.append(
                    self.event_factory.close_event(
                        cycle=cycle,
                        entry=entry,
                        price=exit_price,
                        close_reason=CloseReason.STOP_LOSS,
                        metadata=Metadata.of(
                            realized_pl=str(realized),
                            rebuildable=self.config.rebuild.enabled,
                            planned_rebuild_price=(
                                None
                                if planned_rebuild_price is None
                                else str(planned_rebuild_price)
                            ),
                        ),
                    )
                )
        cycle.refresh_status()
        return events
