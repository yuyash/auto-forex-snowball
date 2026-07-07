"""Runtime adapter that connects Snowball domain services to Core callbacks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from core import (
    Money,
    StrategyContext,
    StrategyExecutionReport,
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

    def __post_init__(self) -> None:
        self.engine = SnowballEngine(self.config)

    def start(self, context: StrategyContext) -> StrategyResult:
        """Initialize or restore strategy state at task start."""
        state = self._state_from_context(context)
        return StrategyResult(state=SnowballStateSerializer.to_strategy_state(state))

    def on_tick(self, tick: Tick, context: StrategyContext) -> StrategyResult:
        """Process a tick and map Snowball events to Core strategy events."""
        state = self._state_from_context(context)
        result = self.engine.process_tick(tick=tick, state=state)
        events = tuple(
            self.event_mapper.to_strategy_event(event=event, tick=tick, context=context)
            for event in result.events
        )
        return StrategyResult(
            events=events,
            state=SnowballStateSerializer.to_strategy_state(result.state),
        )

    def on_execution_reports(
        self,
        reports: Sequence[StrategyExecutionReport],
        context: StrategyContext,
    ) -> StrategyState:
        """Apply broker execution reports to Snowball state."""
        state = self._state_from_context(context)
        for report in reports:
            self._apply_entry_fill(report=report, state=state)
            self._apply_close_fill(report=report, state=state)
        state.refresh_cycle_statuses()
        return SnowballStateSerializer.to_strategy_state(state)

    def _state_from_context(self, context: StrategyContext) -> SnowballState:
        return SnowballStateSerializer.from_strategy_state(context.state)

    def _apply_entry_fill(
        self,
        *,
        report: StrategyExecutionReport,
        state: SnowballState,
    ) -> None:
        order = report.order
        if order is None or not report.filled:
            return
        metadata = report.event.metadata
        if metadata.get("entry_type") != EntryIdType.REQUESTED_ENTRY.value:
            return
        fill_price = order.average_fill_price or report.event.price
        if fill_price is None:
            return
        entry_id = str(metadata.require("entry_id"))
        for cycle in state.cycles:
            for layer in cycle.grid.layers:
                for slot in layer.slots:
                    requested = slot.requested_entry
                    if requested is None or requested.entry_id.value != entry_id:
                        continue
                    slot.fill_entry(
                        requested.fill(
                            filled_entry_price=fill_price,
                            filled_at=report.event.timestamp,
                            filled_units=order.filled_units,
                        )
                    )
                    cycle.refresh_status()
                    return

    def _apply_close_fill(
        self,
        *,
        report: StrategyExecutionReport,
        state: SnowballState,
    ) -> None:
        order = report.order
        if order is None or not report.filled:
            return
        metadata = report.event.metadata
        raw_close_reason = metadata.get("close_reason")
        if raw_close_reason is None:
            return
        entry_id = str(metadata.require("entry_id"))
        close_reason = CloseReason(str(raw_close_reason))
        if close_reason == CloseReason.STOP_LOSS:
            self._apply_stop_loss_fill(report=report, state=state, entry_id=entry_id)
            return
        self._apply_requested_close_fill(report=report, state=state, entry_id=entry_id)

    def _apply_requested_close_fill(
        self,
        *,
        report: StrategyExecutionReport,
        state: SnowballState,
        entry_id: str,
    ) -> None:
        for cycle in state.cycles:
            for layer in cycle.grid.layers:
                for slot in layer.slots:
                    requested = slot.requested_close_entry
                    if requested is None or requested.original_entry.entry_id.value != entry_id:
                        continue
                    slot.fill_close(filled_at=report.event.timestamp)
                    cycle.refresh_status()
                    return

    def _apply_stop_loss_fill(
        self,
        *,
        report: StrategyExecutionReport,
        state: SnowballState,
        entry_id: str,
    ) -> None:
        fill_price = report.order.average_fill_price if report.order is not None else None
        fill_price = fill_price or report.event.price
        if fill_price is None:
            return
        metadata = report.event.metadata
        rebuildable = self._metadata_bool(metadata.get("rebuildable", False))
        raw_trigger_price = metadata.get("planned_rebuild_trigger_price")
        planned_rebuild_trigger_price = (
            None
            if raw_trigger_price in (None, "")
            else Money.of(str(raw_trigger_price), fill_price.currency)
        )
        for cycle in state.cycles:
            for layer in cycle.grid.layers:
                for slot in layer.slots:
                    requested = slot.requested_stop_loss_entry
                    if requested is None or requested.original_entry.entry_id.value != entry_id:
                        continue
                    slot.fill_stop_loss(
                        filled_at=report.event.timestamp,
                        filled_stop_loss_exit_price=fill_price,
                        rebuildable=rebuildable,
                        planned_rebuild_trigger_price=planned_rebuild_trigger_price,
                    )
                    cycle.refresh_status()
                    return

    @staticmethod
    def _metadata_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes"}
