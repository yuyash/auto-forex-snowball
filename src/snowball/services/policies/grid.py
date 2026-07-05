"""Grid ordering policy for Snowball."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from core import Money, PositionSide

from snowball.models.grid import Layer, Slot
from snowball.models.state import Cycle

PresentSlot = tuple[int, int, Money, Money]


@dataclass(frozen=True, slots=True)
class SnowballGridPolicy:
    """Validate and preserve monotonic L/R grid order."""

    def validate_ordering(
        self,
        cycle: Cycle,
        *,
        check_take_profit: bool = True,
    ) -> str | None:
        """Return a violation description, or None when the grid is monotonic."""
        for previous, current in pairwise(self._present_slots(cycle)):
            if cycle.direction == PositionSide.LONG:
                entry_ok = previous[2] >= current[2]
                take_profit_ok = previous[3] >= current[3]
                expected = "descending"
            else:
                entry_ok = previous[2] <= current[2]
                take_profit_ok = previous[3] <= current[3]
                expected = "ascending"
            if entry_ok and (take_profit_ok or not check_take_profit):
                continue
            return (
                f"cycle_id={cycle.cycle_id}, direction={cycle.direction.value}, "
                f"expected={expected}, "
                f"prev=L{previous[0]}/R{previous[1]} entry={previous[2]} tp={previous[3]}, "
                f"curr=L{current[0]}/R{current[1]} entry={current[2]} tp={current[3]}"
            )
        return None

    def preceding_entry_bound(
        self,
        cycle: Cycle,
        layer: Layer,
        retracement_count: int,
    ) -> Money | None:
        """Return the tightest present entry bound before one slot."""
        bound: Money | None = None
        for _layer, slot in self._preceding_slots(cycle, layer, retracement_count):
            entry_price = slot.reference_entry_price()
            if entry_price is None:
                continue
            bound = self._combine(cycle.direction, bound, entry_price)
        return bound

    def preceding_take_profit_bound(
        self,
        cycle: Cycle,
        layer: Layer,
        retracement_count: int,
    ) -> Money | None:
        """Return the tightest live or pending TP bound before one slot."""
        bound: Money | None = None
        for _layer, slot in self._preceding_slots(cycle, layer, retracement_count):
            take_profit_price = slot.reference_take_profit_price()
            if take_profit_price is None:
                continue
            bound = self._combine(cycle.direction, bound, take_profit_price)
        return bound

    def clamp_entry_price(
        self,
        *,
        cycle: Cycle,
        layer: Layer,
        retracement_count: int,
        entry_price: Money,
    ) -> Money:
        """Clamp a rebuild entry so it cannot cross preceding live entries."""
        bound = self.preceding_entry_bound(cycle, layer, retracement_count)
        if bound is None:
            return entry_price
        if cycle.direction == PositionSide.LONG:
            return min(entry_price, bound)
        return max(entry_price, bound)

    def clamp_take_profit(
        self,
        *,
        cycle: Cycle,
        layer: Layer,
        retracement_count: int,
        take_profit_price: Money,
    ) -> Money:
        """Clamp a TP so it cannot cross preceding present entries."""
        bound = self.preceding_take_profit_bound(cycle, layer, retracement_count)
        if bound is None:
            return take_profit_price
        if cycle.direction == PositionSide.LONG:
            return min(take_profit_price, bound)
        return max(take_profit_price, bound)

    def propagate_pending_take_profit(
        self,
        *,
        cycle: Cycle,
        layer: Layer,
        retracement_count: int,
        take_profit_price: Money,
    ) -> None:
        """Adjust preceding pending TPs when a later rebuild sets a tighter bound."""
        for _layer, slot in self._preceding_slots(cycle, layer, retracement_count):
            stop_loss_entry = slot.filled_stop_loss_entry
            if stop_loss_entry is None:
                continue
            if (
                cycle.direction == PositionSide.LONG
                and stop_loss_entry.original_entry.planned_take_profit_price < take_profit_price
            ):
                stop_loss_entry.original_entry.planned_take_profit_price = take_profit_price
            elif (
                cycle.direction == PositionSide.SHORT
                and stop_loss_entry.original_entry.planned_take_profit_price > take_profit_price
            ):
                stop_loss_entry.original_entry.planned_take_profit_price = take_profit_price

    def _present_slots(self, cycle: Cycle) -> list[PresentSlot]:
        present: list[PresentSlot] = []
        for layer, slot in cycle.grid.all_present_slots():
            entry_price = slot.reference_entry_price()
            take_profit_price = slot.reference_take_profit_price()
            if entry_price is None or take_profit_price is None:
                continue
            present.append(
                (
                    cycle.grid.layer_number(layer),
                    layer.retracement_count(slot),
                    entry_price,
                    take_profit_price,
                )
            )
        return present

    def _preceding_slots(
        self,
        cycle: Cycle,
        layer: Layer,
        retracement_count: int,
    ) -> list[tuple[Layer, Slot]]:
        preceding: list[tuple[Layer, Slot]] = []
        layer_number = cycle.grid.layer_number(layer)
        for candidate_layer in cycle.grid.layers:
            if cycle.grid.layer_number(candidate_layer) > layer_number:
                continue
            for slot in candidate_layer.slots:
                if candidate_layer is layer and layer.retracement_count(slot) >= retracement_count:
                    continue
                preceding.append((candidate_layer, slot))
        return preceding

    def _combine(
        self,
        direction: PositionSide,
        existing: Money | None,
        candidate: Money,
    ) -> Money:
        if existing is None:
            return candidate
        if direction == PositionSide.LONG:
            return min(existing, candidate)
        return max(existing, candidate)
