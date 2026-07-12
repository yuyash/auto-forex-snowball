"""L/R grid structure for Snowball cycles."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from core import Money, Units
from pydantic import AwareDatetime

from snowball.enums import CloseReason, EntryRole, SlotStatus
from snowball.models.entries import (
    FilledEntry,
    FilledStopLossEntry,
    RequestedCloseEntry,
    RequestedEntry,
    RequestedStopLossEntry,
    SealedEntry,
)
from snowball.models.grid_index import (
    ContiguousNumbering,
    LayerBuildNumberRegistry,
    ObjectNumberIndex,
)
from snowball.models.grid_queries import GridQuery, LayerQuery
from snowball.models.identifiers import CycleId, EntryId, IntegerIdGenerator
from snowball.models.position import GridPosition
from snowball.models.slot_lifecycle import SlotLifecycle
from snowball.models.slot_validation import SlotEntryValidation

type Entry = (
    RequestedEntry
    | FilledEntry
    | RequestedCloseEntry
    | RequestedStopLossEntry
    | FilledStopLossEntry
    | SealedEntry
)


@dataclass(slots=True, init=False)
class Slot:
    """One retracement slot within a layer."""

    _entry: Entry | None

    def __init__(self) -> None:
        self._entry = None

    @classmethod
    def restore(cls, entry: Entry | None) -> Slot:
        """Restore a slot at a serialization boundary."""
        slot = cls()
        if entry is not None:
            slot._validate_entry(entry)
        slot._entry = entry
        return slot

    @property
    def entry(self) -> Entry | None:
        """Return the current slot entry without exposing mutation."""
        return self._entry

    @property
    def requested_entry(self) -> RequestedEntry | None:
        """Return the requested entry waiting for fill confirmation."""
        return SlotLifecycle.requested_entry(self._entry)

    @property
    def filled_entry(self) -> FilledEntry | None:
        """Return the filled live entry held by this slot."""
        return SlotLifecycle.filled_entry(self._entry)

    @property
    def requested_close_entry(self) -> RequestedCloseEntry | None:
        """Return the requested non-stop-loss close held by this slot."""
        return SlotLifecycle.requested_close_entry(self._entry)

    @property
    def requested_stop_loss_entry(self) -> RequestedStopLossEntry | None:
        """Return the requested stop-loss close held by this slot."""
        return SlotLifecycle.requested_stop_loss_entry(self._entry)

    @property
    def filled_stop_loss_entry(self) -> FilledStopLossEntry | None:
        """Return the filled stop-loss entry held by this slot."""
        return SlotLifecycle.filled_stop_loss_entry(self._entry)

    @property
    def sealed_entry(self) -> SealedEntry | None:
        """Return the sealed-entry marker held by this slot."""
        return SlotLifecycle.sealed_entry(self._entry)

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
            or self.requested_close_entry is not None
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
        return self._entry is not None

    @property
    def is_available(self) -> bool:
        """Return True when a new entry may be placed here."""
        return self.status == SlotStatus.AVAILABLE

    def place_entry(self, entry: RequestedEntry, *, expected_entry_id: EntryId) -> None:
        """Place a requested entry in an available slot."""
        self._entry = SlotLifecycle.place_entry(
            self._entry,
            entry,
            expected_entry_id=expected_entry_id,
        )

    def fill_entry(self, entry: FilledEntry) -> None:
        """Replace a requested entry with its filled entry."""
        self._entry = SlotLifecycle.fill_entry(self._entry, entry)

    def request_close(
        self,
        *,
        planned_at: AwareDatetime,
        planned_exit_price: Money,
        close_reason: CloseReason,
        refillable: bool,
    ) -> FilledEntry:
        """Replace a live entry with a requested non-stop-loss close."""
        requested, entry = SlotLifecycle.request_close(
            self._entry,
            planned_at=planned_at,
            planned_exit_price=planned_exit_price,
            close_reason=close_reason,
            refillable=refillable,
        )
        self._entry = requested
        return entry

    def fill_close(self, *, filled_at: AwareDatetime) -> FilledEntry:
        """Replace a requested non-stop-loss close with its filled state."""
        next_entry, entry = SlotLifecycle.fill_close(self._entry, filled_at=filled_at)
        self._entry = next_entry
        return entry

    def request_stop_loss(
        self,
        *,
        planned_at: AwareDatetime,
        planned_stop_loss_price: Money,
    ) -> FilledEntry:
        """Replace a live entry with a requested stop-loss close."""
        requested, entry = SlotLifecycle.request_stop_loss(
            self._entry,
            planned_at=planned_at,
            planned_stop_loss_price=planned_stop_loss_price,
        )
        self._entry = requested
        return entry

    def fill_stop_loss(
        self,
        *,
        filled_at: AwareDatetime,
        filled_stop_loss_price: Money,
        rebuildable: bool,
        planned_rebuild_price: Money | None,
    ) -> FilledEntry:
        """Replace a requested stop-loss close with its filled state."""
        next_entry, entry = SlotLifecycle.fill_stop_loss(
            self._entry,
            filled_at=filled_at,
            filled_stop_loss_price=filled_stop_loss_price,
            rebuildable=rebuildable,
            planned_rebuild_price=planned_rebuild_price,
        )
        self._entry = next_entry
        return entry

    def complete_rebuild(self, entry: RequestedEntry, *, expected_entry_id: EntryId) -> None:
        """Replace a pending rebuild with a requested rebuilt entry."""
        self._entry = SlotLifecycle.complete_rebuild(
            self._entry,
            entry,
            expected_entry_id=expected_entry_id,
        )

    def unseal(self) -> None:
        """Replace a sealed entry with an available slot."""
        self._entry = SlotLifecycle.unseal(self._entry)

    def validate_entry(self, *, expected_entry_id: EntryId | None = None) -> None:
        """Validate the current entry and its expected grid identity."""
        if self._entry is None:
            return
        self._validate_entry(self._entry, expected_entry_id=expected_entry_id)

    def reference_entry_price(self) -> Money | None:
        """Return the filled entry price retained for grid calculations."""
        entry = self._reference_filled_entry()
        if entry is None:
            return None
        return entry.filled_entry_price

    def reference_filled_units(self) -> Units | None:
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

    def live_or_pending_close_entry(self) -> FilledEntry | None:
        """Return the broker-live entry, including one pending close confirmation."""
        if self.filled_entry is not None:
            return self.filled_entry
        if self.requested_close_entry is not None:
            return self.requested_close_entry.original_entry
        if self.requested_stop_loss_entry is not None:
            return self.requested_stop_loss_entry.original_entry
        return None

    def _reference_filled_entry(self) -> FilledEntry | None:
        if self.filled_entry is not None:
            return self.filled_entry
        if self.requested_close_entry is not None:
            return self.requested_close_entry.original_entry
        if self.requested_stop_loss_entry is not None:
            return self.requested_stop_loss_entry.original_entry
        if self.filled_stop_loss_entry is not None:
            return self.filled_stop_loss_entry.original_entry
        return None

    @classmethod
    def _validate_entry(
        cls,
        entry: Entry,
        *,
        expected_entry_id: EntryId | None = None,
    ) -> None:
        SlotEntryValidation.validate(entry, expected_entry_id=expected_entry_id)


@dataclass(slots=True)
class Layer:
    """One Snowball layer containing R0..Rmax slots.

    The slot list is fixed at creation and never grows or shrinks; only the
    contents of individual slots change. It is kept private so callers cannot
    add or remove slots behind the layer's back; use ``slots`` for a read-only
    view and the ``Slot`` methods to mutate a slot in place.
    """

    base_units: Units
    _slots: dict[int, Slot] = field(default_factory=dict)
    _build_number_generators: dict[int, IntegerIdGenerator] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _slot_numbers_by_id: dict[int, int] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _slot_index: ObjectNumberIndex[Slot] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _build_numbers: LayerBuildNumberRegistry = field(
        init=False,
        repr=False,
        compare=False,
    )
    _query: LayerQuery = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.base_units = Units.of(self.base_units)
        if self.base_units <= 0:
            raise ValueError("layer base units must be positive")
        self._validate_slot_numbers()
        self._slots = dict(sorted(self._slots.items()))
        self._slot_index = ObjectNumberIndex.from_mapping(
            self._slots,
            missing_message="slot does not belong to this layer",
        )
        self._slot_numbers_by_id = dict(self._slot_index.numbers_by_id)
        self._build_number_generators = dict(sorted(self._build_number_generators.items()))
        self._build_numbers = LayerBuildNumberRegistry(self._build_number_generators)
        self._build_numbers.validate_against(self._slots)
        self._build_number_generators = self._build_numbers.generators
        self._query = LayerQuery(self)

    @classmethod
    def create(
        cls,
        *,
        base_units: Units,
        max_retracements: int,
    ) -> Layer:
        """Create a layer with R0 through Rmax."""
        return cls(
            base_units=base_units,
            _slots={slot_number: Slot() for slot_number in range(max_retracements + 1)},
            _build_number_generators={
                slot_number: IntegerIdGenerator() for slot_number in range(max_retracements + 1)
            },
        )

    @classmethod
    def from_slots(
        cls,
        *,
        base_units: Units,
        slots: Mapping[int, Slot],
        build_numbers: Mapping[int, int] | None = None,
    ) -> Layer:
        """Rebuild a layer from previously serialized slots."""
        slot_map = dict(slots)
        number_map = dict(build_numbers or {})
        build_number_registry = LayerBuildNumberRegistry.restore(
            slots=slot_map,
            build_numbers=number_map,
        )
        return cls(
            base_units=base_units,
            _slots=slot_map,
            _build_number_generators=build_number_registry.generators,
        )

    @property
    def slots(self) -> tuple[Slot, ...]:
        """Return the layer's slots in ascending R order (read-only view)."""
        return tuple(self._slots.values())

    def iter_slots(self) -> Iterator[Slot]:
        """Iterate slots in ascending R order without allocating a tuple."""
        return iter(self._slots.values())

    def reversed_slots(self) -> Iterator[Slot]:
        """Iterate slots in descending R order without allocating a tuple."""
        return reversed(self._slots.values())

    def iter_slot_items(self) -> Iterator[tuple[int, Slot]]:
        """Iterate slot numbers and slots in ascending R order."""
        return iter(self._slots.items())

    def reversed_slot_items(self) -> Iterator[tuple[int, Slot]]:
        """Iterate slot numbers and slots in descending R order."""
        return reversed(self._slots.items())

    @property
    def slot_numbers(self) -> tuple[int, ...]:
        """Return slot numbers in ascending R order."""
        return tuple(self._slots)

    def slot(self, slot_number: int) -> Slot:
        """Return one retracement slot."""
        return self._slots[slot_number]

    def slot_number(self, slot: Slot) -> int:
        """Return the slot number derived from this layer's slot map."""
        return self._slot_index.number_for(slot)

    def retracement_count(self, slot: Slot) -> int:
        """Return the R number derived from a slot's position in this layer."""
        return self.slot_number(slot)

    def build_number(self, slot: Slot) -> int:
        """Return the latest build number assigned to this slot."""
        slot_number = self.slot_number(slot)
        return self._build_numbers.current(slot_number)

    def build_numbers(self) -> dict[int, int]:
        """Return latest build numbers keyed by slot number."""
        return self._build_numbers.current_all(tuple(self._slots))

    def next_build_number(self, slot: Slot) -> int:
        """Return the next build number for this slot."""
        return self._build_numbers.next(self.slot_number(slot))

    @staticmethod
    def _entry_build_number(slot: Slot) -> int:
        entry = slot.entry
        if entry is None:
            return 0
        return LayerBuildNumberRegistry.entry_build_number(slot)

    def validate_entries(self, *, cycle_id: CycleId, layer_number: int) -> None:
        """Validate entry identities for this layer position."""
        self._validate_slot_numbers()
        for slot_number, slot in self._slots.items():
            entry = slot.entry
            if entry is None:
                continue
            expected_entry_id = EntryId(
                cycle_id=cycle_id,
                layer_number=layer_number,
                slot_number=slot_number,
                build_number=entry.entry_id.build_number,
            )
            slot.validate_entry(expected_entry_id=expected_entry_id)
            assigned_build_number = self._build_number_generators[slot_number].next_value - 1
            if entry.entry_id.build_number != assigned_build_number:
                raise ValueError("slot entry build number does not match layer build number")

    def _validate_slot_numbers(self) -> None:
        ContiguousNumbering.require_from_zero(self._slots, name="layer slots")

    @property
    def r0(self) -> Slot:
        """Return the layer's R0 slot."""
        return self.slot(0)

    def live_entries(self) -> list[FilledEntry]:
        """Return broker-live entries in ascending R order."""
        return self._query.live_entries()

    def has_live_entries(self) -> bool:
        """Return True when any slot has a broker-live entry."""
        return self._query.has_live_entries()

    def live_entry_count(self) -> int:
        """Return the number of broker-live entries."""
        return self._query.live_entry_count()

    def counter_entries(self) -> list[FilledEntry]:
        """Return broker-live R1+ entries in ascending R order."""
        return self._query.counter_entries()

    def present_slots(self) -> list[Slot]:
        """Return slots with any live, closed, or sealed entry."""
        return self._query.present_slots()

    def highest_present_slot(self) -> Slot | None:
        """Return the highest-R slot with any entry."""
        return self._query.highest_present_slot()

    def highest_present_slot_number(self) -> int | None:
        """Return the highest R number with any entry."""
        return self._query.highest_present_slot_number()

    def highest_live_slot(self) -> Slot | None:
        """Return the highest-R slot with a directly closeable live entry."""
        return self._query.highest_live_slot()

    def is_empty(self) -> bool:
        """Return True when the layer has no slot entries."""
        return self._query.is_empty()


