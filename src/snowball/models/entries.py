"""Entry models for Snowball grid positions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Money
from pydantic import AwareDatetime


@dataclass(slots=True)
class SlotPosition:
    """Common position data for a grid slot."""

    units: Decimal
    entry_price: Money
    opened_at: AwareDatetime


@dataclass(slots=True)
class Entry(SlotPosition):
    """One live Snowball position in a grid slot."""


@dataclass(slots=True)
class SlotExitPlan:
    """Exit prices owned by a grid slot."""

    take_profit_price: Money
    stop_loss_price: Money | None = None


@dataclass(slots=True)
class StopLossSnapshot(SlotPosition):
    """Snapshot retained when a slot is waiting for stop-loss rebuild."""

    exit_plan: SlotExitPlan
    closed_at: AwareDatetime
    stop_loss_exit_price: Money

    @classmethod
    def from_entry(
        cls,
        entry: Entry,
        *,
        exit_plan: SlotExitPlan,
        closed_at: AwareDatetime,
        stop_loss_exit_price: Money,
    ) -> StopLossSnapshot:
        """Create a rebuild snapshot from a live entry closed by stop loss."""
        return cls(
            units=entry.units,
            entry_price=entry.entry_price,
            exit_plan=exit_plan,
            opened_at=entry.opened_at,
            closed_at=closed_at,
            stop_loss_exit_price=stop_loss_exit_price,
        )
