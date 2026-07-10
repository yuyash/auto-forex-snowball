"""Requested-entry service for Snowball."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money, PositionSide, Tick

from snowball.enums import EntryRole
from snowball.models.entries import FilledEntry, FilledStopLossEntry, RequestedEntry
from snowball.models.grid import Grid, Layer, Slot
from snowball.models.identifiers import EntryId
from snowball.services.market_pricing import SnowballMarketPricing
from snowball.services.policies.position_sizing import SnowballPositionSizer
from snowball.services.policies.stop_loss import SnowballStopLossPlanner
from snowball.services.policies.take_profit import SnowballTakeProfitPlanner


@dataclass(frozen=True, slots=True)
class SnowballEntryService:
    """Create requested entries from grid position and market context."""

    pricing: SnowballMarketPricing
    position_sizer: SnowballPositionSizer
    take_profit_planner: SnowballTakeProfitPlanner
    stop_loss_planner: SnowballStopLossPlanner

    def create_initial_entry(
        self,
        *,
        entry_id: EntryId,
        tick: Tick,
        direction: PositionSide,
        grid: Grid,
        layer: Layer,
        slot: Slot,
    ) -> RequestedEntry:
        """Create the R0 requested entry for a new cycle."""
        pip_size = tick.instrument.pip_size
        entry_price = self.pricing.entry_side_price(direction, tick)
        return self._requested_entry(
            entry_id=entry_id,
            tick=tick,
            direction=direction,
            role=grid.role_for(layer, slot),
            layer=layer,
            retracement_count=layer.retracement_count(slot),
            entry_price=entry_price,
            take_profit_price=self.take_profit_planner.cycle_take_profit_price(
                direction=direction,
                entry_price=entry_price,
                pip_size=pip_size,
            ),
        )

    def create_counter_entry(
        self,
        *,
        entry_id: EntryId,
        tick: Tick,
        direction: PositionSide,
        grid: Grid,
        layer: Layer,
        slot: Slot,
        weighted_average_head: FilledEntry | None,
    ) -> RequestedEntry:
        """Create a requested R1+ counter entry."""
        pip_size = tick.instrument.pip_size
        retracement_count = layer.retracement_count(slot)
        role = grid.role_for(layer, slot)
        entry_price = self.pricing.entry_side_price(direction, tick)
        units = self.position_sizer.entry_units(
            role=role,
            layer=layer,
            retracement_count=retracement_count,
        )
        include_head = self.take_profit_planner.counter_weighted_average_head(
            layer=layer,
            cycle_head=weighted_average_head,
        )
        return self._requested_entry(
            entry_id=entry_id,
            tick=tick,
            direction=direction,
            role=role,
            layer=layer,
            retracement_count=retracement_count,
            entry_price=entry_price,
            units=units,
            take_profit_price=self.take_profit_planner.counter_take_profit_price(
                layer=layer,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                units=units,
                pip_size=pip_size,
                include_head=include_head,
            ),
        )

    def create_layer_initial_entry(
        self,
        *,
        entry_id: EntryId,
        tick: Tick,
        direction: PositionSide,
        previous_layer: Layer,
        layer: Layer,
        slot: Slot,
        entry_price: Money | None = None,
    ) -> RequestedEntry:
        """Create an L2+ R0 entry whose TP is bounded by the previous layer."""
        pip_size = tick.instrument.pip_size
        planned_entry_price = (
            self.pricing.entry_side_price(direction, tick)
            if entry_price is None
            else entry_price
        )
        retracement_count = layer.retracement_count(slot)
        return self._requested_entry(
            entry_id=entry_id,
            tick=tick,
            direction=direction,
            role=EntryRole.LAYER_INITIAL,
            layer=layer,
            retracement_count=retracement_count,
            entry_price=planned_entry_price,
            take_profit_price=self.take_profit_planner.layer_initial_take_profit_price(
                new_price=planned_entry_price,
                previous_layer=previous_layer,
                direction=direction,
                pip_size=pip_size,
            ),
        )

    def create_rebuild_entry(
        self,
        *,
        entry_id: EntryId,
        tick: Tick,
        direction: PositionSide,
        grid: Grid,
        layer: Layer,
        slot: Slot,
        rebuild_source: FilledStopLossEntry,
        entry_price: Money,
        take_profit_price: Money,
    ) -> RequestedEntry:
        """Create a requested entry replacing a filled stop-loss entry."""
        retracement_count = layer.retracement_count(slot)
        role = grid.role_for(layer, slot)
        return self._requested_entry(
            entry_id=entry_id,
            tick=tick,
            direction=direction,
            role=role,
            layer=layer,
            retracement_count=retracement_count,
            entry_price=entry_price,
            rebuild_source=rebuild_source,
            take_profit_price=take_profit_price,
        )

    def _requested_entry(
        self,
        *,
        entry_id: EntryId,
        tick: Tick,
        direction: PositionSide,
        role: EntryRole,
        layer: Layer,
        retracement_count: int,
        entry_price: Money,
        take_profit_price: Money,
        rebuild_source: FilledStopLossEntry | None = None,
        units: Decimal | None = None,
    ) -> RequestedEntry:
        requested_units = (
            self.position_sizer.entry_units(
                role=role,
                layer=layer,
                retracement_count=retracement_count,
            )
            if units is None
            else units
        )
        return RequestedEntry(
            entry_id=entry_id,
            requested_units=requested_units,
            requested_entry_price=entry_price,
            requested_at=tick.timestamp,
            planned_take_profit_price=take_profit_price,
            planned_stop_loss_price=self.stop_loss_planner.entry_stop_loss_price(
                tick=tick,
                direction=direction,
                entry_price=entry_price,
                take_profit_price=take_profit_price,
                retracement_count=retracement_count,
                rebuild_source=rebuild_source,
            ),
        )
