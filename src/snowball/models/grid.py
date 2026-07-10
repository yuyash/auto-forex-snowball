"""L/R grid structure for Snowball cycles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from core import Money
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
from snowball.models.identifiers import CycleId, EntryId, EntryIdType, IntegerIdGenerator
from snowball.models.position import GridPosition

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
        return self._entry if isinstance(self._entry, RequestedEntry) else None

    @property
    def filled_entry(self) -> FilledEntry | None:
        """Return the filled live entry held by this slot."""
        return self._entry if isinstance(self._entry, FilledEntry) else None

    @property
    def requested_close_entry(self) -> RequestedCloseEntry | None:
        """Return the requested non-stop-loss close held by this slot."""
        return self._entry if isinstance(self._entry, RequestedCloseEntry) else None

    @property
    def requested_stop_loss_entry(self) -> RequestedStopLossEntry | None:
        """Return the requested stop-loss close held by this slot."""
        return self._entry if isinstance(self._entry, RequestedStopLossEntry) else None

    @property
    def filled_stop_loss_entry(self) -> FilledStopLossEntry | None:
        """Return the filled stop-loss entry held by this slot."""
        return self._entry if isinstance(self._entry, FilledStopLossEntry) else None

    @property
    def sealed_entry(self) -> SealedEntry | None:
        """Return the sealed-entry marker held by this slot."""
        return self._entry if isinstance(self._entry, SealedEntry) else None

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
        if not self.is_available:
            raise ValueError("slot is not available")
        self._validate_entry(entry, expected_entry_id=expected_entry_id)
        self._entry = entry

    def fill_entry(self, entry: FilledEntry) -> None:
        """Replace a requested entry with its filled entry."""
        requested = self.requested_entry
        if requested is None:
            raise ValueError("slot has no requested entry")
        if entry.requested is not requested:
            raise ValueError("filled entry does not belong to this requested entry")
        self._validate_entry(entry, expected_entry_id=requested.entry_id)
        self._entry = entry

    def request_close(
        self,
        *,
        requested_at: AwareDatetime,
        requested_exit_price: Money,
        close_reason: CloseReason,
        refillable: bool,
    ) -> FilledEntry:
        """Replace a live entry with a requested non-stop-loss close."""
        entry = self.filled_entry
        if entry is None:
            raise ValueError("slot has no live entry")
        requested = entry.request_close(
            requested_at=requested_at,
            requested_exit_price=requested_exit_price,
            close_reason=close_reason,
            refillable=refillable,
        )
        self._validate_entry(requested, expected_entry_id=entry.entry_id)
        self._entry = requested
        return entry

    def fill_close(self, *, filled_at: AwareDatetime) -> FilledEntry:
        """Replace a requested non-stop-loss close with its filled state."""
        requested = self.requested_close_entry
        if requested is None:
            raise ValueError("slot has no requested close entry")
        if filled_at < requested.requested_at:
            raise ValueError("close fill timestamp precedes close request")
        next_entry = requested.fill(filled_at=filled_at)
        if next_entry is not None:
            self._validate_entry(
                next_entry,
                expected_entry_id=requested.original_entry.entry_id,
            )
        self._entry = next_entry
        return requested.original_entry

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
        requested = entry.stop_loss(
            requested_at=requested_at,
            requested_stop_loss_exit_price=requested_stop_loss_exit_price,
        )
        self._validate_entry(requested, expected_entry_id=entry.entry_id)
        self._entry = requested
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
        if filled_at < requested.requested_at:
            raise ValueError("stop-loss fill timestamp precedes stop-loss request")
        next_entry = requested.fill(
            filled_at=filled_at,
            filled_stop_loss_exit_price=filled_stop_loss_exit_price,
            rebuildable=rebuildable,
            planned_rebuild_trigger_price=planned_rebuild_trigger_price,
        )
        self._validate_entry(
            next_entry,
            expected_entry_id=requested.original_entry.entry_id,
        )
        self._entry = next_entry
        return requested.original_entry

    def complete_rebuild(self, entry: RequestedEntry, *, expected_entry_id: EntryId) -> None:
        """Replace a pending rebuild with a requested rebuilt entry."""
        stop_loss_entry = self.filled_stop_loss_entry
        if stop_loss_entry is None:
            raise ValueError("slot has no pending rebuild")
        self._validate_entry(stop_loss_entry)
        self._validate_entry(entry, expected_entry_id=expected_entry_id)
        original_id = stop_loss_entry.original_entry.entry_id
        if not self._same_entry_slot(entry.entry_id, original_id):
            raise ValueError("rebuilt entry does not belong to the stopped slot")
        if entry.entry_id.build_count <= original_id.build_count:
            raise ValueError("rebuilt entry build count must advance")
        if entry.requested_at < stop_loss_entry.filled_at:
            raise ValueError("rebuild request timestamp precedes stop-loss fill")
        self._require_same_currency(
            stop_loss_entry.planned_rebuild_trigger_price,
            entry.requested_entry_price,
            "rebuilt entry price",
        )
        self._entry = entry

    def unseal(self) -> None:
        """Replace a sealed entry with an available slot."""
        sealed_entry = self.sealed_entry
        if sealed_entry is None:
            raise ValueError("slot is not sealed")
        self._entry = sealed_entry.unseal()

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
        if isinstance(entry, RequestedEntry):
            cls._validate_requested_entry(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, FilledEntry):
            cls._validate_filled_entry(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, RequestedCloseEntry):
            cls._validate_requested_close_entry(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, RequestedStopLossEntry):
            cls._validate_requested_stop_loss_entry(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, FilledStopLossEntry):
            cls._validate_filled_stop_loss_entry(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, SealedEntry):
            cls._validate_sealed_entry(entry, expected_entry_id=expected_entry_id)
            return
        raise TypeError("unknown slot entry type")

    @classmethod
    def _validate_requested_entry(
        cls,
        entry: RequestedEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        cls._require_entry_id(
            entry.entry_id,
            EntryIdType.REQUESTED_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        cls._require_positive_decimal(entry.requested_units, "requested units")
        cls._require_positive_money(entry.requested_entry_price, "requested entry price")
        cls._require_positive_money(
            entry.planned_take_profit_price,
            "planned take-profit price",
        )
        cls._require_same_currency(
            entry.requested_entry_price,
            entry.planned_take_profit_price,
            "planned take-profit price",
        )
        if entry.planned_stop_loss_price is not None:
            cls._require_positive_money(
                entry.planned_stop_loss_price,
                "planned stop-loss price",
            )
            cls._require_same_currency(
                entry.requested_entry_price,
                entry.planned_stop_loss_price,
                "planned stop-loss price",
            )
        cls._require_aware_datetime(entry.requested_at, "requested_at")

    @classmethod
    def _validate_filled_entry(
        cls,
        entry: FilledEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        cls._validate_requested_entry(
            entry.requested,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.REQUESTED_ENTRY)
            ),
        )
        cls._require_entry_id(
            entry.entry_id,
            EntryIdType.FILLED_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.requested.entry_id.with_type(EntryIdType.FILLED_ENTRY):
            raise ValueError("filled entry id does not match requested entry id")
        cls._require_positive_decimal(entry.filled_units, "filled units")
        cls._require_positive_money(entry.filled_entry_price, "filled entry price")
        cls._require_same_currency(
            entry.requested.requested_entry_price,
            entry.filled_entry_price,
            "filled entry price",
        )
        cls._require_aware_datetime(entry.filled_at, "filled_at")
        if entry.filled_at < entry.requested.requested_at:
            raise ValueError("entry fill timestamp precedes entry request")
        expected_take_profit = cls._fill_shifted_money(
            entry.requested.planned_take_profit_price,
            requested_entry_price=entry.requested.requested_entry_price,
            filled_entry_price=entry.filled_entry_price,
        )
        if entry.planned_take_profit_price != expected_take_profit:
            raise ValueError("filled entry take-profit price is not fill-adjusted")
        expected_stop_loss = cls._fill_shifted_money(
            entry.requested.planned_stop_loss_price,
            requested_entry_price=entry.requested.requested_entry_price,
            filled_entry_price=entry.filled_entry_price,
        )
        if entry.planned_stop_loss_price != expected_stop_loss:
            raise ValueError("filled entry stop-loss price is not fill-adjusted")

    @classmethod
    def _validate_requested_close_entry(
        cls,
        entry: RequestedCloseEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        cls._validate_filled_entry(
            entry.original_entry,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.FILLED_ENTRY)
            ),
        )
        cls._require_entry_id(
            entry.entry_id,
            EntryIdType.REQUESTED_CLOSE_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.original_entry.entry_id.with_type(
            EntryIdType.REQUESTED_CLOSE_ENTRY
        ):
            raise ValueError("requested close id does not match original entry id")
        if entry.close_reason == CloseReason.STOP_LOSS:
            raise ValueError("stop-loss close must use requested stop-loss entry")
        cls._require_positive_money(entry.requested_exit_price, "requested exit price")
        cls._require_same_currency(
            entry.original_entry.filled_entry_price,
            entry.requested_exit_price,
            "requested exit price",
        )
        cls._require_aware_datetime(entry.requested_at, "requested_at")
        if entry.requested_at < entry.original_entry.filled_at:
            raise ValueError("close request timestamp precedes entry fill")

    @classmethod
    def _validate_requested_stop_loss_entry(
        cls,
        entry: RequestedStopLossEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        cls._validate_filled_entry(
            entry.original_entry,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.FILLED_ENTRY)
            ),
        )
        cls._require_entry_id(
            entry.entry_id,
            EntryIdType.REQUESTED_STOP_LOSS_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.original_entry.entry_id.with_type(
            EntryIdType.REQUESTED_STOP_LOSS_ENTRY
        ):
            raise ValueError("requested stop-loss id does not match original entry id")
        planned_stop_loss_price = entry.original_entry.planned_stop_loss_price
        if planned_stop_loss_price is None:
            raise ValueError("stop-loss request requires an original planned stop-loss price")
        if entry.requested_stop_loss_exit_price != planned_stop_loss_price:
            raise ValueError("requested stop-loss exit price differs from planned stop loss")
        cls._require_positive_money(
            entry.requested_stop_loss_exit_price,
            "requested stop-loss exit price",
        )
        cls._require_aware_datetime(entry.requested_at, "requested_at")
        if entry.requested_at < entry.original_entry.filled_at:
            raise ValueError("stop-loss request timestamp precedes entry fill")

    @classmethod
    def _validate_filled_stop_loss_entry(
        cls,
        entry: FilledStopLossEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        cls._validate_requested_stop_loss_entry(
            entry.requested,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.REQUESTED_STOP_LOSS_ENTRY)
            ),
        )
        cls._require_entry_id(
            entry.entry_id,
            EntryIdType.FILLED_STOP_LOSS_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.requested.entry_id.with_type(
            EntryIdType.FILLED_STOP_LOSS_ENTRY
        ):
            raise ValueError("filled stop-loss id does not match stop-loss request id")
        cls._require_aware_datetime(entry.filled_at, "filled_at")
        if entry.filled_at < entry.requested.requested_at:
            raise ValueError("stop-loss fill timestamp precedes stop-loss request")
        cls._require_positive_money(
            entry.filled_stop_loss_exit_price,
            "filled stop-loss exit price",
        )
        cls._require_same_currency(
            entry.requested.requested_stop_loss_exit_price,
            entry.filled_stop_loss_exit_price,
            "filled stop-loss exit price",
        )
        cls._require_positive_money(
            entry.planned_rebuild_trigger_price,
            "planned rebuild trigger price",
        )
        cls._require_same_currency(
            entry.requested.requested_stop_loss_exit_price,
            entry.planned_rebuild_trigger_price,
            "planned rebuild trigger price",
        )

    @classmethod
    def _validate_sealed_entry(
        cls,
        entry: SealedEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        cls._require_entry_id(
            entry.entry_id,
            EntryIdType.SEALED_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        cls._require_aware_datetime(entry.sealed_at, "sealed_at")

    @staticmethod
    def _require_entry_id(
        entry_id: EntryId,
        entry_type: EntryIdType,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        if entry_id.entry_type != entry_type:
            raise ValueError(f"entry id type must be {entry_type.value}")
        if expected_entry_id is not None and entry_id != expected_entry_id.with_type(entry_type):
            raise ValueError("entry id does not match expected slot identity")

    @staticmethod
    def _same_entry_slot(left: EntryId, right: EntryId) -> bool:
        return (
            left.cycle_id == right.cycle_id
            and left.layer_number == right.layer_number
            and left.slot_number == right.slot_number
        )

    @staticmethod
    def _require_positive_decimal(value: Decimal, name: str) -> None:
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    @staticmethod
    def _require_positive_money(value: Money, name: str) -> None:
        if value.amount <= 0:
            raise ValueError(f"{name} must be positive")

    @staticmethod
    def _require_same_currency(reference: Money, value: Money, name: str) -> None:
        if value.currency != reference.currency:
            raise ValueError(f"{name} currency does not match")

    @staticmethod
    def _require_aware_datetime(value: AwareDatetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _fill_shifted_money(
        value: Money | None,
        *,
        requested_entry_price: Money,
        filled_entry_price: Money,
    ) -> Money | None:
        if value is None:
            return None
        fill_delta = (filled_entry_price - requested_entry_price).amount
        if not fill_delta:
            return value
        return Money.of(value.amount + fill_delta, value.currency)


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
        if self.base_units <= 0:
            raise ValueError("layer base units must be positive")
        self._validate_slot_numbers()
        missing_slot_numbers = set(self._slots) - set(self._build_count_generators)
        for slot_number in missing_slot_numbers:
            self._build_count_generators[slot_number] = IntegerIdGenerator(
                self._entry_build_count(self._slots[slot_number]) + 1
            )
        unknown_slot_numbers = set(self._build_count_generators) - set(self._slots)
        if unknown_slot_numbers:
            raise ValueError("build count generator references unknown slot")
        for slot_number, generator in self._build_count_generators.items():
            if generator.next_value < 1:
                raise ValueError("build count generator must be positive")
            entry_build_count = self._entry_build_count(self._slots[slot_number])
            assigned_build_count = generator.next_value - 1
            if entry_build_count and assigned_build_count != entry_build_count:
                raise ValueError("slot entry build count does not match layer build count")

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
        generator_map: dict[int, IntegerIdGenerator] = {}
        for slot_number, slot in slot_map.items():
            entry_build_count = cls._entry_build_count(slot)
            restored_build_count = count_map.get(slot_number, entry_build_count)
            if restored_build_count < 0:
                raise ValueError("build count must not be negative")
            if entry_build_count and restored_build_count != entry_build_count:
                raise ValueError("slot entry build count does not match restored build count")
            generator_map[slot_number] = IntegerIdGenerator(restored_build_count + 1)
        return cls(
            base_units=base_units,
            _slots=slot_map,
            _build_count_generators=generator_map,
        )

    @property
    def slots(self) -> tuple[Slot, ...]:
        """Return the layer's slots in ascending R order (read-only view)."""
        return tuple(self._slots[slot_number] for slot_number in sorted(self._slots))

    @property
    def slot_numbers(self) -> tuple[int, ...]:
        """Return slot numbers in ascending R order."""
        return tuple(sorted(self._slots))

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
                build_count=entry.entry_id.build_count,
            )
            slot.validate_entry(expected_entry_id=expected_entry_id)
            assigned_build_count = self._build_count_generators[slot_number].next_value - 1
            if entry.entry_id.build_count != assigned_build_count:
                raise ValueError("slot entry build count does not match layer build count")

    def _validate_slot_numbers(self) -> None:
        if not self._slots:
            raise ValueError("layer must contain slots")
        expected_slot_numbers = set(range(max(self._slots) + 1))
        if set(self._slots) != expected_slot_numbers:
            raise ValueError("layer slots must be contiguous from R0")

    @property
    def r0(self) -> Slot:
        """Return the layer's R0 slot."""
        return self.slot(0)

    def live_entries(self) -> list[FilledEntry]:
        """Return broker-live entries in ascending R order."""
        entries: list[FilledEntry] = []
        for slot in self.slots:
            entry = slot.live_or_pending_close_entry()
            if entry is not None:
                entries.append(entry)
        return entries

    def counter_entries(self) -> list[FilledEntry]:
        """Return broker-live R1+ entries in ascending R order."""
        entries: list[FilledEntry] = []
        for slot_number in sorted(slot_number for slot_number in self._slots if slot_number > 0):
            slot = self._slots[slot_number]
            entry = slot.live_or_pending_close_entry()
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
        """Return the highest-R slot with a directly closeable live entry."""
        for slot in reversed(self.slots):
            if slot.filled_entry is not None:
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

    def __post_init__(self) -> None:
        self._validate_layer_numbers()

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
        self._validate_layer_numbers()
        return layer

    def layer_number(self, layer: Layer) -> int:
        """Return the L number derived from a layer's position in this grid."""
        for index, candidate in self._layers.items():
            if candidate is layer:
                return index
        raise ValueError("layer does not belong to this grid")

    def role_for(self, layer: Layer, slot: Slot) -> EntryRole:
        """Return the entry role derived from layer and slot positions."""
        return GridPosition(
            layer_number=self.layer_number(layer),
            slot_number=layer.slot_number(slot),
        ).role

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

    def requested_close_slots(self) -> list[tuple[Layer, Slot]]:
        """Return slots with non-stop-loss closes waiting for fill confirmation."""
        requested: list[tuple[Layer, Slot]] = []
        for layer in self.layers:
            requested.extend(
                (layer, slot) for slot in layer.slots if slot.requested_close_entry is not None
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

    def has_requested_closes(self) -> bool:
        """Return True when any non-stop-loss close is waiting for fill confirmation."""
        return bool(self.requested_close_slots())

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
        self._validate_layer_numbers()

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
                if slot.live_or_pending_close_entry() is entry:
                    return layer, slot
        return None

    def validate_for_cycle(self, cycle_id: CycleId) -> None:
        """Validate layer order and every slot entry for a cycle."""
        self._validate_layer_numbers()
        for layer_number, layer in self._layers.items():
            layer.validate_entries(cycle_id=cycle_id, layer_number=layer_number)

    def _validate_layer_numbers(self) -> None:
        if not self._layers:
            raise ValueError("grid must contain layers")
        expected_layer_numbers = set(range(1, max(self._layers) + 1))
        if set(self._layers) != expected_layer_numbers:
            raise ValueError("grid layers must be contiguous from L1")
