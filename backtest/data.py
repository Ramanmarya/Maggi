"""
Historical market data for the backtest, from Alpaca, cached to disk.

Alpaca rather than Polygon: both serve the same daily option OHLCV about 18
months back, but Alpaca returns many contracts per request and does not
throttle at 5 requests a minute, which is the difference between a backtest
that reruns in seconds and one that takes half a day. Neither plan carries
historical NBBO quotes, so there is no bid/ask here — see `costs.py` for how
the spread is modelled instead of measured.

OCC symbols are generated, not looked up: the format is deterministic
(ROOT + YYMMDD + C/P + strike x1000, zero-padded to 8), so a strike grid can
be built directly and contracts that never listed simply return no bars.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

from qqq.black_scholes import bs_delta, implied_vol
from qqq.broker_adapter import OptionContract

from .cache import Bar, BarCache

BATCH = 100  # contracts per Alpaca request


def occ_symbol(root: str, expiry: date, kind: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{'P' if kind == 'put' else 'C'}{int(round(strike * 1000)):08d}"


def parse_occ(symbol: str) -> tuple[date, str, float]:
    tail = symbol[-15:]
    expiry = datetime.strptime(tail[:6], "%y%m%d").date()
    kind = "put" if tail[6] == "P" else "call"
    return expiry, kind, int(tail[7:]) / 1000.0


class HistoricalData:
    def __init__(self, config, cache: BarCache | None = None, verbose: bool = False):
        self._config = config
        self.cache = cache or BarCache()
        self.verbose = verbose
        self.requests = 0
        self._stock = self._option = None
        self._valid_expiries: dict[tuple[date, int, int], list[date]] = {}

    # ---- clients (lazy so tests can run without alpaca-py) ---------------
    def _stock_client(self):
        if self._stock is None:
            from alpaca.data.historical.stock import StockHistoricalDataClient

            self._stock = StockHistoricalDataClient(
                api_key=self._config.alpaca_api_key, secret_key=self._config.alpaca_secret_key
            )
        return self._stock

    def _option_client(self):
        if self._option is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient

            self._option = OptionHistoricalDataClient(
                api_key=self._config.alpaca_api_key, secret_key=self._config.alpaca_secret_key
            )
        return self._option

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"    [data] {msg}")

    # ---- underlying ------------------------------------------------------
    def load_underlying(self, start: date, end: date) -> list[Bar]:
        sym = self._config.symbol
        if not self.cache.have_range(sym, start, end):
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            self._log(f"fetching {sym} bars {start}..{end}")
            resp = self._stock_client().get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Day,
                    start=datetime.combine(start, datetime.min.time()),
                    end=datetime.combine(end, datetime.max.time()),
                    feed=DataFeed.IEX,
                )
            )
            self.requests += 1
            bars = [
                Bar(b.timestamp.date(), b.open, b.high, b.low, b.close, b.volume)
                for b in (resp.data.get(sym, []) if hasattr(resp, "data") else [])
            ]
            self.cache.put_bars(sym, bars)
            self.cache.mark_range(sym, start, end)
            self.cache.commit()
        return self.cache.bars(sym, start, end)

    def load_underlying_symbol(self, symbol: str, as_of: date) -> list[Bar]:
        """Daily bars for any equity, cached. Used for the cash-sweep
        instrument, which is not the arm's own underlying."""
        start = as_of - timedelta(days=400)
        if not self.cache.have_range(symbol, start, as_of):
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            try:
                resp = self._stock_client().get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                        start=datetime.combine(start, datetime.min.time()),
                        end=datetime.combine(as_of, datetime.max.time()),
                        feed=DataFeed.IEX,
                    )
                )
                self.requests += 1
                bars = [Bar(b.timestamp.date(), b.open, b.high, b.low, b.close, b.volume)
                        for b in (resp.data.get(symbol, []) if hasattr(resp, "data") else [])]
            except Exception:
                bars = []
            self.cache.put_bars(symbol, bars)
            self.cache.mark_range(symbol, start, as_of)
            self.cache.commit()
        return self.cache.bars(symbol, None, as_of)

    # ---- options ---------------------------------------------------------
    def load_option_bars(self, symbols: Iterable[str], start: date, end: date) -> None:
        """Fetch and cache daily bars for many contracts, skipping cached ones."""
        todo = [s for s in symbols if not self.cache.have_range(s, start, end)]
        if not todo:
            return
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame

        for i in range(0, len(todo), BATCH):
            chunk = todo[i : i + BATCH]
            self._log(f"fetching {len(chunk)} option contracts {start}..{end}")
            try:
                resp = self._option_client().get_option_bars(
                    OptionBarsRequest(
                        symbol_or_symbols=chunk,
                        timeframe=TimeFrame.Day,
                        start=datetime.combine(start, datetime.min.time()),
                        end=datetime.combine(end, datetime.max.time()),
                    )
                )
                self.requests += 1
                data = resp.data if hasattr(resp, "data") else {}
            except Exception as e:  # a bad symbol must not abort the whole run
                self._log(f"batch failed ({type(e).__name__}: {e}); marking empty")
                data = {}
            for sym in chunk:
                bars = [
                    Bar(b.timestamp.date(), b.open, b.high, b.low, b.close, b.volume)
                    for b in data.get(sym, [])
                ]
                self.cache.put_bars(sym, bars)
                # Marked whether or not bars came back: a contract that never
                # listed must be remembered as empty, or every run refetches it.
                self.cache.mark_range(sym, start, end)
        self.cache.commit()

    def strike_grid(self, spot: float, low_pct: float, high_pct: float, step: float = 1.0) -> list[float]:
        lo = int(spot * (1 - low_pct) / step) * step
        hi = int(spot * (1 + high_pct) / step + 1) * step
        out, k = [], lo
        while k <= hi:
            out.append(round(k, 2))
            k += step
        return out

    def discover_expiries(self, as_of: date, dte_range: tuple[int, int], spot: float) -> list[date]:
        """Which dates in the DTE window are real expiries.

        Probes one near-the-money put per candidate date in a single batched
        request; dates that return bars are listed expiries. Cached per
        (as_of, dte window) so the probe happens once.
        """
        key = (as_of, dte_range[0], dte_range[1])
        if key in self._valid_expiries:
            return self._valid_expiries[key]

        root = self._config.symbol
        atm = round(spot)
        candidates = [as_of + timedelta(days=d) for d in range(dte_range[0], dte_range[1] + 1)]
        candidates = [d for d in candidates if d.weekday() < 5]
        probes = {occ_symbol(root, d, "put", atm): d for d in candidates}
        self.load_option_bars(list(probes), as_of - timedelta(days=5), as_of)

        found = sorted(
            {d for sym, d in probes.items() if self.cache.bar_on(sym, as_of) is not None}
        )
        self._valid_expiries[key] = found
        return found

    def chain(
        self,
        as_of: date,
        spot: float,
        dte_range: tuple[int, int],
        put_band: tuple[float, float] = (0.15, 0.02),
        call_band: tuple[float, float] = (0.02, 0.15),
    ) -> list[OptionContract]:
        """Reconstruct the option chain as it stood on `as_of`.

        Greeks are recovered by backing implied vol out of the daily close and
        computing delta from it — no historical feed on either plan carries
        greeks, so this is the only route. Bands are narrow on purpose: the
        strategy trades 5-25 delta contracts, so sweeping deep ITM/OTM strikes
        would multiply the data cost for contracts it will never select.
        """
        expiries = self.discover_expiries(as_of, dte_range, spot)
        if not expiries:
            return []

        r = self._config.backtest_risk_free_rate
        q = self._config.backtest_dividend_yield_estimate
        root = self._config.symbol

        wanted: list[tuple[str, date, str, float]] = []
        for expiry in expiries:
            for strike in self.strike_grid(spot, put_band[0], put_band[1]):
                wanted.append((occ_symbol(root, expiry, "put", strike), expiry, "put", strike))
            for strike in self.strike_grid(spot, call_band[0], call_band[1]):
                wanted.append((occ_symbol(root, expiry, "call", strike), expiry, "call", strike))

        self.load_option_bars([w[0] for w in wanted], as_of - timedelta(days=3), as_of)

        out: list[OptionContract] = []
        for sym, expiry, kind, strike in wanted:
            bar = self.cache.bar_on(sym, as_of)
            if bar is None or bar.close <= 0:
                continue
            years = max((expiry - as_of).days, 1) / 365.0
            iv = implied_vol(bar.close, spot, strike, years, r, q, kind)
            if iv is None:
                continue
            delta = bs_delta(spot, strike, years, r, q, iv, kind)
            # No historical NBBO on either plan, so bid/ask are synthesised
            # around the close by costs.py's spread model rather than measured.
            out.append(
                OptionContract(
                    symbol=sym, underlying=root, expiry=expiry, strike=strike,
                    option_type=kind, bid=bar.close, ask=bar.close,
                    delta=delta, implied_vol=iv, open_interest=None,
                )
            )
        return out
