"""Runtime adapter that connects Snowball domain services to Core callbacks."""

from __future__ import annotations

from dataclasses import dataclass, field

from core import StrategyContext, StrategyResult, Tick

from snowball.config import SnowballConfig
from snowball.engine import SnowballEngine
from snowball.events import SnowballEventMapper
from snowball.models.state import SnowballState


@dataclass(slots=True)
class SnowballRuntime:
    """Own Snowball execution dependencies for one strategy instance."""

    config: SnowballConfig
    engine: SnowballEngine = field(init=False)
    event_mapper: SnowballEventMapper = field(default_factory=SnowballEventMapper)

    def __post_init__(self) -> None:
        self.engine = SnowballEngine(self.config)

    def start(self, context: StrategyContext) -> StrategyResult:
        """Initialize or restore strategy state at task start."""
        state = self._state_from_context(context)
        return StrategyResult(state=state.to_strategy_state())

    def on_tick(self, tick: Tick, context: StrategyContext) -> StrategyResult:
        """Process a tick and map Snowball intents to Core strategy events."""
        state = self._state_from_context(context)
        result = self.engine.process_tick(tick=tick, state=state, pip_size=context.pip_size)
        events = tuple(
            self.event_mapper.to_strategy_event(intent=intent, tick=tick, context=context)
            for intent in result.intents
        )
        return StrategyResult(events=events, state=result.state.to_strategy_state())

    def _state_from_context(self, context: StrategyContext) -> SnowballState:
        return SnowballState.from_strategy_state(context.state)
