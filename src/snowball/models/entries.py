"""Entry models for Snowball grid slots."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core import Money
from pydantic import AwareDatetime

from snowball.models.identifiers import EntryId, EntryIdType


@dataclass(slots=True)
class RequestedEntry:
    """A slot entry requested by the strategy before broker fill confirmation.

    Attributes:
        entry_id: Stable entry identifier assigned from the cycle and slot.
        requested_units: Position size requested for the slot.
        requested_entry_price: Price used when requesting the entry.
        requested_at: Tick timestamp when the entry was requested.
        planned_take_profit_price: Planned take-profit exit price.
        planned_stop_loss_price: Planned stop-loss exit price, when stop loss is enabled.
    """

    entry_id: EntryId
    requested_units: Decimal
    requested_entry_price: Money
    requested_at: AwareDatetime
    planned_take_profit_price: Money
    planned_stop_loss_price: Money | None = None

    def __post_init__(self) -> None:
        self.entry_id = self.entry_id.with_type(EntryIdType.REQUESTED_ENTRY)

    def fill(
        self,
        *,
        filled_entry_price: Money,
        filled_at: AwareDatetime,
        filled_units: Decimal | None = None,
    ) -> FilledEntry:
        """Return a filled entry from this requested entry."""
        return FilledEntry(
            entry_id=self.entry_id.with_type(EntryIdType.FILLED_ENTRY),
            requested=self,
            filled_units=self.requested_units if filled_units is None else filled_units,
            filled_entry_price=filled_entry_price,
            filled_at=filled_at,
        )


@dataclass(slots=True)
class FilledEntry:
    """A slot entry after broker fill confirmation.

    Attributes:
        entry_id: Stable entry identifier for the filled entry state.
        requested: Entry request that produced this fill.
        filled_units: Actual broker filled position size.
        filled_entry_price: Actual broker fill price.
        filled_at: Broker fill timestamp.
        filled_stop_loss_entry: Filled stop-loss close for this entry, when closed by stop loss.
    """

    entry_id: EntryId
    requested: RequestedEntry
    filled_units: Decimal
    filled_entry_price: Money
    filled_at: AwareDatetime
    filled_stop_loss_entry: FilledStopLossEntry | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        self.entry_id = self.entry_id.with_type(EntryIdType.FILLED_ENTRY)

    @property
    def planned_take_profit_price(self) -> Money:
        """Return the planned take-profit exit price."""
        return self.requested.planned_take_profit_price

    @planned_take_profit_price.setter
    def planned_take_profit_price(self, value: Money) -> None:
        self.requested.planned_take_profit_price = value

    @property
    def planned_stop_loss_price(self) -> Money | None:
        """Return the planned stop-loss exit price."""
        return self.requested.planned_stop_loss_price

    def close(
        self,
        *,
        closed_at: AwareDatetime,
        refillable: bool,
    ) -> SealedEntry | None:
        """Return the slot state after a normal close."""
        if refillable:
            return None
        return self.seal(sealed_at=closed_at)

    def stop_loss(
        self,
        *,
        requested_stop_loss_exit_price: Money,
        requested_at: AwareDatetime,
    ) -> RequestedStopLossEntry:
        """Return a requested stop-loss close for this entry."""
        return RequestedStopLossEntry(
            entry_id=self.entry_id.with_type(EntryIdType.REQUESTED_STOP_LOSS_ENTRY),
            original_entry=self,
            requested_stop_loss_exit_price=requested_stop_loss_exit_price,
            requested_at=requested_at,
        )

    def seal(self, *, sealed_at: AwareDatetime) -> SealedEntry:
        """Return a sealed marker for this closed entry."""
        return SealedEntry(
            entry_id=self.entry_id.with_type(EntryIdType.SEALED_ENTRY),
            sealed_at=sealed_at,
        )

    def record_stop_loss(self, stop_loss_entry: FilledStopLossEntry) -> None:
        """Record the filled stop-loss close for this entry."""
        existing = self.filled_stop_loss_entry
        if existing is not None and existing is not stop_loss_entry:
            raise ValueError("original entry already has a filled stop-loss entry")
        self.filled_stop_loss_entry = stop_loss_entry


@dataclass(slots=True)
class RequestedStopLossEntry:
    """A stop-loss close requested before broker fill confirmation.

    Attributes:
        entry_id: Stable entry identifier for the requested stop-loss state.
        original_entry: FilledEntry state before the stop-loss close.
        requested_stop_loss_exit_price: Price used when requesting the stop-loss close.
        requested_at: Tick timestamp when the stop-loss close was requested.
    """

    entry_id: EntryId
    original_entry: FilledEntry
    requested_stop_loss_exit_price: Money
    requested_at: AwareDatetime

    def __post_init__(self) -> None:
        self.entry_id = self.entry_id.with_type(EntryIdType.REQUESTED_STOP_LOSS_ENTRY)

    @property
    def original_filled_entry_price(self) -> Money:
        """Return the original filled entry price of the stop-loss request."""
        return self.original_entry.filled_entry_price

    @property
    def filled_units(self) -> Decimal:
        """Return the original filled units of the stop-loss request."""
        return self.original_entry.filled_units

    @property
    def planned_take_profit_price(self) -> Money:
        """Return the original planned take-profit price of the stop-loss request."""
        return self.original_entry.planned_take_profit_price

    @property
    def planned_stop_loss_price(self) -> Money | None:
        """Return the original planned stop-loss price of the stop-loss request."""
        return self.original_entry.planned_stop_loss_price

    def fill(
        self,
        *,
        filled_at: AwareDatetime,
        filled_stop_loss_exit_price: Money,
        rebuildable: bool,
        planned_rebuild_trigger_price: Money | None,
    ) -> FilledStopLossEntry | SealedEntry:
        """Return the slot state after the stop-loss close is filled."""
        if not rebuildable:
            return self.original_entry.seal(sealed_at=filled_at)
        if planned_rebuild_trigger_price is None:
            raise ValueError("rebuildable stop loss requires rebuild trigger price")
        return FilledStopLossEntry(
            entry_id=self.entry_id.with_type(EntryIdType.FILLED_STOP_LOSS_ENTRY),
            requested=self,
            filled_at=filled_at,
            filled_stop_loss_exit_price=filled_stop_loss_exit_price,
            planned_rebuild_trigger_price=planned_rebuild_trigger_price,
        )


@dataclass(slots=True)
class FilledStopLossEntry:
    """A stop-loss close after broker fill confirmation, retained for rebuild.

    Attributes:
        entry_id: Stable entry identifier for the filled stop-loss state.
        requested: Stop-loss request that produced this fill.
        filled_at: Broker fill timestamp for the stop-loss close.
        filled_stop_loss_exit_price: Actual broker fill price at the stop-loss close.
        planned_rebuild_trigger_price: Price that must be revisited before rebuilding this slot.
    """

    entry_id: EntryId
    requested: RequestedStopLossEntry
    filled_at: AwareDatetime
    filled_stop_loss_exit_price: Money
    planned_rebuild_trigger_price: Money

    def __post_init__(self) -> None:
        self.entry_id = self.entry_id.with_type(EntryIdType.FILLED_STOP_LOSS_ENTRY)

    @property
    def original_entry(self) -> FilledEntry:
        """Return the filled entry before the stop-loss close."""
        return self.requested.original_entry

    @property
    def original_filled_entry_price(self) -> Money:
        """Return the original filled entry price of the filled stop-loss entry."""
        return self.original_entry.filled_entry_price

    @property
    def filled_units(self) -> Decimal:
        """Return the original filled units of the filled stop-loss entry."""
        return self.original_entry.filled_units

    @property
    def planned_take_profit_price(self) -> Money:
        """Return the original planned take-profit price of the filled stop-loss entry."""
        return self.original_entry.planned_take_profit_price

    @property
    def planned_stop_loss_price(self) -> Money | None:
        """Return the original planned stop-loss price of the filled stop-loss entry."""
        return self.original_entry.planned_stop_loss_price

    def rebuild(self, entry: RequestedEntry | FilledEntry) -> RequestedEntry | FilledEntry:
        """Return the entry replacing this stop-loss entry."""
        return entry


@dataclass(frozen=True, slots=True)
class SealedEntry:
    """A marker for a closed slot that must not be refilled.

    Attributes:
        entry_id: Stable entry identifier for the sealed entry state.
        sealed_at: Tick timestamp when the slot became permanently closed.
    """

    entry_id: EntryId
    sealed_at: AwareDatetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_id", self.entry_id.with_type(EntryIdType.SEALED_ENTRY))

    def unseal(self) -> None:
        """Return the available slot state after unsealing."""
        return None
