from datetime import UTC, datetime
from decimal import Decimal

from core import Money, PositionSide

from snowball.config import (
    RebuildConfig,
    RebuildTriggerConfig,
    SnowballConfig,
)
from snowball.enums import CloseReason, RebuildEntryPriceMode
from snowball.models.entries import FilledEntry, RequestedEntry
from snowball.models.grid import Grid, Layer
from snowball.models.identifiers import EntryId
from snowball.models.state import Cycle
from snowball.services.calculators import SnowballCalculator
from snowball.services.market_pricing import SnowballMarketPricing
from snowball.services.policies.stop_loss import SnowballStopLossPlanner
from snowball.services.policies.take_profit import SnowballTakeProfitPlanner
from snowball.services.selectors.grid import SnowballGridSelector

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def requested_entry(
    *,
    slot_number: int,
    price: str,
    units: str = "1000",
    take_profit_price: str = "151.00",
) -> RequestedEntry:
    return RequestedEntry(
        entry_id=EntryId(
            cycle_id=1,
            layer_number=1,
            slot_number=slot_number,
            build_count=1,
        ),
        requested_units=Decimal(units),
        requested_entry_price=Money.of(price, "JPY"),
        requested_at=NOW,
        planned_take_profit_price=Money.of(take_profit_price, "JPY"),
        planned_stop_loss_price=Money.of("149.00", "JPY"),
    )


def filled_entry(
    *,
    slot_number: int,
    price: str,
    units: str = "1000",
    take_profit_price: str = "151.00",
) -> FilledEntry:
    return requested_entry(
        slot_number=slot_number,
        price=price,
        units=units,
        take_profit_price=take_profit_price,
    ).fill(filled_entry_price=Money.of(price, "JPY"), filled_at=NOW)


def place_filled_entry(layer: Layer, entry: FilledEntry) -> None:
    slot = layer.slot(entry.entry_id.slot_number)
    assigned_build_count = layer.next_build_count(slot)
    if entry.entry_id.build_count != assigned_build_count:
        raise AssertionError("test entry build count does not match layer generator")
    slot.place_entry(entry.requested, expected_entry_id=entry.requested.entry_id)
    slot.fill_entry(entry)


def test_grid_selector_uses_pending_stop_loss_original_as_effective_head() -> None:
    grid = Grid.create(base_units=Decimal("1000"), max_retracements=2)
    layer = grid.current_layer
    original = filled_entry(slot_number=0, price="150.00")
    place_filled_entry(layer, original)
    cycle = Cycle.create(cycle_id=1, direction=PositionSide.LONG, grid=grid)
    selector = SnowballGridSelector()

    assert selector.effective_head(cycle) is original

    layer.r0.request_stop_loss(
        requested_at=NOW,
        requested_stop_loss_exit_price=Money.of("149.00", "JPY"),
    )

    assert selector.effective_head(cycle) is original

    layer.r0.fill_stop_loss(
        filled_at=NOW,
        filled_stop_loss_exit_price=Money.of("149.00", "JPY"),
        rebuildable=True,
        planned_rebuild_trigger_price=Money.of("150.00", "JPY"),
    )

    assert selector.effective_head(cycle) is original
    pending_stop_loss = layer.r0.filled_stop_loss_entry
    assert pending_stop_loss is not None
    assert pending_stop_loss.original_entry is original


def test_grid_selector_respects_refillable_counter_limit() -> None:
    layer = Layer.create(base_units=Decimal("1000"), max_retracements=2)
    place_filled_entry(layer, filled_entry(slot_number=0, price="150.00"))
    place_filled_entry(layer, filled_entry(slot_number=1, price="149.70"))
    r2 = layer.slot(2)
    place_filled_entry(layer, filled_entry(slot_number=2, price="149.40"))
    r2.request_close(
        requested_at=NOW,
        requested_exit_price=Money.of("151.00", "JPY"),
        close_reason=CloseReason.COUNTER_TAKE_PROFIT,
        refillable=True,
    )
    r2.fill_close(filled_at=NOW)

    assert (
        SnowballGridSelector().next_available_counter_slot(
            layer=layer,
            max_refillable_retracement=1,
        )
        is None
    )


def test_take_profit_planner_uses_snapshot_weighted_average_without_mutating_entries() -> None:
    config = SnowballConfig()
    planner = SnowballTakeProfitPlanner(
        config,
        SnowballCalculator(config),
        SnowballMarketPricing(),
    )
    layer = Layer.create(base_units=Decimal("1000"), max_retracements=2)
    head = filled_entry(
        slot_number=0,
        price="150.00",
        units="1000",
        take_profit_price="151.00",
    )
    counter = filled_entry(
        slot_number=1,
        price="149.50",
        units="2000",
        take_profit_price="150.00",
    )
    place_filled_entry(layer, head)
    place_filled_entry(layer, counter)

    take_profit_price = planner.counter_take_profit_price(
        layer=layer,
        direction=PositionSide.LONG,
        retracement_count=2,
        entry_price=Money.of("149.00", "JPY"),
        units=Decimal("3000"),
        pip_size=Decimal("0.01"),
        include_head=None,
    )

    expected = Money.of(
        (
            Decimal("150.00") * Decimal("1000")
            + Decimal("149.50") * Decimal("2000")
            + Decimal("149.00") * Decimal("3000")
        )
        / Decimal("6000"),
        "JPY",
    )
    assert take_profit_price == expected
    assert head.planned_take_profit_price == Money.of("151.00", "JPY")
    stored_counter = layer.slot(1).filled_entry
    assert stored_counter is not None
    assert stored_counter.planned_take_profit_price == Money.of("150.00", "JPY")
    assert counter.requested.planned_take_profit_price == Money.of("150.00", "JPY")


def test_stop_loss_planner_buffers_rebuild_trigger_from_stop_loss_exit_price() -> None:
    config = SnowballConfig(
        rebuild=RebuildConfig(
            trigger=RebuildTriggerConfig(
                entry_price_mode=RebuildEntryPriceMode.STOP_LOSS_EXIT_PRICE,
                buffer_pips=Decimal("5"),
            )
        )
    )
    planner = SnowballStopLossPlanner(
        config,
        SnowballCalculator(config),
        SnowballMarketPricing(),
    )

    trigger = planner.rebuild_trigger_price(
        direction=PositionSide.LONG,
        original_entry_price=Money.of("150.00", "JPY"),
        planned_stop_loss_price=Money.of("149.92", "JPY"),
        stop_loss_exit_price=Money.of("149.90", "JPY"),
        pip_size=Decimal("0.01"),
    )

    assert trigger == Money.of("149.97", "JPY")


def test_stop_loss_planner_does_not_buffer_original_entry_rebuild_trigger() -> None:
    config = SnowballConfig(
        rebuild=RebuildConfig(
            trigger=RebuildTriggerConfig(
                entry_price_mode=RebuildEntryPriceMode.ORIGINAL_ENTRY_PRICE,
                buffer_pips=Decimal("5"),
            )
        )
    )
    planner = SnowballStopLossPlanner(
        config,
        SnowballCalculator(config),
        SnowballMarketPricing(),
    )

    trigger = planner.rebuild_trigger_price(
        direction=PositionSide.LONG,
        original_entry_price=Money.of("150.00", "JPY"),
        planned_stop_loss_price=Money.of("149.92", "JPY"),
        stop_loss_exit_price=Money.of("149.90", "JPY"),
        pip_size=Decimal("0.01"),
    )

    assert trigger == Money.of("150.00", "JPY")
