"""Codecs for Snowball strategy state serialization."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from core import Money, PositionSide, Units

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


class SnowballValueCodec:
    """Serialize and deserialize Snowball primitive value objects."""

    @classmethod
    def entry_id_to_mapping(cls, entry_id: EntryId) -> dict[str, Any]:
        """Serialize an entry id."""
        return {
            "cycle_id": entry_id.cycle_id,
            "layer_number": entry_id.layer_number,
            "slot_number": entry_id.slot_number,
            "build_number": entry_id.build_number,
            "entry_type": entry_id.entry_type.value,
        }

    @classmethod
    def entry_id_from_mapping(cls, data: Mapping[str, Any]) -> EntryId:
        """Deserialize an entry id."""
        return EntryId(
            cycle_id=int(data["cycle_id"]),
            layer_number=int(data["layer_number"]),
            slot_number=int(data["slot_number"]),
            build_number=int(data["build_number"]),
            entry_type=EntryIdType(data["entry_type"]),
        )

    @classmethod
    def aware_datetime(cls, value: Any) -> datetime:
        """Parse a timezone-aware datetime."""
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            raise ValueError("Snowball timestamps must be timezone-aware")
        return parsed

    @classmethod
    def money_to_mapping(cls, value: Money) -> dict[str, Any]:
        """Serialize Money."""
        return {"amount": str(value.amount), "currency": value.currency.code}

    @classmethod
    def money_from_mapping(cls, value: Any) -> Money:
        """Deserialize Money."""
        if isinstance(value, Money):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("money value must be a mapping")
        return Money.of(value["amount"], value["currency"])

    @classmethod
    def optional_money_to_mapping(cls, value: Money | None) -> dict[str, Any] | None:
        """Serialize optional Money."""
        if value is None:
            return None
        return cls.money_to_mapping(value)

    @classmethod
    def optional_money_from_mapping(cls, value: Any) -> Money | None:
        """Deserialize optional Money."""
        if value is None:
            return None
        return cls.money_from_mapping(value)


class EntryStateCodec:
    """Serialize and deserialize Snowball entry lifecycle objects."""

    value = SnowballValueCodec

    @classmethod
    def requested_entry_to_mapping(cls, entry: RequestedEntry) -> dict[str, Any]:
        """Serialize a requested entry."""
        return {
            "entry_id": cls.value.entry_id_to_mapping(entry.entry_id),
            "planned_units": str(entry.planned_units),
            "planned_entry_price": cls.value.money_to_mapping(entry.planned_entry_price),
            "planned_at": entry.planned_at.isoformat(),
            "planned_take_profit_price": cls.value.money_to_mapping(
                entry.planned_take_profit_price
            ),
            "planned_stop_loss_price": cls.value.optional_money_to_mapping(
                entry.planned_stop_loss_price,
            ),
        }

    @classmethod
    def requested_entry_from_mapping(cls, data: Mapping[str, Any]) -> RequestedEntry:
        """Deserialize a requested entry."""
        return RequestedEntry(
            entry_id=cls.value.entry_id_from_mapping(data["entry_id"]),
            planned_units=Units.of(data["planned_units"]),
            planned_entry_price=cls.value.money_from_mapping(data["planned_entry_price"]),
            planned_at=cls.value.aware_datetime(data["planned_at"]),
            planned_take_profit_price=cls.value.money_from_mapping(
                data["planned_take_profit_price"]
            ),
            planned_stop_loss_price=cls.value.optional_money_from_mapping(
                data["planned_stop_loss_price"],
            ),
        )

    @classmethod
    def filled_entry_to_mapping(cls, entry: FilledEntry) -> dict[str, Any]:
        """Serialize a filled entry."""
        return {
            **cls.requested_entry_to_mapping(entry.requested),
            "filled_entry_id": cls.value.entry_id_to_mapping(entry.entry_id),
            "filled_units": str(entry.filled_units),
            "filled_entry_price": cls.value.money_to_mapping(entry.filled_entry_price),
            "filled_at": entry.filled_at.isoformat(),
            "current_planned_take_profit_price": cls.value.money_to_mapping(
                entry.planned_take_profit_price,
            ),
            "current_planned_stop_loss_price": cls.value.optional_money_to_mapping(
                entry.planned_stop_loss_price,
            ),
        }

    @classmethod
    def filled_entry_from_mapping(cls, data: Mapping[str, Any]) -> FilledEntry:
        """Deserialize a filled entry."""
        requested_entry = cls.requested_entry_from_mapping(data)
        return FilledEntry(
            entry_id=cls.value.entry_id_from_mapping(data["filled_entry_id"]),
            requested=requested_entry,
            filled_units=Units.of(data["filled_units"]),
            filled_entry_price=cls.value.money_from_mapping(data["filled_entry_price"]),
            filled_at=cls.value.aware_datetime(data["filled_at"]),
            planned_take_profit_price=cls.value.money_from_mapping(
                data["current_planned_take_profit_price"],
            ),
            planned_stop_loss_price=cls.value.optional_money_from_mapping(
                data["current_planned_stop_loss_price"],
            ),
        )

    @classmethod
    def requested_close_entry_to_mapping(cls, entry: RequestedCloseEntry) -> dict[str, Any]:
        """Serialize a requested close entry."""
        return {
            "entry_id": cls.value.entry_id_to_mapping(entry.entry_id),
            "original_entry": cls.filled_entry_to_mapping(entry.original_entry),
            "planned_exit_price": cls.value.money_to_mapping(entry.planned_exit_price),
            "planned_at": entry.planned_at.isoformat(),
            "close_reason": entry.close_reason.value,
            "refillable": entry.refillable,
        }

    @classmethod
    def requested_close_entry_from_mapping(cls, data: Mapping[str, Any]) -> RequestedCloseEntry:
        """Deserialize a requested close entry."""
        return RequestedCloseEntry(
            entry_id=cls.value.entry_id_from_mapping(data["entry_id"]),
            original_entry=cls.filled_entry_from_mapping(data["original_entry"]),
            planned_exit_price=cls.value.money_from_mapping(data["planned_exit_price"]),
            planned_at=cls.value.aware_datetime(data["planned_at"]),
            close_reason=CloseReason(data["close_reason"]),
            refillable=bool(data["refillable"]),
        )

    @classmethod
    def requested_stop_loss_entry_to_mapping(
        cls,
        entry: RequestedStopLossEntry,
    ) -> dict[str, Any]:
        """Serialize a requested stop-loss entry."""
        return {
            "entry_id": cls.value.entry_id_to_mapping(entry.entry_id),
            "original_entry": cls.filled_entry_to_mapping(entry.original_entry),
            "planned_stop_loss_price": cls.value.money_to_mapping(
                entry.planned_stop_loss_price,
            ),
            "planned_at": entry.planned_at.isoformat(),
        }

    @classmethod
    def requested_stop_loss_entry_from_mapping(
        cls,
        data: Mapping[str, Any],
    ) -> RequestedStopLossEntry:
        """Deserialize a requested stop-loss entry."""
        return RequestedStopLossEntry(
            entry_id=cls.value.entry_id_from_mapping(data["entry_id"]),
            original_entry=cls.filled_entry_from_mapping(data["original_entry"]),
            planned_stop_loss_price=cls.value.money_from_mapping(
                data["planned_stop_loss_price"],
            ),
            planned_at=cls.value.aware_datetime(data["planned_at"]),
        )

    @classmethod
    def filled_stop_loss_entry_to_mapping(
        cls,
        entry: FilledStopLossEntry,
    ) -> dict[str, Any]:
        """Serialize a filled stop-loss entry."""
        return {
            **cls.requested_stop_loss_entry_to_mapping(entry.requested),
            "filled_stop_loss_entry_id": cls.value.entry_id_to_mapping(entry.entry_id),
            "filled_at": entry.filled_at.isoformat(),
            "filled_stop_loss_price": cls.value.money_to_mapping(
                entry.filled_stop_loss_price,
            ),
            "planned_rebuild_price": cls.value.money_to_mapping(
                entry.planned_rebuild_price,
            ),
        }

    @classmethod
    def filled_stop_loss_entry_from_mapping(
        cls,
        data: Mapping[str, Any],
    ) -> FilledStopLossEntry:
        """Deserialize a filled stop-loss entry."""
        requested = cls.requested_stop_loss_entry_from_mapping(data)
        return FilledStopLossEntry(
            entry_id=cls.value.entry_id_from_mapping(data["filled_stop_loss_entry_id"]),
            requested=requested,
            filled_at=cls.value.aware_datetime(data["filled_at"]),
            filled_stop_loss_price=cls.value.money_from_mapping(
                data["filled_stop_loss_price"],
            ),
            planned_rebuild_price=cls.value.money_from_mapping(
                data["planned_rebuild_price"],
            ),
        )


class SlotStateCodec:
    """Serialize and deserialize Snowball slots."""

    entries = EntryStateCodec
    values = SnowballValueCodec

    @classmethod
    def to_mapping(cls, slot: Slot) -> dict[str, Any]:
        """Serialize a slot."""
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
                else cls.entries.requested_entry_to_mapping(requested_entry)
            ),
            "filled_entry": (
                None if filled_entry is None else cls.entries.filled_entry_to_mapping(filled_entry)
            ),
            "requested_close_entry": (
                None
                if requested_close_entry is None
                else cls.entries.requested_close_entry_to_mapping(requested_close_entry)
            ),
            "requested_stop_loss_entry": (
                None
                if requested_stop_loss_entry is None
                else cls.entries.requested_stop_loss_entry_to_mapping(requested_stop_loss_entry)
            ),
            "filled_stop_loss_entry": (
                None
                if filled_stop_loss_entry is None
                else cls.entries.filled_stop_loss_entry_to_mapping(filled_stop_loss_entry)
            ),
            "sealed": slot.is_sealed,
            "sealed_entry_id": (
                None
                if sealed_entry is None
                else cls.values.entry_id_to_mapping(sealed_entry.entry_id)
            ),
            "sealed_at": None if sealed_entry is None else sealed_entry.sealed_at.isoformat(),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Slot:
        """Deserialize a slot."""
        requested_entry = (
            None
            if data["requested_entry"] is None
            else cls.entries.requested_entry_from_mapping(data["requested_entry"])
        )
        filled_entry = (
            None
            if data["filled_entry"] is None
            else cls.entries.filled_entry_from_mapping(data["filled_entry"])
        )
        requested_close_entry = (
            None
            if data.get("requested_close_entry") is None
            else cls.entries.requested_close_entry_from_mapping(data["requested_close_entry"])
        )
        requested_stop_loss_entry = (
            None
            if data["requested_stop_loss_entry"] is None
            else cls.entries.requested_stop_loss_entry_from_mapping(
                data["requested_stop_loss_entry"]
            )
        )
        filled_stop_loss_entry = (
            None
            if data["filled_stop_loss_entry"] is None
            else cls.entries.filled_stop_loss_entry_from_mapping(data["filled_stop_loss_entry"])
        )
        sealed = bool(data["sealed"])
        sealed_at = (
            None if data.get("sealed_at") is None else cls.values.aware_datetime(data["sealed_at"])
        )
        sealed_entry_id = (
            None
            if data["sealed_entry_id"] is None
            else cls.values.entry_id_from_mapping(data["sealed_entry_id"])
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
        return Slot.restore(
            requested_entry
            or filled_entry
            or requested_close_entry
            or requested_stop_loss_entry
            or filled_stop_loss_entry
            or (
                SealedEntry(entry_id=sealed_entry_id, sealed_at=sealed_at)
                if sealed and sealed_at is not None and sealed_entry_id is not None
                else None
            )
        )


class LayerStateCodec:
    """Serialize and deserialize Snowball layers."""

    @classmethod
    def to_mapping(cls, layer: Layer) -> dict[str, Any]:
        """Serialize a layer."""
        return {
            "base_units": str(layer.base_units),
            "build_numbers": {
                str(slot_number): build_number
                for slot_number, build_number in layer.build_numbers().items()
            },
            "slots": {
                str(slot_number): SlotStateCodec.to_mapping(slot)
                for slot_number, slot in layer.iter_slot_items()
            },
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Layer:
        """Deserialize a layer."""
        slots = data["slots"]
        if not isinstance(slots, Mapping):
            raise ValueError("layer slots must be a mapping")
        build_numbers = data["build_numbers"]
        if not isinstance(build_numbers, Mapping):
            raise ValueError("layer build numbers must be a mapping")
        return Layer.from_slots(
            base_units=Units.of(data["base_units"]),
            slots={
                int(slot_number): SlotStateCodec.from_mapping(item)
                for slot_number, item in slots.items()
            },
            build_numbers={
                int(slot_number): int(build_number)
                for slot_number, build_number in build_numbers.items()
            },
        )


class GridStateCodec:
    """Serialize and deserialize Snowball grids."""

    @classmethod
    def to_mapping(cls, grid: Grid) -> dict[str, Any]:
        """Serialize a grid."""
        return {
            "layers": {
                str(layer_number): LayerStateCodec.to_mapping(layer)
                for layer_number, layer in grid.iter_layer_items()
            }
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Grid:
        """Deserialize a grid."""
        layers = data["layers"]
        if not isinstance(layers, Mapping):
            raise ValueError("grid layers must be a mapping")
        return Grid.from_layers(
            {
                int(layer_number): LayerStateCodec.from_mapping(item)
                for layer_number, item in layers.items()
            }
        )


class CycleStateCodec:
    """Serialize and deserialize Snowball cycles."""

    @classmethod
    def to_mapping(cls, cycle: Cycle) -> dict[str, Any]:
        """Serialize a cycle."""
        return {
            "cycle_id": cycle.cycle_id,
            "direction": cycle.direction.value,
            "status": cycle.status.value,
            "grid": GridStateCodec.to_mapping(cycle.grid),
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Cycle:
        """Deserialize a cycle."""
        return Cycle.create(
            cycle_id=int(data["cycle_id"]),
            direction=PositionSide(data["direction"]),
            status=CycleStatus(data["status"]),
            grid=GridStateCodec.from_mapping(data["grid"]),
        )


class SnowballStateCodec:
    """Serialize and deserialize complete Snowball state."""

    @classmethod
    def to_mapping(cls, state: SnowballState) -> dict[str, Any]:
        """Serialize Snowball state to a plain mapping."""
        return {
            "cycles": [CycleStateCodec.to_mapping(cycle) for cycle in state.iter_cycles()],
            "next_cycle_id": state.next_cycle_id_value,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> SnowballState:
        """Deserialize Snowball state from a plain mapping."""
        state = SnowballState.from_cycles(
            CycleStateCodec.from_mapping(item) for item in data["cycles"]
        )
        if "next_cycle_id" in data:
            state.restore_next_cycle_id(int(data["next_cycle_id"]))
        return state
