"""Configuration for the Snowball strategy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Self

from core import Currency, MarginRate, Money, Percent, Pips, StrategyParameters, Units

from snowball.config_parsing import (
    bool_value,
    decimal_value,
    enum_value,
    int_value,
    nested,
    require_manual_length,
    require_positive,
)
from snowball.config_parsing import (
    changes as parse_changes,
)
from snowball.config_serialization import serialize_config
from snowball.enums import (
    CounterTakeProfitMode,
    IntervalMode,
    RebuildEntryPriceMode,
    RebuildStopLossMode,
    RebuildTakeProfitMode,
    StopLossMode,
)


@dataclass(frozen=True, slots=True)
class PipProgressionConfig:
    """Pip-distance progression from a head value to a tail value."""

    mode: IntervalMode = IntervalMode.CONSTANT
    head_pips: Pips = field(default_factory=lambda: Pips("30"))
    tail_pips: Pips = field(default_factory=lambda: Pips("14"))
    flat_steps: int = 2
    gamma: Decimal = Decimal("1.4")
    manual_pips: tuple[Pips, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        *,
        default: PipProgressionConfig | None = None,
    ) -> PipProgressionConfig:
        """Build a pip progression config from nested values."""
        config = default or cls()
        if not values:
            return config
        changes = parse_changes(
            values,
            mode=lambda value: enum_value(IntervalMode, value),
            head_pips=Pips.of,
            tail_pips=Pips.of,
            flat_steps=int_value,
            gamma=decimal_value,
            manual_pips=Pips.tuple_of,
        )
        return replace(config, **changes)

    def validate(self, *, manual_minimum: int | None = None, name: str) -> None:
        """Validate this progression."""
        require_positive(self.head_pips, f"{name}.head_pips")
        require_positive(self.tail_pips, f"{name}.tail_pips")
        require_positive(self.gamma, f"{name}.gamma")
        if self.flat_steps < 0:
            raise ValueError(f"{name}.flat_steps must not be negative")
        if self.mode == IntervalMode.MANUAL:
            if manual_minimum is None:
                raise ValueError(f"{name}.manual_pips minimum length is not configured")
            require_manual_length(self.manual_pips, manual_minimum, f"{name}.manual_pips")


@dataclass(frozen=True, slots=True)
class PositionSizingConfig:
    """Unit sizing for Snowball entries."""

    base_units: Units = field(default_factory=lambda: Units("1000"))
    initial_entry_units_multiplier: Decimal = Decimal("1")
    additional_layer_base_units_multiplier: Decimal = Decimal("1")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> PositionSizingConfig:
        """Build sizing config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                base_units=Units.of,
                initial_entry_units_multiplier=decimal_value,
                additional_layer_base_units_multiplier=decimal_value,
            ),
        )

    def validate(self) -> None:
        """Validate sizing values."""
        require_positive(self.base_units, "sizing.base_units")
        require_positive(
            self.initial_entry_units_multiplier,
            "sizing.initial_entry_units_multiplier",
        )
        require_positive(
            self.additional_layer_base_units_multiplier,
            "sizing.additional_layer_base_units_multiplier",
        )

    def layer_base_units(self, layer_number: int) -> Units:
        """Return the base units for a layer."""
        if layer_number <= 1:
            return self.base_units
        return Units.of(self.base_units * self.additional_layer_base_units_multiplier)

    @property
    def initial_entry_units(self) -> Units:
        """Return units for initial and layer-initial entries."""
        return Units.of(self.base_units * self.initial_entry_units_multiplier)


@dataclass(frozen=True, slots=True)
class SlotRefillConfig:
    """Counter-slot reuse policy after take-profit closes."""

    enabled: bool = True
    max_reusable_retracement: int = 1

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> SlotRefillConfig:
        """Build refill config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                enabled=bool_value,
                max_reusable_retracement=int_value,
            ),
        )


@dataclass(frozen=True, slots=True)
class GridConfig:
    """Layer and retracement limits for the Snowball grid."""

    max_retracements_per_layer: int = 7
    max_layers: int = 3
    refill: SlotRefillConfig = SlotRefillConfig()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> GridConfig:
        """Build grid config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                max_retracements_per_layer=int_value,
                max_layers=int_value,
                refill=SlotRefillConfig.from_mapping,
            ),
        )

    def validate(self) -> None:
        """Validate grid limits."""
        if self.max_retracements_per_layer < 1:
            raise ValueError("grid.max_retracements_per_layer must be at least 1")
        if self.max_layers < 1:
            raise ValueError("grid.max_layers must be at least 1")
        if (
            self.refill.max_reusable_retracement < 0
            or self.refill.max_reusable_retracement > self.max_retracements_per_layer
        ):
            raise ValueError(
                "grid.refill.max_reusable_retracement must be between 0 and "
                "grid.max_retracements_per_layer"
            )

    @property
    def max_refillable_counter_retracement(self) -> int:
        """Return the highest counter R index that may be reused."""
        if not self.refill.enabled:
            return self.max_retracements_per_layer
        return self.refill.max_reusable_retracement


