"""Snowball execution intents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from core import Money, PositionSide

from snowball.enums import CloseReason, SnowballIntentType
from snowball.models.entries import Entry
from snowball.models.grid import GridSlotKey


@dataclass(frozen=True, slots=True)
class SnowballIntent:
    """One broker-neutral action requested by the Snowball engine."""

    type: SnowballIntentType
    cycle_id: UUID | None = None
    direction: PositionSide | None = None
    entry: Entry | None = None
    slot_key: GridSlotKey | None = None
    price: Money | None = None
    close_reason: CloseReason | None = None
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        *,
        cycle_id: UUID,
        direction: PositionSide,
        entry: Entry,
        slot_key: GridSlotKey,
        metadata: dict[str, Any] | None = None,
    ) -> SnowballIntent:
        """Create an open-position intent."""
        return cls(
            type=SnowballIntentType.OPEN,
            cycle_id=cycle_id,
            direction=direction,
            entry=entry,
            slot_key=slot_key,
            price=entry.entry_price,
            metadata=metadata or {},
        )

    @classmethod
    def close(
        cls,
        *,
        cycle_id: UUID,
        direction: PositionSide,
        entry: Entry,
        slot_key: GridSlotKey,
        price: Money,
        close_reason: CloseReason,
        metadata: dict[str, Any] | None = None,
    ) -> SnowballIntent:
        """Create a close-position intent."""
        return cls(
            type=SnowballIntentType.CLOSE,
            cycle_id=cycle_id,
            direction=direction,
            entry=entry,
            slot_key=slot_key,
            price=price,
            close_reason=close_reason,
            metadata=metadata or {},
        )

    @classmethod
    def status(cls, *, message: str, metadata: dict[str, Any] | None = None) -> SnowballIntent:
        """Create a status intent."""
        return cls(type=SnowballIntentType.STATUS, message=message, metadata=metadata or {})

    @classmethod
    def stop(cls, *, message: str, metadata: dict[str, Any] | None = None) -> SnowballIntent:
        """Create a task-stop intent."""
        return cls(type=SnowballIntentType.STOP, message=message, metadata=metadata or {})
