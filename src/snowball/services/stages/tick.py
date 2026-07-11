"""Tick-processing stages for the Snowball engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core import Tick

from snowball.events import SnowballEvent
from snowball.models.state import Cycle, SnowballState
from snowball.services.accounting import AccountSnapshot
from snowball.services.flows.counter import SnowballCounterService
from snowball.services.flows.cycle import SnowballCycleService
from snowball.services.flows.protection import SnowballProtectionService
from snowball.services.flows.rebuild import SnowballRebuildService
from snowball.services.flows.stop_loss_close import SnowballStopLossCloseService
from snowball.services.flows.take_profit_close import SnowballTakeProfitCloseService
from snowball.services.policies.grid import SnowballGridPolicy


@dataclass(slots=True)
class SnowballTickContext:
    """Mutable context shared by one tick-processing pipeline."""

    tick: Tick
    state: SnowballState
    account: AccountSnapshot | None
    events: list[SnowballEvent] = field(default_factory=list)
    halted: bool = False


class SnowballTickStage(Protocol):
    """One global tick-processing stage."""

    def process(self, context: SnowballTickContext) -> None:
        """Apply this stage to one tick context."""


class SnowballCycleStage(Protocol):
    """One per-cycle tick-processing stage."""

    def process(self, *, cycle: Cycle, tick: Tick) -> list[SnowballEvent]:
        """Apply this stage to one cycle."""


@dataclass(frozen=True, slots=True)
class EmergencyStage:
    """Stop processing when emergency protection is triggered."""

    protection_service: SnowballProtectionService

    def process(self, context: SnowballTickContext) -> None:
        if context.account is None:
            return
        emergency = self.protection_service.handle_emergency(
            margin_ratio=context.account.margin_ratio,
        )
        if emergency is None:
            return
        context.events.append(emergency)
        context.halted = True


@dataclass(frozen=True, slots=True)
class ShrinkStage:
    """Process shrink protection before ordinary cycle work."""

    protection_service: SnowballProtectionService

    def process(self, context: SnowballTickContext) -> None:
        if context.account is None:
            return
        shrink_events = self.protection_service.handle_shrink(
            state=context.state,
            tick=context.tick,
            account=context.account,
        )
        if not shrink_events:
            return
        context.events.extend(shrink_events)
        context.state.refresh_cycle_statuses()
        context.state.prune_completed_cycles()
        context.halted = True


@dataclass(frozen=True, slots=True)
class InitialCycleStage:
    """Open initial cycles when the state is empty."""

    cycle_service: SnowballCycleService

    def process(self, context: SnowballTickContext) -> None:
        if context.state.has_cycles():
            return
        context.events.extend(
            self.cycle_service.open_initial_cycles(
                state=context.state,
                tick=context.tick,
            )
        )


@dataclass(frozen=True, slots=True)
class ProcessCyclesStage:
    """Run per-cycle stages for each non-completed cycle."""

    cycle_processor: SnowballCycleProcessor

    def process(self, context: SnowballTickContext) -> None:
        for cycle in context.state.iter_cycles():
            if cycle.completed:
                continue
            context.events.extend(
                self.cycle_processor.process_cycle(
                    cycle=cycle,
                    tick=context.tick,
                )
            )


@dataclass(frozen=True, slots=True)
class ReseedCycleStage:
    """Open replacement cycles for missing managed directions."""

    cycle_service: SnowballCycleService

    def process(self, context: SnowballTickContext) -> None:
        context.events.extend(
            self.cycle_service.reseed_cycles(
                state=context.state,
                tick=context.tick,
            )
        )


@dataclass(frozen=True, slots=True)
class FinalizeTickStage:
    """Normalize state at the end of ordinary tick processing."""

    def process(self, context: SnowballTickContext) -> None:
        context.state.prune_completed_cycles()


@dataclass(frozen=True, slots=True)
class RebuildCycleStage:
    """Run rebuild processing for a cycle."""

    rebuild_service: SnowballRebuildService

    def process(self, *, cycle: Cycle, tick: Tick) -> list[SnowballEvent]:
        return self.rebuild_service.process_rebuilds(cycle=cycle, tick=tick)


@dataclass(frozen=True, slots=True)
class CounterTakeProfitCycleStage:
    """Run counter and layer-initial take-profit closes for a cycle."""

    take_profit_close_service: SnowballTakeProfitCloseService

    def process(self, *, cycle: Cycle, tick: Tick) -> list[SnowballEvent]:
        return self.take_profit_close_service.process_counter_take_profits(
            cycle=cycle,
            tick=tick,
        )


@dataclass(frozen=True, slots=True)
class CycleTakeProfitStage:
    """Run cycle-head take-profit closes for a cycle."""

    take_profit_close_service: SnowballTakeProfitCloseService

    def process(self, *, cycle: Cycle, tick: Tick) -> list[SnowballEvent]:
        return self.take_profit_close_service.process_cycle_take_profit(
            cycle=cycle,
            tick=tick,
        )


@dataclass(frozen=True, slots=True)
class StopLossCycleStage:
    """Run stop-loss closes for a cycle."""

    stop_loss_close_service: SnowballStopLossCloseService

    def process(self, *, cycle: Cycle, tick: Tick) -> list[SnowballEvent]:
        return self.stop_loss_close_service.process_stop_losses(cycle=cycle, tick=tick)


@dataclass(frozen=True, slots=True)
class CounterAddCycleStage:
    """Open counter or next-layer entries when grid ordering allows it."""

    grid_policy: SnowballGridPolicy
    counter_service: SnowballCounterService

    def process(self, *, cycle: Cycle, tick: Tick) -> list[SnowballEvent]:
        if self.grid_policy.validate_ordering(cycle) is not None:
            return []
        return self.counter_service.process_counter_adds(cycle=cycle, tick=tick)


@dataclass(frozen=True, slots=True)
class SnowballCycleProcessor:
    """Run all per-cycle stages in a fixed order."""

    stages: tuple[SnowballCycleStage, ...]

    def process_cycle(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        """Process a cycle and return emitted events."""
        events: list[SnowballEvent] = []
        for stage in self.stages:
            events.extend(stage.process(cycle=cycle, tick=tick))
        return events
