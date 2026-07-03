"""Snowball tick-processing engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core import Metadata, Money, PositionSide, Tick

from snowball.config import SnowballConfig
from snowball.enums import CloseReason, CounterTakeProfitMode, EntryRole
from snowball.events import (
    SnowballCloseEvent,
    SnowballEvent,
    SnowballOpenEvent,
    SnowballStatusEvent,
    SnowballStopEvent,
)
from snowball.models.entries import FilledEntry, FilledStopLossEntry, RequestedEntry
from snowball.models.grid import Grid, Layer, Slot
from snowball.models.identifiers import EntryId
from snowball.models.state import Cycle, SnowballState
from snowball.services.accounting import AccountSnapshot, SnowballAccounting
from snowball.services.calculators import SnowballCalculator
from snowball.services.grid_policy import SnowballGridPolicy
from snowball.services.pricing import SnowballPricing


@dataclass(frozen=True, slots=True)
class SnowballStepResult:
    """Result of processing one market tick."""

    events: tuple[SnowballEvent, ...]
    state: SnowballState


@dataclass(slots=True)
class SnowballEngine:
    """Process Snowball state transitions for market ticks."""

    config: SnowballConfig
    calculator: SnowballCalculator = field(init=False)
    pricing: SnowballPricing = field(default_factory=SnowballPricing)
    grid_policy: SnowballGridPolicy = field(default_factory=SnowballGridPolicy)
    accounting: SnowballAccounting = field(default_factory=SnowballAccounting)

    def __post_init__(self) -> None:
        self.calculator = SnowballCalculator(self.config)

    def process_tick(
        self,
        *,
        tick: Tick,
        state: SnowballState,
        pip_size: Decimal,
    ) -> SnowballStepResult:
        """Process a tick and return emitted Snowball events."""
        events: list[SnowballEvent] = []
        state.prune_completed_cycles()
        account = self.accounting.evaluate(state=state, tick=tick, config=self.config)

        emergency = self._handle_emergency(margin_ratio=account.margin_ratio)
        if emergency is not None:
            return SnowballStepResult(events=(emergency,), state=state)

        shrink_events = self._handle_shrink(
            state=state,
            tick=tick,
            pip_size=pip_size,
            account=account,
        )
        if shrink_events:
            state.refresh_cycle_statuses()
            state.prune_completed_cycles()
            return SnowballStepResult(events=tuple(shrink_events), state=state)

        if not state.cycles:
            events.extend(self._initialize_cycles(state=state, tick=tick, pip_size=pip_size))

        for cycle in list(state.cycles):
            if cycle.completed:
                continue
            events.extend(self._process_cycle(cycle=cycle, tick=tick, pip_size=pip_size))

        events.extend(self._reseed_directions(state=state, tick=tick, pip_size=pip_size))
        state.refresh_cycle_statuses()
        state.prune_completed_cycles()
        return SnowballStepResult(events=tuple(events), state=state)

    def _initialize_cycles(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballEvent]:
        events: list[SnowballEvent] = []
        for direction in self._managed_directions():
            events.extend(
                self._open_cycle(state=state, tick=tick, direction=direction, pip_size=pip_size)
            )
        return events

    def _process_cycle(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballEvent]:
        events: list[SnowballEvent] = []
        events.extend(self._process_rebuilds(cycle=cycle, tick=tick, pip_size=pip_size))
        events.extend(self._process_counter_take_profits(cycle=cycle, tick=tick))
        events.extend(self._process_cycle_take_profit(cycle=cycle, tick=tick))
        events.extend(self._process_stop_losses(cycle=cycle, tick=tick, pip_size=pip_size))
        events.extend(self._process_rebuilds(cycle=cycle, tick=tick, pip_size=pip_size))
        if self.grid_policy.validate_ordering(cycle) is None:
            events.extend(self._process_counter_adds(cycle=cycle, tick=tick, pip_size=pip_size))
        cycle.grid.remove_empty_top_layers()
        cycle.refresh_status()
        return events

    def _managed_directions(self) -> tuple[PositionSide, ...]:
        if self.config.cycle.hedging_enabled:
            return PositionSide.LONG, PositionSide.SHORT
        return (PositionSide.LONG,)

    def _open_cycle(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        direction: PositionSide,
        pip_size: Decimal,
    ) -> list[SnowballEvent]:
        cycle = Cycle.create(
            cycle_id=state.next_cycle_id(),
            direction=direction,
            grid=self._new_grid(),
        )
        layer = cycle.grid.current_layer
        slot = layer.r0
        entry = self._create_entry(
            entry_id=cycle.next_entry_id(layer=layer, slot=slot),
            tick=tick,
            direction=direction,
            grid=cycle.grid,
            layer=layer,
            slot=slot,
            pip_size=pip_size,
        )
        slot.place_entry(entry)
        state.add_cycle(cycle)
        return [
            self._open_event(
                cycle=cycle,
                layer=layer,
                slot=slot,
                entry=entry,
            )
        ]

    def _new_grid(self) -> Grid:
        base_units = self.config.sizing.layer_base_units(1)
        return Grid.create(
            base_units=base_units,
            max_retracements=self.config.grid.max_retracements_per_layer,
        )

    def _create_entry(
        self,
        *,
        entry_id: EntryId,
        tick: Tick,
        direction: PositionSide,
        grid: Grid,
        layer: Layer,
        slot: Slot,
        pip_size: Decimal,
        rebuild_source: FilledStopLossEntry | None = None,
        requested_entry_price: Money | None = None,
    ) -> RequestedEntry:
        role = grid.role_for(layer, slot)
        retracement_count = layer.retracement_count(slot)
        entry_price = requested_entry_price or self.pricing.entry_side_price(direction, tick)
        units = self._entry_units(role=role, layer=layer, retracement_count=retracement_count)

        if role == EntryRole.COUNTER:
            include_head = self._counter_weighted_average_head(layer=layer, cycle_head=None)
            take_profit_price = self.pricing.counter_take_profit_price(
                layer=layer,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                units=units,
                pip_size=pip_size,
                calculator=self.calculator,
                include_head=include_head,
            )
        elif rebuild_source is not None:
            take_profit_price = self.pricing.rebuild_take_profit_price(
                stop_loss_entry=rebuild_source,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                pip_size=pip_size,
                calculator=self.calculator,
            )
        else:
            take_profit_price = self.pricing.take_profit_price(
                direction=direction,
                entry_price=entry_price,
                tp_pips=self.config.cycle.take_profit_pips,
                pip_size=pip_size,
            )

        return RequestedEntry(
            entry_id=entry_id,
            requested_units=units,
            requested_entry_price=entry_price,
            requested_at=tick.timestamp,
            planned_take_profit_price=take_profit_price,
            planned_stop_loss_price=self._stop_loss_price(
                direction=direction,
                entry_price=entry_price,
                retracement_count=retracement_count,
                pip_size=pip_size,
                rebuild_source=rebuild_source,
            ),
        )

    def _stop_loss_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        retracement_count: int,
        pip_size: Decimal,
        rebuild_source: FilledStopLossEntry | None,
    ) -> Money | None:
        if not self.config.stop_loss.enabled:
            return None
        if rebuild_source is not None:
            return self.pricing.rebuild_stop_loss_price(
                stop_loss_entry=rebuild_source,
                direction=direction,
                retracement_count=retracement_count,
                entry_price=entry_price,
                pip_size=pip_size,
                calculator=self.calculator,
            )
        stop_loss_pips = self.calculator.stop_loss_pips(retracement_count + 1)
        return self.pricing.stop_loss_price(
            direction=direction,
            entry_price=entry_price,
            stop_loss_pips=stop_loss_pips,
            pip_size=pip_size,
        )

    def _entry_units(
        self,
        *,
        role: EntryRole,
        layer: Layer,
        retracement_count: int,
    ) -> Decimal:
        if role == EntryRole.COUNTER:
            return Decimal(retracement_count + 1) * layer.base_units
        return self.config.sizing.initial_entry_units_multiplier * layer.base_units

    def _open_event(
        self,
        *,
        cycle: Cycle,
        layer: Layer,
        slot: Slot,
        entry: RequestedEntry,
        metadata: Metadata | None = None,
    ) -> SnowballOpenEvent:
        """Create an open event with a structure-derived entry key."""
        return SnowballOpenEvent(
            cycle_id=cycle.cycle_id,
            direction=cycle.direction,
            entry=entry,
            metadata=metadata or Metadata(),
        )

    def _close_event(
        self,
        *,
        cycle: Cycle,
        layer: Layer,
        slot: Slot,
        entry: FilledEntry,
        price: Money,
        close_reason: CloseReason,
        metadata: Metadata | None = None,
    ) -> SnowballCloseEvent:
        """Create a close event with a structure-derived entry key."""
        return SnowballCloseEvent(
            cycle_id=cycle.cycle_id,
            direction=cycle.direction,
            entry=entry,
            price=price,
            close_reason=close_reason,
            metadata=metadata or Metadata(),
        )

    def _counter_weighted_average_head(
        self,
        *,
        layer: Layer,
        cycle_head: FilledEntry | None,
    ) -> FilledEntry | None:
        if self.config.counter.take_profit.mode != CounterTakeProfitMode.WEIGHTED_AVG:
            return None
        if layer.r0.is_present:
            return None
        return cycle_head

    def _process_counter_take_profits(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        events: list[SnowballEvent] = []
        while True:
            candidate = self._next_counter_take_profit_candidate(cycle=cycle, tick=tick)
            if candidate is None:
                break
            layer, slot = candidate
            entry = slot.filled_entry
            retracement_count = layer.retracement_count(slot)
            role = cycle.grid.role_for(layer, slot)
            if entry is None:
                break
            exit_price = self.pricing.exit_side_price(cycle.direction, tick)
            refillable_counter = (
                role == EntryRole.COUNTER
                and retracement_count <= self.config.grid.max_refillable_counter_retracement
            )
            slot.close_for_take_profit(
                closed_at=tick.timestamp,
                refillable=refillable_counter,
            )
            realized = self.pricing.realized_pl(
                direction=cycle.direction,
                entry=entry,
                exit_price=exit_price,
            )
            if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
                self.pricing.sync_weighted_average_take_profits(layer)
            close_reason = (
                CloseReason.LAYER_INITIAL_TAKE_PROFIT
                if role == EntryRole.LAYER_INITIAL
                else CloseReason.COUNTER_TAKE_PROFIT
            )
            events.append(
                self._close_event(
                    cycle=cycle,
                    layer=layer,
                    slot=slot,
                    entry=entry,
                    price=exit_price,
                    close_reason=close_reason,
                    metadata=Metadata.of(realized_pl=str(realized)),
                )
            )
            cycle.grid.remove_empty_top_layers()
            cycle.refresh_status()
        return events

    def _next_counter_take_profit_candidate(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> tuple[Layer, Slot] | None:
        head = cycle.head()
        for layer in reversed(cycle.grid.layers):
            live_count = len(layer.live_entries())
            for slot in reversed(layer.slots):
                entry = slot.filled_entry
                if entry is None or entry is head:
                    continue
                if cycle.grid.role_for(layer, slot) == EntryRole.LAYER_INITIAL and live_count > 1:
                    continue
                if self.pricing.take_profit_hit(
                    direction=cycle.direction,
                    entry=entry,
                    tick=tick,
                ):
                    return layer, slot
        return None

    def _process_cycle_take_profit(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballEvent]:
        layer = cycle.grid.layers[0]
        slot = layer.r0
        entry = slot.filled_entry
        if entry is None or not self.pricing.take_profit_hit(
            direction=cycle.direction,
            entry=entry,
            tick=tick,
        ):
            return []
        if cycle.counter_entries():
            return []

        exit_price = self.pricing.exit_side_price(cycle.direction, tick)
        slot.close_for_take_profit(closed_at=tick.timestamp, refillable=False)
        realized = self.pricing.realized_pl(
            direction=cycle.direction,
            entry=entry,
            exit_price=exit_price,
        )
        cycle.refresh_status()
        return [
            self._close_event(
                cycle=cycle,
                layer=layer,
                slot=slot,
                entry=entry,
                price=exit_price,
                close_reason=CloseReason.TAKE_PROFIT,
                metadata=Metadata.of(realized_pl=str(realized)),
            )
        ]

    def _process_stop_losses(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballEvent]:
        if not self.config.stop_loss.enabled:
            return []
        events: list[SnowballEvent] = []
        for layer in list(reversed(cycle.grid.layers)):
            highest = layer.highest_live_slot()
            for slot in list(reversed(layer.slots)):
                entry = slot.filled_entry
                if entry is None or not self.pricing.stop_loss_hit(
                    direction=cycle.direction,
                    entry=entry,
                    tick=tick,
                ):
                    continue
                if self._stop_loss_temporarily_protected(layer=layer, slot=slot, highest=highest):
                    continue
                requested_stop_loss_exit_price = entry.planned_stop_loss_price
                if requested_stop_loss_exit_price is None:
                    continue
                slot.request_stop_loss(
                    requested_at=tick.timestamp,
                    requested_stop_loss_exit_price=requested_stop_loss_exit_price,
                )
                exit_price = self.pricing.exit_side_price(cycle.direction, tick)
                realized = self.pricing.realized_pl(
                    direction=cycle.direction,
                    entry=entry,
                    exit_price=exit_price,
                )
                rebuild_trigger_price = (
                    self.pricing.rebuild_trigger_price(
                        direction=cycle.direction,
                        original_entry_price=entry.filled_entry_price,
                        stop_loss_exit_price=exit_price,
                        config=self.config,
                        pip_size=pip_size,
                    )
                    if self.config.rebuild.enabled
                    else None
                )
                slot.fill_stop_loss(
                    filled_at=tick.timestamp,
                    filled_stop_loss_exit_price=exit_price,
                    rebuildable=self.config.rebuild.enabled,
                    planned_rebuild_trigger_price=rebuild_trigger_price,
                )
                events.append(
                    self._close_event(
                        cycle=cycle,
                        layer=layer,
                        slot=slot,
                        entry=entry,
                        price=exit_price,
                        close_reason=CloseReason.STOP_LOSS,
                        metadata=Metadata.of(realized_pl=str(realized)),
                    )
                )
        cycle.grid.remove_empty_top_layers()
        cycle.refresh_status()
        return events

    def _stop_loss_temporarily_protected(
        self,
        *,
        layer: Layer,
        slot: Slot,
        highest: Slot | None,
    ) -> bool:
        if not self.config.stop_loss.protect_highest_retracement.enabled:
            return False
        if highest is None or highest.filled_entry is None or slot.filled_entry is None:
            return False
        protected_from = self.config.stop_loss.protect_highest_retracement.from_retracement
        if layer.retracement_count(slot) < protected_from:
            return False
        return highest is slot

    def _process_rebuilds(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballEvent]:
        if not self.config.stop_loss.enabled or not self.config.rebuild.enabled:
            return []
        events: list[SnowballEvent] = []
        for layer, slot in list(cycle.grid.filled_stop_loss_slots()):
            stop_loss_entry = slot.filled_stop_loss_entry
            if stop_loss_entry is None:
                continue
            if not self.pricing.rebuild_trigger_hit(
                stop_loss_entry=stop_loss_entry,
                direction=cycle.direction,
                tick=tick,
            ):
                continue
            raw_entry_price = self.pricing.entry_side_price(cycle.direction, tick)
            entry_price = self.grid_policy.clamp_entry_price(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                entry_price=raw_entry_price,
            )
            entry = self._create_entry(
                entry_id=cycle.next_entry_id(layer=layer, slot=slot),
                tick=tick,
                direction=cycle.direction,
                grid=cycle.grid,
                layer=layer,
                slot=slot,
                pip_size=pip_size,
                rebuild_source=stop_loss_entry,
                requested_entry_price=entry_price,
            )
            entry.planned_take_profit_price = self.grid_policy.clamp_take_profit(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                take_profit_price=entry.planned_take_profit_price,
            )
            self.grid_policy.propagate_pending_take_profit(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                take_profit_price=entry.planned_take_profit_price,
            )
            slot.complete_rebuild(entry)
            if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
                self.pricing.sync_weighted_average_take_profits(layer)
            events.append(
                self._open_event(
                    cycle=cycle,
                    layer=layer,
                    slot=slot,
                    entry=entry,
                    metadata=Metadata.of(is_rebuild=True),
                )
            )
        cycle.refresh_status()
        return events

    def _process_counter_adds(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballEvent]:
        events: list[SnowballEvent] = []
        max_adds = self.config.grid.max_layers * (self.config.grid.max_retracements_per_layer + 1)
        for _ in range(max_adds):
            event = self._try_add_one_counter_or_layer(
                cycle=cycle,
                tick=tick,
                pip_size=pip_size,
            )
            if event is None:
                break
            events.append(event)
        return events

    def _try_add_one_counter_or_layer(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> SnowballEvent | None:
        head = cycle.effective_head()
        if head is None:
            return None
        current_entry_price = self.pricing.entry_side_price(cycle.direction, tick)
        if not self._is_losing_reference(
            direction=cycle.direction,
            reference=head,
            current_entry_price=current_entry_price,
        ):
            return None

        layer = cycle.grid.current_layer
        slot = layer.next_available_counter_slot(
            max_refillable_retracement=self.config.grid.max_refillable_counter_retracement
        )
        if slot is not None:
            return self._try_open_counter(
                cycle=cycle,
                tick=tick,
                pip_size=pip_size,
                layer=layer,
                slot=slot,
                current_entry_price=current_entry_price,
            )
        if len(cycle.grid.layers) >= self.config.grid.max_layers:
            return None
        return self._try_open_next_layer(
            cycle=cycle,
            tick=tick,
            pip_size=pip_size,
            current_entry_price=current_entry_price,
        )

    def _try_open_counter(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
        layer: Layer,
        slot: Slot,
        current_entry_price: Money,
    ) -> SnowballOpenEvent | None:
        reference = self._counter_reference_price(
            layer=layer, retracement_count=layer.retracement_count(slot)
        )
        if reference is None:
            return None
        adverse = self.pricing.adverse_pips(
            direction=cycle.direction,
            reference_price=reference,
            current_entry_price=current_entry_price,
            pip_size=pip_size,
        )
        interval = self.calculator.counter_interval_pips(layer.retracement_count(slot))
        if adverse < interval:
            return None
        entry = self._create_counter_entry(
            cycle=cycle,
            tick=tick,
            pip_size=pip_size,
            layer=layer,
            slot=slot,
            head=cycle.head(),
        )
        slot.place_entry(entry)
        if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
            self.pricing.sync_weighted_average_take_profits(layer)
        cycle.refresh_status()
        return self._open_event(
            cycle=cycle,
            layer=layer,
            slot=slot,
            entry=entry,
            metadata=Metadata.of(
                expected_interval_pips=str(interval),
                actual_interval_pips=str(adverse),
            ),
        )

    def _create_counter_entry(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
        layer: Layer,
        slot: Slot,
        head: FilledEntry | None,
    ) -> RequestedEntry:
        entry = self._create_entry(
            entry_id=cycle.next_entry_id(layer=layer, slot=slot),
            tick=tick,
            direction=cycle.direction,
            grid=cycle.grid,
            layer=layer,
            slot=slot,
            pip_size=pip_size,
        )
        if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
            include_head = self._counter_weighted_average_head(layer=layer, cycle_head=head)
            entry.planned_take_profit_price = self.pricing.counter_take_profit_price(
                layer=layer,
                direction=cycle.direction,
                retracement_count=layer.retracement_count(slot),
                entry_price=entry.requested_entry_price,
                units=entry.requested_units,
                pip_size=pip_size,
                calculator=self.calculator,
                include_head=include_head,
            )
        return entry

    def _try_open_next_layer(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
        current_entry_price: Money,
    ) -> SnowballOpenEvent | None:
        tail = cycle.grid.tail_present_slot()
        if tail is None:
            return None
        tail_layer, tail_slot = tail
        reference = tail_slot.reference_entry_price()
        if reference is None:
            return None
        next_step = min(
            tail_layer.retracement_count(tail_slot) + 1,
            self.config.grid.max_retracements_per_layer,
        )
        adverse = self.pricing.adverse_pips(
            direction=cycle.direction,
            reference_price=reference,
            current_entry_price=current_entry_price,
            pip_size=pip_size,
        )
        interval = self.calculator.counter_interval_pips(next_step)
        if adverse < interval:
            return None

        previous_layer = cycle.grid.current_layer
        layer = cycle.grid.add_layer(
            base_units=self.config.sizing.layer_base_units(len(cycle.grid.layers) + 1),
            max_retracements=self.config.grid.max_retracements_per_layer,
        )
        slot = layer.r0
        entry_price = self.pricing.entry_side_price(cycle.direction, tick)
        requested_entry = RequestedEntry(
            entry_id=cycle.next_entry_id(layer=layer, slot=slot),
            requested_units=self.config.sizing.initial_entry_units_multiplier * layer.base_units,
            requested_entry_price=entry_price,
            requested_at=tick.timestamp,
            planned_take_profit_price=self.pricing.layer_initial_take_profit_price(
                new_price=entry_price,
                previous_layer=previous_layer,
                direction=cycle.direction,
                pip_size=pip_size,
                take_profit_pips=self.config.cycle.take_profit_pips,
            ),
            planned_stop_loss_price=self._stop_loss_price(
                direction=cycle.direction,
                entry_price=entry_price,
                retracement_count=layer.retracement_count(slot),
                pip_size=pip_size,
                rebuild_source=None,
            ),
        )
        slot.place_entry(requested_entry)
        cycle.refresh_status()
        return self._open_event(
            cycle=cycle,
            layer=layer,
            slot=slot,
            entry=requested_entry,
            metadata=Metadata.of(
                expected_interval_pips=str(interval),
                actual_interval_pips=str(adverse),
            ),
        )

    def _counter_reference_price(
        self,
        *,
        layer: Layer,
        retracement_count: int,
    ) -> Money | None:
        for index in range(retracement_count - 1, -1, -1):
            reference = layer.slot(index).reference_entry_price()
            if reference is not None:
                return reference
        return None

    def _is_losing_reference(
        self,
        *,
        direction: PositionSide,
        reference: FilledEntry,
        current_entry_price: Money,
    ) -> bool:
        if direction == PositionSide.LONG:
            return current_entry_price < reference.filled_entry_price
        return current_entry_price > reference.filled_entry_price

    def _handle_emergency(
        self,
        *,
        margin_ratio: Decimal,
    ) -> SnowballStopEvent | None:
        protection = self.config.protection
        if not protection.emergency_enabled or margin_ratio < protection.emergency_margin_percent:
            return None
        return SnowballStopEvent(
            message="Snowball emergency stop",
            metadata=Metadata.of(
                margin_ratio=str(margin_ratio),
                threshold=str(protection.emergency_margin_percent),
            ),
        )

    def _handle_shrink(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        pip_size: Decimal,
        account: AccountSnapshot,
    ) -> list[SnowballEvent]:
        protection = self.config.protection
        if (
            not protection.shrink_enabled
            or account.margin_ratio < protection.shrink_start_margin_percent
        ):
            return []

        events: list[SnowballEvent] = [
            SnowballStatusEvent(
                message="Snowball shrink entered",
                metadata=Metadata.of(margin_ratio=str(account.margin_ratio)),
            )
        ]
        current_account = account
        while current_account.margin_ratio >= protection.shrink_target_margin_percent:
            target = self._shrink_target(state=state, tick=tick, pip_size=pip_size)
            if target is None:
                events.append(
                    SnowballStopEvent(
                        message="Snowball shrink exhausted",
                        metadata=Metadata.of(margin_ratio=str(current_account.margin_ratio)),
                    )
                )
                return events
            cycle, layer, slot, entry = target
            exit_price = self.pricing.exit_side_price(cycle.direction, tick)
            realized = self.pricing.realized_pl(
                direction=cycle.direction,
                entry=entry,
                exit_price=exit_price,
            )
            slot.close_for_take_profit(closed_at=tick.timestamp, refillable=False)
            cycle.grid.remove_empty_top_layers()
            cycle.refresh_status()
            events.append(
                self._close_event(
                    cycle=cycle,
                    layer=layer,
                    slot=slot,
                    entry=entry,
                    price=exit_price,
                    close_reason=CloseReason.SHRINK,
                    metadata=Metadata.of(
                        realized_pl=str(realized),
                        margin_ratio=str(current_account.margin_ratio),
                        layer_number=cycle.grid.layer_number(layer),
                    ),
                )
            )
            current_account = self.accounting.evaluate(
                state=state,
                tick=tick,
                config=self.config,
            )

        return events

    def _shrink_target(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        pip_size: Decimal,
    ) -> tuple[Cycle, Layer, Slot, FilledEntry] | None:
        candidates: list[tuple[Decimal, Cycle, Layer, Slot, FilledEntry]] = []
        for cycle in state.active_cycles():
            entry = cycle.grid.shrink_front_entry()
            if entry is None or not self.pricing.can_close_on_tick(entry=entry, tick=tick):
                continue
            found = cycle.grid.find_entry_slot(entry)
            if found is None:
                continue
            layer, slot = found
            candidates.append(
                (
                    self.pricing.unrealized_loss_pips(
                        direction=cycle.direction,
                        entry=entry,
                        tick=tick,
                        pip_size=pip_size,
                    ),
                    cycle,
                    layer,
                    slot,
                    entry,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        _loss, cycle, layer, slot, entry = candidates[0]
        return cycle, layer, slot, entry

    def _reseed_directions(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballEvent]:
        events: list[SnowballEvent] = []
        for direction in self._managed_directions():
            has_active = any(
                cycle.direction == direction and cycle.active for cycle in state.cycles
            )
            has_pending = any(
                cycle.direction == direction and cycle.pending for cycle in state.cycles
            )
            if has_active:
                continue
            if has_pending and not self.config.cycle.reseed_when_all_positions_pending_rebuild:
                continue
            events.extend(
                self._open_cycle(state=state, tick=tick, direction=direction, pip_size=pip_size)
            )
        return events
