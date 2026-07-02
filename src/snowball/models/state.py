"""Snowball strategy state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Self
from uuid import UUID

from core import PositionSide, StrategyState

from snowball.enums import CycleStatus
from snowball.models.entries import Entry, SlotPosition
from snowball.models.grid import Grid, GridSlotKey, Layer, Slot

STATE_KEY = "snowball"


@dataclass(slots=True)
class Cycle:
    """One directional Snowball cycle."""

    _cycle_id: UUID
    _direction: PositionSide
    _grid: Grid
    _status: CycleStatus = CycleStatus.ACTIVE

    @classmethod
    def create(
        cls,
        *,
        cycle_id: UUID,
        direction: PositionSide,
        grid: Grid,
        status: CycleStatus = CycleStatus.ACTIVE,
    ) -> Self:
        """Create a cycle from public constructor values."""
        return cls(
            _cycle_id=cycle_id,
            _direction=direction,
            _grid=grid,
            _status=status,
        )

    @property
    def cycle_id(self) -> UUID:
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

    def live_entries(self) -> list[Entry]:
        """Return live entries in grid order."""
        return self.grid.all_live_entries()

    def counter_entries(self) -> list[Entry]:
        """Return counter entries in grid order."""
        return self.grid.all_counter_entries()

    def head(self) -> Entry | None:
        """Return the live cycle head."""
        return self.grid.head_entry()

    def effective_head(self) -> SlotPosition | None:
        """Return the live or pending head used for averaging decisions."""
        return self.grid.effective_head()

    def slot_key(self, *, layer: Layer, slot: Slot) -> GridSlotKey:
        """Return a structure-derived key for a slot in this cycle."""
        return self.grid.slot_key(cycle_id=self.cycle_id, layer=layer, slot=slot)

    def refresh_status(self) -> None:
        """Update the lifecycle status from current grid contents."""
        has_live = bool(self.grid.all_live_entries())
        has_pending = self.grid.has_pending_rebuilds()
        if has_live:
            self._status = CycleStatus.ACTIVE
        elif has_pending:
            self._status = CycleStatus.PENDING
        else:
            self._status = CycleStatus.COMPLETED


@dataclass(slots=True)
class SnowballState:
    """Mutable Snowball engine state."""

    _cycles: list[Cycle] = field(default_factory=list)

    @classmethod
    def new(cls) -> SnowballState:
        """Create a fresh state."""
        return cls()

    @classmethod
    def from_cycles(cls, cycles: Iterable[Cycle]) -> Self:
        """Create state from active or pending cycles."""
        return cls(_cycles=list(cycles))

    @property
    def cycles(self) -> tuple[Cycle, ...]:
        """Return cycles currently carrying live or pending work."""
        return tuple(self._cycles)

    def add_cycle(self, cycle: Cycle) -> None:
        """Add a cycle to state."""
        self._cycles.append(cycle)

    @classmethod
    def from_strategy_state(
        cls,
        state: StrategyState,
    ) -> SnowballState:
        """Build Snowball state from Core strategy state."""
        if STATE_KEY not in state:
            return cls.new()
        from snowball.serialization import SnowballStateSerializer

        return SnowballStateSerializer.from_mapping(state.require(STATE_KEY))

    def to_strategy_state(self) -> StrategyState:
        """Convert to Core strategy state."""
        from snowball.serialization import SnowballStateSerializer

        return StrategyState.of(**{STATE_KEY: SnowballStateSerializer.to_mapping(self)})

    def active_cycles(self) -> list[Cycle]:
        """Return active cycles."""
        return [cycle for cycle in self._cycles if cycle.active]

    def active_cycle_for(self, direction: PositionSide) -> Cycle | None:
        """Return the first active cycle for a direction."""
        for cycle in self._cycles:
            if cycle.direction == direction and cycle.active:
                return cycle
        return None

    def live_entries(self) -> list[Entry]:
        """Return all live entries."""
        entries: list[Entry] = []
        for cycle in self._cycles:
            entries.extend(cycle.live_entries())
        return entries

    def live_units_by_direction(self) -> tuple[Decimal, Decimal]:
        """Return total long and short units."""
        long_units = Decimal("0")
        short_units = Decimal("0")
        for cycle in self._cycles:
            for entry in cycle.live_entries():
                if cycle.direction == PositionSide.LONG:
                    long_units += entry.units
                else:
                    short_units += entry.units
        return long_units, short_units

    def refresh_cycle_statuses(self) -> None:
        """Refresh status for all cycles."""
        for cycle in self._cycles:
            cycle.refresh_status()

    def prune_completed_cycles(self) -> None:
        """Remove cycles that no longer carry live or pending work."""
        self._cycles = [cycle for cycle in self._cycles if not cycle.completed]
