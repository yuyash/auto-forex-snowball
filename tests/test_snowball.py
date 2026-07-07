from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from core import (
    Currency,
    CurrencyPair,
    Money,
    PositionSide,
    StrategyContext,
    StrategyParameters,
    TaskType,
    Tick,
    new_uuid,
)
from pydantic import AwareDatetime

from snowball import SnowballStrategy, __version__
from snowball.config import (
    CycleConfig,
    GridConfig,
    PipProgressionConfig,
    SnowballConfig,
    StopLossConfig,
)
from snowball.engine import SnowballEngine
from snowball.enums import CloseReason, SlotStatus
from snowball.events import SnowballCloseEvent, SnowballOpenEvent
from snowball.models.entries import (
    FilledStopLossEntry,
    RequestedEntry,
    RequestedStopLossEntry,
    SealedEntry,
)
from snowball.models.grid import Grid, Slot
from snowball.models.identifiers import EntryId, EntryIdType
from snowball.models.state import Cycle, SnowballState
from snowball.serialization import SnowballStateSerializer

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


def fill_requested_entries(state: SnowballState, *, filled_at: AwareDatetime) -> None:
    for cycle in state.cycles:
        for layer in cycle.grid.layers:
            for slot in layer.slots:
                requested = slot.requested_entry
                if requested is None:
                    continue
                slot.fill_entry(
                    requested.fill(
                        filled_entry_price=requested.requested_entry_price,
                        filled_at=filled_at,
                    )
                )
        cycle.refresh_status()


class TestSnowballPackage:
    def test_package_version(self) -> None:
        assert __version__ == "0.1.0"


