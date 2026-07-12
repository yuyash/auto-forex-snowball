"""Metadata and side mapping for Snowball strategy events."""

from __future__ import annotations

from core import Metadata, PositionSide, TradeSide

from snowball.events import (
    SnowballCloseEvent,
    SnowballEvent,
    SnowballOpenEvent,
    SnowballStatusEvent,
    SnowballStopEvent,
)
from snowball.models.entries import FilledEntry, RequestedEntry


class SnowballEventName:
    """Map Snowball event classes to stable event names."""

    @classmethod
    def of(cls, event: SnowballEvent) -> str:
        """Return a stable metadata event name."""
        if isinstance(event, SnowballOpenEvent):
            return "open"
        if isinstance(event, SnowballCloseEvent):
            return "close"
        if isinstance(event, SnowballStatusEvent):
            return "status"
        return "stop"


class SnowballEventSideMapper:
    """Map Snowball directions to Core strategy trade sides."""

    @classmethod
    def entry_side(cls, direction: PositionSide) -> TradeSide:
        """Return the side used to open a Snowball entry."""
        return TradeSide.BUY if direction == PositionSide.LONG else TradeSide.SELL

    @classmethod
    def close_side(cls, direction: PositionSide) -> TradeSide:
        """Return the side used to close a Snowball entry."""
        return TradeSide.SELL if direction == PositionSide.LONG else TradeSide.BUY


class SnowballEventMetadataMapper:
    """Build Core metadata for Snowball events."""

    def metadata(self, event: SnowballEvent) -> Metadata:
        """Return Core metadata for one Snowball event."""
        metadata = Metadata.of(
            strategy_type="snowball",
            snowball_event=SnowballEventName.of(event),
        ).merge(event.metadata)
        if isinstance(event, SnowballOpenEvent | SnowballCloseEvent):
            metadata = self.entry_event_metadata(event, metadata)
        if isinstance(event, SnowballOpenEvent):
            metadata = self.open_event_metadata(event, metadata)
        if isinstance(event, SnowballCloseEvent):
            metadata = self.close_event_metadata(event, metadata)
        if isinstance(event, SnowballStatusEvent | SnowballStopEvent) and event.message:
            metadata = metadata.with_value("message", event.message)
        return metadata

    def entry_event_metadata(
        self,
        event: SnowballOpenEvent | SnowballCloseEvent,
        metadata: Metadata,
    ) -> Metadata:
        """Add metadata shared by open and close events."""
        metadata = metadata.merge(
            Metadata.of(
                cycle_id=event.cycle_id,
                direction=event.direction.value,
            )
        )
        metadata = metadata.merge(event.entry.entry_id.to_metadata())
        if "is_rebuild" not in metadata:
            metadata = metadata.with_value("is_rebuild", False)
        return metadata

    def open_event_metadata(self, event: SnowballOpenEvent, metadata: Metadata) -> Metadata:
        """Add requested-entry metadata for open events."""
        metadata = metadata.merge(self.requested_entry_metadata(event.entry))
        metadata = metadata.with_value("price", str(event.entry.planned_entry_price))
        if self.metadata_bool(metadata.get("is_rebuild", False)):
            metadata = metadata.with_value(
                "planned_rebuild_price",
                str(event.entry.planned_entry_price),
            )
        return metadata

    def close_event_metadata(self, event: SnowballCloseEvent, metadata: Metadata) -> Metadata:
        """Add filled-entry metadata for close events."""
        metadata = metadata.merge(self.filled_entry_metadata(event.entry))
        return metadata.merge(
            Metadata.of(
                close_reason=event.close_reason.value,
                price=str(event.price),
            )
        )

    @classmethod
    def requested_entry_metadata(cls, entry: RequestedEntry) -> Metadata:
        """Return requested-entry metadata."""
        return Metadata.of(
            planned_units=str(entry.planned_units),
            planned_entry_price=str(entry.planned_entry_price),
            planned_take_profit_price=str(entry.planned_take_profit_price),
            planned_stop_loss_price=(
                None
                if entry.planned_stop_loss_price is None
                else str(entry.planned_stop_loss_price)
            ),
        )

    @classmethod
    def filled_entry_metadata(cls, entry: FilledEntry) -> Metadata:
        """Return filled-entry metadata."""
        return Metadata.of(
            planned_units=str(entry.requested.planned_units),
            planned_entry_price=str(entry.requested.planned_entry_price),
            filled_units=str(entry.filled_units),
            filled_entry_price=str(entry.filled_entry_price),
            planned_take_profit_price=str(entry.planned_take_profit_price),
            planned_stop_loss_price=(
                None
                if entry.planned_stop_loss_price is None
                else str(entry.planned_stop_loss_price)
            ),
        )

    @classmethod
    def metadata_bool(cls, value: object) -> bool:
        """Parse metadata booleans."""
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes"}


class SnowballRuleMapper:
    """Map Snowball event metadata to Core rule IDs."""

    @classmethod
    def open_rule_id(cls, metadata: Metadata) -> str:
        """Return the rule ID for a Snowball open event."""
        if metadata.get("is_rebuild") is True:
            return "snowball.open.rebuild"
        return "snowball.open"
