"""Composition root for Snowball domain services."""

from __future__ import annotations

from dataclasses import dataclass, field

from snowball.config import SnowballConfig
from snowball.services.accounting import SnowballAccounting
from snowball.services.calculators import SnowballCalculator
from snowball.services.flows.counter import SnowballCounterService
from snowball.services.flows.cycle import SnowballCycleService
from snowball.services.flows.entry import SnowballEntryService
from snowball.services.flows.event_factory import SnowballEventFactory
from snowball.services.flows.protection import SnowballProtectionService
from snowball.services.flows.rebuild import SnowballRebuildService
from snowball.services.flows.stop_loss_close import SnowballStopLossCloseService
from snowball.services.flows.take_profit_close import SnowballTakeProfitCloseService
from snowball.services.market_pricing import SnowballMarketPricing
from snowball.services.policies.grid import SnowballGridPolicy
from snowball.services.policies.position_sizing import SnowballPositionSizer
from snowball.services.policies.stop_loss import SnowballStopLossPlanner
from snowball.services.policies.take_profit import SnowballTakeProfitPlanner
from snowball.services.selectors.grid import SnowballGridSelector
from snowball.services.stages.tick import (
    CounterAddCycleStage,
    CounterTakeProfitCycleStage,
    CycleTakeProfitStage,
    EmergencyStage,
    FinalizeTickStage,
    InitialCycleStage,
    ProcessCyclesStage,
    RebuildCycleStage,
    ReseedCycleStage,
    ShrinkStage,
    SnowballCycleProcessor,
    SnowballTickStage,
    StopLossCycleStage,
)


@dataclass(slots=True)
class SnowballServiceContainer:
    """Build and own the service graph for one Snowball engine."""

    config: SnowballConfig
    calculator: SnowballCalculator = field(init=False)
    pricing: SnowballMarketPricing = field(default_factory=SnowballMarketPricing)
    grid_policy: SnowballGridPolicy = field(default_factory=SnowballGridPolicy)
    grid_selector: SnowballGridSelector = field(default_factory=SnowballGridSelector)
    accounting: SnowballAccounting = field(default_factory=SnowballAccounting)
    event_factory: SnowballEventFactory = field(default_factory=SnowballEventFactory)
    position_sizer: SnowballPositionSizer = field(init=False)
    take_profit_planner: SnowballTakeProfitPlanner = field(init=False)
    stop_loss_planner: SnowballStopLossPlanner = field(init=False)
    entry_service: SnowballEntryService = field(init=False)
    cycle_service: SnowballCycleService = field(init=False)
    take_profit_close_service: SnowballTakeProfitCloseService = field(init=False)
    stop_loss_close_service: SnowballStopLossCloseService = field(init=False)
    protection_service: SnowballProtectionService = field(init=False)
    rebuild_service: SnowballRebuildService = field(init=False)
    counter_service: SnowballCounterService = field(init=False)
    cycle_processor: SnowballCycleProcessor = field(init=False)
    tick_stages: tuple[SnowballTickStage, ...] = field(init=False)

    def __post_init__(self) -> None:
        self.calculator = SnowballCalculator(self.config)
        self.position_sizer = SnowballPositionSizer(self.config)
        self.take_profit_planner = SnowballTakeProfitPlanner(
            self.config,
            self.calculator,
            self.pricing,
        )
        self.stop_loss_planner = SnowballStopLossPlanner(
            self.config,
            self.calculator,
            self.pricing,
        )
        self.entry_service = SnowballEntryService(
            self.pricing,
            self.position_sizer,
            self.take_profit_planner,
            self.stop_loss_planner,
        )
        self.cycle_service = SnowballCycleService(
            self.config,
            self.entry_service,
            self.event_factory,
        )
        self.take_profit_close_service = SnowballTakeProfitCloseService(
            self.config,
            self.pricing,
            self.event_factory,
        )
        self.stop_loss_close_service = SnowballStopLossCloseService(
            self.config,
            self.pricing,
            self.stop_loss_planner,
            self.event_factory,
        )
        self.protection_service = SnowballProtectionService(
            self.config,
            self.pricing,
            self.accounting,
            self.grid_selector,
            self.event_factory,
        )
        self.rebuild_service = SnowballRebuildService(
            self.config,
            self.pricing,
            self.grid_policy,
            self.entry_service,
            self.take_profit_planner,
            self.event_factory,
        )
        self.counter_service = SnowballCounterService(
            self.config,
            self.calculator,
            self.pricing,
            self.entry_service,
            self.grid_selector,
            self.event_factory,
        )
        self.cycle_processor = SnowballCycleProcessor(
            stages=(
                RebuildCycleStage(self.rebuild_service),
                CounterTakeProfitCycleStage(self.take_profit_close_service),
                CycleTakeProfitStage(self.take_profit_close_service),
                StopLossCycleStage(self.stop_loss_close_service),
                RebuildCycleStage(self.rebuild_service),
                CounterAddCycleStage(self.grid_policy, self.counter_service),
            )
        )
        self.tick_stages = (
            EmergencyStage(self.protection_service),
            ShrinkStage(self.protection_service),
            InitialCycleStage(self.cycle_service),
            ProcessCyclesStage(self.cycle_processor),
            ReseedCycleStage(self.cycle_service),
            FinalizeTickStage(),
        )
