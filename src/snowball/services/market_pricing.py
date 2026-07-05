"""Market price calculations for Snowball."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money, PositionSide, Tick

from snowball.models.entries import FilledEntry, FilledStopLossEntry


@dataclass(frozen=True, slots=True)
class SnowballMarketPricing:
    """Own executable price, P/L, and TP/SL market calculations."""

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

    def directional_buffer_price(
        self,
        *,
        direction: PositionSide,
        price: Money,
        buffer_pips: Decimal,
        pip_size: Decimal,
    ) -> Money:
        """Return a price moved by a directional positive buffer."""
        if not buffer_pips:
            return price
        buffer = buffer_pips * pip_size
        amount = price.amount + buffer if direction == PositionSide.LONG else price.amount - buffer
        return Money.of(amount, price.currency)

    def absolute_pips_between(
        self,
        *,
        first_price: Money,
        second_price: Money,
        pip_size: Decimal,
    ) -> Decimal:
        """Return the absolute distance between two prices in pips."""
        return abs(first_price.amount - second_price.amount) / pip_size

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

    def can_close_on_tick(self, *, entry: FilledEntry, tick: Tick) -> bool:
        """Return True when the entry was opened before this tick."""
        return tick.timestamp > entry.filled_at

    def take_profit_hit(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        tick: Tick,
    ) -> bool:
        """Return True when the take-profit is reachable on this tick."""
        if not self.can_close_on_tick(entry=entry, tick=tick):
            return False
        if direction == PositionSide.LONG:
            return tick.bid >= entry.planned_take_profit_price
        return tick.ask <= entry.planned_take_profit_price

    def stop_loss_hit(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        tick: Tick,
    ) -> bool:
        """Return True when the stop-loss is reachable on this tick."""
        if entry.planned_stop_loss_price is None or not self.can_close_on_tick(
            entry=entry,
            tick=tick,
        ):
            return False
        if direction == PositionSide.LONG:
            return tick.bid <= entry.planned_stop_loss_price
        return tick.ask >= entry.planned_stop_loss_price

    def unrealized_pl(self, *, direction: PositionSide, entry: FilledEntry, tick: Tick) -> Money:
        """Return unrealized P/L in quote currency for the current tick."""
        exit_price = self.exit_side_price(direction, tick)
        if direction == PositionSide.LONG:
            amount = (exit_price.amount - entry.filled_entry_price.amount) * entry.filled_units
        else:
            amount = (entry.filled_entry_price.amount - exit_price.amount) * entry.filled_units
        return Money.of(amount, entry.filled_entry_price.currency)

    def unrealized_loss_pips(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        tick: Tick,
        pip_size: Decimal,
    ) -> Decimal:
        """Return positive loss in pips, or zero when not losing."""
        exit_price = self.exit_side_price(direction, tick)
        if direction == PositionSide.LONG:
            return max(
                (entry.filled_entry_price.amount - exit_price.amount) / pip_size,
                Decimal("0"),
            )
        return max(
            (exit_price.amount - entry.filled_entry_price.amount) / pip_size,
            Decimal("0"),
        )

    def realized_pl(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        exit_price: Money,
    ) -> Money:
        """Return realized P/L in quote currency."""
        if direction == PositionSide.LONG:
            amount = (exit_price.amount - entry.filled_entry_price.amount) * entry.filled_units
        else:
            amount = (entry.filled_entry_price.amount - exit_price.amount) * entry.filled_units
        return Money.of(amount, entry.filled_entry_price.currency)

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
        stop_loss_entry: FilledStopLossEntry,
        direction: PositionSide,
        tick: Tick,
    ) -> bool:
        """Return True when price has reached the rebuild trigger."""
        if tick.timestamp <= stop_loss_entry.filled_at:
            return False
        trigger = stop_loss_entry.planned_rebuild_trigger_price
        if direction == PositionSide.LONG:
            return tick.ask >= trigger
        return tick.bid <= trigger
