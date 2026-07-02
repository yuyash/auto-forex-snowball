"""Snowball tick-processing engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core import Money, PositionSide, Tick, new_uuid

from snowball.config import SnowballConfig
from snowball.enums import CloseReason, CounterTakeProfitMode, EntryRole
from snowball.intents import SnowballIntent
from snowball.models.entries import Entry, SlotExitPlan, SlotPosition, StopLossSnapshot
from snowball.models.grid import Grid, Layer, Slot
from snowball.models.state import Cycle, SnowballState
from snowball.services.accounting import AccountSnapshot, SnowballAccounting
from snowball.services.calculators import SnowballCalculator
from snowball.services.grid_policy import SnowballGridPolicy
from snowball.services.pricing import SnowballPricing


@dataclass(frozen=True, slots=True)
class SnowballStepResult:
    """Result of processing one market tick."""

    intents: tuple[SnowballIntent, ...]
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
        """Process a tick and return emitted Snowball intents."""
        intents: list[SnowballIntent] = []
        state.prune_completed_cycles()
        account = self.accounting.evaluate(state=state, tick=tick, config=self.config)

        emergency = self._handle_emergency(margin_ratio=account.margin_ratio)
        if emergency is not None:
            return SnowballStepResult(intents=(emergency,), state=state)

        shrink_intents = self._handle_shrink(
            state=state,
            tick=tick,
            pip_size=pip_size,
            account=account,
        )
        if shrink_intents:
            state.refresh_cycle_statuses()
            state.prune_completed_cycles()
            return SnowballStepResult(intents=tuple(shrink_intents), state=state)

        if not state.cycles:
            intents.extend(self._initialize_cycles(state=state, tick=tick, pip_size=pip_size))

        for cycle in list(state.cycles):
            if cycle.completed:
                continue
            intents.extend(self._process_cycle(cycle=cycle, tick=tick, pip_size=pip_size))

        intents.extend(self._reseed_directions(state=state, tick=tick, pip_size=pip_size))
        state.refresh_cycle_statuses()
        state.prune_completed_cycles()
        return SnowballStepResult(intents=tuple(intents), state=state)

    def _initialize_cycles(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballIntent]:
        intents: list[SnowballIntent] = []
        for direction in self._managed_directions():
            intents.extend(
                self._open_cycle(state=state, tick=tick, direction=direction, pip_size=pip_size)
            )
        return intents

    def _process_cycle(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballIntent]:
        intents: list[SnowballIntent] = []
        intents.extend(self._process_rebuilds(cycle=cycle, tick=tick, pip_size=pip_size))
        intents.extend(self._process_counter_take_profits(cycle=cycle, tick=tick))
        intents.extend(self._process_cycle_take_profit(cycle=cycle, tick=tick))
        intents.extend(self._process_stop_losses(cycle=cycle, tick=tick))
        intents.extend(self._process_rebuilds(cycle=cycle, tick=tick, pip_size=pip_size))
        if self.grid_policy.validate_ordering(cycle) is None:
            intents.extend(self._process_counter_adds(cycle=cycle, tick=tick, pip_size=pip_size))
        cycle.grid.remove_empty_top_layers()
        cycle.refresh_status()
        return intents

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
    ) -> list[SnowballIntent]:
        cycle = Cycle.create(
            cycle_id=new_uuid(),
            direction=direction,
            grid=self._new_grid(),
        )
        layer = cycle.grid.current_layer
        slot = layer.r0
        entry = self._create_entry(
            tick=tick,
            direction=direction,
            grid=cycle.grid,
            layer=layer,
            slot=slot,
            pip_size=pip_size,
        )
        entry, exit_plan = entry
        slot.fill(entry=entry, exit_plan=exit_plan)
        state.add_cycle(cycle)
        return [
            self._open_intent(
                cycle=cycle,
                layer=layer,
                slot=slot,
                entry=entry,
                exit_plan=exit_plan,
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
        tick: Tick,
        direction: PositionSide,
        grid: Grid,
        layer: Layer,
        slot: Slot,
        pip_size: Decimal,
        pending: StopLossSnapshot | None = None,
    ) -> tuple[Entry, SlotExitPlan]:
        role = grid.role_for(layer, slot)
        retracement_count = layer.retracement_count(slot)
        entry_price = self.pricing.entry_side_price(direction, tick)
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
        elif pending is not None:
            take_profit_price = self.pricing.rebuild_take_profit_price(
                pending=pending,
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

        entry = Entry(
            units=units,
            entry_price=entry_price,
            opened_at=tick.timestamp,
        )
        exit_plan = SlotExitPlan(
            take_profit_price=take_profit_price,
            stop_loss_price=self._stop_loss_price(
                direction=direction,
                entry_price=entry_price,
                retracement_count=retracement_count,
                pip_size=pip_size,
                pending=pending,
            ),
        )
        return entry, exit_plan

    def _stop_loss_price(
        self,
        *,
        direction: PositionSide,
        entry_price: Money,
        retracement_count: int,
        pip_size: Decimal,
        pending: StopLossSnapshot | None,
    ) -> Money | None:
        if not self.config.stop_loss.enabled:
            return None
        if pending is not None:
            return self.pricing.rebuild_stop_loss_price(
                pending=pending,
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

    def _open_intent(
        self,
        *,
        cycle: Cycle,
        layer: Layer,
        slot: Slot,
        entry: Entry,
        exit_plan: SlotExitPlan,
        metadata: dict[str, object] | None = None,
    ) -> SnowballIntent:
        """Create an open intent with a structure-derived entry key."""
        return SnowballIntent.open(
            cycle_id=cycle.cycle_id,
            direction=cycle.direction,
            entry=entry,
            slot_key=cycle.slot_key(layer=layer, slot=slot),
            exit_plan=exit_plan,
            metadata=metadata,
        )

    def _close_intent(
        self,
        *,
        cycle: Cycle,
        layer: Layer,
        slot: Slot,
        entry: Entry,
        exit_plan: SlotExitPlan,
        price: Money,
        close_reason: CloseReason,
        metadata: dict[str, object] | None = None,
    ) -> SnowballIntent:
        """Create a close intent with a structure-derived entry key."""
        return SnowballIntent.close(
            cycle_id=cycle.cycle_id,
            direction=cycle.direction,
            entry=entry,
            slot_key=cycle.slot_key(layer=layer, slot=slot),
            exit_plan=exit_plan,
            price=price,
            close_reason=close_reason,
            metadata=metadata,
        )

    def _require_exit_plan(self, slot: Slot) -> SlotExitPlan:
        """Return a live slot exit plan or raise for inconsistent state."""
        if slot.exit_plan is None:
            raise ValueError("slot has no live exit plan")
        return slot.exit_plan

    def _counter_weighted_average_head(
        self,
        *,
        layer: Layer,
        cycle_head: Entry | None,
    ) -> Entry | None:
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
    ) -> list[SnowballIntent]:
        intents: list[SnowballIntent] = []
        while True:
            candidate = self._next_counter_take_profit_candidate(cycle=cycle, tick=tick)
            if candidate is None:
                break
            layer, slot = candidate
            entry = slot.entry
            exit_plan = slot.exit_plan
            retracement_count = layer.retracement_count(slot)
            role = cycle.grid.role_for(layer, slot)
            if entry is None or exit_plan is None:
                break
            exit_price = self.pricing.exit_side_price(cycle.direction, tick)
            refillable_counter = (
                role == EntryRole.COUNTER
                and retracement_count <= self.config.grid.max_refillable_counter_retracement
            )
            slot.close_for_take_profit(
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
            intents.append(
                self._close_intent(
                    cycle=cycle,
                    layer=layer,
                    slot=slot,
                    entry=entry,
                    exit_plan=exit_plan,
                    price=exit_price,
                    close_reason=close_reason,
                    metadata={"realized_pl": str(realized)},
                )
            )
            cycle.grid.remove_empty_top_layers()
            cycle.refresh_status()
        return intents

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
                entry = slot.entry
                if entry is None or entry is head:
                    continue
                exit_plan = slot.exit_plan
                if exit_plan is None:
                    continue
                if cycle.grid.role_for(layer, slot) == EntryRole.LAYER_INITIAL and live_count > 1:
                    continue
                if self.pricing.take_profit_hit(
                    direction=cycle.direction,
                    entry=entry,
                    exit_plan=exit_plan,
                    tick=tick,
                ):
                    return layer, slot
        return None

    def _process_cycle_take_profit(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballIntent]:
        layer = cycle.grid.layers[0]
        slot = layer.r0
        entry = slot.entry
        exit_plan = slot.exit_plan
        if (
            entry is None
            or exit_plan is None
            or not self.pricing.take_profit_hit(
                direction=cycle.direction,
                entry=entry,
                exit_plan=exit_plan,
                tick=tick,
            )
        ):
            return []
        if cycle.counter_entries():
            return []

        exit_price = self.pricing.exit_side_price(cycle.direction, tick)
        slot.close_for_take_profit(refillable=False)
        realized = self.pricing.realized_pl(
            direction=cycle.direction,
            entry=entry,
            exit_price=exit_price,
        )
        cycle.refresh_status()
        return [
            self._close_intent(
                cycle=cycle,
                layer=layer,
                slot=slot,
                entry=entry,
                exit_plan=exit_plan,
                price=exit_price,
                close_reason=CloseReason.TAKE_PROFIT,
                metadata={"realized_pl": str(realized)},
            )
        ]

    def _process_stop_losses(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
    ) -> list[SnowballIntent]:
        if not self.config.stop_loss.enabled:
            return []
        intents: list[SnowballIntent] = []
        for layer in list(reversed(cycle.grid.layers)):
            highest = layer.highest_live_slot()
            for slot in list(reversed(layer.slots)):
                entry = slot.entry
                exit_plan = slot.exit_plan
                if (
                    entry is None
                    or exit_plan is None
                    or not self.pricing.stop_loss_hit(
                        direction=cycle.direction,
                        entry=entry,
                        exit_plan=exit_plan,
                        tick=tick,
                    )
                ):
                    continue
                if self._stop_loss_temporarily_protected(layer=layer, slot=slot, highest=highest):
                    continue
                exit_price = self.pricing.exit_side_price(cycle.direction, tick)
                realized = self.pricing.realized_pl(
                    direction=cycle.direction,
                    entry=entry,
                    exit_price=exit_price,
                )
                snapshot = (
                    StopLossSnapshot.from_entry(
                        entry,
                        exit_plan=exit_plan,
                        closed_at=tick.timestamp,
                        stop_loss_exit_price=exit_price,
                    )
                    if self.config.rebuild.enabled
                    else None
                )
                slot.close_for_stop_loss(snapshot)
                intents.append(
                    self._close_intent(
                        cycle=cycle,
                        layer=layer,
                        slot=slot,
                        entry=entry,
                        exit_plan=exit_plan,
                        price=exit_price,
                        close_reason=CloseReason.STOP_LOSS,
                        metadata={"realized_pl": str(realized)},
                    )
                )
        cycle.grid.remove_empty_top_layers()
        cycle.refresh_status()
        return intents

    def _stop_loss_temporarily_protected(
        self,
        *,
        layer: Layer,
        slot: Slot,
        highest: Slot | None,
    ) -> bool:
        if not self.config.stop_loss.protect_highest_retracement.enabled:
            return False
        if highest is None or highest.entry is None or slot.entry is None:
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
    ) -> list[SnowballIntent]:
        if not self.config.stop_loss.enabled or not self.config.rebuild.enabled:
            return []
        intents: list[SnowballIntent] = []
        for layer, slot in list(cycle.grid.pending_rebuild_slots()):
            pending = slot.pending_rebuild
            if pending is None:
                continue
            if not self.pricing.rebuild_trigger_hit(
                pending=pending,
                direction=cycle.direction,
                tick=tick,
                config=self.config,
                pip_size=pip_size,
            ):
                continue
            entry = self._create_entry(
                tick=tick,
                direction=cycle.direction,
                grid=cycle.grid,
                layer=layer,
                slot=slot,
                pip_size=pip_size,
                pending=pending,
            )
            entry, exit_plan = entry
            entry.entry_price = self.grid_policy.clamp_entry_price(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                entry_price=entry.entry_price,
            )
            exit_plan.take_profit_price = self.grid_policy.clamp_take_profit(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                take_profit_price=exit_plan.take_profit_price,
            )
            self.grid_policy.propagate_pending_take_profit(
                cycle=cycle,
                layer=layer,
                retracement_count=layer.retracement_count(slot),
                take_profit_price=exit_plan.take_profit_price,
            )
            slot.complete_rebuild(entry=entry, exit_plan=exit_plan)
            if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
                self.pricing.sync_weighted_average_take_profits(layer)
            intents.append(
                self._open_intent(
                    cycle=cycle,
                    layer=layer,
                    slot=slot,
                    entry=entry,
                    exit_plan=exit_plan,
                    metadata={"is_rebuild": True},
                )
            )
        cycle.refresh_status()
        return intents

    def _process_counter_adds(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> list[SnowballIntent]:
        intents: list[SnowballIntent] = []
        max_adds = self.config.grid.max_layers * (self.config.grid.max_retracements_per_layer + 1)
        for _ in range(max_adds):
            intent = self._try_add_one_counter_or_layer(
                cycle=cycle,
                tick=tick,
                pip_size=pip_size,
            )
            if intent is None:
                break
            intents.append(intent)
        return intents

    def _try_add_one_counter_or_layer(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
    ) -> SnowballIntent | None:
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
    ) -> SnowballIntent | None:
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
        head = cycle.effective_head()
        entry_head = head if isinstance(head, Entry) else None
        entry, exit_plan = self._create_counter_entry(
            cycle=cycle,
            tick=tick,
            pip_size=pip_size,
            layer=layer,
            slot=slot,
            head=entry_head,
        )
        slot.fill(entry=entry, exit_plan=exit_plan)
        if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
            self.pricing.sync_weighted_average_take_profits(layer)
        cycle.refresh_status()
        return self._open_intent(
            cycle=cycle,
            layer=layer,
            slot=slot,
            entry=entry,
            exit_plan=exit_plan,
            metadata={
                "expected_interval_pips": str(interval),
                "actual_interval_pips": str(adverse),
            },
        )

    def _create_counter_entry(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
        layer: Layer,
        slot: Slot,
        head: Entry | None,
    ) -> tuple[Entry, SlotExitPlan]:
        entry, exit_plan = self._create_entry(
            tick=tick,
            direction=cycle.direction,
            grid=cycle.grid,
            layer=layer,
            slot=slot,
            pip_size=pip_size,
        )
        if self.config.counter.take_profit.mode == CounterTakeProfitMode.WEIGHTED_AVG:
            include_head = self._counter_weighted_average_head(layer=layer, cycle_head=head)
            exit_plan.take_profit_price = self.pricing.counter_take_profit_price(
                layer=layer,
                direction=cycle.direction,
                retracement_count=layer.retracement_count(slot),
                entry_price=entry.entry_price,
                units=entry.units,
                pip_size=pip_size,
                calculator=self.calculator,
                include_head=include_head,
            )
        return entry, exit_plan

    def _try_open_next_layer(
        self,
        *,
        cycle: Cycle,
        tick: Tick,
        pip_size: Decimal,
        current_entry_price: Money,
    ) -> SnowballIntent | None:
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
        entry = Entry(
            units=self.config.sizing.initial_entry_units_multiplier * layer.base_units,
            entry_price=entry_price,
            opened_at=tick.timestamp,
        )
        exit_plan = SlotExitPlan(
            take_profit_price=self.pricing.layer_initial_take_profit_price(
                new_price=entry_price,
                previous_layer=previous_layer,
                direction=cycle.direction,
                pip_size=pip_size,
                take_profit_pips=self.config.cycle.take_profit_pips,
            ),
            stop_loss_price=self._stop_loss_price(
                direction=cycle.direction,
                entry_price=entry_price,
                retracement_count=layer.retracement_count(slot),
                pip_size=pip_size,
                pending=None,
            ),
        )
        slot.fill(entry=entry, exit_plan=exit_plan)
        cycle.refresh_status()
        return self._open_intent(
            cycle=cycle,
            layer=layer,
            slot=slot,
            entry=entry,
            exit_plan=exit_plan,
            metadata={
                "expected_interval_pips": str(interval),
                "actual_interval_pips": str(adverse),
            },
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
        reference: SlotPosition,
        current_entry_price: Money,
    ) -> bool:
        if direction == PositionSide.LONG:
            return current_entry_price < reference.entry_price
        return current_entry_price > reference.entry_price

    def _handle_emergency(
        self,
        *,
        margin_ratio: Decimal,
    ) -> SnowballIntent | None:
        protection = self.config.protection
        if not protection.emergency_enabled or margin_ratio < protection.emergency_margin_percent:
            return None
        return SnowballIntent.stop(
            message="Snowball emergency stop",
            metadata={
                "margin_ratio": str(margin_ratio),
                "threshold": str(protection.emergency_margin_percent),
            },
        )

    def _handle_shrink(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        pip_size: Decimal,
        account: AccountSnapshot,
    ) -> list[SnowballIntent]:
        protection = self.config.protection
        if (
            not protection.shrink_enabled
            or account.margin_ratio < protection.shrink_start_margin_percent
        ):
            return []

        intents: list[SnowballIntent] = [
            SnowballIntent.status(
                message="Snowball shrink entered",
                metadata={"margin_ratio": str(account.margin_ratio)},
            )
        ]
        current_account = account
        while current_account.margin_ratio >= protection.shrink_target_margin_percent:
            target = self._shrink_target(state=state, tick=tick, pip_size=pip_size)
            if target is None:
                intents.append(
                    SnowballIntent.stop(
                        message="Snowball shrink exhausted",
                        metadata={"margin_ratio": str(current_account.margin_ratio)},
                    )
                )
                return intents
            cycle, layer, slot, entry = target
            exit_plan = self._require_exit_plan(slot)
            exit_price = self.pricing.exit_side_price(cycle.direction, tick)
            realized = self.pricing.realized_pl(
                direction=cycle.direction,
                entry=entry,
                exit_price=exit_price,
            )
            slot.close_for_take_profit(refillable=False)
            cycle.grid.remove_empty_top_layers()
            cycle.refresh_status()
            intents.append(
                self._close_intent(
                    cycle=cycle,
                    layer=layer,
                    slot=slot,
                    entry=entry,
                    exit_plan=exit_plan,
                    price=exit_price,
                    close_reason=CloseReason.SHRINK,
                    metadata={
                        "realized_pl": str(realized),
                        "margin_ratio": str(current_account.margin_ratio),
                        "layer_number": cycle.grid.layer_number(layer),
                    },
                )
            )
            current_account = self.accounting.evaluate(
                state=state,
                tick=tick,
                config=self.config,
            )

        return intents

    def _shrink_target(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        pip_size: Decimal,
    ) -> tuple[Cycle, Layer, Slot, Entry] | None:
        candidates: list[tuple[Decimal, Cycle, Layer, Slot, Entry]] = []
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
    ) -> list[SnowballIntent]:
        intents: list[SnowballIntent] = []
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
            intents.extend(
                self._open_cycle(state=state, tick=tick, direction=direction, pip_size=pip_size)
            )
        return intents
