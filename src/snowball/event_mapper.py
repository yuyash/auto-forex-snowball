"""Map Snowball domain events to Core strategy events."""

from __future__ import annotations

from dataclasses import dataclass, field

from core import (
    StrategyAction,
    StrategyContext,
    StrategyDecisionCode,
    StrategyDecisionReason,
    StrategyEventRequest,
    Tick,
)

from snowball.event_metadata import (
    SnowballEventMetadataMapper,
    SnowballEventSideMapper,
    SnowballRuleMapper,
)
from snowball.events import (
    SnowballCloseEvent,
    SnowballEvent,
    SnowballOpenEvent,
    SnowballStopEvent,
)


@dataclass(frozen=True, slots=True)
class SnowballEventMapper:
    """Convert Snowball domain events into Core strategy events."""

    metadata_mapper: SnowballEventMetadataMapper = field(
        default_factory=SnowballEventMetadataMapper
    )

    def to_strategy_event(
        self,
        *,
        event: SnowballEvent,
        tick: Tick,
        context: StrategyContext,
    ) -> StrategyEventRequest:
        """Map one Snowball event to a Core strategy event."""
        metadata = self.metadata_mapper.metadata(event)
        if isinstance(event, SnowballOpenEvent):
            return StrategyEventRequest(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                display_id=event.entry.entry_id.display_id,
                action=StrategyAction.OPEN_TRADE,
                instrument=tick.instrument,
                side=SnowballEventSideMapper.entry_side(event.direction),
                units=event.entry.planned_units,
                price=event.entry.planned_entry_price,
                reason=StrategyDecisionReason(
                    code=StrategyDecisionCode.ENTRY_SIGNAL,
                    rule_id=SnowballRuleMapper.open_rule_id(metadata),
                    evidence=metadata,
                ),
                metadata=metadata,
            )
        if isinstance(event, SnowballCloseEvent):
            return StrategyEventRequest(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                display_id=event.entry.entry_id.display_id,
                action=StrategyAction.CLOSE_TRADE,
                instrument=tick.instrument,
                side=SnowballEventSideMapper.close_side(event.direction),
                units=event.entry.filled_units,
                price=event.price,
                reason=StrategyDecisionReason(
                    code=StrategyDecisionCode.EXIT_SIGNAL,
                    rule_id=f"snowball.close.{event.close_reason.value}",
                    evidence=metadata,
                ),
                metadata=metadata,
            )
        if isinstance(event, SnowballStopEvent):
            return StrategyEventRequest(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                action=StrategyAction.HOLD,
                instrument=tick.instrument,
                reason=StrategyDecisionReason(
                    code=StrategyDecisionCode.RISK_REJECTED,
                    rule_id="snowball.stop",
                    evidence=metadata,
                ),
                metadata=metadata,
            )
        return StrategyEventRequest(
            timestamp=tick.timestamp,
            task_id=context.task_id,
            action=StrategyAction.HOLD,
            instrument=tick.instrument,
            reason=StrategyDecisionReason(
                code=StrategyDecisionCode.HOLD,
                rule_id="snowball.status",
                evidence=metadata,
            ),
            metadata=metadata,
        )
