from datetime import UTC, datetime
from decimal import Decimal

from core import Money, PositionSide

from snowball.config import (
    RebuildConfig,
    RebuildTriggerConfig,
    SnowballConfig,
)
from snowball.enums import RebuildEntryPriceMode
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


def test_grid_selector_uses_pending_stop_loss_original_as_effective_head() -> None:
    grid = Grid.create(base_units=Decimal("1000"), max_retracements=2)
    layer = grid.current_layer
    original = filled_entry(slot_number=0, price="150.00")
    layer.r0.place_entry(original)
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
    assert original.filled_stop_loss_entry is layer.r0.filled_stop_loss_entry


def test_grid_selector_respects_refillable_counter_limit() -> None:
    layer = Layer.create(base_units=Decimal("1000"), max_retracements=2)
    layer.r0.place_entry(filled_entry(slot_number=0, price="150.00"))
    layer.slot(1).place_entry(filled_entry(slot_number=1, price="149.70"))
    r2 = layer.slot(2)
    layer.next_build_count(r2)
    r2.place_entry(filled_entry(slot_number=2, price="149.40"))
    r2.close_for_take_profit(closed_at=NOW, refillable=True)

    assert (
        SnowballGridSelector().next_available_counter_slot(
            layer=layer,
            max_refillable_retracement=1,
        )
        is None
    )


def test_take_profit_planner_syncs_weighted_average_to_counter_entries() -> None:
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
    layer.r0.place_entry(head)
    layer.slot(1).place_entry(counter)

    take_profit_price = planner.sync_weighted_average_take_profits(layer)

    expected = Money.of(
        (Decimal("150.00") * Decimal("1000") + Decimal("149.50") * Decimal("2000"))
        / Decimal("3000"),
        "JPY",
    )
    assert take_profit_price == expected
    assert head.planned_take_profit_price == Money.of("151.00", "JPY")
    assert counter.planned_take_profit_price == expected


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
        stop_loss_exit_price=Money.of("149.90", "JPY"),
        pip_size=Decimal("0.01"),
    )

    assert trigger == Money.of("149.95", "JPY")
