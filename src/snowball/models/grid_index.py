"""Indexing helpers for Snowball grid structure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from snowball.models.identifiers import IntegerIdGenerator


class EntryIdWithBuildNumber(Protocol):
    """Minimal entry-id protocol needed for build-number validation."""

    @property
    def build_number(self) -> int:
        """Return the build number."""


class EntryWithId(Protocol):
    """Minimal entry protocol needed for build-number validation."""

    @property
    def entry_id(self) -> EntryIdWithBuildNumber:
        """Return the entry id."""


class SlotWithEntry(Protocol):
    """Minimal slot protocol needed for build-number validation."""

    @property
    def entry(self) -> EntryWithId | None:
        """Return the slot entry."""


class ContiguousNumbering:
    """Validate contiguous integer keys for grid maps."""

    @classmethod
    def require_from_zero(cls, values: Mapping[int, object], *, name: str) -> None:
        """Require keys to be contiguous from zero."""
        if not values:
            raise ValueError(f"{name} must contain values")
        expected_numbers = set(range(max(values) + 1))
        if set(values) != expected_numbers:
            raise ValueError(f"{name} must be contiguous from R0")

    @classmethod
    def require_from_one(cls, values: Mapping[int, object], *, name: str) -> None:
        """Require keys to be contiguous from one."""
        if not values:
            raise ValueError(f"{name} must contain values")
        expected_numbers = set(range(1, max(values) + 1))
        if set(values) != expected_numbers:
            raise ValueError(f"{name} must be contiguous from L1")


@dataclass(frozen=True, slots=True)
class ObjectNumberIndex[T]:
    """Map object identities back to their numeric grid position."""

    numbers_by_id: dict[int, int]
    missing_message: str

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[int, T],
        *,
        missing_message: str,
    ) -> ObjectNumberIndex[T]:
        """Create an object-id index from a numbered mapping."""
        return cls(
            numbers_by_id={id(value): number for number, value in values.items()},
            missing_message=missing_message,
        )

    def number_for(self, value: T) -> int:
        """Return the numeric position for an indexed object."""
        try:
            return self.numbers_by_id[id(value)]
        except KeyError as exc:
            raise ValueError(self.missing_message) from exc


@dataclass(slots=True)
class LayerBuildNumberRegistry:
    """Own per-slot build-number generators for a layer."""

    generators: dict[int, IntegerIdGenerator]

    @classmethod
    def create(cls, slot_numbers: tuple[int, ...]) -> LayerBuildNumberRegistry:
        """Create fresh build-number generators for slots."""
        return cls({slot_number: IntegerIdGenerator() for slot_number in slot_numbers})

    @classmethod
    def restore(
        cls,
        *,
        slots: Mapping[int, SlotWithEntry],
        build_numbers: Mapping[int, int] | None,
    ) -> LayerBuildNumberRegistry:
        """Restore generators from serialized build numbers and slot entries."""
        number_map = dict(build_numbers or {})
        unknown_slot_numbers = set(number_map) - set(slots)
        if unknown_slot_numbers:
            raise ValueError("build number references unknown slot")
        generator_map: dict[int, IntegerIdGenerator] = {}
        for slot_number, slot in slots.items():
            entry_build_number = cls.entry_build_number(slot)
            restored_build_number = number_map.get(slot_number, entry_build_number)
            if restored_build_number < 0:
                raise ValueError("build number must not be negative")
            if entry_build_number and restored_build_number != entry_build_number:
                raise ValueError("slot entry build number does not match restored build number")
            generator_map[slot_number] = IntegerIdGenerator(restored_build_number + 1)
        return cls(generator_map)

    def complete_for(self, slots: Mapping[int, SlotWithEntry]) -> None:
        """Ensure every slot has a generator and no generator references an unknown slot."""
        missing_slot_numbers = set(slots) - set(self.generators)
        for slot_number in missing_slot_numbers:
            self.generators[slot_number] = IntegerIdGenerator(
                self.entry_build_number(slots[slot_number]) + 1
            )
        self.generators = dict(sorted(self.generators.items()))
        unknown_slot_numbers = set(self.generators) - set(slots)
        if unknown_slot_numbers:
            raise ValueError("build number generator references unknown slot")

    def validate_against(self, slots: Mapping[int, SlotWithEntry]) -> None:
        """Validate generator values against slot entries."""
        self.complete_for(slots)
        for slot_number, generator in self.generators.items():
            if generator.next_value < 1:
                raise ValueError("build number generator must be positive")
            entry_build_number = self.entry_build_number(slots[slot_number])
            assigned_build_number = generator.next_value - 1
            if entry_build_number and assigned_build_number != entry_build_number:
                raise ValueError("slot entry build number does not match layer build number")

    def current(self, slot_number: int) -> int:
        """Return the current build number for a slot."""
        return self.generators[slot_number].next_value - 1

    def current_all(self, slot_numbers: tuple[int, ...]) -> dict[int, int]:
        """Return current build numbers keyed by slot number."""
        return {
            slot_number: self.generators[slot_number].next_value - 1 for slot_number in slot_numbers
        }

    def next(self, slot_number: int) -> int:
        """Return and advance the next build number for a slot."""
        return self.generators[slot_number].next()

    @classmethod
    def entry_build_number(cls, slot: SlotWithEntry) -> int:
        """Return the build number of the slot entry, or zero when empty."""
        entry = slot.entry
        if entry is None:
            return 0
        entry_id = entry.entry_id
        return int(entry_id.build_number)
