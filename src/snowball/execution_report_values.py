"""Value parsing helpers for Snowball execution reports."""

from __future__ import annotations

from core import Currency, Money


class SnowballExecutionReportValueCodec:
    """Parse metadata values carried by execution reports."""

    @classmethod
    def metadata_bool(cls, value: object) -> bool:
        """Parse a boolean metadata value."""
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes"}

    @classmethod
    def money(cls, value: object, *, fallback_currency: Currency) -> Money:
        """Parse a Money value from metadata."""
        if isinstance(value, Money):
            return value
        text = str(value)
        parts = text.split()
        if len(parts) == 2:
            return Money.of(parts[0], parts[1])
        return Money.of(text, fallback_currency)
