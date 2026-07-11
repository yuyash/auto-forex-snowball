"""Snowball identifiers and integer sequence generation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from core import Metadata

from snowball.enums import EntryRole
from snowball.models.position import GridPosition

type CycleId = int


class EntryIdType(StrEnum):
    """Entry state represented by an EntryId."""

    REQUESTED_ENTRY = "REQ"
    FILLED_ENTRY = "FIL"
    REQUESTED_CLOSE_ENTRY = "REQ_CLOSE"
    REQUESTED_STOP_LOSS_ENTRY = "REQ_SL"
    FILLED_STOP_LOSS_ENTRY = "FIL_SL"
    SEALED_ENTRY = "SLD"


@dataclass(slots=True)
class IntegerIdGenerator:
    """Generate monotonically increasing integer identifiers."""

    next_value: int = 1

    @classmethod
    def after(cls, values: Iterable[int]) -> IntegerIdGenerator:
        """Create a generator starting after the highest existing value."""
        return cls(next_value=max(values, default=0) + 1)

    def next(self) -> int:
        """Return the next integer and advance the sequence."""
        value = self.next_value
        self.next_value += 1
        return value


@dataclass(frozen=True, slots=True)
class EntryId:
    """Stable entry identifier derived from cycle and grid position."""

    cycle_id: CycleId
    layer_number: int
    slot_number: int
    build_number: int
    entry_type: EntryIdType = EntryIdType.REQUESTED_ENTRY

    @property
    def value(self) -> str:
        """Return a human-readable entry identifier."""
        return (
            f"C{self.cycle_id}:L{self.layer_number}:S{self.slot_number}:"
            f"{self.entry_type.value}:B{self.build_number}"
        )

    @property
    def display_id(self) -> str:
        """Return the compact identifier shown in strategy events."""
        return (
            f"C{self.cycle_id}L{self.layer_number}"
            f"R{self.slot_number}B{self.build_number}"
        )

    @property
    def retracement_count(self) -> int:
        """Return the Snowball retracement count represented by the slot number."""
        return self.slot_number

    @property
    def role(self) -> EntryRole:
        """Return the entry role derived from the grid position."""
        return GridPosition(self.layer_number, self.slot_number).role

    def to_metadata(self) -> Metadata:
        """Return metadata values for Core strategy events."""
        return Metadata.of(
            entry_id=str(self),
            entry_type=self.entry_type.value,
            entry_role=self.role.value,
            cycle_id=self.cycle_id,
            layer_number=self.layer_number,
            slot_number=self.slot_number,
            retracement_count=self.retracement_count,
            build_number=self.build_number,
        )

    def with_type(self, entry_type: EntryIdType) -> EntryId:
        """Return this identifier with a different entry state type."""
        return EntryId(
            cycle_id=self.cycle_id,
            layer_number=self.layer_number,
            slot_number=self.slot_number,
            build_number=self.build_number,
            entry_type=entry_type,
        )

    def __str__(self) -> str:
        return self.value
