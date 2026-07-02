"""L/R grid structure for Snowball cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from core import Money
from pydantic import AwareDatetime

from snowball.enums import EntryRole, SlotStatus
from snowball.models.entries import Entry, PendingRebuild


@dataclass(frozen=True, slots=True)
class GridSlotKey:
    """Stable entry key derived from cycle and slot structure."""

    cycle_id: UUID
    layer_number: int
    retracement_count: int
    build_count: int

    @property
    def entry_id(self) -> str:
        """Return a structure-derived entry identifier."""
        return (
            f"C{self.cycle_id}:L{self.layer_number}:R{self.retracement_count}:B{self.build_count}"
        )

    @property
    def role(self) -> EntryRole:
        """Return the entry role derived from the grid position."""
        if self.retracement_count > 0:
            return EntryRole.COUNTER
        if self.layer_number == 1:
            return EntryRole.INITIAL
        return EntryRole.LAYER_INITIAL

    def to_metadata(self) -> dict[str, int | str]:
        """Return metadata values for Core strategy events."""
        return {
            "entry_id": self.entry_id,
            "entry_role": self.role.value,
            "layer_number": self.layer_number,
            "retracement_count": self.retracement_count,
            "build_count": self.build_count,
        }


@dataclass(slots=True)
class Slot:
    """One retracement slot within a layer.

    A slot moves through four states, each fully determined by its fields:
    ``AVAILABLE`` (empty), ``OCCUPIED`` (a live ``entry``), ``PENDING_REBUILD``
    (a stopped entry awaiting rebuild), and ``SEALED`` (closed, not refillable).
    All transitions go through the methods below so the state stays consistent.
    """

    entry: Entry | None = None
    pending_rebuild: PendingRebuild | None = None
    sealed: bool = False
    build_count: int = 0

    @property
    def status(self) -> SlotStatus:
        """Return the slot lifecycle state."""
        if self.entry is not None:
            return SlotStatus.OCCUPIED
        if self.pending_rebuild is not None:
            return SlotStatus.PENDING_REBUILD
        if self.sealed:
            return SlotStatus.SEALED
        return SlotStatus.AVAILABLE

    @property
    def is_present(self) -> bool:
        """Return True when the slot blocks lower-numbered refill."""
        return self.entry is not None or self.pending_rebuild is not None

    @property
    def is_available(self) -> bool:
        """Return True when a new entry may be placed here."""
        return self.status == SlotStatus.AVAILABLE

    def place(self, entry: Entry) -> None:
        """Place a live entry in an available slot."""
        if not self.is_available:
            raise ValueError("slot is not available")
        self.entry = entry
        self.pending_rebuild = None
        self.sealed = False
        self.build_count += 1

    def close_for_take_profit(self, *, refillable: bool) -> Entry:
        """Remove a live entry after a normal take-profit close."""
        if self.entry is None:
            raise ValueError("slot has no live entry")
        entry = self.entry
        self.entry = None
        self.sealed = not refillable
        return entry

    def close_for_stop_loss(
        self,
        *,
        closed_at: AwareDatetime,
        stop_loss_exit_price: Money,
        rebuildable: bool,
    ) -> Entry:
        """Remove a live entry after stop loss, optionally staging a rebuild."""
        if self.entry is None:
            raise ValueError("slot has no live entry")
        entry = self.entry
        self.entry = None
        if rebuildable:
            self.pending_rebuild = PendingRebuild(
                entry=entry,
                closed_at=closed_at,
                stop_loss_exit_price=stop_loss_exit_price,
            )
            self.sealed = False
        else:
            self.pending_rebuild = None
            self.sealed = True
        return entry

    def complete_rebuild(self, entry: Entry) -> None:
        """Replace a pending rebuild with a live rebuilt entry."""
        if self.pending_rebuild is None:
            raise ValueError("slot has no pending rebuild")
        self.entry = entry
        self.pending_rebuild = None
        self.sealed = False
        self.build_count += 1

    def reference_entry_price(self) -> Money | None:
        """Return the live or pending entry price used for distance checks."""
        if self.entry is not None:
            return self.entry.entry_price
        if self.pending_rebuild is not None:
            return self.pending_rebuild.entry_price
        return None

    def reference_take_profit_price(self) -> Money | None:
        """Return the live or pending take-profit price."""
        if self.entry is not None:
            return self.entry.take_profit_price
        if self.pending_rebuild is not None:
            return self.pending_rebuild.take_profit_price
        return None

    def reference_stop_loss_price(self) -> Money | None:
        """Return the live or pending stop-loss price."""
        if self.entry is not None:
            return self.entry.stop_loss_price
        if self.pending_rebuild is not None:
            return self.pending_rebuild.stop_loss_price
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
    _slots: list[Slot] = field(default_factory=list)

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
            _slots=[Slot() for _ in range(max_retracements + 1)],
        )

    @classmethod
    def from_slots(cls, *, base_units: Decimal, slots: list[Slot]) -> Layer:
        """Rebuild a layer from previously serialized slots."""
        return cls(base_units=base_units, _slots=list(slots))

    @property
    def slots(self) -> tuple[Slot, ...]:
        """Return the layer's slots in ascending R order (read-only view)."""
        return tuple(self._slots)

    def slot(self, retracement_count: int) -> Slot:
        """Return one retracement slot."""
        return self._slots[retracement_count]

    def retracement_count(self, slot: Slot) -> int:
        """Return the R number derived from a slot's position in this layer."""
        for index, candidate in enumerate(self._slots):
            if candidate is slot:
                return index
        raise ValueError("slot does not belong to this layer")

    @property
    def r0(self) -> Slot:
        """Return the layer's R0 slot."""
        return self.slot(0)

    def live_entries(self) -> list[Entry]:
        """Return live entries in ascending R order."""
        return [slot.entry for slot in self._slots if slot.entry is not None]

    def counter_entries(self) -> list[Entry]:
        """Return live R1+ entries in ascending R order."""
        return [slot.entry for slot in self._slots[1:] if slot.entry is not None]

    def present_slots(self) -> list[Slot]:
        """Return slots with live or pending logical presence."""
        return [slot for slot in self._slots if slot.is_present]

    def highest_present_slot(self) -> Slot | None:
        """Return the highest-R live or pending slot."""
        for slot in reversed(self._slots):
            if slot.is_present:
                return slot
        return None

    def highest_live_slot(self) -> Slot | None:
        """Return the highest-R live slot."""
        for slot in reversed(self._slots):
            if slot.entry is not None:
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
        for retracement_count, slot in enumerate(self._slots[1:], start=1):
            if slot.pending_rebuild is not None:
                continue
            if slot.entry is not None:
                continue
            if slot.sealed:
                return None
            if retracement_count > max_refillable_retracement and slot.build_count > 0:
                return None
            higher_present = any(
                higher.is_present for higher in self._slots[retracement_count + 1 :]
            )
            if higher_present and slot.build_count > 0:
                continue
            return slot
        return None

    def is_empty(self) -> bool:
        """Return True when the layer has no live or pending entries."""
        return not any(slot.is_present for slot in self._slots)