@dataclass(frozen=True, slots=True)
class ForwardConfig:
    """Forward-direction entry behavior."""

    take_profit_pips: Pips = field(default_factory=lambda: Pips("50"))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ForwardConfig:
        """Build forward-direction config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                take_profit_pips=Pips.of,
            ),
        )

    def validate(self) -> None:
        """Validate forward-direction values."""
        require_positive(self.take_profit_pips, "forward.take_profit_pips")


@dataclass(frozen=True, slots=True)
class CycleConfig:
    """Cycle-level Snowball behavior."""

    hedging_enabled: bool = True
    reseed_when_all_positions_pending_rebuild: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> CycleConfig:
        """Build cycle config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                hedging_enabled=bool_value,
                reseed_when_all_positions_pending_rebuild=bool_value,
            ),
        )


@dataclass(frozen=True, slots=True)
class CounterTakeProfitConfig:
    """Take-profit policy for R1+ counter entries."""

    mode: CounterTakeProfitMode = CounterTakeProfitMode.WEIGHTED_AVG
    fixed_pips: Pips = field(default_factory=lambda: Pips("5"))
    step_pips: Pips = field(default_factory=lambda: Pips("1"))
    multiplier: Decimal = Decimal("1.2")

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
    ) -> CounterTakeProfitConfig:
        """Build counter take-profit config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                mode=lambda value: enum_value(CounterTakeProfitMode, value),
                fixed_pips=Pips.of,
                step_pips=Pips.of,
                multiplier=decimal_value,
            ),
        )

    def validate(self) -> None:
        """Validate counter take-profit values."""
        require_positive(self.fixed_pips, "counter.take_profit.fixed_pips")
        require_positive(self.step_pips, "counter.take_profit.step_pips")
        require_positive(self.multiplier, "counter.take_profit.multiplier")


@dataclass(frozen=True, slots=True)
class CounterConfig:
    """Counter-entry add and take-profit settings."""

    interval: PipProgressionConfig = PipProgressionConfig()
    take_profit: CounterTakeProfitConfig = CounterTakeProfitConfig()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> CounterConfig:
        """Build counter config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                interval=PipProgressionConfig.from_mapping,
                take_profit=CounterTakeProfitConfig.from_mapping,
            ),
        )

    def validate(self, *, max_retracements_per_layer: int) -> None:
        """Validate counter config."""
        self.interval.validate(
            manual_minimum=max_retracements_per_layer,
            name="counter.interval",
        )
        self.take_profit.validate()


@dataclass(frozen=True, slots=True)
class StopLossProtectionConfig:
    """Temporary stop-loss suppression for a layer's highest R."""

    enabled: bool = False
    from_retracement: int = 1

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> StopLossProtectionConfig:
        """Build stop-loss protection config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                enabled=bool_value,
                from_retracement=int_value,
            ),
        )


@dataclass(frozen=True, slots=True)
class StopLossConfig:
    """Stop-loss placement for live Snowball entries."""

    enabled: bool = False
    mode: StopLossMode = StopLossMode.AUTO
    distance: PipProgressionConfig = PipProgressionConfig(
        mode=IntervalMode.CONSTANT,
        head_pips=Pips("50"),
        tail_pips=Pips("20"),
        flat_steps=0,
    )
    protect_highest_retracement: StopLossProtectionConfig = StopLossProtectionConfig()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> StopLossConfig:
        """Build stop-loss config from nested values."""
        config = cls()
        if not values:
            return config
        changes = parse_changes(
            values,
            enabled=bool_value,
            mode=lambda value: enum_value(StopLossMode, value),
            distance=lambda value: PipProgressionConfig.from_mapping(
                value,
                default=config.distance,
            ),
            protect_highest_retracement=StopLossProtectionConfig.from_mapping,
        )
        if "mode" not in values and "distance" in values:
            changes["mode"] = StopLossMode.DISTANCE
        return replace(config, **changes)

    def validate(self, *, max_retracements_per_layer: int) -> None:
        """Validate stop-loss config."""
        if self.mode == StopLossMode.DISTANCE:
            self.distance.validate(
                manual_minimum=max_retracements_per_layer + 1,
                name="stop_loss.distance",
            )
        if self.protect_highest_retracement.from_retracement < 1:
            raise ValueError("stop_loss.protect_highest_retracement.from_retracement must be >= 1")


@dataclass(frozen=True, slots=True)
class RebuildPriceConfig:
    """Planned rebuild price policy for stopped slots waiting for rebuild."""

    entry_price_mode: RebuildEntryPriceMode = RebuildEntryPriceMode.STOP_LOSS_EXIT_PRICE
    buffer_pips: Pips = field(default_factory=lambda: Pips("0"))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> RebuildPriceConfig:
        """Build rebuild price config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                entry_price_mode=lambda value: enum_value(RebuildEntryPriceMode, value),
                buffer_pips=Pips.of,
            ),
        )


