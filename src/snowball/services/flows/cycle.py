"""Cycle opening flow for Snowball."""

from __future__ import annotations

from dataclasses import dataclass

from core import PositionSide, Tick

from snowball.config import SnowballConfig
from snowball.events import SnowballEvent
from snowball.models.grid import Grid
from snowball.models.state import Cycle, SnowballState
from snowball.services.flows.entry import SnowballEntryService
from snowball.services.flows.event_factory import SnowballEventFactory


@dataclass(frozen=True, slots=True)
class SnowballCycleService:
    """Open initial and replacement cycles."""

    config: SnowballConfig
    entry_service: SnowballEntryService
    event_factory: SnowballEventFactory

    def open_initial_cycles(
        self,
        *,
        state: SnowballState,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Open the first managed cycle set."""
        events: list[SnowballEvent] = []
        for direction in self._managed_directions():
            events.extend(self._open_cycle(state=state, tick=tick, direction=direction))
        return events

    def reseed_cycles(
        self,
        *,
        state: SnowballState,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Open missing managed directions after completed cycles were removed."""
        events: list[SnowballEvent] = []
        for direction in self._managed_directions():
            has_active = False
            has_pending = False
            for cycle in state.iter_cycles():
                if cycle.direction != direction:
                    continue
                has_active = has_active or cycle.active
                has_pending = has_pending or cycle.pending
                if has_active and has_pending:
                    break
            if has_active:
                continue
            if has_pending and not self.config.cycle.reseed_when_all_positions_pending_rebuild:
                continue
            events.extend(self._open_cycle(state=state, tick=tick, direction=direction))
        return events

    def _managed_directions(self) -> tuple[PositionSide, ...]:
        if self.config.cycle.hedging_enabled:
            return PositionSide.LONG, PositionSide.SHORT
        return (PositionSide.LONG,)

    def _open_cycle(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        direction: PositionSide,
    ) -> list[SnowballEvent]:
        cycle = Cycle.create(
            cycle_id=state.next_cycle_id(),
            direction=direction,
            grid=self._new_grid(),
        )
        layer = cycle.grid.current_layer
        slot = layer.r0
        entry_id = cycle.next_entry_id(layer=layer, slot=slot)
        entry = self.entry_service.create_initial_entry(
            entry_id=entry_id,
            tick=tick,
            direction=direction,
            grid=cycle.grid,
            layer=layer,
            slot=slot,
        )
        slot.place_entry(entry, expected_entry_id=entry_id)
        cycle.refresh_status()
        state.add_cycle(cycle)
        return [
            self.event_factory.open_event(
                cycle=cycle,
                entry=entry,
            )
        ]

    def _new_grid(self) -> Grid:
        base_units = self.config.sizing.layer_base_units(1)
        return Grid.create(
            base_units=base_units,
            max_retracements=self.config.grid.max_retracements_per_layer,
        )
