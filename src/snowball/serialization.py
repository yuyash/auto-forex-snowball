"""Serialization boundary for Snowball strategy state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from core import Money, PositionSide

from snowball.enums import CycleStatus
from snowball.models.entries import Entry, SlotExitPlan, StopLossSnapshot
from snowball.models.grid import Grid, Layer, Slot
from snowball.models.state import Cycle, SnowballState


class SnowballStateSerializer:
    """Convert Snowball object state to and from Core strategy-state mappings."""

    @classmethod
    def to_mapping(cls, state: SnowballState) -> dict[str, Any]:
        """Serialize Snowball state to a plain mapping."""
        return {
            "cycles": [cls._cycle_to_mapping(cycle) for cycle in state.cycles],
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SnowballState:
        """Deserialize Snowball state from a strategy-state mapping."""
        return SnowballState.from_cycles(cls._cycle_from_mapping(item) for item in data["cycles"])

    @classmethod
    def _cycle_to_mapping(cls, cycle: Cycle) -> dict[str, Any]:
        return {
            "cycle_id": str(cycle.cycle_id),
            "direction": cycle.direction.value,
            "status": cycle.status.value,
            "grid": cls._grid_to_mapping(cycle.grid),
        }

    @classmethod
    def _cycle_from_mapping(cls, data: Mapping[str, Any]) -> Cycle:
        return Cycle.create(
            cycle_id=UUID(str(data["cycle_id"])),
            direction=PositionSide(data["direction"]),
            status=CycleStatus(data["status"]),
            grid=cls._grid_from_mapping(data["grid"]),
        )

    @classmethod
    def _grid_to_mapping(cls, grid: Grid) -> dict[str, Any]:
        return {"layers": [cls._layer_to_mapping(layer) for layer in grid.layers]}

    @classmethod
    def _grid_from_mapping(cls, data: Mapping[str, Any]) -> Grid:
        return Grid(layers=[cls._layer_from_mapping(item) for item in data["layers"]])

    @classmethod
    def _layer_to_mapping(cls, layer: Layer) -> dict[str, Any]:
        return {
            "base_units": str(layer.base_units),
            "slots": [cls._slot_to_mapping(slot) for slot in layer.slots],
        }

    @classmethod
    def _layer_from_mapping(cls, data: Mapping[str, Any]) -> Layer:
        return Layer(
            base_units=Decimal(str(data["base_units"])),
            slots=[cls._slot_from_mapping(item) for item in data["slots"]],
        )

    @classmethod
    def _slot_to_mapping(cls, slot: Slot) -> dict[str, Any]:
        return {
            "entry": None if slot.entry is None else cls._entry_to_mapping(slot.entry),
            "exit_plan": cls._optional_exit_plan_to_mapping(slot.exit_plan),
            "pending_rebuild": (
                None
                if slot.pending_rebuild is None
                else cls._snapshot_to_mapping(slot.pending_rebuild)
            ),
            "sealed": slot.sealed,
            "build_count": slot.build_count,
        }

    @classmethod
    def _slot_from_mapping(cls, data: Mapping[str, Any]) -> Slot:
        return Slot(
            entry=None if data["entry"] is None else cls._entry_from_mapping(data["entry"]),
            exit_plan=cls._optional_exit_plan_from_mapping(data["exit_plan"]),
            pending_rebuild=(
                None
                if data["pending_rebuild"] is None
                else cls._snapshot_from_mapping(data["pending_rebuild"])
            ),
            sealed=bool(data["sealed"]),
            build_count=int(data["build_count"]),
        )

    @classmethod
    def _entry_to_mapping(cls, entry: Entry) -> dict[str, Any]:
        return {
            "units": str(entry.units),
            "entry_price": cls._money_to_mapping(entry.entry_price),
            "opened_at": entry.opened_at.isoformat(),
        }

    @classmethod
    def _entry_from_mapping(cls, data: Mapping[str, Any]) -> Entry:
        return Entry(
            units=Decimal(str(data["units"])),
            entry_price=cls._money_from_mapping(data["entry_price"]),
            opened_at=datetime.fromisoformat(str(data["opened_at"])),
        )

    @classmethod
    def _snapshot_to_mapping(cls, snapshot: StopLossSnapshot) -> dict[str, Any]:
        return {
            "units": str(snapshot.units),
            "entry_price": cls._money_to_mapping(snapshot.entry_price),
            "exit_plan": cls._exit_plan_to_mapping(snapshot.exit_plan),
            "opened_at": snapshot.opened_at.isoformat(),
            "closed_at": snapshot.closed_at.isoformat(),
            "stop_loss_exit_price": cls._money_to_mapping(snapshot.stop_loss_exit_price),
        }

    @classmethod
    def _snapshot_from_mapping(cls, data: Mapping[str, Any]) -> StopLossSnapshot:
        return StopLossSnapshot(
            units=Decimal(str(data["units"])),
            entry_price=cls._money_from_mapping(data["entry_price"]),
            exit_plan=cls._exit_plan_from_mapping(data["exit_plan"]),
            opened_at=datetime.fromisoformat(str(data["opened_at"])),
            closed_at=datetime.fromisoformat(str(data["closed_at"])),
            stop_loss_exit_price=cls._money_from_mapping(data["stop_loss_exit_price"]),
        )

    @classmethod
    def _exit_plan_to_mapping(cls, exit_plan: SlotExitPlan) -> dict[str, Any]:
        return {
            "take_profit_price": cls._money_to_mapping(exit_plan.take_profit_price),
            "stop_loss_price": cls._optional_money_to_mapping(exit_plan.stop_loss_price),
        }

    @classmethod
    def _exit_plan_from_mapping(cls, data: Mapping[str, Any]) -> SlotExitPlan:
        return SlotExitPlan(
            take_profit_price=cls._money_from_mapping(data["take_profit_price"]),
            stop_loss_price=cls._optional_money_from_mapping(data["stop_loss_price"]),
        )

    @classmethod
    def _optional_exit_plan_to_mapping(
        cls,
        exit_plan: SlotExitPlan | None,
    ) -> dict[str, Any] | None:
        if exit_plan is None:
            return None
        return cls._exit_plan_to_mapping(exit_plan)

    @classmethod
    def _optional_exit_plan_from_mapping(cls, data: Any) -> SlotExitPlan | None:
        if data is None:
            return None
        if not isinstance(data, Mapping):
            raise ValueError("exit plan value must be a mapping")
        return cls._exit_plan_from_mapping(data)

    @classmethod
    def _money_to_mapping(cls, value: Money) -> dict[str, Any]:
        return {"amount": str(value.amount), "currency": value.currency.code}

    @classmethod
    def _money_from_mapping(cls, value: Any) -> Money:
        if isinstance(value, Money):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("money value must be a mapping")
        return Money.of(value["amount"], value["currency"])

    @classmethod
    def _optional_money_to_mapping(cls, value: Money | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return cls._money_to_mapping(value)

    @classmethod
    def _optional_money_from_mapping(cls, value: Any) -> Money | None:
        if value is None:
            return None
        return cls._money_from_mapping(value)
