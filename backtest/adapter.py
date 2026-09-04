"""
BacktestBroker — a BrokerAdapter backed by historical data and a real ledger.

The strategy code cannot tell this apart from AlpacaAdapter, which is the
point: cycle.py, the engines and the risk manager run byte-identically in a
backtest and in paper. Anything that only works here would be testing a
different algorithm than the one that trades.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from qqq.broker_adapter import (
    DividendEvent,
    OptionContract,
    OrderResult,
    PortfolioSnapshot,
    PositionSnapshot,
    SingleLegOrder,
    VerticalSpreadOrder,
)

from .costs import CostModel
from .data import HistoricalData, parse_occ
from .ledger import Ledger


class BacktestBroker:
    def __init__(self, config, data: HistoricalData, starting_equity: float = 100_000.0,
                 costs: CostModel | None = None):
        self._config = config
        self._data = data
        self._costs = costs or CostModel()
        self.ledger = Ledger(cash=starting_equity)
        self._as_of: date | None = None
        self._bars: list = []
        self._spread_legs: dict[str, tuple[str, str, int]] = {}
        self.rejected_orders = 0

    # ---- clock -----------------------------------------------------------
    def set_as_of(self, day: date) -> None:
        self._as_of = day

    def prime(self, start: date, end: date) -> None:
        """Load the underlying history once, with enough lead-in for the 200DMA."""
        self._bars = self._data.load_underlying(start - timedelta(days=420), end)

    def _bars_through(self, day: date) -> list:
        return [b for b in self._bars if b.day <= day]

    def today(self) -> date:
        return self._as_of

    def is_market_open(self) -> bool:
        return True  # the runner only ever steps on real NYSE sessions

    # ---- market data -----------------------------------------------------
    def get_underlying_price(self) -> float:
        bars = self._bars_through(self._as_of)
        if not bars:
            raise RuntimeError(f"no underlying bars on or before {self._as_of}")
        return bars[-1].close

    def get_atr(self, period: int = 20) -> float:
        bars = self._bars_through(self._as_of)[-(period + 1):]
        if len(bars) < 2:
            raise RuntimeError("not enough bars for ATR")
        trs = [
            max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
            for p, b in zip(bars, bars[1:])
        ]
        return sum(trs) / len(trs)

    def get_200dma(self) -> tuple[float, float]:
        bars = self._bars_through(self._as_of)[-200:]
        closes = [b.close for b in bars]
        if len(closes) < 40:
            raise RuntimeError("not enough bars for 200DMA")
        dma = sum(closes) / len(closes)
        look = self._config.regime_slope_lookback_days
        recent = closes[-look:]
        changes = [(recent[i] - recent[i - 1]) / recent[i - 1] for i in range(1, len(recent))]
        return dma, (sum(changes) / len(changes) if changes else 0.0)

    def get_option_chain(self, dte_range: tuple[int, int]) -> list[OptionContract]:
        spot = self.get_underlying_price()
        raw = self._data.chain(self._as_of, spot, dte_range)
        out = []
        for c in raw:
            mid = c.bid  # data layer stores the daily close in both fields
            half = self._costs.option_half_spread(mid)
            out.append(
                OptionContract(
                    symbol=c.symbol, underlying=c.underlying, expiry=c.expiry,
                    strike=c.strike, option_type=c.option_type,
                    bid=max(0.0, mid - half), ask=mid + half,
                    delta=c.delta, implied_vol=c.implied_vol,
                )
            )
        return out

    def equity_price(self, symbol: str) -> float | None:
        if symbol == self._config.symbol:
            return self.get_underlying_price()
        bars = self._data.load_underlying_symbol(symbol, self._as_of)
        return bars[-1].close if bars else None

    def option_delta(self, symbol: str) -> float | None:
        """Back the delta out of the day's close, same route the chain uses."""
        from qqq.black_scholes import bs_delta, implied_vol

        mark = self.option_mark(symbol)
        if mark is None or mark <= 0:
            return None
        try:
            expiry, kind, strike = parse_occ(symbol)
        except (ValueError, IndexError):
            return None
        years = max((expiry - self._as_of).days, 1) / 365.0
        spot = self.get_underlying_price()
        r, q = self._config.backtest_risk_free_rate, self._config.backtest_dividend_yield_estimate
        iv = implied_vol(mark, spot, strike, years, r, q, kind)
        if iv is None:
            return None
        return bs_delta(spot, strike, years, r, q, iv, kind)

    def option_mark(self, symbol: str) -> float | None:
        self._data.load_option_bars([symbol], self._as_of - timedelta(days=3), self._as_of)
        bar = self._data.cache.bar_on(symbol, self._as_of)
        return bar.close if bar else None

    def get_dividend_calendar(self) -> list[DividendEvent]:
        return []  # not wired; Engine C is dormant until excess units exist

    def unit_multiplier(self) -> float:
        return float(self._config.core_unit_shares)

    # ---- account ---------------------------------------------------------
    def mark_prices(self) -> dict[str, float]:
        """Closing marks for everything currently held."""
        prices: dict[str, float] = {}
        spot = self.get_underlying_price()
        prices[self._config.symbol] = spot
        for pos in self.ledger.positions.values():
            if pos.kind == "equity" and pos.symbol != self._config.symbol:
                px = self.equity_price(pos.symbol)
                if px is not None:
                    prices[pos.symbol] = px
        held = [p.symbol for p in self.ledger.open_options()]
        if held:
            self._data.load_option_bars(held, self._as_of - timedelta(days=3), self._as_of)
            for sym in held:
                bar = self._data.cache.bar_on(sym, self._as_of)
                if bar is not None:
                    prices[sym] = bar.close
                else:
                    # No print that day: fall back to intrinsic rather than the
                    # stale entry price, which would freeze a losing short at
                    # its opening credit and hide the drawdown entirely.
                    try:
                        _, kind, strike = parse_occ(sym)
                        prices[sym] = max(0.0, strike - spot) if kind == "put" else max(0.0, spot - strike)
                    except (ValueError, IndexError):
                        pass
        return prices

    def get_current_positions(self) -> PortfolioSnapshot:
        prices = self.mark_prices()
        equity = self.ledger.equity(prices)
        positions = []
        for pos in self.ledger.positions.values():
            # Only the arm's own instrument reaches the strategy, matching the
            # live adapter. Without this the cash-sweep's 452 SGOV shares were
            # counted as 4.52 units of QQQ exposure, which put total delta far
            # above the target curve and silently stopped every spread.
            own = (
                pos.symbol == self._config.symbol
                or pos.symbol.startswith(self._config.symbol)
                or pos.symbol == self._config.cash_sweep_symbol
            )
            if not own:
                continue
            px = prices.get(pos.symbol, pos.avg_price)
            positions.append(
                PositionSnapshot(
                    symbol=pos.symbol, qty=pos.qty, avg_entry_price=pos.avg_price,
                    current_price=px, market_value=pos.qty * px * pos.multiplier,
                    unrealized_pl=(px - pos.avg_price) * pos.qty * pos.multiplier,
                    asset_class="option" if pos.kind == "option" else "equity",
                )
            )
        return PortfolioSnapshot(
            equity=equity, cash=self.ledger.cash,
            buying_power=max(0.0, self.ledger.cash), positions=positions,
        )

    # ---- execution -------------------------------------------------------
    def submit_vertical_spread(self, spread: VerticalSpreadOrder) -> OrderResult:
        day = self._as_of
        short_mid = (spread.short_leg.bid + spread.short_leg.ask) / 2
        long_mid = (spread.long_leg.bid + spread.long_leg.ask) / 2
        short_px = self._costs.option_fill_price(short_mid, "sell")
        long_px = self._costs.option_fill_price(long_mid, "buy")

        self.ledger.fill(day, spread.short_leg.symbol, -spread.contracts, short_px, "option", "open_spread_short")
        self.ledger.fill(day, spread.long_leg.symbol, spread.contracts, long_px, "option", "open_spread_long")
        self.ledger.cash -= self._costs.option_commission(spread.contracts * 2)

        order_id = f"bt-spread-{uuid.uuid4().hex[:8]}"
        self._spread_legs[order_id] = (
            spread.short_leg.symbol, spread.long_leg.symbol, spread.contracts
        )
        return OrderResult(True, order_id, short_px - long_px, "filled")

    def submit_single_leg(self, order: SingleLegOrder) -> OrderResult:
        day = self._as_of
        signed = order.qty if order.side == "buy" else -order.qty
        if order.contract is None:
            # Price by the symbol being traded, not by the arm's underlying.
            # This filled SGOV at QQQ's price the moment the cash sweep began
            # trading an instrument other than QQQ.
            ref = self.equity_price(order.symbol)
            if ref is None:
                return OrderResult(False, None, None, "error",
                                   error=f"no price for {order.symbol}")
            px = self._costs.equity_fill_price(ref, order.side)
            self.ledger.fill(day, order.symbol, signed, px, "equity", "share_order")
            self.ledger.cash -= self._costs.equity_commission(order.qty)
        else:
            mid = (order.contract.bid + order.contract.ask) / 2
            px = self._costs.option_fill_price(mid, order.side)
            self.ledger.fill(day, order.symbol, signed, px, "option", "single_leg")
            self.ledger.cash -= self._costs.option_commission(order.qty)
        return OrderResult(True, f"bt-leg-{uuid.uuid4().hex[:8]}", px, "filled")

    def close_position(self, position_id: str, limit_pct: float | None) -> OrderResult:
        """Close both legs of a spread previously opened here.

        The strategy hands back the order id it was given at open, so the legs
        are resolved through the map built at submission — there is no way to
        recover them from the id alone.
        """
        legs = self._spread_legs.get(position_id)
        if legs is None:
            self.rejected_orders += 1
            return OrderResult(False, None, None, "error", error=f"unknown position {position_id}")

        short_sym, long_sym, contracts = legs
        prices = self.mark_prices()
        self._data.load_option_bars([short_sym, long_sym], self._as_of - timedelta(days=3), self._as_of)
        total = 0.0
        for sym, qty, side in ((short_sym, contracts, "buy"), (long_sym, -contracts, "sell")):
            if sym not in self.ledger.positions:
                continue
            bar = self._data.cache.bar_on(sym, self._as_of)
            mid = bar.close if bar else prices.get(sym, 0.0)
            px = self._costs.option_fill_price(mid, side)
            self.ledger.fill(self._as_of, sym, qty, px, "option", "close_spread")
            total += px if side == "buy" else -px
        self.ledger.cash -= self._costs.option_commission(contracts * 2)
        del self._spread_legs[position_id]
        return OrderResult(True, position_id, total, "closed")

    # ---- end of day ------------------------------------------------------
    def settle(self) -> list[str]:
        return self.ledger.settle_expiries(self._as_of, self.get_underlying_price(), self._config.symbol)
