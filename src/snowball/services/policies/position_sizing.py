"""Position sizing policy for Snowball entries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from snowball.config import SnowballConfig
from snowball.enums import EntryRole
from snowball.models.grid import Layer


@dataclass(frozen=True, slots=True)
class SnowballPositionSizer:
    """Return requested units for Snowball entry roles."""

    config: SnowballConfig

    def entry_units(
        self,
        *,
        role: EntryRole,
        layer: Layer,
        retracement_count: int,
    ) -> Decimal:
        """Return requested units for one entry."""
        if role == EntryRole.COUNTER:
            return Decimal(retracement_count + 1) * layer.base_units
        return self.config.sizing.initial_entry_units_multiplier * layer.base_units
