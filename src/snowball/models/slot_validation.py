"""Validation policies for Snowball slot entry lifecycles."""

from __future__ import annotations

from decimal import Decimal

from core import Money
from pydantic import AwareDatetime

from snowball.enums import CloseReason
from snowball.models.entries import (
    FilledEntry,
    FilledStopLossEntry,
    RequestedCloseEntry,
    RequestedEntry,
    RequestedStopLossEntry,
    SealedEntry,
)
from snowball.models.identifiers import EntryId, EntryIdType

type SlotEntry = (
    RequestedEntry
    | FilledEntry
    | RequestedCloseEntry
    | RequestedStopLossEntry
    | FilledStopLossEntry
    | SealedEntry
)


class EntryValueValidator:
    """Validate primitive values shared by slot entries."""

    @classmethod
    def require_positive_decimal(cls, value: Decimal, name: str) -> None:
        """Require a positive decimal value."""
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    @classmethod
    def require_positive_money(cls, value: Money, name: str) -> None:
        """Require a positive Money amount."""
        if value.amount <= 0:
            raise ValueError(f"{name} must be positive")

    @classmethod
    def require_same_currency(cls, reference: Money, value: Money, name: str) -> None:
        """Require matching currencies."""
        if value.currency != reference.currency:
            raise ValueError(f"{name} currency does not match")

    @classmethod
    def require_aware_datetime(cls, value: AwareDatetime, name: str) -> None:
        """Require a timezone-aware datetime."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")


class EntryIdentityValidator:
    """Validate entry identifiers and slot identity."""

    @classmethod
    def require(
        cls,
        entry_id: EntryId,
        entry_type: EntryIdType,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        """Validate entry id type and slot identity."""
        if entry_id.entry_type != entry_type:
            raise ValueError(f"entry id type must be {entry_type.value}")
        if expected_entry_id is not None and entry_id != expected_entry_id.with_type(entry_type):
            raise ValueError("entry id does not match expected slot identity")

    @classmethod
    def same_slot(cls, left: EntryId, right: EntryId) -> bool:
        """Return whether two entry ids point to the same cycle/layer/slot."""
        return (
            left.cycle_id == right.cycle_id
            and left.layer_number == right.layer_number
            and left.slot_number == right.slot_number
        )


class EntryPriceShift:
    """Price-shift rules derived from entry fills."""

    @classmethod
    def shifted_money(
        cls,
        value: Money | None,
        *,
        planned_entry_price: Money,
        filled_entry_price: Money,
    ) -> Money | None:
        """Shift planned prices by the entry fill delta."""
        if value is None:
            return None
        fill_delta = (filled_entry_price - planned_entry_price).amount
        if not fill_delta:
            return value
        return Money.of(value.amount + fill_delta, value.currency)


class RequestedEntryValidator:
    """Validate requested entries."""

    values = EntryValueValidator
    identity = EntryIdentityValidator

    @classmethod
    def validate(
        cls,
        entry: RequestedEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        """Validate a requested entry."""
        cls.identity.require(
            entry.entry_id,
            EntryIdType.REQUESTED_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        cls.values.require_positive_decimal(entry.planned_units, "planned units")
        cls.values.require_positive_money(entry.planned_entry_price, "planned entry price")
        cls.values.require_positive_money(
            entry.planned_take_profit_price,
            "planned take-profit price",
        )
        cls.values.require_same_currency(
            entry.planned_entry_price,
            entry.planned_take_profit_price,
            "planned take-profit price",
        )
        if entry.planned_stop_loss_price is not None:
            cls.values.require_positive_money(
                entry.planned_stop_loss_price,
                "planned stop-loss price",
            )
            cls.values.require_same_currency(
                entry.planned_entry_price,
                entry.planned_stop_loss_price,
                "planned stop-loss price",
            )
        cls.values.require_aware_datetime(entry.planned_at, "planned_at")


class FilledEntryValidator:
    """Validate filled entries."""

    values = EntryValueValidator
    identity = EntryIdentityValidator
    shifts = EntryPriceShift

    @classmethod
    def validate(
        cls,
        entry: FilledEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        """Validate a filled entry."""
        RequestedEntryValidator.validate(
            entry.requested,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.REQUESTED_ENTRY)
            ),
        )
        cls.identity.require(
            entry.entry_id,
            EntryIdType.FILLED_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.requested.entry_id.with_type(EntryIdType.FILLED_ENTRY):
            raise ValueError("filled entry id does not match requested entry id")
        cls.values.require_positive_decimal(entry.filled_units, "filled units")
        cls.values.require_positive_money(entry.filled_entry_price, "filled entry price")
        cls.values.require_same_currency(
            entry.requested.planned_entry_price,
            entry.filled_entry_price,
            "filled entry price",
        )
        cls.values.require_aware_datetime(entry.filled_at, "filled_at")
        if entry.filled_at < entry.requested.planned_at:
            raise ValueError("entry fill timestamp precedes entry request")
        cls._validate_adjusted_exit_prices(entry)

    @classmethod
    def _validate_adjusted_exit_prices(cls, entry: FilledEntry) -> None:
        expected_take_profit = cls.shifts.shifted_money(
            entry.requested.planned_take_profit_price,
            planned_entry_price=entry.requested.planned_entry_price,
            filled_entry_price=entry.filled_entry_price,
        )
        if entry.planned_take_profit_price != expected_take_profit:
            raise ValueError("filled entry take-profit price is not fill-adjusted")
        expected_stop_loss = cls.shifts.shifted_money(
            entry.requested.planned_stop_loss_price,
            planned_entry_price=entry.requested.planned_entry_price,
            filled_entry_price=entry.filled_entry_price,
        )
        if entry.planned_stop_loss_price != expected_stop_loss:
            raise ValueError("filled entry stop-loss price is not fill-adjusted")


class RequestedCloseEntryValidator:
    """Validate requested non-stop-loss close entries."""

    values = EntryValueValidator
    identity = EntryIdentityValidator

    @classmethod
    def validate(
        cls,
        entry: RequestedCloseEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        """Validate a requested non-stop-loss close."""
        FilledEntryValidator.validate(
            entry.original_entry,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.FILLED_ENTRY)
            ),
        )
        cls.identity.require(
            entry.entry_id,
            EntryIdType.REQUESTED_CLOSE_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.original_entry.entry_id.with_type(
            EntryIdType.REQUESTED_CLOSE_ENTRY
        ):
            raise ValueError("requested close id does not match original entry id")
        if entry.close_reason == CloseReason.STOP_LOSS:
            raise ValueError("stop-loss close must use requested stop-loss entry")
        cls.values.require_positive_money(entry.planned_exit_price, "planned exit price")
        cls.values.require_same_currency(
            entry.original_entry.filled_entry_price,
            entry.planned_exit_price,
            "planned exit price",
        )
        cls.values.require_aware_datetime(entry.planned_at, "planned_at")
        if entry.planned_at < entry.original_entry.filled_at:
            raise ValueError("close request timestamp precedes entry fill")


class RequestedStopLossEntryValidator:
    """Validate requested stop-loss close entries."""

    values = EntryValueValidator
    identity = EntryIdentityValidator

    @classmethod
    def validate(
        cls,
        entry: RequestedStopLossEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        """Validate a requested stop-loss close."""
        FilledEntryValidator.validate(
            entry.original_entry,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.FILLED_ENTRY)
            ),
        )
        cls.identity.require(
            entry.entry_id,
            EntryIdType.REQUESTED_STOP_LOSS_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.original_entry.entry_id.with_type(
            EntryIdType.REQUESTED_STOP_LOSS_ENTRY
        ):
            raise ValueError("requested stop-loss id does not match original entry id")
        planned_stop_loss_price = entry.original_entry.planned_stop_loss_price
        if planned_stop_loss_price is None:
            raise ValueError("stop-loss request requires an original planned stop-loss price")
        if entry.planned_stop_loss_price != planned_stop_loss_price:
            raise ValueError("stop-loss request price differs from planned stop loss")
        cls.values.require_positive_money(
            entry.planned_stop_loss_price,
            "planned stop-loss price",
        )
        cls.values.require_aware_datetime(entry.planned_at, "planned_at")
        if entry.planned_at < entry.original_entry.filled_at:
            raise ValueError("stop-loss request timestamp precedes entry fill")


class FilledStopLossEntryValidator:
    """Validate filled stop-loss close entries."""

    values = EntryValueValidator
    identity = EntryIdentityValidator

    @classmethod
    def validate(
        cls,
        entry: FilledStopLossEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        """Validate a filled stop-loss close."""
        RequestedStopLossEntryValidator.validate(
            entry.requested,
            expected_entry_id=(
                None
                if expected_entry_id is None
                else expected_entry_id.with_type(EntryIdType.REQUESTED_STOP_LOSS_ENTRY)
            ),
        )
        cls.identity.require(
            entry.entry_id,
            EntryIdType.FILLED_STOP_LOSS_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        if entry.entry_id != entry.requested.entry_id.with_type(EntryIdType.FILLED_STOP_LOSS_ENTRY):
            raise ValueError("filled stop-loss id does not match stop-loss request id")
        cls.values.require_aware_datetime(entry.filled_at, "filled_at")
        if entry.filled_at < entry.requested.planned_at:
            raise ValueError("stop-loss fill timestamp precedes stop-loss request")
        cls.values.require_positive_money(
            entry.filled_stop_loss_price,
            "filled stop-loss price",
        )
        cls.values.require_same_currency(
            entry.requested.planned_stop_loss_price,
            entry.filled_stop_loss_price,
            "filled stop-loss price",
        )
        cls.values.require_positive_money(entry.planned_rebuild_price, "planned rebuild price")
        cls.values.require_same_currency(
            entry.requested.planned_stop_loss_price,
            entry.planned_rebuild_price,
            "planned rebuild price",
        )


class SealedEntryValidator:
    """Validate sealed entries."""

    values = EntryValueValidator
    identity = EntryIdentityValidator

    @classmethod
    def validate(
        cls,
        entry: SealedEntry,
        *,
        expected_entry_id: EntryId | None,
    ) -> None:
        """Validate a sealed entry."""
        cls.identity.require(
            entry.entry_id,
            EntryIdType.SEALED_ENTRY,
            expected_entry_id=expected_entry_id,
        )
        cls.values.require_aware_datetime(entry.sealed_at, "sealed_at")


class SlotEntryValidation:
    """Dispatch slot-entry validation to the matching lifecycle validator."""

    @classmethod
    def validate(cls, entry: SlotEntry, *, expected_entry_id: EntryId | None = None) -> None:
        """Validate a slot entry."""
        if isinstance(entry, RequestedEntry):
            RequestedEntryValidator.validate(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, FilledEntry):
            FilledEntryValidator.validate(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, RequestedCloseEntry):
            RequestedCloseEntryValidator.validate(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, RequestedStopLossEntry):
            RequestedStopLossEntryValidator.validate(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, FilledStopLossEntry):
            FilledStopLossEntryValidator.validate(entry, expected_entry_id=expected_entry_id)
            return
        if isinstance(entry, SealedEntry):
            SealedEntryValidator.validate(entry, expected_entry_id=expected_entry_id)
            return
        raise TypeError("unknown slot entry type")
