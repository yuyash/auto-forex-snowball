"""Read-only queries over Snowball grid structures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from snowball.models.entries import FilledEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from snowball.models.grid import Grid, Layer, Slot


@dataclass(frozen=True, slots=True)
class LayerQuery:
    """Read-only queries for one layer."""

    layer: Layer

    def live_entries(self) -> list[FilledEntry]:
        """Return broker-live entries in ascending R order."""
        entries: list[FilledEntry] = []
        for slot in self.layer.iter_slots():
            entry = slot.live_or_pending_close_entry()
            if entry is not None:
                entries.append(entry)
        return entries

    def has_live_entries(self) -> bool:
        """Return True when any slot has a broker-live entry."""
        return any(
            slot.live_or_pending_close_entry() is not None for slot in self.layer.iter_slots()
        )

    def live_entry_count(self) -> int:
        """Return the number of broker-live entries."""
        return sum(
            1 for slot in self.layer.iter_slots() if slot.live_or_pending_close_entry() is not None
        )

    def counter_entries(self) -> list[FilledEntry]:
        """Return broker-live R1+ entries in ascending R order."""
        entries: list[FilledEntry] = []
        for slot_number, slot in self.layer.iter_slot_items():
            if slot_number <= 0:
                continue
            entry = slot.live_or_pending_close_entry()
            if entry is not None:
                entries.append(entry)
        return entries

    def present_slots(self) -> list[Slot]:
        """Return slots with any live, closed, or sealed entry."""
        return [slot for slot in self.layer.iter_slots() if slot.is_present]

    def highest_present_slot(self) -> Slot | None:
        """Return the highest-R slot with any entry."""
        for slot in self.layer.reversed_slots():
            if slot.is_present:
                return slot
        return None

    def highest_present_slot_number(self) -> int | None:
        """Return the highest R number with any entry."""
        for slot_number, slot in self.layer.reversed_slot_items():
            if slot.is_present:
                return slot_number
        return None

    def highest_live_slot(self) -> Slot | None:
        """Return the highest-R slot with a directly closeable live entry."""
        for slot in self.layer.reversed_slots():
            if slot.filled_entry is not None:
                return slot
        return None

    def is_empty(self) -> bool:
        """Return True when the layer has no slot entries."""
        return not any(slot.is_present for slot in self.layer.iter_slots())


@dataclass(frozen=True, slots=True)
class GridQuery:
    """Read-only queries for a layered grid."""

    grid: Grid

    def all_live_entries(self) -> list[FilledEntry]:
        """Return all live entries in grid order."""
        entries: list[FilledEntry] = []
        for layer in self.grid.iter_layers():
            entries.extend(layer.live_entries())
        return entries

    def has_live_entries(self) -> bool:
        """Return True when any layer has broker-live entries."""
        return any(layer.has_live_entries() for layer in self.grid.iter_layers())

    def all_counter_entries(self) -> list[FilledEntry]:
        """Return live counter entries in grid order."""
        entries: list[FilledEntry] = []
        for layer in self.grid.iter_layers():
            entries.extend(layer.counter_entries())
        return entries

    def all_present_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with any entry in grid order."""
        return list(self.iter_present_slots())

    def iter_present_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with any entry in grid order."""
        for layer in self.grid.iter_layers():
            for slot in layer.iter_slots():
                if slot.is_present:
                    yield layer, slot

    def filled_stop_loss_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots holding filled stop-loss entries waiting for rebuild."""
        return list(self.iter_filled_stop_loss_slots())

    def iter_filled_stop_loss_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots holding filled stop-loss entries waiting for rebuild."""
        for layer in self.grid.iter_layers():
            for slot in layer.iter_slots():
                if slot.filled_stop_loss_entry is not None:
                    yield layer, slot

    def requested_stop_loss_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with stop-loss closes waiting for fill confirmation."""
        return list(self.iter_requested_stop_loss_slots())

    def iter_requested_stop_loss_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with stop-loss closes waiting for fill confirmation."""
        for layer in self.grid.iter_layers():
            for slot in layer.iter_slots():
                if slot.requested_stop_loss_entry is not None:
                    yield layer, slot

    def requested_close_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with non-stop-loss closes waiting for fill confirmation."""
        return list(self.iter_requested_close_slots())

    def iter_requested_close_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with non-stop-loss closes waiting for fill confirmation."""
        for layer in self.grid.iter_layers():
            for slot in layer.iter_slots():
                if slot.requested_close_entry is not None:
                    yield layer, slot

    def requested_entry_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with entries waiting for fill confirmation."""
        return list(self.iter_requested_entry_slots())

    def iter_requested_entry_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with entries waiting for fill confirmation."""
        for layer in self.grid.iter_layers():
            for slot in layer.iter_slots():
                if slot.requested_entry is not None:
                    yield layer, slot

    def head_entry(self) -> FilledEntry | None:
        """Return the lowest L/R live entry."""
        for layer in self.grid.iter_layers():
            for slot in layer.iter_slots():
                entry = slot.live_or_pending_close_entry()
                if entry is not None:
                    return entry
        return None

    def tail_present_slot(self) -> tuple[Layer, Slot] | None:
        """Return the highest L/R slot with any entry."""
        for layer in self.grid.reversed_layers():
            for slot in layer.reversed_slots():
                if slot.is_present:
                    return layer, slot
        return None

    def has_filled_stop_loss_entries(self) -> bool:
        """Return True when any slot holds a filled stop-loss entry."""
        return next(self.iter_filled_stop_loss_slots(), None) is not None

    def has_requested_stop_losses(self) -> bool:
        """Return True when any stop-loss close is waiting for fill confirmation."""
        return next(self.iter_requested_stop_loss_slots(), None) is not None

    def has_requested_closes(self) -> bool:
        """Return True when any non-stop-loss close is waiting for fill confirmation."""
        return next(self.iter_requested_close_slots(), None) is not None

    def has_requested_entries(self) -> bool:
        """Return True when any entry is waiting for fill confirmation."""
        return next(self.iter_requested_entry_slots(), None) is not None

    def is_empty(self) -> bool:
        """Return True when there are no live entries."""
        return not self.has_live_entries()
