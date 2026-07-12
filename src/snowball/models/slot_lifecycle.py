"""Lifecycle transitions for one Snowball slot entry."""

from __future__ import annotations

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
from snowball.models.identifiers import EntryId
from snowball.models.slot_validation import (
    EntryIdentityValidator,
    EntryValueValidator,
    SlotEntry,
    SlotEntryValidation,
)


class SlotLifecycle:
    """Perform validated slot-entry lifecycle transitions."""

    @classmethod
    def place_entry(
        cls,
        current: SlotEntry | None,
        entry: RequestedEntry,
        *,
        expected_entry_id: EntryId,
    ) -> RequestedEntry:
        """Place a requested entry in an available slot."""
        if current is not None:
            raise ValueError("slot is not available")
        SlotEntryValidation.validate(entry, expected_entry_id=expected_entry_id)
        return entry

    @classmethod
    def fill_entry(cls, current: SlotEntry | None, entry: FilledEntry) -> FilledEntry:
        """Replace a requested entry with its filled entry."""
        requested = cls.requested_entry(current)
        if requested is None:
            raise ValueError("slot has no requested entry")
        if entry.requested is not requested:
            raise ValueError("filled entry does not belong to this requested entry")
        SlotEntryValidation.validate(entry, expected_entry_id=requested.entry_id)
        return entry

    @classmethod
    def request_close(
        cls,
        current: SlotEntry | None,
        *,
        planned_at: AwareDatetime,
        planned_exit_price: Money,
        close_reason: CloseReason,
        refillable: bool,
    ) -> tuple[RequestedCloseEntry, FilledEntry]:
        """Replace a live entry with a requested non-stop-loss close."""
        entry = cls.filled_entry(current)
        if entry is None:
            raise ValueError("slot has no live entry")
        requested = entry.request_close(
            planned_at=planned_at,
            planned_exit_price=planned_exit_price,
            close_reason=close_reason,
            refillable=refillable,
        )
        SlotEntryValidation.validate(requested, expected_entry_id=entry.entry_id)
        return requested, entry

    @classmethod
    def fill_close(
        cls,
        current: SlotEntry | None,
        *,
        filled_at: AwareDatetime,
    ) -> tuple[SlotEntry | None, FilledEntry]:
        """Replace a requested non-stop-loss close with its filled state."""
        requested = cls.requested_close_entry(current)
        if requested is None:
            raise ValueError("slot has no requested close entry")
        if filled_at < requested.planned_at:
            raise ValueError("close fill timestamp precedes close request")
        next_entry = requested.fill(filled_at=filled_at)
        if next_entry is not None:
            SlotEntryValidation.validate(
                next_entry,
                expected_entry_id=requested.original_entry.entry_id,
            )
        return next_entry, requested.original_entry

    @classmethod
    def request_stop_loss(
        cls,
        current: SlotEntry | None,
        *,
        planned_at: AwareDatetime,
        planned_stop_loss_price: Money,
    ) -> tuple[RequestedStopLossEntry, FilledEntry]:
        """Replace a live entry with a requested stop-loss close."""
        entry = cls.filled_entry(current)
        if entry is None:
            raise ValueError("slot has no live entry")
        requested = entry.stop_loss(
            planned_at=planned_at,
            planned_stop_loss_price=planned_stop_loss_price,
        )
        SlotEntryValidation.validate(requested, expected_entry_id=entry.entry_id)
        return requested, entry

    @classmethod
    def fill_stop_loss(
        cls,
        current: SlotEntry | None,
        *,
        filled_at: AwareDatetime,
        filled_stop_loss_price: Money,
        rebuildable: bool,
        planned_rebuild_price: Money | None,
    ) -> tuple[FilledStopLossEntry | SealedEntry, FilledEntry]:
        """Replace a requested stop-loss close with its filled state."""
        requested = cls.requested_stop_loss_entry(current)
        if requested is None:
            raise ValueError("slot has no requested stop-loss entry")
        if filled_at < requested.planned_at:
            raise ValueError("stop-loss fill timestamp precedes stop-loss request")
        next_entry = requested.fill(
            filled_at=filled_at,
            filled_stop_loss_price=filled_stop_loss_price,
            rebuildable=rebuildable,
            planned_rebuild_price=planned_rebuild_price,
        )
        SlotEntryValidation.validate(
            next_entry,
            expected_entry_id=requested.original_entry.entry_id,
        )
        return next_entry, requested.original_entry

    @classmethod
    def complete_rebuild(
        cls,
        current: SlotEntry | None,
        entry: RequestedEntry,
        *,
        expected_entry_id: EntryId,
    ) -> RequestedEntry:
        """Replace a pending rebuild with a requested rebuilt entry."""
        stop_loss_entry = cls.filled_stop_loss_entry(current)
        if stop_loss_entry is None:
            raise ValueError("slot has no pending rebuild")
        SlotEntryValidation.validate(stop_loss_entry)
        SlotEntryValidation.validate(entry, expected_entry_id=expected_entry_id)
        original_id = stop_loss_entry.original_entry.entry_id
        if not EntryIdentityValidator.same_slot(entry.entry_id, original_id):
            raise ValueError("rebuilt entry does not belong to the stopped slot")
        if entry.entry_id.build_number <= original_id.build_number:
            raise ValueError("rebuilt entry build number must advance")
        if entry.planned_at < stop_loss_entry.filled_at:
            raise ValueError("rebuild request timestamp precedes stop-loss fill")
        EntryValueValidator.require_same_currency(
            stop_loss_entry.planned_rebuild_price,
            entry.planned_entry_price,
            "rebuilt entry price",
        )
        return entry

    @classmethod
    def unseal(cls, current: SlotEntry | None) -> None:
        """Replace a sealed entry with an available slot."""
        sealed_entry = cls.sealed_entry(current)
        if sealed_entry is None:
            raise ValueError("slot is not sealed")
        return sealed_entry.unseal()

    @staticmethod
    def requested_entry(entry: SlotEntry | None) -> RequestedEntry | None:
        """Return a requested entry when present."""
        return entry if isinstance(entry, RequestedEntry) else None

    @staticmethod
    def filled_entry(entry: SlotEntry | None) -> FilledEntry | None:
        """Return a filled entry when present."""
        return entry if isinstance(entry, FilledEntry) else None

    @staticmethod
    def requested_close_entry(entry: SlotEntry | None) -> RequestedCloseEntry | None:
        """Return a requested close entry when present."""
        return entry if isinstance(entry, RequestedCloseEntry) else None

    @staticmethod
    def requested_stop_loss_entry(entry: SlotEntry | None) -> RequestedStopLossEntry | None:
        """Return a requested stop-loss entry when present."""
        return entry if isinstance(entry, RequestedStopLossEntry) else None

    @staticmethod
    def filled_stop_loss_entry(entry: SlotEntry | None) -> FilledStopLossEntry | None:
        """Return a filled stop-loss entry when present."""
        return entry if isinstance(entry, FilledStopLossEntry) else None

    @staticmethod
    def sealed_entry(entry: SlotEntry | None) -> SealedEntry | None:
        """Return a sealed entry when present."""
        return entry if isinstance(entry, SealedEntry) else None
