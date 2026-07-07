"""Grid selection policies for Snowball flows."""

from __future__ import annotations

from dataclasses import dataclass

from core import Money

from snowball.models.entries import FilledEntry
from snowball.models.grid import Layer, Slot
from snowball.models.state import Cycle


@dataclass(frozen=True, slots=True)
class SnowballGridSelector:
    """Select grid references and candidate slots for strategy flows."""

    def effective_head(self, cycle: Cycle) -> FilledEntry | None:
        """Return the live or pending head used for averaging decisions."""
        head = cycle.head()
        if head is not None:
            return head
        for _layer, slot in cycle.grid.requested_stop_loss_slots():
            if slot.requested_stop_loss_entry is not None:
                return slot.requested_stop_loss_entry.original_entry
        for _layer, slot in cycle.grid.filled_stop_loss_slots():
            if slot.filled_stop_loss_entry is not None:
                return slot.filled_stop_loss_entry.original_entry
        return None

    def next_available_counter_slot(
        self,
        *,
        layer: Layer,
        max_refillable_retracement: int,
    ) -> Slot | None:
        """Return the next R1+ slot that can receive a counter entry."""
        slot_numbers = [slot_number for slot_number in layer.slot_numbers if slot_number > 0]
        for retracement_count in slot_numbers:
            slot = layer.slot(retracement_count)
            if slot.is_sealed:
                return None
            if slot.entry is not None:
                continue
            if retracement_count > max_refillable_retracement and layer.build_count(slot) > 0:
                return None
            higher_present = any(
                layer.slot(higher_number).is_present
                for higher_number in slot_numbers
                if higher_number > retracement_count
            )
            if higher_present and layer.build_count(slot) > 0:
                continue
            return slot
        return None

    def counter_reference_price(
        self,
        *,
        layer: Layer,
        retracement_count: int,
    ) -> Money | None:
        """Return the nearest preceding reference entry price for a counter slot."""
        for index in range(retracement_count - 1, -1, -1):
            reference = layer.slot(index).reference_entry_price()
            if reference is not None:
                return reference
        return None

    def shrink_front_entry(self, cycle: Cycle) -> FilledEntry | None:
        """Return the lowest L/R live entry eligible as a shrink candidate."""
        for layer in cycle.grid.layers:
            for slot in layer.slots:
                if slot.filled_entry is not None:
                    return slot.filled_entry
        return None
