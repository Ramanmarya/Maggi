"""
BrokerAdapter — the interface every engine (regime, ladder, put, call, risk,
delta) depends on instead of talking to Alpaca/Polygon directly. This is
what lets run_live.py and run_backtest.py exercise identical strategy logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol


@dataclass(frozen=True)
class OptionContract:
    symbol: str  # OCC-style symbol
    underlying: str
    expiry: date
    strike: float
    option_type: Literal["call", "put"]
    bid: float
    ask: float
    delta: float | None
    implied_vol: float | None
    open_interest: int | None = None


@dataclass(frozen=True)
class DividendEvent:
    ex_date: date
    pay_date: date
    amount_per_share: float


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: float  # shares, or contracts (negative = short) for options
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    asset_class: Literal["equity", "option"]


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: float
    cash: float
    buying_power: float
    positions: list[PositionSnapshot]


@dataclass(frozen=True)
class VerticalSpreadOrder:
    underlying: str
    short_leg: OptionContract
    long_leg: OptionContract
    contracts: int
    limit_net_credit: float | None  # None => market/mid, not recommended
    client_order_id: str


@dataclass(frozen=True)
class SingleLegOrder:
    contract: OptionContract | None  # None for underlying share orders
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    order_type: Literal["market", "limit"]
    limit_price: float | None
    client_order_id: str


@dataclass(frozen=True)
class OrderResult:
    success: bool
    order_id: str | None
    filled_avg_price: float | None
    status: str
    raw: dict | None = None
    error: str | None = None


class BrokerAdapter(Protocol):
    def today(self) -> date:
        """The date the strategy should reason about.

        Live this is the wall clock; in a backtest it is the session being
        replayed. Reading the system clock inside the engines instead makes
        every historical run compute DTE against the present day.
        """
        ...

    def is_market_open(self) -> bool: ...

    def get_underlying_price(self) -> float: ...

    def get_atr(self, period: int = 20) -> float: ...

    def get_200dma(self) -> tuple[float, float]:
        """Returns (dma_value, slope_per_day)."""
        ...

    def get_option_chain(self, dte_range: tuple[int, int]) -> list[OptionContract]: ...

    def option_delta(self, symbol: str) -> float | None:
        """Per-contract delta for one option, or None if unavailable.

        Needed because option exposure cannot be inferred from contract count:
        a short 20-delta put is bullish, not equivalent to being short 100
        shares.
        """
        ...

    def option_mark(self, symbol: str) -> float | None:
        """Current mid for one contract, or None if unavailable.

        Needed to know what an open spread is worth now, which is what a
        profit-capture rule requires. Without it the only exit is time.
        """
        ...

    def get_current_positions(self) -> PortfolioSnapshot: ...

    def submit_vertical_spread(self, spread: VerticalSpreadOrder) -> OrderResult: ...

    def submit_single_leg(self, order: SingleLegOrder) -> OrderResult: ...

    def close_position(self, position_id: str, limit_pct: float | None) -> OrderResult: ...

    def get_dividend_calendar(self) -> list[DividendEvent]: ...

    def unit_multiplier(self) -> float:
        """Shares (or notional units) per 'unit'. QQQ: 100 (1 option contract's worth)."""
        ...
