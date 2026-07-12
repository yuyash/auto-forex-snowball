from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from core import (
    CurrencyPair,
    Money,
    Order,
    OrderSide,
    OrderStatus,
    Pips,
    PositionSide,
    StrategyContext,
    StrategyExecutionResponse,
    StrategyParameters,
    TaskType,
    Tick,
    Units,
    new_uuid,
)
from pydantic import AwareDatetime

from snowball import SnowballStrategy, __version__
from snowball.config import (
    CycleConfig,
    ForwardConfig,
    GridConfig,
    PipProgressionConfig,
    ProtectionConfig,
    SnowballConfig,
    StopLossConfig,
)
from snowball.engine import SnowballEngine
from snowball.enums import CloseReason, IntervalMode, SlotStatus, StopLossMode
from snowball.events import SnowballCloseEvent, SnowballOpenEvent
from snowball.models.entries import (
    FilledEntry,
    FilledStopLossEntry,
    RequestedCloseEntry,
    RequestedEntry,
    RequestedStopLossEntry,
    SealedEntry,
)
from snowball.models.grid import Grid, Layer, Slot
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
                        filled_entry_price=requested.planned_entry_price,
                        filled_at=filled_at,
                    )
                )
        cycle.refresh_status()


class TestSnowballPackage:
    def test_package_version(self) -> None:
        assert __version__ == "0.1.0"


