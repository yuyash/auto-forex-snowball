"""Runtime adapter that connects Snowball domain services to Core callbacks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core import (
    StrategyContext,
    StrategyExecutionResponse,
    StrategyResult,
    StrategyState,
    Tick,
)

from snowball.config import SnowballConfig
from snowball.engine import SnowballEngine
from snowball.event_mapper import SnowballEventMapper
from snowball.execution_reports import SnowballExecutionReportApplier
from snowball.models.state import SnowballState
from snowball.serialization import SnowballStateSerializer


@dataclass(slots=True)
class SnowballRuntime:
    """Own Snowball execution dependencies for one strategy instance."""

    config: SnowballConfig
    engine: SnowballEngine = field(init=False)
    event_mapper: SnowballEventMapper = field(default_factory=SnowballEventMapper)
    execution_reports: SnowballExecutionReportApplier = field(
        default_factory=SnowballExecutionReportApplier
    )
    _state: SnowballState | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.engine = SnowballEngine(self.config)

    def start(self, context: StrategyContext) -> StrategyResult:
        """Initialize or restore strategy state at task start."""
        self._state = self._state_from_context(context)
        return StrategyResult(state=self.strategy_state())

    def on_tick(self, tick: Tick, context: StrategyContext) -> StrategyResult:
        """Process a tick and map Snowball events to Core strategy events."""
        state = self._runtime_state(context)
        result = self.engine.process_tick(tick=tick, state=state)
        events = tuple(
            self.event_mapper.to_strategy_event(event=event, tick=tick, context=context)
            for event in result.events
        )
        return StrategyResult(
            events=events,
            state=self.strategy_state() if events else None,
        )

    def on_execution_reports(
        self,
        reports: Sequence[StrategyExecutionResponse],
        context: StrategyContext,
    ) -> StrategyState:
        """Apply broker execution reports to Snowball state."""
        state = self._runtime_state(context)
        self.execution_reports.apply_many(reports, state)
        return self.strategy_state()

    @property
    def state(self) -> SnowballState:
        """Return the current runtime Snowball state."""
        if self._state is None:
            self._state = SnowballState.new()
        return self._state

    def strategy_state(self) -> StrategyState:
        """Return the current runtime state serialized for Core boundaries."""
        return SnowballStateSerializer.to_strategy_state(self.state)

    def _runtime_state(self, context: StrategyContext) -> SnowballState:
        if self._state is None:
            self._state = self._state_from_context(context)
        return self._state

    def _state_from_context(self, context: StrategyContext) -> SnowballState:
        return SnowballStateSerializer.from_strategy_state(context.state)
