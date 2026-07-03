"""Core Strategy adapter for Snowball."""

from __future__ import annotations

from core import Strategy, StrategyContext, StrategyParameters, StrategyResult, Tick

from snowball.config import SnowballConfig
from snowball.runtime import SnowballRuntime


class SnowballStrategy(Strategy):
    """Snowball strategy exposed through Core's Strategy interface."""

    def __init__(
        self,
        *,
        name: str = "snowball",
        parameters: StrategyParameters | None = None,
    ) -> None:
        super().__init__(name=name, parameters=parameters)
        self._config = SnowballConfig.from_parameters(self.parameters)
        self._runtime = SnowballRuntime(self._config)

    @classmethod
    def default_parameters(cls) -> StrategyParameters:
        """Return Snowball default parameters."""
        return StrategyParameters.of(**SnowballConfig().to_dict())

    @classmethod
    def normalize_parameters(
        cls,
        parameters: StrategyParameters,
    ) -> StrategyParameters:
        """Normalize external parameters to canonical Snowball config values."""
        merged = cls.default_parameters().merge(parameters)
        return StrategyParameters.of(**SnowballConfig.from_parameters(merged).to_dict())

    @classmethod
    def validate_parameters(cls, parameters: StrategyParameters) -> None:
        """Validate Snowball parameters."""
        SnowballConfig.from_parameters(parameters)

    def on_start(self, context: StrategyContext) -> StrategyResult:
        """Initialize Snowball state for a task."""
        return self._runtime.start(context)

    def on_tick(self, tick: Tick, context: StrategyContext) -> StrategyResult:
        """Process a tick and emit Snowball strategy events."""
        return self._runtime.on_tick(tick, context)

    @property
    def config(self) -> SnowballConfig:
        """Return normalized Snowball config."""
        return self._config
