from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from core import Currency, CurrencyPair, Money, StrategyContext, TaskType, Tick, new_uuid

from snowball import SnowballStrategy, __version__
from snowball.config import (
    CycleConfig,
    GridConfig,
    PipProgressionConfig,
    SnowballConfig,
    StopLossConfig,
)
from snowball.engine import SnowballEngine
from snowball.enums import CloseReason, SnowballIntentType
from snowball.models.state import SnowballState

USD_JPY = CurrencyPair.of("USD_JPY")


class TickFactory:
    @staticmethod
    def tick_at(
        seconds: int,
        *,
        bid: str,
        ask: str,
    ) -> Tick:
        return Tick(
            instrument=USD_JPY,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds),
            bid=Money.of(bid, "JPY"),
            ask=Money.of(ask, "JPY"),
        )


class TestSnowballPackage:
    def test_package_version(self) -> None:
        assert __version__ == "0.1.0"


class TestSnowballEngine:
    def test_first_tick_opens_long_and_short_cycles_when_hedging_enabled(self) -> None:
        config = SnowballConfig()
        state = SnowballState.new()
        result = SnowballEngine(config).process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        assert [intent.type for intent in result.intents] == [
            SnowballIntentType.OPEN,
            SnowballIntentType.OPEN,
        ]
        assert [cycle.direction.value for cycle in result.state.cycles] == ["long", "short"]
        assert result.state.cycles[0].grid.layers[0].r0.entry is not None

    def test_counter_entry_is_added_when_price_moves_adversely_by_interval(self) -> None:
        config = SnowballConfig(cycle=CycleConfig(hedging_enabled=False))
        state = SnowballState.new()
        engine = SnowballEngine(config)
        engine.process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        result = engine.process_tick(
            tick=TickFactory.tick_at(1, bid="149.70", ask="149.72"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        layer = result.state.cycles[0].grid.layers[0]
        slot = layer.slot(1)
        counter = slot.entry
        assert counter is not None
        assert result.state.cycles[0].grid.layer_number(layer) == 1
        assert layer.retracement_count(slot) == 1
        assert counter.units == Decimal("2000")
        assert result.intents[0].metadata["actual_interval_pips"] == "30"

    def test_cycle_take_profit_closes_head_and_reseeds(self) -> None:
        config = SnowballConfig(cycle=CycleConfig(hedging_enabled=False))
        state = SnowballState.new()
        engine = SnowballEngine(config)
        engine.process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        result = engine.process_tick(
            tick=TickFactory.tick_at(1, bid="150.52", ask="150.54"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        assert result.intents[0].close_reason == CloseReason.TAKE_PROFIT
        assert result.intents[1].type == SnowballIntentType.OPEN
        assert len(result.state.cycles) == 1
        assert result.state.cycles[0].active

    def test_stop_loss_creates_pending_rebuild_and_rebuilds_on_revisit(self) -> None:
        config = SnowballConfig(
            cycle=CycleConfig(hedging_enabled=False),
            stop_loss=StopLossConfig(
                enabled=True,
                distance=PipProgressionConfig(
                    head_pips=Decimal("10"),
                    tail_pips=Decimal("10"),
                    flat_steps=0,
                ),
            ),
        )
        state = SnowballState.new()
        engine = SnowballEngine(config)
        engine.process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        stopped = engine.process_tick(
            tick=TickFactory.tick_at(1, bid="149.90", ask="149.92"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )
        pending_slot = stopped.state.cycles[0].grid.layers[0].r0

        assert stopped.intents[0].close_reason == CloseReason.STOP_LOSS
        assert pending_slot.entry is None
        assert pending_slot.pending_rebuild is not None
        assert stopped.state.cycles[0].pending

        rebuilt = engine.process_tick(
            tick=TickFactory.tick_at(2, bid="150.01", ask="150.03"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        rebuilt_slot = rebuilt.state.cycles[0].grid.layers[0].r0
        rebuilt_entry = rebuilt_slot.entry
        assert rebuilt.intents[0].type == SnowballIntentType.OPEN
        assert rebuilt_entry is not None
        assert rebuilt_slot.build_count == 2
        assert rebuilt.state.cycles[0].active


class TestSnowballStrategy:
    def test_account_parameters_use_core_money_and_currency(self) -> None:
        config = SnowballConfig.from_parameters(
            {"account": {"currency": "JPY", "balance": "1200000"}}
        )

        assert config.account.currency == Currency.of("JPY")
        assert config.account.balance == Money.of("1200000", "JPY")
        assert SnowballConfig.from_parameters(
            {"account": {"currency": "JPY"}}
        ).account.balance == Money.of("10000", "JPY")

    def test_strategy_normalizes_nested_parameters(self) -> None:
        strategy = SnowballStrategy(
            parameters={
                "cycle": {"hedging_enabled": False},
                "grid": {"max_retracements_per_layer": 3},
            }
        )

        assert strategy.config.cycle.hedging_enabled is False
        assert strategy.config.grid == GridConfig(max_retracements_per_layer=3)

    def test_strategy_maps_engine_intents_to_core_strategy_events(self) -> None:
        strategy = SnowballStrategy(parameters={"cycle": {"hedging_enabled": False}})
        context = StrategyContext(
            task_id=new_uuid(),
            task_type=TaskType.BACKTEST,
            instrument=USD_JPY,
        )

        result = strategy.on_tick(
            TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            context,
        )

        assert result.events[0].action.value == "open_position"
        assert result.events[0].metadata["strategy_type"] == "snowball"
        assert result.events[0].metadata["layer_number"] == 1
        cycle_id = UUID(result.state["snowball"]["cycles"][0]["cycle_id"])
        assert cycle_id.version == 7


class TestSnowballStateSerialization:
    def test_state_round_trips_through_core_strategy_state(self) -> None:
        config = SnowballConfig(cycle=CycleConfig(hedging_enabled=False))
        state = SnowballState.new()
        result = SnowballEngine(config).process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
            pip_size=USD_JPY.pip_size,
        )

        restored = SnowballState.from_strategy_state(result.state.to_strategy_state())

        restored_entry = restored.cycles[0].grid.layers[0].r0.entry
        assert restored.cycles[0].cycle_id == result.state.cycles[0].cycle_id
        assert restored_entry is not None
        assert restored_entry.entry_price == Money.of("150.02", "JPY")
