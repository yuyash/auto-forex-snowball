"""Snowball strategy configuration parsing."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any


class SnowballConfigParser:
    """Parse and validate raw Snowball strategy configuration values."""

    @staticmethod
    def nested(values: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        """Return a nested config mapping."""
        value = values.get(key)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"{key} must be an object")
        return value

    @staticmethod
    def changes(values: Mapping[str, Any], **parsers: Any) -> dict[str, Any]:
        """Return parsed changes for keys present in values."""
        return {key: parser(values[key]) for key, parser in parsers.items() if key in values}

    @staticmethod
    def decimal_value(value: Any) -> Decimal:
        """Parse a decimal config value."""
        if isinstance(value, bool | int | float):
            raise TypeError("decimal config values must be provided as Decimal or str")
        return Decimal(str(value))

    @staticmethod
    def int_value(value: Any) -> int:
        """Parse an integer config value."""
        return int(value)

    @staticmethod
    def bool_value(value: Any) -> bool:
        """Parse a boolean config value."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def enum_value[EnumT: StrEnum](enum_type: type[EnumT], value: Any) -> EnumT:
        """Parse a StrEnum config value."""
        if isinstance(value, enum_type):
            return value
        return enum_type(str(value))

    @staticmethod
    def require_positive(value: Decimal, field_name: str) -> None:
        """Require a positive decimal value."""
        if value <= 0:
            raise ValueError(f"{field_name} must be greater than 0")

    @classmethod
    def require_manual_length(
        cls,
        values: tuple[Decimal, ...],
        minimum: int,
        field_name: str,
    ) -> None:
        """Require a minimum manual sequence length and positive values."""
        if len(values) < minimum:
            raise ValueError(f"{field_name} must contain at least {minimum} values")
        for value in values:
            cls.require_positive(value, field_name)
