"""L/R grid structure for Snowball cycles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from core import Money
from pydantic import AwareDatetime

from snowball.enums import EntryRole, SlotStatus
from snowball.models.entries import (
    FilledEntry,
    FilledStopLossEntry,
    RequestedEntry,
    RequestedStopLossEntry,
    SealedEntry,
)
from snowball.models.identifiers import CycleId, EntryId, IntegerIdGenerator

type Entry = (
    RequestedEntry | FilledEntry | RequestedStopLossEntry | FilledStopLossEntry | SealedEntry
)


@dataclass(slots=True)
class Slot:
    """One retracement slot within a layer."""

    entry: Entry | None = None

    @property
    def requested_entry(self) -> RequestedEntry | None:
        """Return the requested entry waiting for fill confirmation."""
        return self.entry if isinstance(self.entry, RequestedEntry) else None

    @property
    def filled_entry(self) -> FilledEntry | None:
        """Return the filled live entry held by this slot."""
        return self.entry if isinstance(self.entry, FilledEntry) else None

    @property
    def requested_stop_loss_entry(self) -> RequestedStopLossEntry | None:
        """Return the requested stop-loss close held by this slot."""
        return self.entry if isinstance(self.entry, RequestedStopLossEntry) else None

    @property
    def filled_stop_loss_entry(self) -> FilledStopLossEntry | None:
        """Return the filled stop-loss entry held by this slot."""
        return self.entry if isinstance(self.entry, FilledStopLossEntry) else None

    @property
    def sealed_entry(self) -> SealedEntry | None:
        """Return the sealed-entry marker held by this slot."""
        return self.entry if isinstance(self.entry, SealedEntry) else None

    @property
    def is_sealed(self) -> bool:
        """Return True when the slot is closed and not refillable."""
        return self.sealed_entry is not None

    @property
    def status(self) -> SlotStatus:
        """Return the slot lifecycle state."""
        if (
            self.requested_entry is not None
            or self.filled_entry is not None
            or self.requested_stop_loss_entry is not None
        ):
            return SlotStatus.OCCUPIED
        if self.filled_stop_loss_entry is not None:
            return SlotStatus.PENDING_REBUILD
        if self.sealed_entry is not None:
            return SlotStatus.SEALED
        return SlotStatus.AVAILABLE

    @property
    def is_present(self) -> bool:
        """Return True when the slot blocks lower-numbered refill."""
        return self.entry is not None

    @property
    def is_available(self) -> bool:
        """Return True when a new entry may be placed here."""
        return self.status == SlotStatus.AVAILABLE

    def place_entry(self, entry: RequestedEntry | FilledEntry) -> None:
        """Place a requested or filled entry in an available slot."""
        if not self.is_available:
            raise ValueError("slot is not available")
        self.entry = entry

    def fill_entry(self, entry: FilledEntry) -> None:
        """Replace a requested entry with its filled entry."""
        requested = self.requested_entry
        if requested is None:
            raise ValueError("slot has no requested entry")
        if entry.requested is not requested:
            raise ValueError("filled entry does not belong to this requested entry")
        self.entry = entry

    def close_for_take_profit(
        self,
        *,
        closed_at: AwareDatetime,
        refillable: bool,
    ) -> FilledEntry:
        """Remove a live entry after a normal take-profit close."""
        entry = self.filled_entry
        if entry is None:
            raise ValueError("slot has no live entry")
        self.entry = entry.close(closed_at=closed_at, refillable=refillable)
        return entry

    def request_stop_loss(
        self,
        *,
        requested_at: AwareDatetime,
        requested_stop_loss_exit_price: Money,
    ) -> FilledEntry:
        """Replace a live entry with a requested stop-loss close."""
        entry = self.filled_entry
        if entry is None:
            raise ValueError("slot has no live entry")
        self.entry = entry.stop_loss(
            requested_at=requested_at,
            requested_stop_loss_exit_price=requested_stop_loss_exit_price,
        )
        return entry

    def fill_stop_loss(
        self,
        *,
        filled_at: AwareDatetime,
        filled_stop_loss_exit_price: Money,
        rebuildable: bool,
        planned_rebuild_trigger_price: Money | None,
    ) -> FilledEntry:
        """Replace a requested stop-loss close with its filled state."""
        requested = self.requested_stop_loss_entry
        if requested is None:
            raise ValueError("slot has no requested stop-loss entry")
        self.entry = requested.fill(
            filled_at=filled_at,
            filled_stop_loss_exit_price=filled_stop_loss_exit_price,
            rebuildable=rebuildable,
            planned_rebuild_trigger_price=planned_rebuild_trigger_price,
        )
        return requested.original_entry

    def complete_rebuild(self, entry: RequestedEntry | FilledEntry) -> None:
        """Replace a pending rebuild with a requested or filled rebuilt entry."""
        stop_loss_entry = self.filled_stop_loss_entry
        if stop_loss_entry is None:
            raise ValueError("slot has no pending rebuild")
        self.entry = stop_loss_entry.rebuild(entry)

    def unseal(self) -> None:
        """Replace a sealed entry with an available slot."""
        sealed_entry = self.sealed_entry
        if sealed_entry is None:
            raise ValueError("slot is not sealed")
        self.entry = sealed_entry.unseal()

    def reference_entry_price(self) -> Money | None:
        """Return the filled entry price retained for grid calculations."""
        entry = self._reference_filled_entry()
        if entry is None:
            return None
        return entry.filled_entry_price

    def reference_filled_units(self) -> Decimal | None:
        """Return the filled units retained for grid calculations."""
        entry = self._reference_filled_entry()
        if entry is None:
            return None
        return entry.filled_units

    def reference_take_profit_price(self) -> Money | None:
        """Return the planned take-profit price retained for grid calculations."""
        entry = self._reference_filled_entry()
        if entry is None:
            return None
        return entry.planned_take_profit_price

    def reference_stop_loss_price(self) -> Money | None:
        """Return the planned stop-loss price retained for grid calculations."""
        entry = self._reference_filled_entry()
        if entry is None:
            return None
        return entry.planned_stop_loss_price

    def _reference_filled_entry(self) -> FilledEntry | None:
        if self.filled_entry is not None:
            return self.filled_entry
        if self.requested_stop_loss_entry is not None:
            return self.requested_stop_loss_entry.original_entry
        if self.filled_stop_loss_entry is not None:
            return self.filled_stop_loss_entry.original_entry
        return None


@dataclass(slots=True)
class Layer:
    """One Snowball layer containing R0..Rmax slots.

    The slot list is fixed at creation and never grows or shrinks; only the
    contents of individual slots change. It is kept private so callers cannot
    add or remove slots behind the layer's back; use ``slots`` for a read-only
    view and the ``Slot`` methods to mutate a slot in place.
    """

    base_units: Decimal
    _slots: dict[int, Slot] = field(default_factory=dict)
    _build_count_generators: dict[int, IntegerIdGenerator] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        missing_slot_numbers = set(self._slots) - set(self._build_count_generators)
        for slot_number in missing_slot_numbers:
            self._build_count_generators[slot_number] = IntegerIdGenerator(
                self._entry_build_count(self._slots[slot_number]) + 1
            )
        unknown_slot_numbers = set(self._build_count_generators) - set(self._slots)
        if unknown_slot_numbers:
            raise ValueError("build count generator references unknown slot")

    @classmethod
    def create(
        cls,
        *,
        base_units: Decimal,
        max_retracements: int,
    ) -> Layer:
        """Create a layer with R0 through Rmax."""
        return cls(
            base_units=base_units,
            _slots={slot_number: Slot() for slot_number in range(max_retracements + 1)},
            _build_count_generators={
                slot_number: IntegerIdGenerator() for slot_number in range(max_retracements + 1)
            },
        )

    @classmethod
    def from_slots(
        cls,
        *,
        base_units: Decimal,
        slots: Mapping[int, Slot],
        build_counts: Mapping[int, int] | None = None,
    ) -> Layer:
        """Rebuild a layer from previously serialized slots."""
        slot_map = dict(slots)
        count_map = dict(build_counts or {})
        unknown_slot_numbers = set(count_map) - set(slot_map)
        if unknown_slot_numbers:
            raise ValueError("build count references unknown slot")
        return cls(
            base_units=base_units,
            _slots=slot_map,
            _build_count_generators={
                slot_number: IntegerIdGenerator(
                    max(count_map.get(slot_number, 0), cls._entry_build_count(slot)) + 1
                )
                for slot_number, slot in slot_map.items()
            },
        )

    @property
    def slots(self) -> tuple[Slot, ...]:
        """Return the layer's slots in ascending R order (read-only view)."""
        return tuple(self._slots[slot_number] for slot_number in sorted(self._slots))

    def slot(self, slot_number: int) -> Slot:
        """Return one retracement slot."""
        return self._slots[slot_number]

    def slot_number(self, slot: Slot) -> int:
        """Return the slot number derived from this layer's slot map."""
        for slot_number, candidate in self._slots.items():
            if candidate is slot:
                return slot_number
        raise ValueError("slot does not belong to this layer")

    def retracement_count(self, slot: Slot) -> int:
        """Return the R number derived from a slot's position in this layer."""
        return self.slot_number(slot)

    def build_count(self, slot: Slot) -> int:
        """Return the latest build count assigned to this slot."""
        slot_number = self.slot_number(slot)
        return self._build_count_generators[slot_number].next_value - 1

    def build_counts(self) -> dict[int, int]:
        """Return latest build counts keyed by slot number."""
        return {
            slot_number: self._build_count_generators[slot_number].next_value - 1
            for slot_number in sorted(self._slots)
        }

    def next_build_count(self, slot: Slot) -> int:
        """Return the next build count for this slot."""
        return self._build_count_generators[self.slot_number(slot)].next()

    @staticmethod
    def _entry_build_count(slot: Slot) -> int:
        entry = slot.entry
        if entry is None:
            return 0
        return entry.entry_id.build_count

    @property
    def r0(self) -> Slot:
        """Return the layer's R0 slot."""
        return self.slot(0)

    def live_entries(self) -> list[FilledEntry]:
        """Return live entries in ascending R order."""
        entries: list[FilledEntry] = []
        for slot in self.slots:
            entry = slot.filled_entry
            if entry is not None:
                entries.append(entry)
        return entries

    def counter_entries(self) -> list[FilledEntry]:
        """Return live R1+ entries in ascending R order."""
        entries: list[FilledEntry] = []
        for slot_number in sorted(slot_number for slot_number in self._slots if slot_number > 0):
            slot = self._slots[slot_number]
            entry = slot.filled_entry
            if entry is not None:
                entries.append(entry)
        return entries

    def present_slots(self) -> list[Slot]:
        """Return slots with any live, closed, or sealed entry."""
        return [slot for slot in self.slots if slot.is_present]

    def highest_present_slot(self) -> Slot | None:
        """Return the highest-R slot with any entry."""
        for slot in reversed(self.slots):
            if slot.is_present:
                return slot
        return None

    def highest_live_slot(self) -> Slot | None:
        """Return the highest-R live slot."""
        for slot in reversed(self.slots):
            if slot.filled_entry is not None:
                return slot
        return None

    def next_available_counter_slot(
        self,
        *,
        max_refillable_retracement: int,
    ) -> Slot | None:
        """Return the next R1+ slot that can receive a counter entry.

        Lower refillable slots are not reused while any higher R slot is present.
        This preserves the Snowball grid progression.
        """
        slot_numbers = sorted(slot_number for slot_number in self._slots if slot_number > 0)
        for retracement_count in slot_numbers:
            slot = self._slots[retracement_count]
            if slot.is_sealed:
                return None
            if slot.entry is not None:
                continue
            if retracement_count > max_refillable_retracement and self.build_count(slot) > 0:
                return None
            higher_present = any(
                self._slots[higher_number].is_present
                for higher_number in slot_numbers
                if higher_number > retracement_count
            )
            if higher_present and self.build_count(slot) > 0:
                continue
            return slot
        return None

    def is_empty(self) -> bool:
        """Return True when the layer has no slot entries."""
        return not any(slot.is_present for slot in self.slots)