class TestSnowballEngine:
    def test_entry_transition_methods_create_slot_states(self) -> None:
        requested = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=0, build_count=1),
            requested_units=Decimal("1000"),
            requested_entry_price=Money.of("150.00", "JPY"),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
            planned_stop_loss_price=Money.of("149.90", "JPY"),
        )
        filled = requested.fill(
            filled_entry_price=Money.of("150.01", "JPY"),
            filled_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        )

        assert filled.requested is requested
        with pytest.raises(FrozenInstanceError):
            requested.__setattr__("planned_take_profit_price", Money.of("150.60", "JPY"))
        assert requested.entry_id.entry_type == EntryIdType.REQUESTED_ENTRY
        assert requested.entry_id.value == "C1:L1:S0:REQ:B1"
        assert filled.entry_id.entry_type == EntryIdType.FILLED_ENTRY
        assert filled.entry_id.value == "C1:L1:S0:FIL:B1"
        assert (
            filled.close(
                closed_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
                refillable=True,
            )
            is None
        )
        sealed = filled.seal(sealed_at=datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC))
        assert isinstance(sealed, SealedEntry)
        assert sealed.entry_id.entry_type == EntryIdType.SEALED_ENTRY
        assert sealed.unseal() is None

        requested_stop_loss = filled.stop_loss(
            requested_stop_loss_exit_price=Money.of("149.90", "JPY"),
            requested_at=datetime(2026, 1, 1, 0, 0, 4, tzinfo=UTC),
        )
        stop_loss_entry = requested_stop_loss.fill(
            filled_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
            filled_stop_loss_exit_price=Money.of("149.89", "JPY"),
            rebuildable=True,
            planned_rebuild_trigger_price=Money.of("150.00", "JPY"),
        )
        rebuilt = requested.fill(
            filled_entry_price=Money.of("150.02", "JPY"),
            filled_at=datetime(2026, 1, 1, 0, 0, 6, tzinfo=UTC),
        )
        assert isinstance(requested_stop_loss, RequestedStopLossEntry)
        assert isinstance(stop_loss_entry, FilledStopLossEntry)
        assert requested_stop_loss.entry_id.entry_type == EntryIdType.REQUESTED_STOP_LOSS_ENTRY
        assert stop_loss_entry.entry_id.entry_type == EntryIdType.FILLED_STOP_LOSS_ENTRY
        assert stop_loss_entry.requested is requested_stop_loss
        assert filled.filled_stop_loss_entry is None
        assert stop_loss_entry.rebuild(rebuilt) is rebuilt

    def test_non_refillable_close_stores_sealed_entry(self) -> None:
        closed_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
        requested_entry = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=0, build_count=1),
            requested_units=Decimal("1000"),
            requested_entry_price=Money.of("150.00", "JPY"),
            requested_at=datetime(2026, 1, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
        )
        entry = requested_entry.fill(
            filled_entry_price=Money.of("150.00", "JPY"),
            filled_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        slot = Slot()
        slot.place_entry(entry)

        closed = slot.close_for_take_profit(closed_at=closed_at, refillable=False)

        assert closed is entry
        assert isinstance(slot.entry, SealedEntry)
        assert slot.entry.sealed_at == closed_at
        assert slot.status == SlotStatus.SEALED
        assert not slot.is_available

    def test_cycle_refresh_status_removes_empty_top_layers(self) -> None:
        grid = Grid.create(base_units=Decimal("1000"), max_retracements=1)
        grid.add_layer(base_units=Decimal("2000"), max_retracements=1)
        cycle = Cycle.create(cycle_id=1, direction=PositionSide.LONG, grid=grid)

        cycle.refresh_status()

        assert len(cycle.grid.layers) == 1

    def test_first_tick_opens_long_and_short_cycles_when_hedging_enabled(self) -> None:
        config = SnowballConfig()
        state = SnowballState.new()
        result = SnowballEngine(config).process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
        )

        assert [type(event) for event in result.events] == [
            SnowballOpenEvent,
            SnowballOpenEvent,
        ]
        open_events = tuple(
            event for event in result.events if isinstance(event, SnowballOpenEvent)
        )
        assert [event.entry.entry_id.cycle_id for event in open_events] == [1, 2]
        assert [cycle.direction.value for cycle in result.state.cycles] == ["long", "short"]
        assert result.state.cycles[0].grid.layers[0].r0.requested_entry is not None

    def test_counter_entry_is_added_when_price_moves_adversely_by_interval(self) -> None:
        config = SnowballConfig(cycle=CycleConfig(hedging_enabled=False))
        state = SnowballState.new()
        engine = SnowballEngine(config)
        first_tick = TickFactory.tick_at(0, bid="150.00", ask="150.02")
        engine.process_tick(
            tick=first_tick,
            state=state,
        )
        fill_requested_entries(state, filled_at=first_tick.timestamp)

        result = engine.process_tick(
            tick=TickFactory.tick_at(1, bid="149.70", ask="149.72"),
            state=state,
        )

        layer = result.state.cycles[0].grid.layers[0]
        slot = layer.slot(1)
        counter = slot.requested_entry
        assert counter is not None
        assert result.state.cycles[0].grid.layer_number(layer) == 1
        assert layer.retracement_count(slot) == 1
        assert counter.requested_units == Decimal("2000")
        assert result.events[0].metadata["actual_interval_pips"] == "30"

    def test_cycle_take_profit_closes_head_and_reseeds(self) -> None:
        config = SnowballConfig(cycle=CycleConfig(hedging_enabled=False))
        state = SnowballState.new()
        engine = SnowballEngine(config)
        first_tick = TickFactory.tick_at(0, bid="150.00", ask="150.02")
        engine.process_tick(
            tick=first_tick,
            state=state,
        )
        fill_requested_entries(state, filled_at=first_tick.timestamp)

        result = engine.process_tick(
            tick=TickFactory.tick_at(1, bid="150.52", ask="150.54"),
            state=state,
        )

        close_event = result.events[0]
        assert isinstance(close_event, SnowballCloseEvent)
        assert close_event.close_reason == CloseReason.TAKE_PROFIT
        assert isinstance(result.events[1], SnowballOpenEvent)
        assert len(result.state.cycles) == 1
        assert result.state.cycles[0].active

    def test_stop_loss_creates_filled_stop_loss_entry_and_rebuilds_on_revisit(self) -> None:
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
        first_tick = TickFactory.tick_at(0, bid="150.00", ask="150.02")
        engine.process_tick(
            tick=first_tick,
            state=state,
        )
        fill_requested_entries(state, filled_at=first_tick.timestamp)

        stopped = engine.process_tick(
            tick=TickFactory.tick_at(1, bid="149.90", ask="149.92"),
            state=state,
        )
        pending_slot = stopped.state.cycles[0].grid.layers[0].r0

        stopped_event = stopped.events[0]
        assert isinstance(stopped_event, SnowballCloseEvent)
        assert stopped_event.close_reason == CloseReason.STOP_LOSS
        assert isinstance(pending_slot.entry, FilledStopLossEntry)
        pending_entry = pending_slot.filled_stop_loss_entry
        assert pending_entry is not None
        assert pending_entry.requested.requested_stop_loss_exit_price == Money.of("149.92", "JPY")
        assert pending_entry.filled_stop_loss_exit_price == Money.of("149.90", "JPY")
        assert pending_entry.planned_rebuild_trigger_price == Money.of("150.02", "JPY")
        assert pending_entry.original_entry.filled_stop_loss_entry is pending_entry
        assert stopped.state.cycles[0].pending
        restored_pending_entry = (
            SnowballStateSerializer.from_strategy_state(
                SnowballStateSerializer.to_strategy_state(stopped.state)
            )
            .cycles[0]
            .grid.layers[0]
            .r0.filled_stop_loss_entry
        )
        assert restored_pending_entry is not None
        assert (
            restored_pending_entry.original_entry.filled_stop_loss_entry is restored_pending_entry
        )
        assert (
            restored_pending_entry.planned_rebuild_trigger_price
            == pending_entry.planned_rebuild_trigger_price
        )

        rebuilt = engine.process_tick(
            tick=TickFactory.tick_at(2, bid="150.01", ask="150.03"),
            state=state,
        )

        rebuilt_layer = rebuilt.state.cycles[0].grid.layers[0]
        rebuilt_slot = rebuilt_layer.r0
        rebuilt_entry = rebuilt_slot.requested_entry
        assert isinstance(rebuilt.events[0], SnowballOpenEvent)
        assert rebuilt_entry is not None
        assert rebuilt_entry.entry_id.build_count == 2
        assert rebuilt_layer.build_count(rebuilt_slot) == 2
        assert rebuilt.state.cycles[0].active


