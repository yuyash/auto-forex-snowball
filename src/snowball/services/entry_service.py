"""Requested-entry service for Snowball."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money, PositionSide, Tick

from snowball.config import SnowballConfig
from snowball.enums import CounterTakeProfitMode, EntryRole
from snowball.models.entries import FilledEntry, FilledStopLossEntry, RequestedEntry
from snowball.models.grid import Grid, Layer, Slot
from snowball.models.identifiers import EntryId
from snowball.services.calculators import SnowballCalculator
from snowball.services.pricing import SnowballPricing


@dataclass(frozen=True, slots=True)
class SnowballEntryService:
    """Create requested entries from grid position and market context."""

    config: SnowballConfig
    calculator: SnowballCalculator
    pricing: SnowballPricing

    def create_entry(
        self,
        *,
        entry_id: EntryId,
        tick: Tick,
        direction: PositionSide,
        grid: Grid,
        layer: Layer,
        slot: Slot,
        rebuild_source: FilledStopLossEntry | None = None,
        requested_entry_price: Money | None = None,
        weighted_average_head: FilledEntry | None = None,
    ) -> RequestedEntry:
        """Create a requested entry for an existing grid slot."""
        role = grid.role_for(layer, slot)
        retracement_count = layer.retracement_count(slot)
        pip_size = tick.instrument.pip_size
        entry_price = requested_entry_price or self.pricing.entry_side_price(direction, tick)
        units = self._entry_units(role=role, layer=layer, retracement_count=retracement_count)

        if role == EntryRole.COUNTER:
            include_head = self._counter_weighted_average_head(
                layer=layer,
                cycle_head=weighted_average_head,
            )
            take_profit_price = self.pricing.counter_take_profit_price(
                layer=layer,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                units=units,
                pip_size=pip_size,
                calculator=self.calculator,
                include_head=include_head,
            )
        elif rebuild_source is not None:
            take_profit_price = self.pricing.rebuild_take_profit_price(
                stop_loss_entry=rebuild_source,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                pip_size=pip_size,
                calculator=self.calculator,
            )
        else:
            take_profit_price = self.pricing.take_profit_price(
                direction=direction,
                entry_price=entry_price,
                tp_pips=self.config.cycle.take_profit_pips,
                pip_size=pip_size,
            )

        return RequestedEntry(
            entry_id=entry_id,
            requested_units=units,
            requested_entry_price=entry_price,
            requested_at=tick.timestamp,
            planned_take_profit_price=take_profit_price,
            planned_stop_loss_price=self._stop_loss_price(
                tick=tick,
                direction=direction,
                entry_price=entry_price,
                retracement_count=retracement_count,
                rebuild_source=rebuild_source,
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
    ) -> RequestedEntry:
        """Create an L2+ R0 entry whose TP is bounded by the previous layer."""
        pip_size = tick.instrument.pip_size
        entry_price = self.pricing.entry_side_price(direction, tick)
        retracement_count = layer.retracement_count(slot)
        return RequestedEntry(
            entry_id=entry_id,
            requested_units=self.config.sizing.initial_entry_units_multiplier * layer.base_units,
            requested_entry_price=entry_price,
            requested_at=tick.timestamp,
            planned_take_profit_price=self.pricing.layer_initial_take_profit_price(
                new_price=entry_price,
                previous_layer=previous_layer,
                direction=direction,
                pip_size=pip_size,
                take_profit_pips=self.config.cycle.take_profit_pips,
            ),
            planned_stop_loss_price=self._stop_loss_price(
                tick=tick,
                direction=direction,
                entry_price=entry_price,
                retracement_count=retracement_count,
                rebuild_source=None,
            ),
        )

    def _stop_loss_price(
        self,
        *,
        tick: Tick,
        direction: PositionSide,
        entry_price: Money,
        retracement_count: int,
        rebuild_source: FilledStopLossEntry | None,
    ) -> Money | None:
        pip_size = tick.instrument.pip_size
        if not self.config.stop_loss.enabled:
            return None
        if rebuild_source is not None:
            return self.pricing.rebuild_stop_loss_price(
                stop_loss_entry=rebuild_source,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                pip_size=pip_size,
                calculator=self.calculator,
            )
        stop_loss_pips = self.calculator.stop_loss_pips(retracement_count + 1)
        return self.pricing.stop_loss_price(
            direction=direction,
            entry_price=entry_price,
            stop_loss_pips=stop_loss_pips,
            pip_size=pip_size,
        )

    def _entry_units(
        self,
        *,
        role: EntryRole,
        layer: Layer,
        retracement_count: int,
    ) -> Decimal:
        if role == EntryRole.COUNTER:
            return Decimal(retracement_count + 1) * layer.base_units
        return self.config.sizing.initial_entry_units_multiplier * layer.base_units

    def _counter_weighted_average_head(
        self,
        *,
        layer: Layer,
        cycle_head: FilledEntry | None,
    ) -> FilledEntry | None:
        if self.config.counter.take_profit.mode != CounterTakeProfitMode.WEIGHTED_AVG:
            return None
        if layer.r0.is_present:
            return None
        return cycle_head