@dataclass(slots=True)
class Grid:
    """Layered L/R grid for a single directional cycle.

    The layer list is kept private so callers cannot append or pop layers
    directly; use ``add_layer`` / ``remove_empty_top_layers`` to change the
    structure and ``layers`` for a read-only view.
    """

    _layers: dict[int, Layer]

    @classmethod
    def create(cls, *, base_units: Decimal, max_retracements: int) -> Grid:
        """Create a grid with one empty L1 layer."""
        return cls(
            _layers={
                1: Layer.create(
                    base_units=base_units,
                    max_retracements=max_retracements,
                )
            }
        )

    @classmethod
    def from_layers(cls, layers: Mapping[int, Layer]) -> Grid:
        """Rebuild a grid from previously serialized layers."""
        return cls(_layers=dict(layers))

    @property
    def layers(self) -> tuple[Layer, ...]:
        """Return the grid's layers from L1 upward (read-only view)."""
        return tuple(self._layers[layer_number] for layer_number in sorted(self._layers))

    @property
    def current_layer(self) -> Layer:
        """Return the highest-numbered layer."""
        return self._layers[max(self._layers)]

    def add_layer(self, *, base_units: Decimal, max_retracements: int) -> Layer:
        """Append and return a new layer."""
        layer_number = max(self._layers) + 1
        layer = Layer.create(
            base_units=base_units,
            max_retracements=max_retracements,
        )
        self._layers[layer_number] = layer
        return layer

    def layer_number(self, layer: Layer) -> int:
        """Return the L number derived from a layer's position in this grid."""
        for index, candidate in self._layers.items():
            if candidate is layer:
                return index
        raise ValueError("layer does not belong to this grid")

    def role_for(self, layer: Layer, slot: Slot) -> EntryRole:
        """Return the entry role derived from layer and slot positions."""
        if layer.retracement_count(slot) > 0:
            return EntryRole.COUNTER
        if self.layer_number(layer) == 1:
            return EntryRole.INITIAL
        return EntryRole.LAYER_INITIAL

    def all_live_entries(self) -> list[FilledEntry]:
        """Return all live entries in grid order."""
        entries: list[FilledEntry] = []
        for layer in self.layers:
            entries.extend(layer.live_entries())
        return entries

    def all_counter_entries(self) -> list[FilledEntry]:
        """Return live counter entries in grid order."""
        entries: list[FilledEntry] = []
        for layer in self.layers:
            entries.extend(layer.counter_entries())
        return entries

    def all_present_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with any entry in grid order."""
        present: list[tuple[Layer, Slot]] = []
        for layer in self.layers:
            present.extend((layer, slot) for slot in layer.slots if slot.is_present)
        return present

    def filled_stop_loss_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots holding filled stop-loss entries waiting for rebuild."""
        slots: list[tuple[Layer, Slot]] = []
        for layer in self.layers:
            slots.extend(
                (layer, slot) for slot in layer.slots if slot.filled_stop_loss_entry is not None
            )
        return slots

    def requested_stop_loss_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with stop-loss closes waiting for fill confirmation."""
        requested: list[tuple[Layer, Slot]] = []
        for layer in self.layers:
            requested.extend(
                (layer, slot) for slot in layer.slots if slot.requested_stop_loss_entry is not None
            )
        return requested

    def requested_entry_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with entries waiting for fill confirmation."""
        requested: list[tuple[Layer, Slot]] = []
        for layer in self.layers:
            requested.extend((layer, slot) for slot in layer.slots if slot.requested_entry)
        return requested

    def head_entry(self) -> FilledEntry | None:
        """Return the lowest L/R live entry."""
        entries = self.all_live_entries()
        return entries[0] if entries else None

    def effective_head(self) -> FilledEntry | None:
        """Return the live head entry, falling back to retained original entries."""
        head = self.head_entry()
        if head is not None:
            return head
        for _layer, slot in self.requested_stop_loss_slots():
            if slot.requested_stop_loss_entry is not None:
                return slot.requested_stop_loss_entry.original_entry
        for _layer, slot in self.filled_stop_loss_slots():
            if slot.filled_stop_loss_entry is not None:
                return slot.filled_stop_loss_entry.original_entry
        return None

    def tail_present_slot(self) -> tuple[Layer, Slot] | None:
        """Return the highest L/R slot with any entry."""
        present = self.all_present_slots()
        return present[-1] if present else None

    def has_filled_stop_loss_entries(self) -> bool:
        """Return True when any slot holds a filled stop-loss entry."""
        return bool(self.filled_stop_loss_slots())

    def has_requested_stop_losses(self) -> bool:
        """Return True when any stop-loss close is waiting for fill confirmation."""
        return bool(self.requested_stop_loss_slots())

    def has_requested_entries(self) -> bool:
        """Return True when any entry is waiting for fill confirmation."""
        return bool(self.requested_entry_slots())

    def is_empty(self) -> bool:
        """Return True when there are no live entries."""
        return not self.all_live_entries()

    def remove_empty_top_layers(self) -> None:
        """Remove empty non-L1 layers from the top of the grid."""
        while len(self._layers) > 1:
            top_layer_number = max(self._layers)
            if not self._layers[top_layer_number].is_empty():
                return
            del self._layers[top_layer_number]

    def shrink_front_entry(self) -> FilledEntry | None:
        """Return the lowest L/R live entry eligible as a shrink candidate."""
        return self.head_entry()

    def next_entry_id(self, *, cycle_id: CycleId, layer: Layer, slot: Slot) -> EntryId:
        """Return the next entry identifier for one slot."""
        return EntryId(
            cycle_id=cycle_id,
            layer_number=self.layer_number(layer),
            slot_number=layer.slot_number(slot),
            build_count=layer.next_build_count(slot),
        )

    def find_entry_slot(self, entry: FilledEntry) -> tuple[Layer, Slot] | None:
        """Find the layer and slot containing an entry."""
        for layer in self.layers:
            for slot in layer.slots:
                if slot.filled_entry is entry:
                    return layer, slot
        return None
