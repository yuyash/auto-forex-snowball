"""Mapping from Snowball intents to Core strategy events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core import (
    Metadata,
    PositionSide,
    StrategyAction,
    StrategyContext,
    StrategyDecisionCode,
    StrategyDecisionReason,
    StrategyEvent,
    Tick,
    TradeSide,
)

from snowball.enums import SnowballIntentType
from snowball.intents import SnowballIntent
from snowball.models.entries import Entry


@dataclass(frozen=True, slots=True)
class SnowballEventMapper:
    """Convert Snowball domain intents into Core strategy events."""

    def to_strategy_event(
        self,
        *,
        intent: SnowballIntent,
        tick: Tick,
        context: StrategyContext,
    ) -> StrategyEvent:
        """Map one Snowball intent to a Core StrategyEvent."""
        metadata = self._metadata(intent)
        if intent.type == SnowballIntentType.OPEN:
            entry = self._require_entry(intent)
            direction = self._require_direction(intent)
            return StrategyEvent(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                action=StrategyAction.OPEN_POSITION,
                instrument=tick.instrument,
                side=self._entry_side(direction),
                units=entry.units,
                price=intent.price or entry.entry_price,
                reason=StrategyDecisionReason(
                    code=StrategyDecisionCode.ENTRY_SIGNAL,
                    rule_id="snowball.open",
                    evidence=Metadata.of(**metadata),
                ),
                metadata=Metadata.of(**metadata),
            )
        if intent.type == SnowballIntentType.CLOSE:
            entry = self._require_entry(intent)
            direction = self._require_direction(intent)
            return StrategyEvent(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                action=StrategyAction.CLOSE_POSITION,
                instrument=tick.instrument,
                side=self._close_side(direction),
                units=entry.units,
                price=intent.price,
                reason=StrategyDecisionReason(
                    code=StrategyDecisionCode.EXIT_SIGNAL,
                    rule_id=f"snowball.close.{intent.close_reason or 'unknown'}",
                    evidence=Metadata.of(**metadata),
                ),
                metadata=Metadata.of(**metadata),
            )
        if intent.type == SnowballIntentType.STOP:
            return StrategyEvent(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                action=StrategyAction.HOLD,
                instrument=tick.instrument,
                reason=StrategyDecisionReason(
                    code=StrategyDecisionCode.RISK_REJECTED,
                    rule_id="snowball.stop",
                    evidence=Metadata.of(**metadata),
                ),
                metadata=Metadata.of(**metadata),
            )
        return StrategyEvent(
            timestamp=tick.timestamp,
            task_id=context.task_id,
            action=StrategyAction.HOLD,
            instrument=tick.instrument,
            reason=StrategyDecisionReason(
                code=StrategyDecisionCode.HOLD,
                rule_id="snowball.status",
                evidence=Metadata.of(**metadata),
            ),
            metadata=Metadata.of(**metadata),
        )

    def _metadata(self, intent: SnowballIntent) -> dict[str, Any]:
        metadata = {
            "strategy_type": "snowball",
            "snowball_intent": intent.type.value,
            **intent.metadata,
        }
        if intent.cycle_id is not None:
            metadata["cycle_id"] = str(intent.cycle_id)
        if intent.direction is not None:
            metadata["direction"] = intent.direction.value
        if intent.close_reason is not None:
            metadata["close_reason"] = intent.close_reason.value
        if intent.message:
            metadata["message"] = intent.message
        if intent.price is not None:
            metadata["price"] = str(intent.price)
        if intent.slot_key is not None:
            metadata.update(intent.slot_key.to_metadata())
            metadata.setdefault("is_rebuild", False)
        if intent.exit_plan is not None:
            metadata["planned_exit_price"] = str(intent.exit_plan.take_profit_price)
            metadata["stop_loss_price"] = (
                None
                if intent.exit_plan.stop_loss_price is None
                else str(intent.exit_plan.stop_loss_price)
            )
        if intent.entry is not None:
            metadata.update(self._entry_metadata(intent.entry))
        return metadata

    def _entry_metadata(self, entry: Entry) -> dict[str, Any]:
        return {
            "planned_entry_price": str(entry.entry_price),
        }

    def _require_entry(self, intent: SnowballIntent) -> Entry:
        if intent.entry is None:
            raise ValueError("Snowball intent requires an entry")
        return intent.entry

    def _require_direction(self, intent: SnowballIntent) -> PositionSide:
        if intent.direction is None:
            raise ValueError("Snowball intent requires a direction")
        return intent.direction

    def _entry_side(self, direction: PositionSide) -> TradeSide:
        return TradeSide.BUY if direction == PositionSide.LONG else TradeSide.SELL

    def _close_side(self, direction: PositionSide) -> TradeSide:
        return TradeSide.SELL if direction == PositionSide.LONG else TradeSide.BUY
