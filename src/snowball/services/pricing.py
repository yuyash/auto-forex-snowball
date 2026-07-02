"""Price calculations for Snowball."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money, PositionSide, Tick

from snowball.config import SnowballConfig
from snowball.enums import (
    CounterTakeProfitMode,
    RebuildEntryPriceMode,
    RebuildStopLossMode,
    RebuildTakeProfitMode,
)
from snowball.models.entries import Entry, PendingRebuild
from snowball.models.grid import Layer
from snowball.services.calculators import SnowballCalculator


@dataclass(frozen=True, slots=True)
class SnowballPricing:
    """Own executable price, P/L, and TP/SL price calculations."""

    def entry_side_price(self, direction: PositionSide, tick: Tick) -> Money:
        """Return the executable entry-side price for a direction."""
        return tick.ask if direction == PositionSide.LONG else tick.bid

    def exit_side_price(self, direction: PositionSide, tick: Tick) -> Money:
        """Return the executable exit-side price for a direction."""
        return tick.bid if direction == PositionSide.LONG else tick.ask

    def take_profit_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        tp_pips: Decimal,
        pip_size: Decimal,
    ) -> Money:
        """Return a take-profit price from pips."""
        if direction == PositionSide.LONG:
            amount = entry_price.amount + tp_pips * pip_size
        else:
            amount = entry_price.amount - tp_pips * pip_size
        return Money.of(amount, entry_price.currency)

    def stop_loss_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        stop_loss_pips: Decimal,
        pip_size: Decimal,
    ) -> Money:
        """Return a stop-loss price from pips."""
        if direction == PositionSide.LONG:
            amount = entry_price.amount - stop_loss_pips * pip_size
        else:
            amount = entry_price.amount + stop_loss_pips * pip_size
        return Money.of(amount, entry_price.currency)

    def adverse_pips(
        self,
        *,
        direction: PositionSide,
        reference_price: Money,
        current_entry_price: Money,
        pip_size: Decimal,
    ) -> Decimal:
        """Return adverse movement from reference to current price."""
        if direction == PositionSide.LONG:
            return (reference_price.amount - current_entry_price.amount) / pip_size
        return (current_entry_price.amount - reference_price.amount) / pip_size

    def can_close_on_tick(self, *, entry: Entry, tick: Tick) -> bool:
        """Return True when the entry was opened before this tick."""
        return tick.timestamp > entry.opened_at

    def take_profit_hit(
        self,
        *,
        direction: PositionSide,
        entry: Entry,
        tick: Tick,
    ) -> bool:
        """Return True when the take-profit is reachable on this tick."""
        if not self.can_close_on_tick(entry=entry, tick=tick):
            return False
        if direction == PositionSide.LONG:
            return tick.bid >= entry.take_profit_price
        return tick.ask <= entry.take_profit_price

    def stop_loss_hit(
        self,
        *,
        direction: PositionSide,
        entry: Entry,
        tick: Tick,
    ) -> bool:
        """Return True when the stop-loss is reachable on this tick."""
        if entry.stop_loss_price is None or not self.can_close_on_tick(entry=entry, tick=tick):
            return False
        if direction == PositionSide.LONG:
            return tick.bid <= entry.stop_loss_price
        return tick.ask >= entry.stop_loss_price

    def unrealized_pl(self, *, direction: PositionSide, entry: Entry, tick: Tick) -> Money:
        """Return unrealized P/L in quote currency for the current tick."""
        exit_price = self.exit_side_price(direction, tick)
        if direction == PositionSide.LONG:
            amount = (exit_price.amount - entry.entry_price.amount) * entry.units
        else:
            amount = (entry.entry_price.amount - exit_price.amount) * entry.units
        return Money.of(amount, entry.entry_price.currency)

    def unrealized_loss_pips(
        self,
        *,
        direction: PositionSide,
        entry: Entry,
        tick: Tick,
        pip_size: Decimal,
    ) -> Decimal:
        """Return positive loss in pips, or zero when not losing."""
        exit_price = self.exit_side_price(direction, tick)
        if direction == PositionSide.LONG:
            return max((entry.entry_price.amount - exit_price.amount) / pip_size, Decimal("0"))
        return max((exit_price.amount - entry.entry_price.amount) / pip_size, Decimal("0"))

    def realized_pl(
        self,
        *,
        direction: PositionSide,
        entry: Entry,
        exit_price: Money,
    ) -> Money:
        """Return realized P/L in quote currency."""
        if direction == PositionSide.LONG:
            amount = (exit_price.amount - entry.entry_price.amount) * entry.units
        else:
            amount = (entry.entry_price.amount - exit_price.amount) * entry.units
        return Money.of(amount, entry.entry_price.currency)

    def counter_take_profit_price(
        self,
        *,
        layer: Layer,
        direction: PositionSide,
        retracement_count: int,
        entry_price: Money,
        units: Decimal,
        pip_size: Decimal,
        calculator: SnowballCalculator,
        include_head: Entry | None = None,
    ) -> Money:
        """Return the TP price for a new counter entry."""
        config = calculator.config
        if config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
            return self.weighted_average_price(
                layer=layer,
                new_price=entry_price,
                new_units=units,
                include_ref=include_head,
            )
        tp_pips = calculator.counter_take_profit_pips(retracement_count)
        return self.take_profit_price(
            direction=direction,
            entry_price=entry_price,
            tp_pips=tp_pips,
            pip_size=pip_size,
        )

    def weighted_average_price(
        self,
        *,
        layer: Layer,
        new_price: Money,
        new_units: Decimal,
        include_ref: Entry | None = None,
    ) -> Money:
        """Compute weighted average of live/pending layer entries plus a new entry."""
        total_cost = new_price.amount * new_units
        total_units = new_units
        for slot in layer.slots:
            if slot.entry is not None:
                total_cost += slot.entry.entry_price.amount * slot.entry.units
                total_units += slot.entry.units
            elif slot.pending_rebuild is not None:
                total_cost += slot.pending_rebuild.entry_price.amount * slot.pending_rebuild.units
                total_units += slot.pending_rebuild.units
        if include_ref is not None:
            total_cost += include_ref.entry_price.amount * include_ref.units
            total_units += include_ref.units
        return Money.of(total_cost / total_units, new_price.currency)

    def sync_weighted_average_take_profits(self, layer: Layer) -> Money | None:
        """Apply the current weighted average TP to present counter slots."""
        total_cost = Decimal("0")
        total_units = Decimal("0")
        currency = None
        for slot in layer.slots:
            if slot.entry is not None:
                currency = slot.entry.entry_price.currency
                total_cost += slot.entry.entry_price.amount * slot.entry.units
                total_units += slot.entry.units
            elif slot.pending_rebuild is not None:
                currency = slot.pending_rebuild.entry_price.currency
                total_cost += slot.pending_rebuild.entry_price.amount * slot.pending_rebuild.units
                total_units += slot.pending_rebuild.units
        if total_units <= 0 or currency is None:
            return None
        take_profit_price = Money.of(total_cost / total_units, currency)
        for slot in layer.slots:
            if slot.entry is not None and layer.retracement_count(slot) > 0:
                slot.entry.take_profit_price = take_profit_price
            elif slot.pending_rebuild is not None and layer.retracement_count(slot) > 0:
                slot.pending_rebuild.entry.take_profit_price = take_profit_price
        return take_profit_price

    def layer_initial_take_profit_price(
        self,
        *,
        new_price: Money,
        previous_layer: Layer,
        direction: PositionSide,
        pip_size: Decimal,
        take_profit_pips: Decimal,
    ) -> Money:
        """Return L2+ R0 TP price clamped by the previous layer's highest TP."""
        planned_take_profit_price = self.take_profit_price(
            direction=direction,
            entry_price=new_price,
            tp_pips=take_profit_pips,
            pip_size=pip_size,
        )
        highest = previous_layer.highest_present_slot()
        if highest is None:
            return planned_take_profit_price
        previous_tp = highest.reference_take_profit_price()
        if previous_tp is None:
            return planned_take_profit_price
        if direction == PositionSide.LONG:
            return min(planned_take_profit_price, previous_tp)
        return max(planned_take_profit_price, previous_tp)

    def rebuild_take_profit_price(
        self,
        *,
        pending: PendingRebuild,
        direction: PositionSide,
        retracement_count: int,
        entry_price: Money,
        pip_size: Decimal,
        calculator: SnowballCalculator,
    ) -> Money:
        """Return TP price for a rebuilt entry."""
        config = calculator.config
        if config.rebuild.take_profit.mode == RebuildTakeProfitMode.SAME_PRICE:
            return pending.take_profit_price
        if config.rebuild.take_profit.mode == RebuildTakeProfitMode.SAME_DISTANCE:
            tp_pips = abs(pending.take_profit_price.amount - pending.entry_price.amount) / pip_size
        else:
            tp_pips = calculator.rebuild_take_profit_pips(retracement_count + 1)
        return self.take_profit_price(
            direction=direction,
            entry_price=entry_price,
            tp_pips=tp_pips,
            pip_size=pip_size,
        )

    def rebuild_stop_loss_price(
        self,
        *,
        pending: PendingRebuild,
        direction: PositionSide,
        retracement_count: int,
        entry_price: Money,
        pip_size: Decimal,
        calculator: SnowballCalculator,
    ) -> Money | None:
        """Return SL price for a rebuilt entry."""
        config = calculator.config
        if not config.stop_loss.enabled:
            return None
        mode = config.rebuild.stop_loss.mode
        if mode == RebuildStopLossMode.SAME_PRICE:
            copied = pending.stop_loss_price
            if self.stop_loss_on_loss_side(
                direction=direction,
                entry_price=entry_price,
                stop_loss_price=copied,
            ):
                return copied
            return self.reproject_stop_loss(
                direction=direction,
                entry_price=entry_price,
                source_entry_price=pending.entry_price,
                source_stop_loss_price=pending.stop_loss_price,
            )
        if mode == RebuildStopLossMode.SAME_DISTANCE:
            return self.reproject_stop_loss(
                direction=direction,
                entry_price=entry_price,
                source_entry_price=pending.entry_price,
                source_stop_loss_price=pending.stop_loss_price,
            )
        if mode == RebuildStopLossMode.MANUAL_DISTANCE:
            values = config.rebuild.stop_loss.manual_distances_pips
            pips = values[min(retracement_count, len(values) - 1)]
            return self.stop_loss_price(
                direction=direction,
                entry_price=entry_price,
                stop_loss_pips=pips,
                pip_size=pip_size,
            )
        return None

    def stop_loss_on_loss_side(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        stop_loss_price: Money | None,
    ) -> bool:
        """Return True when an SL is absent or on the loss side."""
        if stop_loss_price is None:
            return True
        if direction == PositionSide.LONG:
            return stop_loss_price < entry_price
        return stop_loss_price > entry_price

    def reproject_stop_loss(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        source_entry_price: Money | None,
        source_stop_loss_price: Money | None,
    ) -> Money | None:
        """Rebuild an SL by preserving its previous absolute distance."""
        if source_entry_price is None or source_stop_loss_price is None:
            return None
        distance = abs(source_entry_price.amount - source_stop_loss_price.amount)
        if distance <= 0:
            return None
        if direction == PositionSide.LONG:
            return Money.of(entry_price.amount - distance, entry_price.currency)
        return Money.of(entry_price.amount + distance, entry_price.currency)

    def rebuild_trigger_hit(
        self,
        *,
        pending: PendingRebuild,
        direction: PositionSide,
        tick: Tick,
        config: SnowballConfig,
        pip_size: Decimal,
    ) -> bool:
        """Return True when price has reached the rebuild trigger."""
        if tick.timestamp <= pending.closed_at:
            return False
        if config.rebuild.trigger.entry_price_mode == RebuildEntryPriceMode.STOP_LOSS_EXIT_PRICE:
            trigger = pending.stop_loss_exit_price
        else:
            trigger = pending.entry_price
        if config.rebuild.trigger.buffer_pips:
            buffer = config.rebuild.trigger.buffer_pips * pip_size
            amount = (
                trigger.amount + buffer
                if direction == PositionSide.LONG
                else trigger.amount - buffer
            )
            trigger = Money.of(amount, trigger.currency)
        if direction == PositionSide.LONG:
            return tick.ask >= trigger
        return tick.bid <= trigger