class TestSnowballEngine:
    def test_default_counter_interval_mode_is_constant(self) -> None:
        assert SnowballConfig().counter.interval.mode == IntervalMode.CONSTANT

    def test_entry_transition_methods_create_slot_states(self) -> None:
        requested = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=0, build_number=1),
            planned_units=Units("1000"),
            planned_entry_price=Money.of("150.00", "JPY"),
            planned_at=datetime(2026, 1, 1, tzinfo=UTC),
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
        with pytest.raises(FrozenInstanceError):
            filled.__setattr__("planned_take_profit_price", Money.of("150.60", "JPY"))
        assert requested.entry_id.entry_type == EntryIdType.REQUESTED_ENTRY
        assert requested.entry_id.value == "C1:L1:S0:REQ:B1"
        assert requested.entry_id.display_id == "C1L1R0B1"
        assert filled.entry_id.entry_type == EntryIdType.FILLED_ENTRY
        assert filled.entry_id.value == "C1:L1:S0:FIL:B1"
        assert filled.entry_id.display_id == "C1L1R0B1"
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
            planned_stop_loss_price=Money.of("149.90", "JPY"),
            planned_at=datetime(2026, 1, 1, 0, 0, 4, tzinfo=UTC),
        )
        stop_loss_entry = requested_stop_loss.fill(
            filled_at=datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC),
            filled_stop_loss_price=Money.of("149.89", "JPY"),
            rebuildable=True,
            planned_rebuild_price=Money.of("150.00", "JPY"),
        )
        assert isinstance(requested_stop_loss, RequestedStopLossEntry)
        assert isinstance(stop_loss_entry, FilledStopLossEntry)
        assert requested_stop_loss.entry_id.entry_type == EntryIdType.REQUESTED_STOP_LOSS_ENTRY
        assert stop_loss_entry.entry_id.entry_type == EntryIdType.FILLED_STOP_LOSS_ENTRY
        assert stop_loss_entry.requested is requested_stop_loss
        assert stop_loss_entry.original_entry is filled
        with pytest.raises(FrozenInstanceError):
            requested_stop_loss.__setattr__(
                "planned_stop_loss_price",
                Money.of("149.80", "JPY"),
            )
        with pytest.raises(FrozenInstanceError):
            stop_loss_entry.__setattr__(
                "planned_rebuild_price",
                Money.of("150.10", "JPY"),
            )

    def test_non_refillable_close_stores_sealed_entry(self) -> None:
        closed_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
        requested_entry = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=0, build_number=1),
            planned_units=Units("1000"),
            planned_entry_price=Money.of("150.00", "JPY"),
            planned_at=datetime(2026, 1, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
        )
        entry = requested_entry.fill(
            filled_entry_price=Money.of("150.00", "JPY"),
            filled_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        slot = Slot()
        slot.place_entry(requested_entry, expected_entry_id=requested_entry.entry_id)
        slot.fill_entry(entry)

        closed = slot.request_close(
            planned_at=closed_at,
            planned_exit_price=Money.of("150.50", "JPY"),
            close_reason=CloseReason.TAKE_PROFIT,
            refillable=False,
        )
        slot.fill_close(filled_at=closed_at)

        assert closed is entry
        assert isinstance(slot.entry, SealedEntry)
        assert slot.entry.sealed_at == closed_at
        assert slot.status == SlotStatus.SEALED
        assert not slot.is_available

    def test_slot_entry_is_read_only_from_outside(self) -> None:
        slot = Slot()
        requested = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=0, build_number=1),
            planned_units=Units("1000"),
            planned_entry_price=Money.of("150.00", "JPY"),
            planned_at=datetime(2026, 1, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
        )

        slot_any: Any = slot
        with pytest.raises(AttributeError):
            slot_any.entry = requested

    def test_slot_rejects_entry_for_different_grid_position(self) -> None:
        slot = Slot()
        requested = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=1, build_number=1),
            planned_units=Units("1000"),
            planned_entry_price=Money.of("150.00", "JPY"),
            planned_at=datetime(2026, 1, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
        )

        with pytest.raises(ValueError, match="expected slot identity"):
            slot.place_entry(
                requested,
                expected_entry_id=EntryId(
                    cycle_id=1,
                    layer_number=1,
                    slot_number=0,
                    build_number=1,
                ),
            )

    def test_slot_rejects_filled_entry_with_inconsistent_price_shift(self) -> None:
        requested = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=0, build_number=1),
            planned_units=Units("1000"),
            planned_entry_price=Money.of("150.00", "JPY"),
            planned_at=datetime(2026, 1, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
            planned_stop_loss_price=Money.of("149.90", "JPY"),
        )
        invalid_filled = FilledEntry(
            entry_id=requested.entry_id.with_type(EntryIdType.FILLED_ENTRY),
            requested=requested,
            filled_units=Units("1000"),
            filled_entry_price=Money.of("150.01", "JPY"),
            filled_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
            planned_stop_loss_price=Money.of("149.91", "JPY"),
        )
        slot = Slot()
        slot.place_entry(requested, expected_entry_id=requested.entry_id)

        with pytest.raises(ValueError, match="take-profit price is not fill-adjusted"):
            slot.fill_entry(invalid_filled)

    def test_cycle_rejects_restored_entry_for_different_slot_position(self) -> None:
        requested = RequestedEntry(
            entry_id=EntryId(cycle_id=1, layer_number=1, slot_number=1, build_number=1),
            planned_units=Units("1000"),
            planned_entry_price=Money.of("150.00", "JPY"),
            planned_at=datetime(2026, 1, 1, tzinfo=UTC),
            planned_take_profit_price=Money.of("150.50", "JPY"),
        )
        grid = Grid.from_layers(
            {
                1: Layer.from_slots(
                    base_units=Units("1000"),
                    slots={0: Slot.restore(requested)},
                    build_numbers={0: 1},
                )
            }
        )

        with pytest.raises(ValueError, match="expected slot identity"):
            Cycle.create(cycle_id=1, direction=PositionSide.LONG, grid=grid)

    def test_cycle_refresh_status_removes_empty_top_layers(self) -> None:
        grid = Grid.create(base_units=Units("1000"), max_retracements=1)
        grid.add_layer(base_units=Units("2000"), max_retracements=1)
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
        assert counter.planned_units == Units("2000")
        assert result.events[0].metadata["actual_interval_pips"] == "30"

    def test_stop_loss_default_auto_uses_next_counter_interval(self) -> None:
        config = SnowballConfig(
            cycle=CycleConfig(hedging_enabled=False),
            stop_loss=StopLossConfig(enabled=True),
        )
        state = SnowballState.new()
        result = SnowballEngine(config).process_tick(
            tick=TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            state=state,
        )

        entry = result.state.cycles[0].grid.layers[0].r0.requested_entry
        assert entry is not None
        assert entry.planned_stop_loss_price == Money.of("149.72", "JPY")

    def test_counter_adds_at_most_one_entry_per_cycle_per_tick(self) -> None:
        config = SnowballConfig(cycle=CycleConfig(hedging_enabled=False))
        state = SnowballState.new()
        engine = SnowballEngine(config)
        first_tick = TickFactory.tick_at(0, bid="150.00", ask="150.02")
        engine.process_tick(tick=first_tick, state=state)
        fill_requested_entries(state, filled_at=first_tick.timestamp)

        result = engine.process_tick(
            tick=TickFactory.tick_at(1, bid="149.40", ask="149.42"),
            state=state,
        )

        layer = result.state.cycles[0].grid.layers[0]
        assert len(result.events) == 1
        assert isinstance(result.events[0], SnowballOpenEvent)
        assert layer.slot(1).requested_entry is result.events[0].entry
        assert layer.slot(2).entry is None

    def test_next_layer_initial_uses_highest_slot_grid_price(self) -> None:
        config = SnowballConfig(
            cycle=CycleConfig(hedging_enabled=False),
            grid=GridConfig(max_retracements_per_layer=1, max_layers=2),
            protection=ProtectionConfig(emergency_enabled=False),
        )
        state = SnowballState.new()
        engine = SnowballEngine(config)
        first_tick = TickFactory.tick_at(0, bid="150.00", ask="150.02")
        engine.process_tick(tick=first_tick, state=state)
        fill_requested_entries(state, filled_at=first_tick.timestamp)
        second_tick = TickFactory.tick_at(1, bid="149.70", ask="149.72")
        engine.process_tick(tick=second_tick, state=state)
        fill_requested_entries(state, filled_at=second_tick.timestamp)

        third_tick = TickFactory.tick_at(2, bid="149.00", ask="149.02")
        result = engine.process_tick(tick=third_tick, state=state)

        layer = result.state.cycles[0].grid.layers[1]
        requested_entry = layer.r0.requested_entry
        assert len(result.events) == 1
        assert isinstance(result.events[0], SnowballOpenEvent)
        assert requested_entry is result.events[0].entry
        assert requested_entry.planned_entry_price == Money.of("149.42", "JPY")
        assert requested_entry.planned_entry_price != third_tick.ask
        assert result.events[0].metadata["expected_interval_pips"] == "30"
        assert result.events[0].metadata["actual_interval_pips"] == "70"

    def test_cycle_take_profit_requests_head_close(self) -> None:
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
        assert len(result.events) == 1
        assert len(result.state.cycles) == 1
        assert result.state.cycles[0].active
        requested_close = result.state.cycles[0].grid.layers[0].r0.requested_close_entry
        assert isinstance(requested_close, RequestedCloseEntry)
        assert requested_close.original_entry is close_event.entry
        assert requested_close.close_reason == CloseReason.TAKE_PROFIT

    def test_stop_loss_requests_close_and_rebuilds_after_fill(self) -> None:
        config = SnowballConfig(
            cycle=CycleConfig(hedging_enabled=False),
            stop_loss=StopLossConfig(
                enabled=True,
                mode=StopLossMode.DISTANCE,
                distance=PipProgressionConfig(
                    head_pips=Pips("10"),
                    tail_pips=Pips("10"),
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

        stop_tick = TickFactory.tick_at(1, bid="149.90", ask="149.92")
        stopped = engine.process_tick(tick=stop_tick, state=state)
        pending_slot = stopped.state.cycles[0].grid.layers[0].r0

        stopped_event = stopped.events[0]
        assert isinstance(stopped_event, SnowballCloseEvent)
        assert stopped_event.close_reason == CloseReason.STOP_LOSS
        requested_stop_loss = pending_slot.requested_stop_loss_entry
        assert isinstance(requested_stop_loss, RequestedStopLossEntry)
        assert requested_stop_loss.planned_stop_loss_price == Money.of("149.92", "JPY")
        assert stopped.state.cycles[0].active

        pending_slot.fill_stop_loss(
            filled_at=stop_tick.timestamp,
            filled_stop_loss_price=stopped_event.price,
            rebuildable=True,
            planned_rebuild_price=Money.of("149.92", "JPY"),
        )
        stopped.state.cycles[0].refresh_status()
        pending_entry = pending_slot.filled_stop_loss_entry
        assert pending_entry is not None
        assert pending_entry.requested.planned_stop_loss_price == Money.of("149.92", "JPY")
        assert pending_entry.filled_stop_loss_price == Money.of("149.90", "JPY")
        assert pending_entry.planned_rebuild_price == Money.of("149.92", "JPY")
        assert pending_entry.original_entry is pending_entry.requested.original_entry
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
            restored_pending_entry.original_entry is restored_pending_entry.requested.original_entry
        )
        assert restored_pending_entry.planned_rebuild_price == pending_entry.planned_rebuild_price

        rebuilt = engine.process_tick(
            tick=TickFactory.tick_at(2, bid="150.01", ask="150.03"),
            state=state,
        )

        rebuilt_layer = rebuilt.state.cycles[0].grid.layers[0]
        rebuilt_slot = rebuilt_layer.r0
        rebuilt_entry = rebuilt_slot.requested_entry
        assert isinstance(rebuilt.events[0], SnowballOpenEvent)
        assert rebuilt_entry is not None
        assert rebuilt_entry.entry_id.build_number == 2
        assert rebuilt_entry.planned_entry_price == Money.of("149.92", "JPY")
        assert rebuilt_layer.build_number(rebuilt_slot) == 2
        assert rebuilt.state.cycles[0].active


class TestSnowballStrategy:
    def test_balance_based_sizing_uses_core_context_account_balance(self) -> None:
        strategy = SnowballStrategy(
            parameters=StrategyParameters.of(
                cycle={"hedging_enabled": False},
                sizing={
                    "mode": "balance_based",
                    "balance_based": {
                        "reference_balance": {
                            "amount": "1000000",
                            "currency": "JPY",
                        },
                        "reference_units": "1000",
                        "round_step_units": "100",
                        "min_units": "100",
                    },
                },
            )
        )
        context = StrategyContext(
            task_id=new_uuid(),
            task_type=TaskType.BACKTEST,
            instrument=USD_JPY,
            account_balance=Money.of("3000000", "JPY"),
        )

        result = strategy.on_tick(
            TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            context,
        )

        assert result.events[0].units == Units("3000")
        assert strategy.config.sizing.base_units == Units("3000")

    def test_account_balance_is_not_a_snowball_parameter(self) -> None:
        with pytest.raises(ValueError, match="Core task account balance"):
            SnowballConfig.from_parameters(
                StrategyParameters.of(
                    account={
                        "balance": {
                            "amount": "3000000",
                            "currency": "JPY",
                        },
                    }
                )
            )

    def test_strategy_normalizes_nested_parameters(self) -> None:
        strategy = SnowballStrategy(
            parameters=StrategyParameters.of(
                cycle={"hedging_enabled": False},
                forward={"take_profit_pips": "20"},
                grid={"max_retracements_per_layer": 3},
            )
        )

        assert strategy.config.cycle.hedging_enabled is False
        assert strategy.config.forward == ForwardConfig(take_profit_pips=Pips("20"))
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

        assert result.events[0].action.value == "open_trade"
        assert result.events[0].display_id == "C1L1R0B1"
        assert result.events[0].metadata["strategy_type"] == "snowball"
        assert result.events[0].metadata["entry_type"] == EntryIdType.REQUESTED_ENTRY.value
        assert result.events[0].metadata["layer_number"] == 1
        assert result.events[0].metadata["build_number"] == 1
        assert result.events[0].metadata["cycle_id"] == 1
        assert result.events[0].metadata["planned_entry_price"] == "150.02 JPY"
        result_state = result.state
        assert result_state is not None
        assert result_state["snowball"]["cycles"][0]["cycle_id"] == 1

        filled_state = strategy.on_execution_reports(
            (
                StrategyExecutionResponse(
                    event=result.events[0],
                    order=Order(
                        instrument=USD_JPY,
                        side=OrderSide.BUY,
                        units=Units("1000"),
                        price=Money.of("150.02", "JPY"),
                        status=OrderStatus.FILLED,
                        filled_units=Units("1000"),
                        average_fill_price=Money.of("150.02", "JPY"),
                    ),
                ),
            ),
            context.with_state(result_state),
        )

        filled_slot = filled_state["snowball"]["cycles"][0]["grid"]["layers"]["1"]["slots"]["0"]
        assert filled_slot["requested_entry"] is None
        assert filled_slot["filled_entry"]["filled_units"] == "1000"

        close_result = strategy.on_tick(
            TickFactory.tick_at(1, bid="150.52", ask="150.54"),
            context.with_state(filled_state),
        )

        assert close_result.events[0].action.value == "close_trade"
        assert close_result.events[0].display_id == "C1L1R0B1"
        assert close_result.events[0].reason.rule_id == "snowball.close.take_profit"
        assert close_result.events[0].metadata["close_reason"] == "take_profit"
        assert close_result.events[0].metadata["planned_entry_price"] == "150.02 JPY"
        assert close_result.events[0].metadata["filled_entry_price"] == "150.02 JPY"
        close_result_state = close_result.state
        assert close_result_state is not None
        requested_close_slot = close_result_state["snowball"]["cycles"][0]["grid"]["layers"]["1"][
            "slots"
        ]["0"]
        assert requested_close_slot["filled_entry"] is None
        assert (
            requested_close_slot["requested_close_entry"]["close_reason"]
            == CloseReason.TAKE_PROFIT.value
        )

        closed_state = strategy.on_execution_reports(
            (
                StrategyExecutionResponse(
                    event=close_result.events[0],
                    order=Order(
                        instrument=USD_JPY,
                        side=OrderSide.SELL,
                        units=Units("1000"),
                        price=Money.of("150.52", "JPY"),
                        status=OrderStatus.FILLED,
                        filled_units=Units("1000"),
                        average_fill_price=Money.of("150.52", "JPY"),
                    ),
                ),
            ),
            context.with_state(close_result_state),
        )

        closed_slot = closed_state["snowball"]["cycles"][0]["grid"]["layers"]["1"]["slots"]["0"]
        assert closed_slot["requested_close_entry"] is None
        assert closed_slot["sealed"]

    def test_execution_fill_shifts_planned_take_profit_and_stop_loss_by_fill_delta(
        self,
    ) -> None:
        strategy = SnowballStrategy(
            parameters=StrategyParameters.of(
                cycle={"hedging_enabled": False},
                stop_loss={"enabled": True},
            )
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
        result_state = result.state
        assert result_state is not None

        filled_state = strategy.on_execution_reports(
            (
                StrategyExecutionResponse(
                    event=result.events[0],
                    order=Order(
                        instrument=USD_JPY,
                        side=OrderSide.BUY,
                        units=Units("1000"),
                        price=Money.of("150.02", "JPY"),
                        status=OrderStatus.FILLED,
                        filled_units=Units("1000"),
                        average_fill_price=Money.of("150.05", "JPY"),
                    ),
                ),
            ),
            context.with_state(result_state),
        )

        filled_entry = filled_state["snowball"]["cycles"][0]["grid"]["layers"]["1"]["slots"]["0"][
            "filled_entry"
        ]
        assert filled_entry["planned_entry_price"]["amount"] == "150.02"
        assert filled_entry["planned_take_profit_price"]["amount"] == "150.52"
        assert filled_entry["planned_stop_loss_price"]["amount"] == "149.72"
        assert filled_entry["filled_entry_price"]["amount"] == "150.05"
        assert filled_entry["current_planned_take_profit_price"]["amount"] == "150.55"
        assert filled_entry["current_planned_stop_loss_price"]["amount"] == "149.75"

    def test_rebuild_uses_planned_stop_loss_price_and_fill_based_take_profit(
        self,
    ) -> None:
        strategy = SnowballStrategy(
            parameters=StrategyParameters.of(
                cycle={"hedging_enabled": False},
                stop_loss={
                    "enabled": True,
                    "mode": "distance",
                    "distance": {
                        "mode": "constant",
                        "head_pips": "10",
                        "tail_pips": "10",
                        "flat_steps": 0,
                    },
                },
                rebuild={
                    "price": {"entry_price_mode": "stop_loss_exit_price"},
                },
            )
        )
        context = StrategyContext(
            task_id=new_uuid(),
            task_type=TaskType.BACKTEST,
            instrument=USD_JPY,
        )
        opened = strategy.on_tick(
            TickFactory.tick_at(0, bid="150.00", ask="150.02"),
            context,
        )
        assert opened.events[0].display_id == "C1L1R0B1"
        assert opened.events[0].reason.rule_id == "snowball.open"
        opened_state = opened.state
        assert opened_state is not None
        filled = strategy.on_execution_reports(
            (
                StrategyExecutionResponse(
                    event=opened.events[0],
                    order=Order(
                        instrument=USD_JPY,
                        side=OrderSide.BUY,
                        units=Units("1000"),
                        price=Money.of("150.02", "JPY"),
                        status=OrderStatus.FILLED,
                        filled_units=Units("1000"),
                        average_fill_price=Money.of("150.02", "JPY"),
                    ),
                ),
            ),
            context.with_state(opened_state),
        )

        stopped = strategy.on_tick(
            TickFactory.tick_at(1, bid="149.90", ask="149.92"),
            context.with_state(filled),
        )
        assert stopped.events[0].display_id == "C1L1R0B1"
        assert stopped.events[0].metadata["planned_rebuild_price"] == "149.92 JPY"
        stopped_state = stopped.state
        assert stopped_state is not None
        stop_filled = strategy.on_execution_reports(
            (
                StrategyExecutionResponse(
                    event=stopped.events[0],
                    order=Order(
                        instrument=USD_JPY,
                        side=OrderSide.SELL,
                        units=Units("1000"),
                        price=Money.of("149.90", "JPY"),
                        status=OrderStatus.FILLED,
                        filled_units=Units("1000"),
                        average_fill_price=Money.of("149.88", "JPY"),
                    ),
                ),
            ),
            context.with_state(stopped_state),
        )
        pending_entry = stop_filled["snowball"]["cycles"][0]["grid"]["layers"]["1"]["slots"]["0"][
            "filled_stop_loss_entry"
        ]
        assert pending_entry["filled_stop_loss_price"]["amount"] == "149.88"
        assert pending_entry["planned_rebuild_price"]["amount"] == "149.92"

        not_rebuilt = strategy.on_tick(
            TickFactory.tick_at(2, bid="149.91", ask="149.95"),
            context.with_state(stop_filled),
        )
        assert not_rebuilt.events == ()
        assert not_rebuilt.state is None

        rebuilt = strategy.on_tick(
            TickFactory.tick_at(3, bid="149.92", ask="149.95"),
            context.with_state(strategy.strategy_state()),
        )
        assert rebuilt.events[0].display_id == "C1L1R0B2"
        assert rebuilt.events[0].reason.rule_id == "snowball.open.rebuild"
        assert rebuilt.events[0].metadata["is_rebuild"] is True
        assert rebuilt.events[0].metadata["planned_entry_price"] == "149.92 JPY"
        assert rebuilt.events[0].metadata["planned_rebuild_price"] == "149.92 JPY"
        rebuilt_state = rebuilt.state
        assert rebuilt_state is not None
        requested_rebuild = rebuilt_state["snowball"]["cycles"][0]["grid"]["layers"]["1"]["slots"][
            "0"
        ]["requested_entry"]
        assert rebuilt.events[0].price == Money.of("149.92", "JPY")
        assert requested_rebuild["planned_entry_price"]["amount"] == "149.92"
        assert requested_rebuild["planned_take_profit_price"]["amount"] == "150.42"

        rebuild_filled = strategy.on_execution_reports(
            (
                StrategyExecutionResponse(
                    event=rebuilt.events[0],
                    order=Order(
                        instrument=USD_JPY,
                        side=OrderSide.BUY,
                        units=Units("1000"),
                        price=Money.of("149.92", "JPY"),
                        status=OrderStatus.FILLED,
                        filled_units=Units("1000"),
                        average_fill_price=Money.of("149.95", "JPY"),
                    ),
                ),
            ),
            context.with_state(rebuilt_state),
        )
        rebuilt_entry = rebuild_filled["snowball"]["cycles"][0]["grid"]["layers"]["1"]["slots"][
            "0"
        ]["filled_entry"]
        assert rebuilt_entry["filled_entry_price"]["amount"] == "149.95"
        assert rebuilt_entry["current_planned_take_profit_price"]["amount"] == "150.45"


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
        assert isinstance(serialized["cycles"][0]["grid"]["layers"], Mapping)
        serialized_layer = serialized["cycles"][0]["grid"]["layers"]["1"]
        assert isinstance(serialized_layer["slots"], Mapping)
        assert serialized_layer["build_numbers"]["0"] == 1

        restored = SnowballStateSerializer.from_strategy_state(strategy_state)

        event = result.events[0]
        assert isinstance(event, SnowballOpenEvent)
        restored_entry = restored.cycles[0].grid.layers[0].r0.requested_entry
        assert restored.cycles[0].cycle_id == result.state.cycles[0].cycle_id
        assert restored_entry is not None
        assert restored_entry.entry_id == event.entry.entry_id
        assert restored_entry.entry_id.entry_type == EntryIdType.REQUESTED_ENTRY
        assert restored_entry.planned_entry_price == Money.of("150.02", "JPY")
