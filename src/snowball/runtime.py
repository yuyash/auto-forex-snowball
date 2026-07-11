"""Runtime adapter that connects Snowball domain services to Core callbacks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core import (
    Currency,
    Money,
    StrategyContext,
    StrategyExecutionResponse,
    StrategyResult,
    StrategyState,
    Tick,
)

from snowball.config import SnowballConfig
from snowball.engine import SnowballEngine
from snowball.enums import CloseReason
from snowball.event_mapper import SnowballEventMapper
from snowball.models.identifiers import EntryIdType
from snowball.models.state import SnowballState
from snowball.serialization import SnowballStateSerializer


@dataclass(slots=True)
class SnowballRuntime:
    """Own Snowball execution dependencies for one strategy instance."""

    config: SnowballConfig
    engine: SnowballEngine = field(init=False)
    event_mapper: SnowballEventMapper = field(default_factory=SnowballEventMapper)
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
        for report in reports:
            self._apply_entry_fill(report=report, state=state)
            self._apply_close_fill(report=report, state=state)
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

    def _apply_entry_fill(
        self,
        *,
        report: StrategyExecutionResponse,
        state: SnowballState,
    ) -> bool:
        order = report.order
        if order is None or not report.filled:
            return False
        metadata = report.event.metadata
        if metadata.get("entry_type") != EntryIdType.REQUESTED_ENTRY.value:
            return False
        fill_price = order.average_fill_price or report.event.price
        if fill_price is None:
            return False
        entry_id = str(metadata.require("entry_id"))
        for cycle in state.iter_cycles():
            for layer in cycle.grid.iter_layers():
                for slot in layer.iter_slots():
                    requested = slot.requested_entry
                    if requested is None or requested.entry_id.value != entry_id:
                        continue
                    filled_entry = requested.fill(
                        filled_entry_price=fill_price,
                        filled_at=report.event.timestamp,
                        filled_units=order.filled_units,
                    )
                    slot.fill_entry(filled_entry)
                    cycle.refresh_status()
                    return True
        return False

    def _apply_close_fill(
        self,
        *,
        report: StrategyExecutionResponse,
        state: SnowballState,
    ) -> bool:
        order = report.order
        if order is None or not report.filled:
            return False
        metadata = report.event.metadata
        raw_close_reason = metadata.get("close_reason")
        if raw_close_reason is None:
            return False
        entry_id = str(metadata.require("entry_id"))
        close_reason = CloseReason(str(raw_close_reason))
        if close_reason == CloseReason.STOP_LOSS:
            return self._apply_stop_loss_fill(report=report, state=state, entry_id=entry_id)
        return self._apply_requested_close_fill(report=report, state=state, entry_id=entry_id)

    def _apply_requested_close_fill(
        self,
        *,
        report: StrategyExecutionResponse,
        state: SnowballState,
        entry_id: str,
    ) -> bool:
        for cycle in state.iter_cycles():
            for layer in cycle.grid.iter_layers():
                for slot in layer.iter_slots():
                    requested = slot.requested_close_entry
                    if requested is None or requested.original_entry.entry_id.value != entry_id:
                        continue
                    slot.fill_close(filled_at=report.event.timestamp)
                    cycle.refresh_status()
                    return True
        return False

    def _apply_stop_loss_fill(
        self,
        *,
        report: StrategyExecutionResponse,
        state: SnowballState,
        entry_id: str,
    ) -> bool:
        fill_price = report.order.average_fill_price if report.order is not None else None
        fill_price = fill_price or report.event.price
        if fill_price is None:
            return False
        metadata = report.event.metadata
        rebuildable = self._metadata_bool(metadata.get("rebuildable", False))
        raw_rebuild_price = metadata.get("planned_rebuild_price")
        planned_rebuild_price = (
            None
            if raw_rebuild_price in (None, "")
            else self._money_from_metadata(raw_rebuild_price, fallback_currency=fill_price.currency)
        )
        for cycle in state.iter_cycles():
            for layer in cycle.grid.iter_layers():
                for slot in layer.iter_slots():
                    requested = slot.requested_stop_loss_entry
                    if requested is None or requested.original_entry.entry_id.value != entry_id:
                        continue
                    slot.fill_stop_loss(
                        filled_at=report.event.timestamp,
                        filled_stop_loss_price=fill_price,
                        rebuildable=rebuildable,
                        planned_rebuild_price=planned_rebuild_price,
                    )
                    cycle.refresh_status()
                    return True
        return False

    @staticmethod
    def _metadata_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes"}

    @staticmethod
    def _money_from_metadata(value: object, *, fallback_currency: Currency) -> Money:
        if isinstance(value, Money):
            return value
        text = str(value)
        parts = text.split()
        if len(parts) == 2:
            return Money.of(parts[0], parts[1])
        return Money.of(text, fallback_currency)
