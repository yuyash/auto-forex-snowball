"""Apply Core execution reports to Snowball domain state."""

from __future__ import annotations

from collections.abc import Iterable

from core import StrategyExecutionResponse

from snowball.enums import CloseReason
from snowball.execution_report_values import SnowballExecutionReportValueCodec
from snowball.models.identifiers import EntryIdType
from snowball.models.state import SnowballState


class SnowballExecutionReportApplier:
    """Apply broker execution reports to Snowball state."""

    def apply_many(
        self,
        reports: Iterable[StrategyExecutionResponse],
        state: SnowballState,
    ) -> bool:
        """Apply execution reports and return whether state changed."""
        changed = False
        for report in reports:
            changed = self.apply(report=report, state=state) or changed
        return changed

    def apply(self, *, report: StrategyExecutionResponse, state: SnowballState) -> bool:
        """Apply one execution report."""
        return self._apply_entry_fill(report=report, state=state) or self._apply_close_fill(
            report=report,
            state=state,
        )

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
        rebuildable = SnowballExecutionReportValueCodec.metadata_bool(
            metadata.get("rebuildable", False)
        )
        raw_rebuild_price = metadata.get("planned_rebuild_price")
        planned_rebuild_price = (
            None
            if raw_rebuild_price in (None, "")
            else SnowballExecutionReportValueCodec.money(
                raw_rebuild_price,
                fallback_currency=fill_price.currency,
            )
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
