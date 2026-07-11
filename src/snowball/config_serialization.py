"""Snowball strategy configuration serialization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from core import Currency, Money


class SnowballConfigSerializer:
    """Serialize Snowball configuration objects into strategy parameters."""

    @classmethod
    def serialize(cls, value: Any) -> Any:
        """Return nested normalized config values."""
        if isinstance(value, Decimal):
            return value
        if isinstance(value, Currency):
            return value.code
        if isinstance(value, Money):
            return {"amount": value.amount, "currency": value.currency.code}
        if isinstance(value, StrEnum):
            return value.value
        if is_dataclass(value):
            return cls.serialize(asdict(value))
        if isinstance(value, Mapping):
            return {key: cls.serialize(item) for key, item in value.items()}
        if isinstance(value, tuple | list):
            return [cls.serialize(item) for item in value]
        return value