@dataclass(frozen=True, slots=True)
class RebuildStopLossConfig:
    """Stop-loss policy for rebuilt entries."""

    mode: RebuildStopLossMode = RebuildStopLossMode.SAME_DISTANCE
    manual_distances_pips: tuple[Pips, ...] = ()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> RebuildStopLossConfig:
        """Build rebuild stop-loss config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                mode=lambda value: enum_value(RebuildStopLossMode, value),
                manual_distances_pips=Pips.tuple_of,
            ),
        )

    def validate(self, *, max_retracements_per_layer: int) -> None:
        """Validate rebuild stop-loss config."""
        if self.mode == RebuildStopLossMode.MANUAL_DISTANCE:
            require_manual_length(
                self.manual_distances_pips,
                max_retracements_per_layer + 1,
                "rebuild.stop_loss.manual_distances_pips",
            )


@dataclass(frozen=True, slots=True)
class RebuildTakeProfitConfig:
    """Take-profit policy for rebuilt entries."""

    mode: RebuildTakeProfitMode = RebuildTakeProfitMode.SAME_DISTANCE
    distance: PipProgressionConfig = PipProgressionConfig(
        mode=IntervalMode.ADDITIVE,
        head_pips=Pips("25"),
        tail_pips=Pips("10"),
        flat_steps=0,
    )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> RebuildTakeProfitConfig:
        """Build rebuild take-profit config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                mode=lambda value: enum_value(RebuildTakeProfitMode, value),
                distance=lambda value: PipProgressionConfig.from_mapping(
                    value,
                    default=config.distance,
                ),
            ),
        )

    def validate(self, *, max_retracements_per_layer: int) -> None:
        """Validate rebuild take-profit config."""
        if self.mode == RebuildTakeProfitMode.PROGRESSIVE_DISTANCE:
            self.distance.validate(
                manual_minimum=max_retracements_per_layer + 1,
                name="rebuild.take_profit.distance",
            )


