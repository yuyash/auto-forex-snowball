"""Snowball domain events."""

from __future__ import annotations

from dataclasses import dataclass, field

from core import Metadata, Money, PositionSide

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

    def __post_init__(self) -> None:
        if self.cycle_id != self.entry.entry_id.cycle_id:
            raise ValueError("open event cycle_id does not match entry cycle_id")


@dataclass(frozen=True, slots=True)
class SnowballCloseEvent:
    """A Snowball entry was closed."""

    cycle_id: CycleId
    direction: PositionSide
    entry: FilledEntry
    price: Money
    close_reason: CloseReason
    metadata: Metadata = field(default_factory=Metadata)

    def __post_init__(self) -> None:
        if self.cycle_id != self.entry.entry_id.cycle_id:
            raise ValueError("close event cycle_id does not match entry cycle_id")


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