class TestSnowballStrategy:
    def test_account_parameters_use_core_money_and_currency(self) -> None:
        config = SnowballConfig.from_parameters(
            StrategyParameters.of(account={"currency": "JPY", "balance": "1200000"})
        )

        assert config.account.currency == Currency.of("JPY")
        assert config.account.balance == Money.of("1200000", "JPY")
        assert SnowballConfig.from_parameters(
            StrategyParameters.of(account={"currency": "JPY"})
        ).account.balance == Money.of("10000", "JPY")

    def test_strategy_normalizes_nested_parameters(self) -> None:
        strategy = SnowballStrategy(
            parameters=StrategyParameters.of(
                cycle={"hedging_enabled": False},
                grid={"max_retracements_per_layer": 3},
            )
        )

        assert strategy.config.cycle.hedging_enabled is False
        assert strategy.config.grid == GridConfig(max_retracements_per_layer=3)

    def test_strategy_maps_engine_events_to_core_strategy_events(self) -> None:
        strategy = SnowballStrategy(
            parameters=StrategyParameters.of(cycle={"hedging_enabled": False})
        )
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
        assert result.events[0].metadata["entry_type"] == EntryIdType.REQUESTED_ENTRY.value
        assert result.events[0].metadata["layer_number"] == 1
        assert result.events[0].metadata["cycle_id"] == 1
        assert result.state["snowball"]["cycles"][0]["cycle_id"] == 1


class TestSnowballStateSerialization:
    def test_state_round_trips_through_core_strategy_state(self) -> None:
        config = SnowballConfig(cycle=CycleConfig(hedging_enabled=False))
        state = SnowballState.new()
        result = SnowballEngine(config).process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
        )

        strategy_state = SnowballStateSerializer.to_strategy_state(result.state)
        serialized = strategy_state["snowball"]
        assert serialized["next_cycle_id"] == 2
        assert isinstance(serialized["cycles"][0]["grid"]["layers"], dict)
        serialized_layer = serialized["cycles"][0]["grid"]["layers"]["1"]
        assert isinstance(serialized_layer["slots"], dict)
        assert serialized_layer["build_counts"]["0"] == 1

        restored = SnowballStateSerializer.from_strategy_state(strategy_state)

        event = result.events[0]
        assert isinstance(event, SnowballOpenEvent)
        restored_entry = restored.cycles[0].grid.layers[0].r0.requested_entry
        assert restored.cycles[0].cycle_id == result.state.cycles[0].cycle_id
        assert restored_entry is not None
        assert restored_entry.entry_id == event.entry.entry_id
        assert restored_entry.entry_id.entry_type == EntryIdType.REQUESTED_ENTRY
        assert restored_entry.requested_entry_price == Money.of("150.02", "JPY")