@dataclass(frozen=True, slots=True)
class RebuildConfig:
    """Rebuild behavior for stop-loss-closed slots."""

    enabled: bool = True
    price: RebuildPriceConfig = RebuildPriceConfig()
    stop_loss: RebuildStopLossConfig = RebuildStopLossConfig()
    take_profit: RebuildTakeProfitConfig = RebuildTakeProfitConfig()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> RebuildConfig:
        """Build rebuild config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                enabled=bool_value,
                price=RebuildPriceConfig.from_mapping,
                stop_loss=RebuildStopLossConfig.from_mapping,
                take_profit=RebuildTakeProfitConfig.from_mapping,
            ),
        )

    def validate(self, *, max_retracements_per_layer: int) -> None:
        """Validate rebuild config."""
        if self.price.buffer_pips < 0:
            raise ValueError("rebuild.price.buffer_pips must not be negative")
        self.stop_loss.validate(max_retracements_per_layer=max_retracements_per_layer)
        self.take_profit.validate(max_retracements_per_layer=max_retracements_per_layer)


@dataclass(frozen=True, slots=True)
class ProtectionConfig:
    """Margin protection behavior."""

    shrink_enabled: bool = False
    shrink_start_margin_percent: Percent = field(default_factory=lambda: Percent("70"))
    shrink_target_margin_percent: Percent = field(default_factory=lambda: Percent("50"))
    emergency_enabled: bool = True
    emergency_margin_percent: Percent = field(default_factory=lambda: Percent("95"))

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> ProtectionConfig:
        """Build protection config from nested values."""
        config = cls()
        if not values:
            return config
        return replace(
            config,
            **parse_changes(
                values,
                shrink_enabled=bool_value,
                shrink_start_margin_percent=Percent.of,
                shrink_target_margin_percent=Percent.of,
                emergency_enabled=bool_value,
                emergency_margin_percent=Percent.of,
            ),
        )

    def validate(self) -> None:
        """Validate protection thresholds."""
        if not (
            Decimal("0")
            < self.shrink_target_margin_percent
            < self.shrink_start_margin_percent
            < Decimal("100")
        ):
            raise ValueError(
                "protection margin thresholds must satisfy "
                "0 < shrink_target_margin_percent < shrink_start_margin_percent < 100"
            )
        if not Decimal("0") < self.emergency_margin_percent <= Decimal("100"):
            raise ValueError("protection.emergency_margin_percent must satisfy 0 < value <= 100")


@dataclass(frozen=True, slots=True)
class AccountValuationConfig:
    """Account and margin inputs used for strategy-side protection estimates."""

    currency: Currency = field(default_factory=lambda: Currency.of("USD"))
    balance: Money = field(default_factory=lambda: Money.of("10000", "USD"))
    margin_rate: MarginRate = field(default_factory=lambda: MarginRate("0.04"))
    quote_to_account_rate: Decimal = Decimal("1")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> AccountValuationConfig:
        """Build account valuation config from nested values."""
        config = cls()
        if not values:
            return config
        currency = Currency.of(values["currency"]) if "currency" in values else config.currency
        balance = (
            Money.coerce_positive(values["balance"], currency)
            if "balance" in values
            else Money.of(config.balance.amount, currency)
        )
        return replace(
            config,
            currency=currency,
            balance=balance,
            **parse_changes(
                values,
                margin_rate=MarginRate.of,
                quote_to_account_rate=decimal_value,
            ),
        )

    def validate(self) -> None:
        """Validate account valuation inputs."""
        self.balance.require_currency(self.currency).require_positive()
        require_positive(self.margin_rate, "account.margin_rate")
        require_positive(self.quote_to_account_rate, "account.quote_to_account_rate")


@dataclass(frozen=True, slots=True)
class SnowballConfig:
    """Normal Snowball strategy configuration."""

    sizing: PositionSizingConfig = PositionSizingConfig()
    grid: GridConfig = GridConfig()
    cycle: CycleConfig = CycleConfig()
    forward: ForwardConfig = ForwardConfig()
    counter: CounterConfig = CounterConfig()
    stop_loss: StopLossConfig = StopLossConfig()
    rebuild: RebuildConfig = RebuildConfig()
    protection: ProtectionConfig = ProtectionConfig()
    account: AccountValuationConfig = AccountValuationConfig()

    @classmethod
    def from_parameters(
        cls,
        parameters: StrategyParameters | None,
    ) -> SnowballConfig:
        """Build a validated config from nested strategy parameters."""
        if parameters is None:
            return cls().validate()
        values = parameters.to_dict()
        config = cls(
            sizing=PositionSizingConfig.from_mapping(nested(values, "sizing")),
            grid=GridConfig.from_mapping(nested(values, "grid")),
            cycle=CycleConfig.from_mapping(nested(values, "cycle")),
            forward=ForwardConfig.from_mapping(nested(values, "forward")),
            counter=CounterConfig.from_mapping(nested(values, "counter")),
            stop_loss=StopLossConfig.from_mapping(nested(values, "stop_loss")),
            rebuild=RebuildConfig.from_mapping(nested(values, "rebuild")),
            protection=ProtectionConfig.from_mapping(nested(values, "protection")),
            account=AccountValuationConfig.from_mapping(nested(values, "account")),
        )
        return config.validate()

    def validate(self) -> Self:
        """Return self when the configuration is internally consistent."""
        self.sizing.validate()
        self.grid.validate()
        self.forward.validate()
        self.counter.validate(
            max_retracements_per_layer=self.grid.max_retracements_per_layer,
        )
        self.stop_loss.validate(
            max_retracements_per_layer=self.grid.max_retracements_per_layer,
        )
        self.rebuild.validate(
            max_retracements_per_layer=self.grid.max_retracements_per_layer,
        )
        self.protection.validate()
        self.account.validate()
        if self.stop_loss.enabled and self.protection.shrink_enabled:
            raise ValueError("stop_loss.enabled and protection.shrink_enabled cannot both be true")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return nested normalized values suitable for StrategyParameters."""
        return serialize_config(self)