@dataclass(slots=True)
class Grid:
    """Layered L/R grid for a single directional cycle.

    The layer list is kept private so callers cannot append or pop layers
    directly; use ``add_layer`` / ``remove_empty_top_layers`` to change the
    structure and ``layers`` for a read-only view.
    """

    _layers: list[Layer]

    @classmethod
    def create(cls, *, base_units: Decimal, max_retracements: int) -> Grid:
        """Create a grid with one empty L1 layer."""
        return cls(
            _layers=[
                Layer.create(
                    base_units=base_units,
                    max_retracements=max_retracements,
                )
            ]
        )

    @classmethod
    def from_layers(cls, layers: list[Layer]) -> Grid:
        """Rebuild a grid from previously serialized layers."""
        return cls(_layers=list(layers))

    @property
    def layers(self) -> tuple[Layer, ...]:
        """Return the grid's layers from L1 upward (read-only view)."""
        return tuple(self._layers)

    @property
    def current_layer(self) -> Layer:
        """Return the highest-numbered layer."""
        return self._layers[-1]

    def add_layer(self, *, base_units: Decimal, max_retracements: int) -> Layer:
        """Append and return a new layer."""
        layer = Layer.create(
            base_units=base_units,
            max_retracements=max_retracements,
        )
        self._layers.append(layer)
        return layer

    def layer_number(self, layer: Layer) -> int:
        """Return the L number derived from a layer's position in this grid."""
        for index, candidate in enumerate(self._layers, start=1):
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

    def all_live_entries(self) -> list[Entry]:
        """Return all live entries in grid order."""
        entries: list[Entry] = []
        for layer in self._layers:
            entries.extend(layer.live_entries())
        return entries

    def all_counter_entries(self) -> list[Entry]:
        """Return live counter entries in grid order."""
        entries: list[Entry] = []
        for layer in self._layers:
            entries.extend(layer.counter_entries())
        return entries

    def all_present_slots(self) -> list[tuple[Layer, Slot]]:
        """Return live or pending slots in grid order."""
        present: list[tuple[Layer, Slot]] = []
        for layer in self._layers:
            present.extend((layer, slot) for slot in layer.slots if slot.is_present)
        return present

    def pending_rebuild_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots waiting for rebuild."""
        pending: list[tuple[Layer, Slot]] = []
        for layer in self._layers:
            pending.extend(
                (layer, slot) for slot in layer.slots if slot.pending_rebuild is not None
            )
        return pending

    def head_entry(self) -> Entry | None:
        """Return the lowest L/R live entry."""
        entries = self.all_live_entries()
        return entries[0] if entries else None

    def effective_head(self) -> Entry | None:
        """Return the live head entry, falling back to the oldest pending rebuild."""
        head = self.head_entry()
        if head is not None:
            return head
        for _layer, slot in self.pending_rebuild_slots():
            if slot.pending_rebuild is not None:
                return slot.pending_rebuild.entry
        return None

    def tail_present_slot(self) -> tuple[Layer, Slot] | None:
        """Return the highest L/R live or pending slot."""
        present = self.all_present_slots()
        return present[-1] if present else None

    def has_pending_rebuilds(self) -> bool:
        """Return True when any slot is waiting for rebuild."""
        return bool(self.pending_rebuild_slots())

    def is_empty(self) -> bool:
        """Return True when there are no live entries."""
        return not self.all_live_entries()

    def remove_empty_top_layers(self) -> None:
        """Remove empty non-L1 layers from the top of the grid."""
        while len(self._layers) > 1 and self._layers[-1].is_empty():
            self._layers.pop()

    def shrink_front_entry(self) -> Entry | None:
        """Return the lowest L/R live entry eligible as a shrink candidate."""
        return self.head_entry()

    def slot_key(self, *, cycle_id: UUID, layer: Layer, slot: Slot) -> GridSlotKey:
        """Return a structure-derived key for one slot."""
        return GridSlotKey(
            cycle_id=cycle_id,
            layer_number=self.layer_number(layer),
            retracement_count=layer.retracement_count(slot),
            build_count=slot.build_count,
        )

    def find_entry_slot(self, entry: Entry) -> tuple[Layer, Slot] | None:
        """Find the layer and slot containing an entry."""
        for layer in self._layers:
            for slot in layer.slots:
                if slot.entry is entry:
                    return layer, slot
        return None
