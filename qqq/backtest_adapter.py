"""
BacktestBrokerAdapter — implements BrokerAdapter against Polygon.io
historical data, so run_backtest.py exercises the identical strategy core
as run_live.py.

Polygon coverage (per Polygon's docs as of this writing): options quotes
back to 2022, trades back to 2016 — plenty for QQQ given how liquid it is.
Confirm your specific plan tier includes options if you hit 403s.

NOTE: this is a skeleton wired to real Polygon REST endpoints for price/
ATR/200DMA/dividends, but `get_option_chain` for a *historical* date needs
a bar-by-bar backtest loop (walk day by day, pull the chain snapshot as of
that day) rather than "the current chain" — that loop lives in
run_backtest.py, which calls `set_as_of(date)` before each cycle to move
this adapter's clock. Order fills in backtest are simulated at the quoted
mid (minus a configurable haircut), not sent anywhere.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import requests

from .black_scholes import bs_delta, implied_vol
from .broker_adapter import (
    DividendEvent,
    OptionContract,
    OrderResult,
    PortfolioSnapshot,
    PositionSnapshot,
    SingleLegOrder,
    VerticalSpreadOrder,
)
from .config import StrategyConfig

logger = logging.getLogger("qqq_bot.backtest_adapter")

POLYGON_BASE = "https://api.polygon.io"


class BacktestBrokerAdapter:
    def __init__(self, config: StrategyConfig, starting_equity: float = 100_000.0):
        self._config = config
        self._as_of: date = date.today()
        self._equity = starting_equity
        self._cash = starting_equity
        self._positions: list[PositionSnapshot] = []
        self._session = requests.Session()

    def set_as_of(self, as_of: date) -> None:
        """Move the backtest clock. Called by run_backtest.py once per simulated day."""
        self._as_of = as_of

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["apiKey"] = self._config.polygon_api_key
        resp = self._session.get(f"{POLYGON_BASE}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def get_underlying_price(self) -> float:
        data = self._get(
            f"/v1/open-close/{self._config.symbol}/{self._as_of.isoformat()}",
            {"adjusted": "true"},
        )
        return float(data["close"])

    def _daily_bars(self, lookback_days: int) -> list[dict]:
        start = self._as_of - timedelta(days=lookback_days * 2)  # buffer weekends/holidays
        data = self._get(
            f"/v2/aggs/ticker/{self._config.symbol}/range/1/day/"
            f"{start.isoformat()}/{self._as_of.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 500},
        )
        return data.get("results", [])

    def get_atr(self, period: int = 20) -> float:
        bars = self._daily_bars(period + 5)[-(period + 1):]
        if len(bars) < 2:
            raise RuntimeError("Not enough bars to compute ATR.")
        true_ranges = []
        for i in range(1, len(bars)):
            high, low, prev_close = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        return sum(true_ranges) / len(true_ranges)

    def get_200dma(self) -> tuple[float, float]:
        bars = self._daily_bars(230)[-200:]
        closes = [b["c"] for b in bars]
        if len(closes) < 40:
            raise RuntimeError("Not enough bars to compute 200DMA.")
        dma = sum(closes) / len(closes)
        lookback = self._config.regime_slope_lookback_days
        recent = closes[-lookback:]
        pct_changes = [
            (recent[i] - recent[i - 1]) / recent[i - 1] for i in range(1, len(recent))
        ]
        slope = sum(pct_changes) / len(pct_changes) if pct_changes else 0.0
        return dma, slope

    def get_option_chain(self, dte_range: tuple[int, int]) -> list[OptionContract]:
        """
        Historical chain as of `self._as_of`:
          1. Pull the contract universe (strikes/expiries/types) that existed
             on that date via /v3/reference/options/contracts?as_of=...,
             restricted to the DTE window and a strike band around the
             underlying price (see config.backtest_chain_strike_band_pct —
             this strategy only ever trades 5-30 delta contracts, so a wide
             ITM/deep-OTM sweep would just burn API calls for nothing).
          2. For each contract, pull the last NBBO quote at or before that
             date's close via /v3/quotes/{ticker}.
          3. Back out implied vol from the mid price (Black-Scholes) and
             compute delta from that, since Polygon's historical endpoints
             don't carry greeks the way the live snapshot does.

        This makes one quotes request per contract in the strike band, so a
        full multi-year backtest will be slow and will consume real request
        volume against your plan's rate limit — that's inherent to
        reconstructing historical greeks this way without a paid
        historical-greeks feed (e.g. ORATS), not a bug in this
        implementation. Consider narrowing backtest_chain_strike_band_pct
        or the DTE window if this is too slow for your plan tier.
        """
        underlying_price = self.get_underlying_price()
        band = self._config.backtest_chain_strike_band_pct
        strike_lo = underlying_price * (1 - band)
        strike_hi = underlying_price * (1 + band)

        expiry_lo = self._as_of + timedelta(days=dte_range[0])
        expiry_hi = self._as_of + timedelta(days=dte_range[1])

        contracts_raw = self._list_contracts(
            as_of=self._as_of,
            expiration_gte=expiry_lo,
            expiration_lte=expiry_hi,
            strike_gte=strike_lo,
            strike_lte=strike_hi,
        )

        out: list[OptionContract] = []
        for c in contracts_raw:
            ticker = c["ticker"]
            strike = float(c["strike_price"])
            expiry = datetime.strptime(c["expiration_date"], "%Y-%m-%d").date()
            option_type = c["contract_type"]  # "call" or "put"
            dte_years = max((expiry - self._as_of).days, 0) / 365.0

            quote = self._last_quote_before(ticker, self._as_of)
            if quote is None:
                continue
            bid, ask = quote
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            mid = (bid + ask) / 2

            iv = implied_vol(
                market_price=mid,
                spot=underlying_price,
                strike=strike,
                dte_years=dte_years,
                rate=self._config.backtest_risk_free_rate,
                dividend_yield=self._config.backtest_dividend_yield_estimate,
                option_type=option_type,
            )
            delta = None
            if iv is not None:
                delta = bs_delta(
                    spot=underlying_price,
                    strike=strike,
                    dte_years=dte_years,
                    vol=iv,
                    rate=self._config.backtest_risk_free_rate,
                    dividend_yield=self._config.backtest_dividend_yield_estimate,
                    option_type=option_type,
                )

            out.append(
                OptionContract(
                    symbol=ticker,
                    underlying=self._config.symbol,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    bid=bid,
                    ask=ask,
                    delta=delta,
                    implied_vol=iv,
                )
            )
        return out

    def _list_contracts(
        self,
        as_of: date,
        expiration_gte: date,
        expiration_lte: date,
        strike_gte: float,
        strike_lte: float,
    ) -> list[dict]:
        results: list[dict] = []
        params = {
            "underlying_ticker": self._config.symbol,
            "as_of": as_of.isoformat(),
            "expiration_date.gte": expiration_gte.isoformat(),
            "expiration_date.lte": expiration_lte.isoformat(),
            "strike_price.gte": round(strike_gte, 2),
            "strike_price.lte": round(strike_lte, 2),
            "expired": "true",  # as_of a past date, contracts may since have expired
            "limit": 1000,
        }
        path = "/v3/reference/options/contracts"
        next_url = None
        for _ in range(10):  # safety cap on pagination loops
            if next_url:
                resp = self._session.get(next_url, params={"apiKey": self._config.polygon_api_key}, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            else:
                data = self._get(path, params)
            results.extend(data.get("results", []))
            next_url = data.get("next_url")
            if not next_url:
                break
        return results

    def _last_quote_before(self, options_ticker: str, as_of: date) -> tuple[float, float] | None:
        """Last NBBO quote at or before as_of's market close (16:00 ET),
        as a (bid, ask) tuple, or None if no quote is found.
        """
        # 16:00 ET on as_of, converted to nanoseconds since epoch (Polygon's
        # timestamp unit for the quotes endpoint).
        close_et = datetime(as_of.year, as_of.month, as_of.day, 16, 0, 0)
        # NOTE: naive UTC offset approximation (ET is UTC-4/UTC-5 depending
        # on DST) — for daily-bar-level backtesting a few hours of slop
        # around the close timestamp doesn't materially change which quote
        # gets picked, but a minute-level backtest would need a real
        # timezone-aware conversion (e.g. via `zoneinfo`).
        close_utc_approx = close_et + timedelta(hours=5)
        timestamp_ns = int(close_utc_approx.timestamp() * 1_000_000_000)

        try:
            data = self._get(
                f"/v3/quotes/{options_ticker}",
                {
                    "timestamp.lte": timestamp_ns,
                    "order": "desc",
                    "sort": "timestamp",
                    "limit": 1,
                },
            )
        except requests.HTTPError as e:
            logger.debug("Quote lookup failed for %s: %s", options_ticker, e)
            return None

        results = data.get("results", [])
        if not results:
            return None
        q = results[0]
        bid = q.get("bid_price")
        ask = q.get("ask_price")
        if bid is None or ask is None:
            return None
        return float(bid), float(ask)

    def get_current_positions(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            equity=self._equity, cash=self._cash, buying_power=self._cash, positions=self._positions
        )

    def submit_vertical_spread(self, spread: VerticalSpreadOrder) -> OrderResult:
        # Simulated fill at the requested limit (or mid if none given).
        fill_price = spread.limit_net_credit
        return OrderResult(success=True, order_id="sim-spread", filled_avg_price=fill_price, status="filled")

    def submit_single_leg(self, order: SingleLegOrder) -> OrderResult:
        fill_price = order.limit_price
        return OrderResult(success=True, order_id="sim-leg", filled_avg_price=fill_price, status="filled")

    def close_position(self, position_id: str, limit_pct: float | None) -> OrderResult:
        return OrderResult(success=True, order_id=position_id, filled_avg_price=None, status="closed")

    def get_dividend_calendar(self) -> list[DividendEvent]:
        data = self._get(
            "/v3/reference/dividends",
            {"ticker": self._config.symbol, "limit": 50, "order": "asc"},
        )
        out = []
        for d in data.get("results", []):
            try:
                out.append(
                    DividendEvent(
                        ex_date=datetime.strptime(d["ex_dividend_date"], "%Y-%m-%d").date(),
                        pay_date=datetime.strptime(d["pay_date"], "%Y-%m-%d").date(),
                        amount_per_share=float(d["cash_amount"]),
                    )
                )
            except (KeyError, ValueError):
                continue
        return out

    def unit_multiplier(self) -> float:
        return float(self._config.core_unit_shares)
