"""L/R grid structure for Snowball cycles."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from core import Money

from snowball.enums import EntryRole, SlotStatus
from snowball.models.entries import Entry, SlotExitPlan, SlotPosition, StopLossSnapshot


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
    """One retracement slot within a layer."""

    entry: Entry | None = None
    exit_plan: SlotExitPlan | None = None
    pending_rebuild: StopLossSnapshot | None = None
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

    def fill(self, *, entry: Entry, exit_plan: SlotExitPlan) -> None:
        """Place a live entry in the slot."""
        if not self.is_available and self.pending_rebuild is None:
            raise ValueError("slot is not fillable")
        self.entry = entry
        self.exit_plan = exit_plan
        self.pending_rebuild = None
        self.sealed = False
        self.build_count += 1

    def close_for_take_profit(self, *, refillable: bool) -> Entry:
        """Remove a live entry after normal TP close."""
        if self.entry is None:
            raise ValueError("slot has no live entry")
        entry = self.entry
        self.entry = None
        self.exit_plan = None
        self.sealed = not refillable
        return entry

    def close_for_stop_loss(self, snapshot: StopLossSnapshot | None) -> Entry:
        """Remove a live entry after stop loss."""
        if self.entry is None or self.exit_plan is None:
            raise ValueError("slot has no live entry")
        entry = self.entry
        self.entry = None
        self.exit_plan = None
        self.pending_rebuild = snapshot
        self.sealed = snapshot is None
        return entry

    def complete_rebuild(self, *, entry: Entry, exit_plan: SlotExitPlan) -> None:
        """Replace a pending rebuild snapshot with a live rebuilt entry."""
        if self.pending_rebuild is None:
            raise ValueError("slot has no pending rebuild")
        self.entry = entry
        self.exit_plan = exit_plan
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
        if self.exit_plan is not None:
            return self.exit_plan.take_profit_price
        if self.pending_rebuild is not None:
            return self.pending_rebuild.exit_plan.take_profit_price
        return None

    def reference_stop_loss_price(self) -> Money | None:
        """Return the live or pending stop-loss price."""
        if self.exit_plan is not None:
            return self.exit_plan.stop_loss_price
        if self.pending_rebuild is not None:
            return self.pending_rebuild.exit_plan.stop_loss_price
        return None


@dataclass(slots=True)
class Layer:
    """One Snowball layer containing R0..Rmax slots."""

    base_units: Decimal
    slots: list[Slot] = field(default_factory=list)

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
            slots=[Slot() for _ in range(max_retracements + 1)],
        )

    def slot(self, retracement_count: int) -> Slot:
        """Return one retracement slot."""
        return self.slots[retracement_count]

    def retracement_count(self, slot: Slot) -> int:
        """Return the R number derived from a slot's position in this layer."""
        for index, candidate in enumerate(self.slots):
            if candidate is slot:
                return index
        raise ValueError("slot does not belong to this layer")

    @property
    def r0(self) -> Slot:
        """Return the layer's R0 slot."""
        return self.slot(0)

    def live_entries(self) -> list[Entry]:
        """Return live entries in ascending R order."""
        return [slot.entry for slot in self.slots if slot.entry is not None]

    def counter_entries(self) -> list[Entry]:
        """Return live R1+ entries in ascending R order."""
        return [slot.entry for slot in self.slots[1:] if slot.entry is not None]

    def present_slots(self) -> list[Slot]:
        """Return slots with live or pending logical presence."""
        return [slot for slot in self.slots if slot.is_present]

    def highest_present_slot(self) -> Slot | None:
        """Return the highest-R live or pending slot."""
        for slot in reversed(self.slots):
            if slot.is_present:
                return slot
        return None

    def highest_live_slot(self) -> Slot | None:
        """Return the highest-R live slot."""
        for slot in reversed(self.slots):
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
        for retracement_count, slot in enumerate(self.slots[1:], start=1):
            if slot.pending_rebuild is not None:
                continue
            if slot.entry is not None:
                continue
            if slot.sealed:
                return None
            if retracement_count > max_refillable_retracement and slot.build_count > 0:
                return None
            higher_present = any(
                higher.is_present for higher in self.slots[retracement_count + 1 :]
            )
            if higher_present and slot.build_count > 0:
                continue
            return slot
        return None

    def is_empty(self) -> bool:
        """Return True when the layer has no live or pending entries."""
        return not any(slot.is_present for slot in self.slots)


@dataclass(slots=True)
class Grid:
    """Layered L/R grid for a single directional cycle."""

    layers: list[Layer]

    @classmethod
    def create(cls, *, base_units: Decimal, max_retracements: int) -> Grid:
        """Create a grid with one empty L1 layer."""
        return cls(
            layers=[
                Layer.create(
                    base_units=base_units,
                    max_retracements=max_retracements,
                )
            ]
        )

    @property
    def current_layer(self) -> Layer:
        """Return the highest-numbered layer."""
        return self.layers[-1]

    def add_layer(self, *, base_units: Decimal, max_retracements: int) -> Layer:
        """Append and return a new layer."""
        layer = Layer.create(
            base_units=base_units,
            max_retracements=max_retracements,
        )
        self.layers.append(layer)
        return layer

    def layer_number(self, layer: Layer) -> int:
        """Return the L number derived from a layer's position in this grid."""
        for index, candidate in enumerate(self.layers, start=1):
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
        for layer in self.layers:
            entries.extend(layer.live_entries())
        return entries

    def all_counter_entries(self) -> list[Entry]:
        """Return live counter entries in grid order."""
        entries: list[Entry] = []
        for layer in self.layers:
            entries.extend(layer.counter_entries())
        return entries

    def all_present_slots(self) -> list[tuple[Layer, Slot]]:
        """Return live or pending slots in grid order."""
        present: list[tuple[Layer, Slot]] = []
        for layer in self.layers:
            present.extend((layer, slot) for slot in layer.slots if slot.is_present)
        return present

    def pending_rebuild_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots waiting for rebuild."""
        pending: list[tuple[Layer, Slot]] = []
        for layer in self.layers:
            pending.extend(
                (layer, slot) for slot in layer.slots if slot.pending_rebuild is not None
            )
        return pending

    def head_entry(self) -> Entry | None:
        """Return the lowest L/R live entry."""
        entries = self.all_live_entries()
        return entries[0] if entries else None

    def effective_head(self) -> SlotPosition | None:
        """Return live head, falling back to the oldest pending rebuild."""
        head = self.head_entry()
        if head is not None:
            return head
        for _layer, slot in self.pending_rebuild_slots():
            if slot.pending_rebuild is not None:
                return slot.pending_rebuild
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
        while len(self.layers) > 1 and self.layers[-1].is_empty():
            self.layers.pop()

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
        for layer in self.layers:
            for slot in layer.slots:
                if slot.entry is entry:
                    return layer, slot
        return None