@dataclass(slots=True)
class Grid:
    """Layered L/R grid for a single directional cycle.

    The layer list is kept private so callers cannot append or pop layers
    directly; use ``add_layer`` / ``remove_empty_top_layers`` to change the
    structure and ``layers`` for a read-only view.
    """

    _layers: dict[int, Layer]
    _layer_numbers_by_id: dict[int, int] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _layer_index: ObjectNumberIndex[Layer] = field(
        init=False,
        repr=False,
        compare=False,
    )
    _query: GridQuery = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._validate_layer_numbers()
        self._layers = dict(sorted(self._layers.items()))
        self._layer_index = ObjectNumberIndex.from_mapping(
            self._layers,
            missing_message="layer does not belong to this grid",
        )
        self._layer_numbers_by_id = dict(self._layer_index.numbers_by_id)
        self._query = GridQuery(self)

    @classmethod
    def create(cls, *, base_units: Units, max_retracements: int) -> Grid:
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
        return tuple(self._layers.values())

    def iter_layers(self) -> Iterator[Layer]:
        """Iterate layers from L1 upward without allocating a tuple."""
        return iter(self._layers.values())

    def reversed_layers(self) -> Iterator[Layer]:
        """Iterate layers from highest L down to L1 without allocating a tuple."""
        return reversed(self._layers.values())

    def iter_layer_items(self) -> Iterator[tuple[int, Layer]]:
        """Iterate layer numbers and layers from L1 upward."""
        return iter(self._layers.items())

    @property
    def layer_count(self) -> int:
        """Return the number of layers."""
        return len(self._layers)

    @property
    def first_layer(self) -> Layer:
        """Return L1."""
        return self._layers[1]

    @property
    def current_layer(self) -> Layer:
        """Return the highest-numbered layer."""
        return next(reversed(self._layers.values()))

    def add_layer(self, *, base_units: Units, max_retracements: int) -> Layer:
        """Append and return a new layer."""
        layer_number = max(self._layers) + 1
        layer = Layer.create(
            base_units=base_units,
            max_retracements=max_retracements,
        )
        self._layers[layer_number] = layer
        self._layer_index = ObjectNumberIndex.from_mapping(
            self._layers,
            missing_message="layer does not belong to this grid",
        )
        self._layer_numbers_by_id = dict(self._layer_index.numbers_by_id)
        self._validate_layer_numbers()
        return layer

    def layer_number(self, layer: Layer) -> int:
        """Return the L number derived from a layer's position in this grid."""
        return self._layer_index.number_for(layer)

    def role_for(self, layer: Layer, slot: Slot) -> EntryRole:
        """Return the entry role derived from layer and slot positions."""
        return GridPosition(
            layer_number=self.layer_number(layer),
            slot_number=layer.slot_number(slot),
        ).role

    def all_live_entries(self) -> list[FilledEntry]:
        """Return all live entries in grid order."""
        return self._query.all_live_entries()

    def has_live_entries(self) -> bool:
        """Return True when any layer has broker-live entries."""
        return self._query.has_live_entries()

    def all_counter_entries(self) -> list[FilledEntry]:
        """Return live counter entries in grid order."""
        return self._query.all_counter_entries()

    def all_present_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with any entry in grid order."""
        return self._query.all_present_slots()

    def iter_present_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with any entry in grid order."""
        yield from self._query.iter_present_slots()

    def filled_stop_loss_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots holding filled stop-loss entries waiting for rebuild."""
        return self._query.filled_stop_loss_slots()

    def iter_filled_stop_loss_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots holding filled stop-loss entries waiting for rebuild."""
        yield from self._query.iter_filled_stop_loss_slots()

    def requested_stop_loss_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with stop-loss closes waiting for fill confirmation."""
        return self._query.requested_stop_loss_slots()

    def iter_requested_stop_loss_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with stop-loss closes waiting for fill confirmation."""
        yield from self._query.iter_requested_stop_loss_slots()

    def requested_close_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with non-stop-loss closes waiting for fill confirmation."""
        return self._query.requested_close_slots()

    def iter_requested_close_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with non-stop-loss closes waiting for fill confirmation."""
        yield from self._query.iter_requested_close_slots()

    def requested_entry_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with entries waiting for fill confirmation."""
        return self._query.requested_entry_slots()

    def iter_requested_entry_slots(self) -> Iterator[tuple[Layer, Slot]]:
        """Iterate slots with entries waiting for fill confirmation."""
        yield from self._query.iter_requested_entry_slots()

    def head_entry(self) -> FilledEntry | None:
        """Return the lowest L/R live entry."""
        return self._query.head_entry()

    def tail_present_slot(self) -> tuple[Layer, Slot] | None:
        """Return the highest L/R slot with any entry."""
        return self._query.tail_present_slot()

    def has_filled_stop_loss_entries(self) -> bool:
        """Return True when any slot holds a filled stop-loss entry."""
        return self._query.has_filled_stop_loss_entries()

    def has_requested_stop_losses(self) -> bool:
        """Return True when any stop-loss close is waiting for fill confirmation."""
        return self._query.has_requested_stop_losses()

    def has_requested_closes(self) -> bool:
        """Return True when any non-stop-loss close is waiting for fill confirmation."""
        return self._query.has_requested_closes()

    def has_requested_entries(self) -> bool:
        """Return True when any entry is waiting for fill confirmation."""
        return self._query.has_requested_entries()

    def is_empty(self) -> bool:
        """Return True when there are no live entries."""
        return self._query.is_empty()

    def remove_empty_top_layers(self) -> None:
        """Remove empty non-L1 layers from the top of the grid."""
        while len(self._layers) > 1:
            top_layer_number = max(self._layers)
            top_layer = self._layers[top_layer_number]
            if not top_layer.is_empty():
                return
            del self._layers[top_layer_number]
            self._layer_index = ObjectNumberIndex.from_mapping(
                self._layers,
                missing_message="layer does not belong to this grid",
            )
            self._layer_numbers_by_id = dict(self._layer_index.numbers_by_id)
        self._validate_layer_numbers()

    def next_entry_id(self, *, cycle_id: CycleId, layer: Layer, slot: Slot) -> EntryId:
        """Return the next entry identifier for one slot."""
        return EntryId(
            cycle_id=cycle_id,
            layer_number=self.layer_number(layer),
            slot_number=layer.slot_number(slot),
            build_number=layer.next_build_number(slot),
        )

    def find_entry_slot(self, entry: FilledEntry) -> tuple[Layer, Slot] | None:
        """Find the layer and slot containing an entry."""
        for layer in self.iter_layers():
            for slot in layer.iter_slots():
                if slot.live_or_pending_close_entry() is entry:
                    return layer, slot
        return None

    def validate_for_cycle(self, cycle_id: CycleId) -> None:
        """Validate layer order and every slot entry for a cycle."""
        self._validate_layer_numbers()
        for layer_number, layer in self._layers.items():
            layer.validate_entries(cycle_id=cycle_id, layer_number=layer_number)

    def _validate_layer_numbers(self) -> None:
        ContiguousNumbering.require_from_one(self._layers, name="grid layers")
