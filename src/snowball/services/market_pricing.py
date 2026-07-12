"""Market price calculations for Snowball."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core import Money, Pips, PositionSide, Tick

from snowball.models.entries import FilledEntry, FilledStopLossEntry


@dataclass(frozen=True, slots=True)
class MarketPriceSelector:
    """Select executable bid/ask prices for a direction."""

    def entry_side_price(self, direction: PositionSide, tick: Tick) -> Money:
        """Return the executable entry-side price for a direction."""
        return tick.ask if direction == PositionSide.LONG else tick.bid

    def exit_side_price(self, direction: PositionSide, tick: Tick) -> Money:
        """Return the executable exit-side price for a direction."""
        return tick.bid if direction == PositionSide.LONG else tick.ask


@dataclass(frozen=True, slots=True)
class PriceProjector:
    """Project prices and pips distances from directional rules."""

    def take_profit_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        tp_pips: Pips,
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
        stop_loss_pips: Pips,
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
        buffer_pips: Pips,
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
    ) -> Pips:
        """Return the absolute distance between two prices in pips."""
        return Pips.of(abs(first_price.amount - second_price.amount) / pip_size)

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


@dataclass(frozen=True, slots=True)
class MarketTriggerDetector:
    """Detect TP, SL, and rebuild price triggers on ticks."""

    selector: MarketPriceSelector = field(default_factory=MarketPriceSelector)

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

    def rebuild_price_hit(
        self,
        *,
        stop_loss_entry: FilledStopLossEntry,
        direction: PositionSide,
        tick: Tick,
    ) -> bool:
        """Return True when price has reached the planned rebuild price."""
        if tick.timestamp <= stop_loss_entry.filled_at:
            return False
        rebuild_price = stop_loss_entry.planned_rebuild_price
        if direction == PositionSide.LONG:
            return tick.bid >= rebuild_price
        return tick.ask <= rebuild_price


@dataclass(frozen=True, slots=True)
class ProfitCalculator:
    """Calculate realized and unrealized Snowball profit/loss."""

    selector: MarketPriceSelector = field(default_factory=MarketPriceSelector)

    def unrealized_pl(self, *, direction: PositionSide, entry: FilledEntry, tick: Tick) -> Money:
        """Return unrealized P/L in quote currency for the current tick."""
        exit_price = self.selector.exit_side_price(direction, tick)
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
    ) -> Pips:
        """Return positive loss in pips, or zero when not losing."""
        exit_price = self.selector.exit_side_price(direction, tick)
        if direction == PositionSide.LONG:
            return Pips.of(
                max(
                    (entry.filled_entry_price.amount - exit_price.amount) / pip_size,
                    Decimal("0"),
                )
            )
        return Pips.of(
            max(
                (exit_price.amount - entry.filled_entry_price.amount) / pip_size,
                Decimal("0"),
            )
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


@dataclass(frozen=True, slots=True)
class SnowballMarketPricing:
    """Coordinate Snowball market-price services."""

    selector: MarketPriceSelector = field(default_factory=MarketPriceSelector)
    projector: PriceProjector = field(default_factory=PriceProjector)
    triggers: MarketTriggerDetector = field(default_factory=MarketTriggerDetector)
    profit: ProfitCalculator = field(default_factory=ProfitCalculator)

    def entry_side_price(self, direction: PositionSide, tick: Tick) -> Money:
        """Return the executable entry-side price for a direction."""
        return self.selector.entry_side_price(direction, tick)

    def exit_side_price(self, direction: PositionSide, tick: Tick) -> Money:
        """Return the executable exit-side price for a direction."""
        return self.selector.exit_side_price(direction, tick)

    def take_profit_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        tp_pips: Pips,
        pip_size: Decimal,
    ) -> Money:
        """Return a take-profit price from pips."""
        return self.projector.take_profit_price(
            direction=direction,
            entry_price=entry_price,
            tp_pips=tp_pips,
            pip_size=pip_size,
        )

    def stop_loss_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        stop_loss_pips: Pips,
        pip_size: Decimal,
    ) -> Money:
        """Return a stop-loss price from pips."""
        return self.projector.stop_loss_price(
            direction=direction,
            entry_price=entry_price,
            stop_loss_pips=stop_loss_pips,
            pip_size=pip_size,
        )

    def directional_buffer_price(
        self,
        *,
        direction: PositionSide,
        price: Money,
        buffer_pips: Pips,
        pip_size: Decimal,
    ) -> Money:
        """Return a price moved by a directional positive buffer."""
        return self.projector.directional_buffer_price(
            direction=direction,
            price=price,
            buffer_pips=buffer_pips,
            pip_size=pip_size,
        )

    def absolute_pips_between(
        self,
        *,
        first_price: Money,
        second_price: Money,
        pip_size: Decimal,
    ) -> Pips:
        """Return the absolute distance between two prices in pips."""
        return self.projector.absolute_pips_between(
            first_price=first_price,
            second_price=second_price,
            pip_size=pip_size,
        )

    def adverse_pips(
        self,
        *,
        direction: PositionSide,
        reference_price: Money,
        current_entry_price: Money,
        pip_size: Decimal,
    ) -> Decimal:
        """Return adverse movement from reference to current price."""
        return self.projector.adverse_pips(
            direction=direction,
            reference_price=reference_price,
            current_entry_price=current_entry_price,
            pip_size=pip_size,
        )

    def can_close_on_tick(self, *, entry: FilledEntry, tick: Tick) -> bool:
        """Return True when the entry was opened before this tick."""
        return self.triggers.can_close_on_tick(entry=entry, tick=tick)

    def take_profit_hit(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        tick: Tick,
    ) -> bool:
        """Return True when the take-profit is reachable on this tick."""
        return self.triggers.take_profit_hit(direction=direction, entry=entry, tick=tick)

    def stop_loss_hit(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        tick: Tick,
    ) -> bool:
        """Return True when the stop-loss is reachable on this tick."""
        return self.triggers.stop_loss_hit(direction=direction, entry=entry, tick=tick)

    def unrealized_pl(self, *, direction: PositionSide, entry: FilledEntry, tick: Tick) -> Money:
        """Return unrealized P/L in quote currency for the current tick."""
        return self.profit.unrealized_pl(direction=direction, entry=entry, tick=tick)

    def unrealized_loss_pips(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        tick: Tick,
        pip_size: Decimal,
    ) -> Pips:
        """Return positive loss in pips, or zero when not losing."""
        return self.profit.unrealized_loss_pips(
            direction=direction,
            entry=entry,
            tick=tick,
            pip_size=pip_size,
        )

    def realized_pl(
        self,
        *,
        direction: PositionSide,
        entry: FilledEntry,
        exit_price: Money,
    ) -> Money:
        """Return realized P/L in quote currency."""
        return self.profit.realized_pl(direction=direction, entry=entry, exit_price=exit_price)

    def stop_loss_on_loss_side(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        stop_loss_price: Money | None,
    ) -> bool:
        """Return True when an SL is absent or on the loss side."""
        return self.projector.stop_loss_on_loss_side(
            direction=direction,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
        )

    def reproject_stop_loss(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        source_entry_price: Money | None,
        source_stop_loss_price: Money | None,
    ) -> Money | None:
        """Rebuild an SL by preserving its previous absolute distance."""
        return self.projector.reproject_stop_loss(
            direction=direction,
            entry_price=entry_price,
            source_entry_price=source_entry_price,
            source_stop_loss_price=source_stop_loss_price,
        )

    def rebuild_price_hit(
        self,
        *,
        stop_loss_entry: FilledStopLossEntry,
        direction: PositionSide,
        tick: Tick,
    ) -> bool:
        """Return True when price has reached the planned rebuild price."""
        return self.triggers.rebuild_price_hit(
            stop_loss_entry=stop_loss_entry,
            direction=direction,
            tick=tick,
        )
