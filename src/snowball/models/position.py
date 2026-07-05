"""Grid position value objects."""

from __future__ import annotations

from dataclasses import dataclass

from snowball.enums import EntryRole


@dataclass(frozen=True, slots=True)
class GridPosition:
    """Stable L/R grid position."""

    layer_number: int
    slot_number: int

    @property
    def retracement_count(self) -> int:
        """Return the Snowball retracement count represented by this position."""
        return self.slot_number

    @property
    def role(self) -> EntryRole:
        """Return the entry role derived from the grid position."""
        if self.slot_number > 0:
            return EntryRole.COUNTER
        if self.layer_number == 1:
            return EntryRole.INITIAL
        return EntryRole.LAYER_INITIAL
