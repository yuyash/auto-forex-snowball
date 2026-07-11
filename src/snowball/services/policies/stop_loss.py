"""Stop-loss planning policies for Snowball entries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money, Pips, PositionSide, Tick

from snowball.config import SnowballConfig
from snowball.enums import RebuildEntryPriceMode, RebuildStopLossMode, StopLossMode
from snowball.models.entries import FilledStopLossEntry
from snowball.models.grid import Layer, Slot
from snowball.services.calculators import SnowballCalculator
from snowball.services.market_pricing import SnowballMarketPricing


@dataclass(frozen=True, slots=True)
class SnowballStopLossPlanner:
    """Plan stop-loss and rebuild prices."""

    config: SnowballConfig
    calculator: SnowballCalculator
    pricing: SnowballMarketPricing

    def entry_stop_loss_price(
        self,
        *,
        tick: Tick,
        direction: PositionSide,
        entry_price: Money,
        take_profit_price: Money,
        retracement_count: int,
        rebuild_source: FilledStopLossEntry | None,
    ) -> Money | None:
        """Return the planned stop-loss price for a requested entry."""
        pip_size = tick.instrument.pip_size
        if not self.config.stop_loss.enabled:
            return None
        if rebuild_source is not None:
            return self.rebuild_stop_loss_price(
                stop_loss_entry=rebuild_source,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                pip_size=pip_size,
            )
        if self.config.stop_loss.mode == StopLossMode.AUTO:
            return self.auto_stop_loss_price(
                direction=direction,
                entry_price=entry_price,
                take_profit_price=take_profit_price,
                retracement_count=retracement_count,
                pip_size=pip_size,
            )
        stop_loss_pips = self.calculator.stop_loss_pips(retracement_count + 1)
        return self.pricing.stop_loss_price(
            direction=direction,
            entry_price=entry_price,
            stop_loss_pips=stop_loss_pips,
            pip_size=pip_size,
        )

    def auto_stop_loss_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        take_profit_price: Money,
        retracement_count: int,
        pip_size: Decimal,
    ) -> Money | None:
        """Place SL at the next grid interval, or one more interval past it."""
        next_interval_pips = self.calculator.counter_interval_pips(retracement_count + 1)
        if next_interval_pips <= 0 or pip_size <= 0:
            return None
        interval = next_interval_pips * pip_size
        tp_pips = self.pricing.absolute_pips_between(
            first_price=take_profit_price,
            second_price=entry_price,
            pip_size=pip_size,
        )
        extra_interval = retracement_count > 0 and tp_pips >= next_interval_pips
        if direction == PositionSide.LONG:
            amount = entry_price.amount - interval
            if extra_interval:
                amount -= interval
        else:
            amount = entry_price.amount + interval
            if extra_interval:
                amount += interval
        if amount <= 0:
            return None
        return Money.of(amount, entry_price.currency)

    def rebuild_stop_loss_price(
        self,
        *,
        stop_loss_entry: FilledStopLossEntry,
        direction: PositionSide,
        retracement_count: int,
        entry_price: Money,
        pip_size: Decimal,
    ) -> Money | None:
        """Return the rebuilt-entry stop-loss price."""
        mode = self.config.rebuild.stop_loss.mode
        if mode == RebuildStopLossMode.SAME_PRICE:
            copied = stop_loss_entry.planned_stop_loss_price
            if self.pricing.stop_loss_on_loss_side(
                direction=direction,
                entry_price=entry_price,
                stop_loss_price=copied,
            ):
                return copied
            return self.pricing.reproject_stop_loss(
                direction=direction,
                entry_price=entry_price,
                source_entry_price=stop_loss_entry.original_filled_entry_price,
                source_stop_loss_price=stop_loss_entry.planned_stop_loss_price,
            )
        if mode == RebuildStopLossMode.SAME_DISTANCE:
            return self.pricing.reproject_stop_loss(
                direction=direction,
                entry_price=entry_price,
                source_entry_price=stop_loss_entry.original_filled_entry_price,
                source_stop_loss_price=stop_loss_entry.planned_stop_loss_price,
            )
        if mode == RebuildStopLossMode.MANUAL_DISTANCE:
            values = self.config.rebuild.stop_loss.manual_distances_pips
            pips = values[min(retracement_count, len(values) - 1)]
            return self.pricing.stop_loss_price(
                direction=direction,
                entry_price=entry_price,
                stop_loss_pips=pips,
                pip_size=pip_size,
            )
        return None

    def planned_rebuild_price(
        self,
        *,
        direction: PositionSide,
        original_entry_price: Money,
        planned_stop_loss_price: Money | None,
        stop_loss_exit_price: Money,
        pip_size: Decimal,
    ) -> Money:
        """Return the planned price for rebuilding a stopped slot."""
        if self.config.rebuild.price.entry_price_mode == RebuildEntryPriceMode.STOP_LOSS_EXIT_PRICE:
            base_price = planned_stop_loss_price or stop_loss_exit_price
            buffer_pips = self.config.rebuild.price.buffer_pips
        else:
            base_price = original_entry_price
            buffer_pips = Pips("0")
        return self.pricing.directional_buffer_price(
            direction=direction,
            price=base_price,
            buffer_pips=buffer_pips,
            pip_size=pip_size,
        )

    def stop_loss_temporarily_protected(
        self,
        *,
        layer: Layer,
        slot: Slot,
        highest: Slot | None,
    ) -> bool:
        """Return True when a highest-retracement protection suppresses this SL."""
        if not self.config.stop_loss.protect_highest_retracement.enabled:
            return False
        if highest is None or highest.filled_entry is None or slot.filled_entry is None:
            return False
        protected_from = self.config.stop_loss.protect_highest_retracement.from_retracement
        if layer.retracement_count(slot) < protected_from:
            return False
        return highest is slot
