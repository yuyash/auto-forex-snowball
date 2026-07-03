"""Snowball tick-processing engine."""

from __future__ import annotations

from dataclasses import dataclass, field

from core import Tick

from snowball.config import SnowballConfig
from snowball.events import SnowballEvent
from snowball.models.state import Cycle, SnowballState
from snowball.services.accounting import SnowballAccounting
from snowball.services.calculators import SnowballCalculator
from snowball.services.close_service import SnowballCloseService
from snowball.services.counter_service import SnowballCounterService
from snowball.services.cycle_service import SnowballCycleService
from snowball.services.entry_service import SnowballEntryService
from snowball.services.grid_policy import SnowballGridPolicy
from snowball.services.pricing import SnowballPricing
from snowball.services.rebuild_service import SnowballRebuildService


@dataclass(frozen=True, slots=True)
class SnowballStepResult:
    """Result of processing one market tick."""

    events: tuple[SnowballEvent, ...]
    state: SnowballState


@dataclass(slots=True)
class SnowballEngine:
    """Coordinate Snowball state transitions for market ticks."""

    config: SnowballConfig
    calculator: SnowballCalculator = field(init=False)
    pricing: SnowballPricing = field(default_factory=SnowballPricing)
    grid_policy: SnowballGridPolicy = field(default_factory=SnowballGridPolicy)
    accounting: SnowballAccounting = field(default_factory=SnowballAccounting)
    entry_service: SnowballEntryService = field(init=False)
    cycle_service: SnowballCycleService = field(init=False)
    close_service: SnowballCloseService = field(init=False)
    rebuild_service: SnowballRebuildService = field(init=False)
    counter_service: SnowballCounterService = field(init=False)

    def __post_init__(self) -> None:
        self.calculator = SnowballCalculator(self.config)
        self.entry_service = SnowballEntryService(
            self.config,
            self.calculator,
            self.pricing,
        )
        self.cycle_service = SnowballCycleService(
            self.config,
            self.entry_service,
        )
        self.close_service = SnowballCloseService(
            self.config,
            self.pricing,
            self.accounting,
        )
        self.rebuild_service = SnowballRebuildService(
            self.config,
            self.pricing,
            self.grid_policy,
            self.entry_service,
        )
        self.counter_service = SnowballCounterService(
            self.config,
            self.calculator,
            self.pricing,
            self.entry_service,
        )

    def process_tick(
        self,
        *,
        tick: Tick,
        state: SnowballState,
    ) -> SnowballStepResult:
        """Process a tick and return emitted Snowball events."""
        events: list[SnowballEvent] = []
        state.prune_completed_cycles()
        account = self.accounting.evaluate(state=state, tick=tick, config=self.config)

        # Emergency actions take precedence over all other events, so we check for them first.
        emergency = self.close_service.handle_emergency(margin_ratio=account.margin_ratio)
        if emergency is not None:
            return SnowballStepResult(events=(emergency,), state=state)

        # Shrink events are processed before any other events.
        shrink_events = self.close_service.handle_shrink(
            state=state,
            tick=tick,
            account=account,
        )
        if shrink_events:
            state.refresh_cycle_statuses()
            state.prune_completed_cycles()
            return SnowballStepResult(events=tuple(shrink_events), state=state)

        # If there are no cycles, we need to initialize them. Otherwise, we process each cycle.
        if not state.cycles:
            events.extend(self.cycle_service.open_initial_cycles(state=state, tick=tick))

        # Process each cycle in the state, skipping completed cycles.
        for cycle in list(state.cycles):
            if cycle.completed:
                continue
            events.extend(self._process_cycle(cycle=cycle, tick=tick))

        # After processing all cycles, we check if we need to reseed any directions.
        events.extend(self.cycle_service.reseed_cycles(state=state, tick=tick))

        # Finally, we refresh the cycle statuses and prune any completed cycles.
        state.refresh_cycle_statuses()
        state.prune_completed_cycles()
        return SnowballStepResult(events=tuple(events), state=state)

    def _process_cycle(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        events: list[SnowballEvent] = []
        events.extend(self.rebuild_service.process_rebuilds(cycle=cycle, tick=tick))
        events.extend(self.close_service.process_counter_take_profits(cycle=cycle, tick=tick))
        events.extend(self.close_service.process_cycle_take_profit(cycle=cycle, tick=tick))
        events.extend(self.close_service.process_stop_losses(cycle=cycle, tick=tick))
        events.extend(self.rebuild_service.process_rebuilds(cycle=cycle, tick=tick))
        if self.grid_policy.validate_ordering(cycle) is None:
            events.extend(self.counter_service.process_counter_adds(cycle=cycle, tick=tick))
        cycle.refresh_status()
        return events
