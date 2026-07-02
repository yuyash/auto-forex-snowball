"""Entry models for Snowball grid slots."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money
from pydantic import AwareDatetime


@dataclass(slots=True)
class Entry:
    """One entered trade held in a grid slot, with its planned exit prices.

    An ``Entry`` bundles sizing, entry, and exit data for a single grid slot so
    that a live trade and its take-profit/stop-loss can never fall out of sync.
    """

    units: Decimal
    entry_price: Money
    opened_at: AwareDatetime
    take_profit_price: Money
    stop_loss_price: Money | None = None


@dataclass(slots=True)
class PendingRebuild:
    """A stop-loss-closed slot entry waiting to revisit its rebuild price."""

    entry: Entry
    closed_at: AwareDatetime
    stop_loss_exit_price: Money

    @property
    def entry_price(self) -> Money:
        """Return the original entry price of the stopped entry."""
        return self.entry.entry_price

    @property
    def units(self) -> Decimal:
        """Return the original units of the stopped entry."""
        return self.entry.units

    @property
    def take_profit_price(self) -> Money:
        """Return the original take-profit price of the stopped entry."""
        return self.entry.take_profit_price

    @property
    def stop_loss_price(self) -> Money | None:
        """Return the original stop-loss price of the stopped entry."""
        return self.entry.stop_loss_price
