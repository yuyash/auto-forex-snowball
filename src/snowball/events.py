"""Snowball domain events and mapping to Core strategy events."""

from __future__ import annotations

from dataclasses import dataclass, field

from core import (
    Metadata,
    Money,
    PositionSide,
    StrategyAction,
    StrategyContext,
    StrategyDecisionCode,
    StrategyDecisionReason,
    StrategyEvent,
    Tick,
    TradeSide,
)

from snowball.enums import CloseReason
from snowball.models.entries import FilledEntry, RequestedEntry
from snowball.models.identifiers import CycleId


@dataclass(frozen=True, slots=True)
class SnowballOpenEvent:
    """A Snowball entry request was emitted."""

    cycle_id: CycleId
    direction: PositionSide
    entry: RequestedEntry
    metadata: Metadata = field(default_factory=Metadata)


@dataclass(frozen=True, slots=True)
class SnowballCloseEvent:
    """A Snowball entry was closed."""

    cycle_id: CycleId
    direction: PositionSide
    entry: FilledEntry
    price: Money
    close_reason: CloseReason
    metadata: Metadata = field(default_factory=Metadata)


@dataclass(frozen=True, slots=True)
class SnowballStatusEvent:
    """A Snowball status update was emitted."""

    message: str
    metadata: Metadata = field(default_factory=Metadata)


@dataclass(frozen=True, slots=True)
class SnowballStopEvent:
    """A Snowball risk stop was emitted."""

    message: str
    metadata: Metadata = field(default_factory=Metadata)


type SnowballEvent = (
    SnowballOpenEvent | SnowballCloseEvent | SnowballStatusEvent | SnowballStopEvent
)


@dataclass(frozen=True, slots=True)
class SnowballEventMapper:
    """Convert Snowball domain events into Core strategy events."""

    def to_strategy_event(
        self,
        *,
        event: SnowballEvent,
        tick: Tick,
        context: StrategyContext,
    ) -> StrategyEvent:
        """Map one Snowball event to a Core StrategyEvent."""
        metadata = self._metadata(event)
        if isinstance(event, SnowballOpenEvent):
            return StrategyEvent(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                action=StrategyAction.OPEN_POSITION,
                instrument=tick.instrument,
                side=self._entry_side(event.direction),
                units=event.entry.requested_units,
                price=event.entry.requested_entry_price,
                reason=StrategyDecisionReason(
                    code=StrategyDecisionCode.ENTRY_SIGNAL,
                    rule_id="snowball.open",
                    evidence=metadata,
                ),
                metadata=metadata,
            )
        if isinstance(event, SnowballCloseEvent):
            return StrategyEvent(
                timestamp=tick.timestamp,
                task_id=context.task_id,
                action=StrategyAction.CLOSE_POSITION,
                instrument=tick.instrument,
                side=self._close_side(event.direction),
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
            return StrategyEvent(
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
        return StrategyEvent(
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

    def _metadata(self, event: SnowballEvent) -> Metadata:
        metadata = Metadata.of(
            strategy_type="snowball",
            snowball_event=self._event_name(event),
        ).merge(event.metadata)
        if isinstance(event, SnowballOpenEvent | SnowballCloseEvent):
            metadata = metadata.merge(
                Metadata.of(
                    cycle_id=event.cycle_id,
                    direction=event.direction.value,
                )
            )
            metadata = metadata.merge(event.entry.entry_id.to_metadata())
            if "is_rebuild" not in metadata:
                metadata = metadata.with_value("is_rebuild", False)
        if isinstance(event, SnowballOpenEvent):
            metadata = metadata.merge(self._requested_entry_metadata(event.entry))
            metadata = metadata.with_value("price", str(event.entry.requested_entry_price))
        if isinstance(event, SnowballCloseEvent):
            metadata = metadata.merge(self._filled_entry_metadata(event.entry))
            metadata = metadata.merge(
                Metadata.of(
                    close_reason=event.close_reason.value,
                    price=str(event.price),
                )
            )
        if isinstance(event, SnowballStatusEvent | SnowballStopEvent) and event.message:
            metadata = metadata.with_value("message", event.message)
        return metadata

    def _event_name(self, event: SnowballEvent) -> str:
        if isinstance(event, SnowballOpenEvent):
            return "open"
        if isinstance(event, SnowballCloseEvent):
            return "close"
        if isinstance(event, SnowballStatusEvent):
            return "status"
        return "stop"

    def _requested_entry_metadata(self, entry: RequestedEntry) -> Metadata:
        return Metadata.of(
            requested_units=str(entry.requested_units),
            requested_entry_price=str(entry.requested_entry_price),
            planned_take_profit_price=str(entry.planned_take_profit_price),
            planned_stop_loss_price=(
                None
                if entry.planned_stop_loss_price is None
                else str(entry.planned_stop_loss_price)
            ),
        )

    def _filled_entry_metadata(self, entry: FilledEntry) -> Metadata:
        return Metadata.of(
            requested_units=str(entry.requested.requested_units),
            requested_entry_price=str(entry.requested.requested_entry_price),
            filled_units=str(entry.filled_units),
            filled_entry_price=str(entry.filled_entry_price),
            planned_take_profit_price=str(entry.planned_take_profit_price),
            planned_stop_loss_price=(
                None
                if entry.planned_stop_loss_price is None
                else str(entry.planned_stop_loss_price)
            ),
        )

    def _entry_side(self, direction: PositionSide) -> TradeSide:
        return TradeSide.BUY if direction == PositionSide.LONG else TradeSide.SELL

    def _close_side(self, direction: PositionSide) -> TradeSide:
        return TradeSide.SELL if direction == PositionSide.LONG else TradeSide.BUY
