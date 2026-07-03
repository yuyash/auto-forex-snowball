"""Snowball-specific enumerations."""

from __future__ import annotations

from enum import StrEnum


class EntryRole(StrEnum):
    """Role of one Snowball grid entry."""

    INITIAL = "initial"
    COUNTER = "counter"
    LAYER_INITIAL = "layer_initial"


class CycleStatus(StrEnum):
    """Lifecycle state of one directional Snowball cycle."""

    ACTIVE = "active"
    PENDING = "pending"
    COMPLETED = "completed"


class SlotStatus(StrEnum):
    """State of one L/R slot."""

    AVAILABLE = "available"
    OCCUPIED = "occupied"
    PENDING_REBUILD = "pending_rebuild"
    SEALED = "sealed"


class IntervalMode(StrEnum):
    """Progression mode for pip distances."""

    CONSTANT = "constant"
    ADDITIVE = "additive"
    SUBTRACTIVE = "subtractive"
    MULTIPLICATIVE = "multiplicative"
    DIVISIVE = "divisive"
    MANUAL = "manual"


class CounterTakeProfitMode(StrEnum):
    """Policy for counter-entry take-profit distance."""

    WEIGHTED_AVG = "weighted_avg"
    FIXED = "fixed"
    ADDITIVE = "additive"
    SUBTRACTIVE = "subtractive"
    MULTIPLICATIVE = "multiplicative"
    DIVISIVE = "divisive"


class RebuildEntryPriceMode(StrEnum):
    """Price that must be revisited before rebuilding a stopped slot."""

    ORIGINAL_ENTRY_PRICE = "original_entry_price"
    STOP_LOSS_EXIT_PRICE = "stop_loss_exit_price"


class RebuildTakeProfitMode(StrEnum):
    """Take-profit policy for rebuilt entries."""

    SAME_PRICE = "same_price"
    SAME_DISTANCE = "same_distance"
    PROGRESSIVE_DISTANCE = "progressive_distance"


class RebuildStopLossMode(StrEnum):
    """Stop-loss policy for rebuilt entries."""

    SAME_PRICE = "same_price"
    SAME_DISTANCE = "same_distance"
    MANUAL_DISTANCE = "manual_distance"


class CloseReason(StrEnum):
    """Reason a Snowball entry is closed."""

    TAKE_PROFIT = "tp"
    COUNTER_TAKE_PROFIT = "counter_tp"
    LAYER_INITIAL_TAKE_PROFIT = "layer_initial_tp"
    STOP_LOSS = "stop_loss"
    SHRINK = "shrink"
    EMERGENCY = "emergency"
