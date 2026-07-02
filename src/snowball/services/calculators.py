"""Pure Snowball formula calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from snowball.config import SnowballConfig
from snowball.enums import CounterTakeProfitMode, IntervalMode, RebuildTakeProfitMode


@dataclass(frozen=True, slots=True)
class SnowballCalculator:
    """Config-bound Snowball formula calculator."""

    config: SnowballConfig

    def counter_interval_pips(self, step: int) -> Decimal:
        """Return the pip interval before the 1-based counter step."""
        interval = self.config.counter.interval
        return progression_pips(
            step=step,
            mode=interval.mode,
            head=interval.head_pips,
            tail=interval.tail_pips,
            flat_steps=interval.flat_steps,
            gamma=interval.gamma,
            max_steps=self.config.grid.max_retracements_per_layer,
            manual_values=interval.manual_pips,
        )

    def stop_loss_pips(self, slot_number: int) -> Decimal:
        """Return the stop-loss distance for a 1-based slot number."""
        distance = self.config.stop_loss.distance
        return progression_pips(
            step=slot_number,
            mode=distance.mode,
            head=distance.head_pips,
            tail=distance.tail_pips,
            flat_steps=distance.flat_steps,
            gamma=distance.gamma,
            max_steps=self.config.grid.max_retracements_per_layer,
            manual_values=distance.manual_pips,
        )

    def rebuild_take_profit_pips(self, slot_number: int) -> Decimal:
        """Return the rebuilt entry take-profit distance."""
        take_profit = self.config.rebuild.take_profit
        if take_profit.mode in {
            RebuildTakeProfitMode.SAME_PRICE,
            RebuildTakeProfitMode.SAME_DISTANCE,
        }:
            return Decimal("0")
        return progression_pips(
            step=slot_number,
            mode=take_profit.distance.mode,
            head=take_profit.distance.head_pips,
            tail=take_profit.distance.tail_pips,
            flat_steps=take_profit.distance.flat_steps,
            gamma=take_profit.distance.gamma,
            max_steps=self.config.grid.max_retracements_per_layer,
            manual_values=take_profit.distance.manual_pips,
        )

    def counter_take_profit_pips(self, step: int) -> Decimal:
        """Return the take-profit distance for a 1-based counter step."""
        take_profit = self.config.counter.take_profit
        mode = take_profit.mode
        if mode == CounterTakeProfitMode.WEIGHTED_AVG:
            return Decimal("0")
        if mode == CounterTakeProfitMode.FIXED or step <= 1:
            return take_profit.fixed_pips

        n = Decimal(step - 1)
        if mode == CounterTakeProfitMode.ADDITIVE:
            return take_profit.fixed_pips + take_profit.step_pips * n
        if mode == CounterTakeProfitMode.SUBTRACTIVE:
            return max(
                take_profit.fixed_pips - take_profit.step_pips * n,
                Decimal("0"),
            )
        if mode == CounterTakeProfitMode.MULTIPLICATIVE:
            return take_profit.fixed_pips * (take_profit.multiplier**n)
        if mode == CounterTakeProfitMode.DIVISIVE:
            return take_profit.fixed_pips / (take_profit.multiplier**n)
        return take_profit.fixed_pips


def progression_pips(
    *,
    step: int,
    mode: IntervalMode,
    head: Decimal,
    tail: Decimal,
    flat_steps: int,
    gamma: Decimal,
    max_steps: int,
    manual_values: tuple[Decimal, ...],
) -> Decimal:
    """Return a pips value for the shared head-to-tail progression."""
    if step < 1:
        raise ValueError("step must be 1-based")
    if mode == IntervalMode.MANUAL and manual_values:
        return manual_values[min(step - 1, len(manual_values) - 1)]
    if mode == IntervalMode.CONSTANT:
        return head
    if step <= flat_steps:
        return head

    decayed_steps = max_steps - flat_steps
    if decayed_steps <= 0:
        return tail

    progress = Decimal(step - flat_steps) / Decimal(decayed_steps)
    curved = progress**gamma
    return max(head - (head - tail) * curved, tail)
