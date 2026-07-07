"""Rebuild flow for stop-loss-closed Snowball entries."""

from __future__ import annotations

from dataclasses import dataclass, replace

from core import Metadata, Tick

from snowball.config import SnowballConfig
from snowball.events import SnowballEvent
from snowball.models.state import Cycle
from snowball.services.flows.entry import SnowballEntryService
from snowball.services.flows.event_factory import SnowballEventFactory
from snowball.services.market_pricing import SnowballMarketPricing
from snowball.services.policies.grid import SnowballGridPolicy
from snowball.services.policies.take_profit import SnowballTakeProfitPlanner


@dataclass(frozen=True, slots=True)
class SnowballRebuildService:
    """Rebuild entries that are waiting after a stop-loss fill."""

    config: SnowballConfig
    pricing: SnowballMarketPricing
    grid_policy: SnowballGridPolicy
    entry_service: SnowballEntryService
    take_profit_planner: SnowballTakeProfitPlanner
    event_factory: SnowballEventFactory

    def process_rebuilds(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Rebuild stop-loss entries whose trigger price was revisited."""
        if not self.config.stop_loss.enabled or not self.config.rebuild.enabled:
            return []
        events: list[SnowballEvent] = []
        for layer, slot in list(cycle.grid.filled_stop_loss_slots()):
            stop_loss_entry = slot.filled_stop_loss_entry
            if stop_loss_entry is None:
                continue
            if not self.pricing.rebuild_trigger_hit(
                stop_loss_entry=stop_loss_entry,
                direction=cycle.direction,
                tick=tick,
            ):
                continue
            raw_entry_price = self.pricing.entry_side_price(cycle.direction, tick)
            entry_price = self.grid_policy.clamp_entry_price(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                entry_price=raw_entry_price,
            )
            entry = self.entry_service.create_rebuild_entry(
                entry_id=cycle.next_entry_id(layer=layer, slot=slot),
                tick=tick,
                direction=cycle.direction,
                grid=cycle.grid,
                layer=layer,
                slot=slot,
                rebuild_source=stop_loss_entry,
                entry_price=entry_price,
            )
            take_profit_price = self.grid_policy.clamp_take_profit(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                take_profit_price=entry.planned_take_profit_price,
            )
            entry = replace(entry, planned_take_profit_price=take_profit_price)
            self.grid_policy.propagate_pending_take_profit(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                take_profit_price=take_profit_price,
            )
            slot.complete_rebuild(entry)
            self.take_profit_planner.sync_weighted_average_take_profits(layer)
            events.append(
                self.event_factory.open_event(
                    cycle=cycle,
                    entry=entry,
                    metadata=Metadata.of(is_rebuild=True),
                )
            )
        cycle.refresh_status()
        return events
