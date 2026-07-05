"""Accounting calculations for Snowball protection logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from core import Money, Tick

from snowball.config import SnowballConfig
from snowball.models.state import SnowballState
from snowball.services.market_pricing import SnowballMarketPricing


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Derived account values for one market tick."""

    balance: Money
    nav: Money
    margin_ratio: Decimal


@dataclass(frozen=True, slots=True)
class SnowballAccounting:
    """Compute NAV and margin ratio from Snowball state."""

    pricing: SnowballMarketPricing = field(default_factory=SnowballMarketPricing)

    def evaluate(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        config: SnowballConfig,
    ) -> AccountSnapshot:
        """Return account NAV and margin ratio for one tick."""
        balance = config.account.balance
        unrealized = Money.of("0", balance.currency)
        for cycle in state.cycles:
            for entry in cycle.live_entries():
                entry_unrealized = self.pricing.unrealized_pl(
                    direction=cycle.direction,
                    entry=entry,
                    tick=tick,
                )
                unrealized += Money.of(
                    entry_unrealized.amount * config.account.quote_to_account_rate,
                    balance.currency,
                )
        nav = balance + unrealized
        return AccountSnapshot(
            balance=balance,
            nav=nav,
            margin_ratio=self.margin_ratio(state=state, tick=tick, nav=nav, config=config),
        )

    def margin_ratio(
        self,
        *,
        state: SnowballState,
        tick: Tick,
        nav: Money,
        config: SnowballConfig,
    ) -> Decimal:
        """Return required margin / NAV as a percentage."""
        mid = tick.effective_mid
        if nav.amount <= 0 or mid.amount <= 0:
            return Decimal("0")
        long_units, short_units = state.live_units_by_direction()
        required_units = max(long_units, short_units)
        if required_units <= 0:
            return Decimal("0")
        required_margin = (
            mid.amount
            * required_units
            * config.account.margin_rate
            * config.account.quote_to_account_rate
        )
        return (required_margin / nav.amount) * Decimal("100")
