"""Snowball tick-processing engine."""

from __future__ import annotations

from dataclasses import dataclass, field

from core import Tick

from snowball.composition import SnowballServiceContainer
from snowball.config import SnowballConfig
from snowball.events import SnowballEvent
from snowball.models.state import SnowballState
from snowball.services.stages.tick import SnowballTickContext


@dataclass(frozen=True, slots=True)
class SnowballStepResult:
    """Result of processing one market tick."""

    events: tuple[SnowballEvent, ...]
    state: SnowballState


@dataclass(slots=True)
class SnowballEngine:
    """Coordinate Snowball state transitions for market ticks."""

    config: SnowballConfig
    services: SnowballServiceContainer = field(init=False)

    def __post_init__(self) -> None:
        self.services = SnowballServiceContainer(self.config)

    def process_tick(
        self,
        *,
        tick: Tick,
        state: SnowballState,
    ) -> SnowballStepResult:
        """Process a tick and return emitted Snowball events."""
        state.prune_completed_cycles()
        account = self.services.accounting.evaluate(state=state, tick=tick, config=self.config)
        context = SnowballTickContext(
            tick=tick,
            state=state,
            account=account,
        )
        for stage in self.services.tick_stages:
            stage.process(context)
            if context.halted:
                break
        return SnowballStepResult(events=tuple(context.events), state=state)
