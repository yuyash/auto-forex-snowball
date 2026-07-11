"""Snowball strategy state."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Self

from core import PositionSide, Units

from snowball.enums import CycleStatus
from snowball.models.entries import FilledEntry
from snowball.models.grid import Grid, Layer, Slot
from snowball.models.identifiers import CycleId, EntryId, IntegerIdGenerator


@dataclass(slots=True)
class Cycle:
    """One directional Snowball cycle."""

    _cycle_id: CycleId
    _direction: PositionSide
    _grid: Grid
    _status: CycleStatus = CycleStatus.ACTIVE

    @classmethod
    def create(
        cls,
        *,
        cycle_id: CycleId,
        direction: PositionSide,
        grid: Grid,
        status: CycleStatus = CycleStatus.ACTIVE,
    ) -> Self:
        """Create a cycle from public constructor values."""
        grid.validate_for_cycle(cycle_id)
        return cls(
            _cycle_id=cycle_id,
            _direction=direction,
            _grid=grid,
            _status=status,
        )

    @property
    def cycle_id(self) -> CycleId:
        """Return the stable cycle identifier."""
        return self._cycle_id

    @property
    def direction(self) -> PositionSide:
        """Return the cycle direction."""
        return self._direction

    @property
    def grid(self) -> Grid:
        """Return the cycle grid."""
        return self._grid

    @property
    def status(self) -> CycleStatus:
        """Return the cycle lifecycle status."""
        return self._status

    @property
    def active(self) -> bool:
        """Return True when the cycle can process live entries."""
        return self._status == CycleStatus.ACTIVE

    @property
    def pending(self) -> bool:
        """Return True when the cycle has pending rebuilds but no live entries."""
        return self._status == CycleStatus.PENDING

    @property
    def completed(self) -> bool:
        """Return True when the cycle is complete."""
        return self._status == CycleStatus.COMPLETED

    @property
    def is_long(self) -> bool:
        """Return True for a long cycle."""
        return self._direction == PositionSide.LONG

    @property
    def is_short(self) -> bool:
        """Return True for a short cycle."""
        return self._direction == PositionSide.SHORT

    def live_entries(self) -> list[FilledEntry]:
        """Return live entries in grid order."""
        return self.grid.all_live_entries()

    def counter_entries(self) -> list[FilledEntry]:
        """Return counter entries in grid order."""
        return self.grid.all_counter_entries()

    def head(self) -> FilledEntry | None:
        """Return the live cycle head."""
        return self.grid.head_entry()

    def next_entry_id(self, *, layer: Layer, slot: Slot) -> EntryId:
        """Return the next entry identifier for a slot in this cycle."""
        return self.grid.next_entry_id(cycle_id=self.cycle_id, layer=layer, slot=slot)

    def refresh_status(self, *, validate: bool = False) -> None:
        """Normalize the grid and update the lifecycle status from current contents."""
        if validate:
            self.grid.validate_for_cycle(self.cycle_id)
        self.grid.remove_empty_top_layers()
        if validate:
            self.grid.validate_for_cycle(self.cycle_id)
        has_live = self.grid.has_live_entries()
        has_requested_entry = self.grid.has_requested_entries()
        has_requested_close = self.grid.has_requested_closes()
        has_requested_stop_loss = self.grid.has_requested_stop_losses()
        has_filled_stop_loss = self.grid.has_filled_stop_loss_entries()
        if has_live or has_requested_entry or has_requested_close or has_requested_stop_loss:
            self._status = CycleStatus.ACTIVE
        elif has_filled_stop_loss:
            self._status = CycleStatus.PENDING
        else:
            self._status = CycleStatus.COMPLETED


@dataclass(slots=True)
class SnowballState:
    """Mutable Snowball engine state."""

    _cycles: list[Cycle] = field(default_factory=list)
    _cycle_id_generator: IntegerIdGenerator = field(default_factory=IntegerIdGenerator)

    @classmethod
    def new(cls) -> SnowballState:
        """Create a fresh state."""
        return cls()

    @classmethod
    def from_cycles(cls, cycles: Iterable[Cycle]) -> Self:
        """Create state from active or pending cycles."""
        cycle_list = list(cycles)
        return cls(
            _cycles=cycle_list,
            _cycle_id_generator=IntegerIdGenerator.after(cycle.cycle_id for cycle in cycle_list),
        )

    @property
    def cycles(self) -> tuple[Cycle, ...]:
        """Return cycles currently carrying live or pending work."""
        return tuple(self._cycles)

    def iter_cycles(self) -> Iterator[Cycle]:
        """Iterate cycles without allocating a tuple."""
        return iter(self._cycles)

    def has_cycles(self) -> bool:
        """Return True when the state contains any cycle."""
        return bool(self._cycles)

    def add_cycle(self, cycle: Cycle) -> None:
        """Add a cycle to state."""
        self._cycles.append(cycle)

    @property
    def next_cycle_id_value(self) -> int:
        """Return the next cycle id without advancing the generator."""
        return self._cycle_id_generator.next_value

    def next_cycle_id(self) -> CycleId:
        """Return the next cycle id and advance the generator."""
        return self._cycle_id_generator.next()

    def restore_next_cycle_id(self, next_cycle_id: int) -> None:
        """Restore the next cycle id after deserialization."""
        self._cycle_id_generator = IntegerIdGenerator(next_cycle_id)

    def active_cycles(self) -> list[Cycle]:
        """Return active cycles."""
        return [cycle for cycle in self._cycles if cycle.active]

    def iter_active_cycles(self) -> Iterator[Cycle]:
        """Iterate active cycles without allocating a list."""
        return (cycle for cycle in self._cycles if cycle.active)

    def active_cycle_for(self, direction: PositionSide) -> Cycle | None:
        """Return the first active cycle for a direction."""
        for cycle in self._cycles:
            if cycle.direction == direction and cycle.active:
                return cycle
        return None

    def live_entries(self) -> list[FilledEntry]:
        """Return all live entries."""
        entries: list[FilledEntry] = []
        for cycle in self.iter_cycles():
            entries.extend(cycle.live_entries())
        return entries

    def live_units_by_direction(self) -> tuple[Units, Units]:
        """Return total long and short units."""
        long_units = Decimal("0")
        short_units = Decimal("0")
        for cycle in self.iter_cycles():
            for entry in cycle.live_entries():
                if cycle.direction == PositionSide.LONG:
                    long_units += entry.filled_units
                else:
                    short_units += entry.filled_units
        return Units.of(long_units), Units.of(short_units)

    def refresh_cycle_statuses(self) -> None:
        """Refresh status for all cycles."""
        for cycle in self._cycles:
            cycle.refresh_status()

    def prune_completed_cycles(self) -> None:
        """Remove cycles that no longer carry live or pending work."""
        self._cycles = [cycle for cycle in self._cycles if not cycle.completed]
