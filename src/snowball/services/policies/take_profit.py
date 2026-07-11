"""Take-profit planning policies for Snowball entries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money, PositionSide, Units

from snowball.config import SnowballConfig
from snowball.enums import CounterTakeProfitMode, RebuildTakeProfitMode
from snowball.models.entries import FilledEntry, FilledStopLossEntry
from snowball.models.grid import Layer
from snowball.services.calculators import SnowballCalculator
from snowball.services.market_pricing import SnowballMarketPricing


@dataclass(frozen=True, slots=True)
class SnowballTakeProfitPlanner:
    """Plan take-profit prices from Snowball config and grid state."""

    config: SnowballConfig
    calculator: SnowballCalculator
    pricing: SnowballMarketPricing

    def forward_take_profit_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        pip_size: Decimal,
    ) -> Money:
        """Return the forward-direction take-profit price."""
        return self.pricing.take_profit_price(
            direction=direction,
            entry_price=entry_price,
            tp_pips=self.config.forward.take_profit_pips,
            pip_size=pip_size,
        )

    def counter_take_profit_price(
        self,
        *,
        layer: Layer,
        direction: PositionSide,
        retracement_count: int,
        entry_price: Money,
        units: Units,
        pip_size: Decimal,
        include_head: FilledEntry | None,
    ) -> Money:
        """Return the counter-entry take-profit price."""
        if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
            return self.weighted_average_price(
                layer=layer,
                new_price=entry_price,
                new_units=units,
                include_ref=include_head,
            )
        tp_pips = self.calculator.counter_take_profit_pips(retracement_count)
        return self.pricing.take_profit_price(
            direction=direction,
            entry_price=entry_price,
            tp_pips=tp_pips,
            pip_size=pip_size,
        )

    def layer_initial_take_profit_price(
        self,
        *,
        new_price: Money,
        previous_layer: Layer,
        direction: PositionSide,
        pip_size: Decimal,
    ) -> Money:
        """Return L2+ R0 TP price clamped by the previous layer's highest TP."""
        planned_take_profit_price = self.forward_take_profit_price(
            direction=direction,
            entry_price=new_price,
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
        stop_loss_entry: FilledStopLossEntry,
        direction: PositionSide,
        retracement_count: int,
        entry_price: Money,
        pip_size: Decimal,
    ) -> Money:
        """Return rebuilt-entry take-profit price."""
        mode = self.config.rebuild.take_profit.mode
        if mode == RebuildTakeProfitMode.SAME_PRICE:
            return stop_loss_entry.planned_take_profit_price
        if mode == RebuildTakeProfitMode.SAME_DISTANCE:
            tp_pips = self.pricing.absolute_pips_between(
                first_price=stop_loss_entry.planned_take_profit_price,
                second_price=stop_loss_entry.original_filled_entry_price,
                pip_size=pip_size,
            )
        else:
            tp_pips = self.calculator.rebuild_take_profit_pips(retracement_count + 1)
        return self.pricing.take_profit_price(
            direction=direction,
            entry_price=entry_price,
            tp_pips=tp_pips,
            pip_size=pip_size,
        )

    def counter_weighted_average_head(
        self,
        *,
        layer: Layer,
        cycle_head: FilledEntry | None,
    ) -> FilledEntry | None:
        """Return the cycle head to include in a weighted-average counter TP."""
        if self.config.counter.take_profit.mode != CounterTakeProfitMode.WEIGHTED_AVG:
            return None
        if layer.r0.is_present:
            return None
        return cycle_head

    def weighted_average_price(
        self,
        *,
        layer: Layer,
        new_price: Money,
        new_units: Units,
        include_ref: FilledEntry | None = None,
    ) -> Money:
        """Compute weighted average of live/pending layer entries plus a new entry."""
        total_cost = new_price.amount * new_units
        total_units = new_units
        for slot in layer.iter_slots():
            reference_price = slot.reference_entry_price()
            reference_units = slot.reference_filled_units()
            if reference_price is None or reference_units is None:
                continue
            total_cost += reference_price.amount * reference_units
            total_units += reference_units
        if include_ref is not None:
            total_cost += include_ref.filled_entry_price.amount * include_ref.filled_units
            total_units += include_ref.filled_units
        return Money.of(total_cost / total_units, new_price.currency)
