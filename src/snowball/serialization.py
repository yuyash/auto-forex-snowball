"""Serialization boundary for Snowball strategy state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from core import Money, PositionSide, StrategyState

from snowball.enums import CloseReason, CycleStatus
from snowball.models.entries import (
    FilledEntry,
    FilledStopLossEntry,
    RequestedCloseEntry,
    RequestedEntry,
    RequestedStopLossEntry,
    SealedEntry,
)
from snowball.models.grid import Grid, Layer, Slot
from snowball.models.identifiers import EntryId, EntryIdType
from snowball.models.state import Cycle, SnowballState

STATE_KEY = "snowball"


class SnowballStateSerializer:
    """Convert Snowball object state to and from Core strategy-state mappings."""

    @classmethod
    def to_mapping(cls, state: SnowballState) -> dict[str, Any]:
        """Serialize Snowball state to a plain mapping."""
        return {
            "cycles": [cls._cycle_to_mapping(cycle) for cycle in state.cycles],
            "next_cycle_id": state.next_cycle_id_value,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SnowballState:
        """Deserialize Snowball state from a strategy-state mapping."""
        state = SnowballState.from_cycles(cls._cycle_from_mapping(item) for item in data["cycles"])
        if "next_cycle_id" in data:
            state.restore_next_cycle_id(int(data["next_cycle_id"]))
        return state

    @classmethod
    def to_strategy_state(cls, state: SnowballState) -> StrategyState:
        """Serialize Snowball state to Core StrategyState."""
        return StrategyState.of(**{STATE_KEY: cls.to_mapping(state)})

    @classmethod
    def from_strategy_state(cls, state: StrategyState) -> SnowballState:
        """Deserialize Snowball state from Core StrategyState."""
        if STATE_KEY not in state:
            return SnowballState.new()
        return cls.from_mapping(state.require(STATE_KEY))

    @classmethod
    def _cycle_to_mapping(cls, cycle: Cycle) -> dict[str, Any]:
        return {
            "cycle_id": cycle.cycle_id,
            "direction": cycle.direction.value,
            "status": cycle.status.value,
            "grid": cls._grid_to_mapping(cycle.grid),
        }

    @classmethod
    def _cycle_from_mapping(cls, data: Mapping[str, Any]) -> Cycle:
        return Cycle.create(
            cycle_id=int(data["cycle_id"]),
            direction=PositionSide(data["direction"]),
            status=CycleStatus(data["status"]),
            grid=cls._grid_from_mapping(data["grid"]),
        )

    @classmethod
    def _grid_to_mapping(cls, grid: Grid) -> dict[str, Any]:
        return {
            "layers": {
                str(grid.layer_number(layer)): cls._layer_to_mapping(layer) for layer in grid.layers
            }
        }

    @classmethod
    def _grid_from_mapping(cls, data: Mapping[str, Any]) -> Grid:
        layers = data["layers"]
        if not isinstance(layers, Mapping):
            raise ValueError("grid layers must be a mapping")
        return Grid.from_layers(
            {
                int(layer_number): cls._layer_from_mapping(item)
                for layer_number, item in layers.items()
            }
        )

    @classmethod
    def _layer_to_mapping(cls, layer: Layer) -> dict[str, Any]:
        return {
            "base_units": str(layer.base_units),
            "build_counts": {
                str(slot_number): build_count
                for slot_number, build_count in layer.build_counts().items()
            },
            "slots": {
                str(layer.slot_number(slot)): cls._slot_to_mapping(slot) for slot in layer.slots
            },
        }

    @classmethod
    def _layer_from_mapping(cls, data: Mapping[str, Any]) -> Layer:
        slots = data["slots"]
        if not isinstance(slots, Mapping):
            raise ValueError("layer slots must be a mapping")
        build_counts = data["build_counts"]
        if not isinstance(build_counts, Mapping):
            raise ValueError("layer build counts must be a mapping")
        return Layer.from_slots(
            base_units=Decimal(str(data["base_units"])),
            slots={
                int(slot_number): cls._slot_from_mapping(item)
                for slot_number, item in slots.items()
            },
            build_counts={
                int(slot_number): int(build_count)
                for slot_number, build_count in build_counts.items()
            },
        )

    @classmethod
    def _slot_to_mapping(cls, slot: Slot) -> dict[str, Any]:
        requested_entry = slot.requested_entry
        filled_entry = slot.filled_entry
        requested_close_entry = slot.requested_close_entry
        requested_stop_loss_entry = slot.requested_stop_loss_entry
        filled_stop_loss_entry = slot.filled_stop_loss_entry
        sealed_entry = slot.sealed_entry
        return {
            "requested_entry": (
                None
                if requested_entry is None
                else cls._requested_entry_to_mapping(requested_entry)
            ),
            "filled_entry": (
                None if filled_entry is None else cls._filled_entry_to_mapping(filled_entry)
            ),
            "requested_close_entry": (
                None
                if requested_close_entry is None
                else cls._requested_close_entry_to_mapping(requested_close_entry)
            ),
            "requested_stop_loss_entry": (
                None
                if requested_stop_loss_entry is None
                else cls._requested_stop_loss_entry_to_mapping(requested_stop_loss_entry)
            ),
            "filled_stop_loss_entry": (
                None
                if filled_stop_loss_entry is None
                else cls._filled_stop_loss_entry_to_mapping(filled_stop_loss_entry)
            ),
            "sealed": slot.is_sealed,
            "sealed_entry_id": (
                None if sealed_entry is None else cls._entry_id_to_mapping(sealed_entry.entry_id)
            ),
            "sealed_at": None if sealed_entry is None else sealed_entry.sealed_at.isoformat(),
        }

    @classmethod
    def _slot_from_mapping(cls, data: Mapping[str, Any]) -> Slot:
        requested_entry = (
            None
            if data["requested_entry"] is None
            else cls._requested_entry_from_mapping(data["requested_entry"])
        )
        filled_entry = (
            None
            if data["filled_entry"] is None
            else cls._filled_entry_from_mapping(data["filled_entry"])
        )
        requested_close_entry = (
            None
            if data.get("requested_close_entry") is None
            else cls._requested_close_entry_from_mapping(data["requested_close_entry"])
        )
        requested_stop_loss_entry = (
            None
            if data["requested_stop_loss_entry"] is None
            else cls._requested_stop_loss_entry_from_mapping(data["requested_stop_loss_entry"])
        )
        filled_stop_loss_entry = (
            None
            if data["filled_stop_loss_entry"] is None
            else cls._filled_stop_loss_entry_from_mapping(data["filled_stop_loss_entry"])
        )
        sealed = bool(data["sealed"])
        sealed_at = (
            None if data.get("sealed_at") is None else cls._aware_datetime(data["sealed_at"])
        )
        sealed_entry_id = (
            None
            if data["sealed_entry_id"] is None
            else cls._entry_id_from_mapping(data["sealed_entry_id"])
        )
        if sealed and sealed_at is None:
            raise ValueError("sealed slot requires sealed_at")
        if sealed and sealed_entry_id is None:
            raise ValueError("sealed slot requires sealed_entry_id")
        if not sealed and sealed_at is not None:
            raise ValueError("unsealed slot must not include sealed_at")
        if not sealed and sealed_entry_id is not None:
            raise ValueError("unsealed slot must not include sealed_entry_id")
        populated_count = sum(
            item is not None
            for item in (
                requested_entry,
                filled_entry,
                requested_close_entry,
                requested_stop_loss_entry,
                filled_stop_loss_entry,
            )
        ) + int(sealed)
        if populated_count > 1:
            raise ValueError("slot cannot contain multiple entry states")
        return Slot(
            entry=requested_entry
            or filled_entry
            or requested_close_entry
            or requested_stop_loss_entry
            or filled_stop_loss_entry
            or (
                SealedEntry(entry_id=sealed_entry_id, sealed_at=sealed_at)
                if sealed and sealed_at is not None and sealed_entry_id is not None
                else None
            ),
        )

    @classmethod
    def _requested_entry_to_mapping(cls, entry: RequestedEntry) -> dict[str, Any]:
        return {
            "entry_id": cls._entry_id_to_mapping(entry.entry_id),
            "requested_units": str(entry.requested_units),
            "requested_entry_price": cls._money_to_mapping(entry.requested_entry_price),
            "requested_at": entry.requested_at.isoformat(),
            "planned_take_profit_price": cls._money_to_mapping(entry.planned_take_profit_price),
            "planned_stop_loss_price": cls._optional_money_to_mapping(
                entry.planned_stop_loss_price,
            ),
        }

    @classmethod
    def _requested_entry_from_mapping(cls, data: Mapping[str, Any]) -> RequestedEntry:
        return RequestedEntry(
            entry_id=cls._entry_id_from_mapping(data["entry_id"]),
            requested_units=Decimal(str(data["requested_units"])),
            requested_entry_price=cls._money_from_mapping(data["requested_entry_price"]),
            requested_at=cls._aware_datetime(data["requested_at"]),
            planned_take_profit_price=cls._money_from_mapping(data["planned_take_profit_price"]),
            planned_stop_loss_price=cls._optional_money_from_mapping(
                data["planned_stop_loss_price"],
            ),
        )

    @classmethod
    def _entry_id_to_mapping(cls, entry_id: EntryId) -> dict[str, Any]:
        return {
            "cycle_id": entry_id.cycle_id,
            "layer_number": entry_id.layer_number,
            "slot_number": entry_id.slot_number,
            "build_count": entry_id.build_count,
            "entry_type": entry_id.entry_type.value,
        }

    @classmethod
    def _entry_id_from_mapping(cls, data: Mapping[str, Any]) -> EntryId:
        return EntryId(
            cycle_id=int(data["cycle_id"]),
            layer_number=int(data["layer_number"]),
            slot_number=int(data["slot_number"]),
            build_count=int(data["build_count"]),
            entry_type=EntryIdType(data["entry_type"]),
        )

    @classmethod
    def _filled_entry_to_mapping(cls, entry: FilledEntry) -> dict[str, Any]:
        return {
            **cls._requested_entry_to_mapping(entry.requested),
            "filled_entry_id": cls._entry_id_to_mapping(entry.entry_id),
            "filled_units": str(entry.filled_units),
            "filled_entry_price": cls._money_to_mapping(entry.filled_entry_price),
            "filled_at": entry.filled_at.isoformat(),
            "current_planned_take_profit_price": cls._money_to_mapping(
                entry.planned_take_profit_price,
            ),
            "current_planned_stop_loss_price": cls._optional_money_to_mapping(
                entry.planned_stop_loss_price,
            ),
        }

    @classmethod
    def _filled_entry_from_mapping(cls, data: Mapping[str, Any]) -> FilledEntry:
        requested_entry = cls._requested_entry_from_mapping(data)
        return FilledEntry(
            entry_id=cls._entry_id_from_mapping(data["filled_entry_id"]),
            requested=requested_entry,
            filled_units=Decimal(str(data["filled_units"])),
            filled_entry_price=cls._money_from_mapping(data["filled_entry_price"]),
            filled_at=cls._aware_datetime(data["filled_at"]),
            planned_take_profit_price=cls._money_from_mapping(
                data["current_planned_take_profit_price"],
            ),
            planned_stop_loss_price=cls._optional_money_from_mapping(
                data["current_planned_stop_loss_price"],
            ),
        )

    @classmethod
    def _requested_close_entry_to_mapping(
        cls,
        entry: RequestedCloseEntry,
    ) -> dict[str, Any]:
        return {
            "entry_id": cls._entry_id_to_mapping(entry.entry_id),
            "original_entry": cls._filled_entry_to_mapping(entry.original_entry),
            "requested_exit_price": cls._money_to_mapping(entry.requested_exit_price),
            "requested_at": entry.requested_at.isoformat(),
            "close_reason": entry.close_reason.value,
            "refillable": entry.refillable,
        }

    @classmethod
    def _requested_close_entry_from_mapping(
        cls,
        data: Mapping[str, Any],
    ) -> RequestedCloseEntry:
        return RequestedCloseEntry(
            entry_id=cls._entry_id_from_mapping(data["entry_id"]),
            original_entry=cls._filled_entry_from_mapping(data["original_entry"]),
            requested_exit_price=cls._money_from_mapping(data["requested_exit_price"]),
            requested_at=cls._aware_datetime(data["requested_at"]),
            close_reason=CloseReason(data["close_reason"]),
            refillable=bool(data["refillable"]),
        )

    @classmethod
    def _requested_stop_loss_entry_to_mapping(
        cls,
        entry: RequestedStopLossEntry,
    ) -> dict[str, Any]:
        return {
            "entry_id": cls._entry_id_to_mapping(entry.entry_id),
            "original_entry": cls._filled_entry_to_mapping(entry.original_entry),
            "requested_stop_loss_exit_price": cls._money_to_mapping(
                entry.requested_stop_loss_exit_price,
            ),
            "requested_at": entry.requested_at.isoformat(),
        }

    @classmethod
    def _requested_stop_loss_entry_from_mapping(
        cls,
        data: Mapping[str, Any],
    ) -> RequestedStopLossEntry:
        return RequestedStopLossEntry(
            entry_id=cls._entry_id_from_mapping(data["entry_id"]),
            original_entry=cls._filled_entry_from_mapping(data["original_entry"]),
            requested_stop_loss_exit_price=cls._money_from_mapping(
                data["requested_stop_loss_exit_price"],
            ),
            requested_at=cls._aware_datetime(data["requested_at"]),
        )

    @classmethod
    def _filled_stop_loss_entry_to_mapping(
        cls,
        entry: FilledStopLossEntry,
    ) -> dict[str, Any]:
        return {
            **cls._requested_stop_loss_entry_to_mapping(entry.requested),
            "filled_stop_loss_entry_id": cls._entry_id_to_mapping(entry.entry_id),
            "filled_at": entry.filled_at.isoformat(),
            "filled_stop_loss_exit_price": cls._money_to_mapping(
                entry.filled_stop_loss_exit_price,
            ),
            "planned_rebuild_trigger_price": cls._money_to_mapping(
                entry.planned_rebuild_trigger_price,
            ),
        }

    @classmethod
    def _filled_stop_loss_entry_from_mapping(
        cls,
        data: Mapping[str, Any],
    ) -> FilledStopLossEntry:
        requested = cls._requested_stop_loss_entry_from_mapping(data)
        return FilledStopLossEntry(
            entry_id=cls._entry_id_from_mapping(data["filled_stop_loss_entry_id"]),
            requested=requested,
            filled_at=cls._aware_datetime(data["filled_at"]),
            filled_stop_loss_exit_price=cls._money_from_mapping(
                data["filled_stop_loss_exit_price"],
            ),
            planned_rebuild_trigger_price=cls._money_from_mapping(
                data["planned_rebuild_trigger_price"],
            ),
        )

    @classmethod
    def _aware_datetime(cls, value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError("Snowball timestamps must be timezone-aware")
        return parsed

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
