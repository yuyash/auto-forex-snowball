"""Position sizing policy for Snowball entries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core import Units

from snowball.config import SnowballConfig
from snowball.enums import EntryRole
from snowball.models.grid import Layer


@dataclass(frozen=True, slots=True)
class SnowballPositionSizer:
    """Return planned units for Snowball entry roles."""

    config: SnowballConfig

    def entry_units(
        self,
        *,
        role: EntryRole,
        layer: Layer,
        retracement_count: int,
    ) -> Units:
        """Return planned units for one entry."""
        if role == EntryRole.COUNTER:
            return Units.of(Decimal(retracement_count + 1) * layer.base_units)
        return Units.of(self.config.sizing.initial_entry_units_multiplier * layer.base_units)
