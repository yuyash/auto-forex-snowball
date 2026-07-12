"""Serialization boundary for Snowball strategy state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core import StrategyState

from snowball.models.state import SnowballState
from snowball.serialization_codecs import SnowballStateCodec

STATE_KEY = "snowball"


class SnowballStateSerializer:
    """Convert Snowball state to and from Core strategy-state mappings."""

    codec = SnowballStateCodec

    @classmethod
    def to_mapping(cls, state: SnowballState) -> dict[str, Any]:
        """Serialize Snowball state to a plain mapping."""
        return cls.codec.to_mapping(state)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SnowballState:
        """Deserialize Snowball state from a strategy-state mapping."""
        return cls.codec.from_mapping(data)

    @classmethod
    def to_strategy_state(cls, state: SnowballState) -> StrategyState:
        """Serialize Snowball state to Core StrategyState."""
        return StrategyState.of(**{STATE_KEY: cls.to_mapping(state)})

    @classmethod
    def from_strategy_state(cls, state: StrategyState) -> SnowballState:
        """Deserialize Snowball state from Core StrategyState."""
        if STATE_KEY not in state:
            return SnowballState.new()
        return cls.from_mapping(state.require(STATE_KEY))
