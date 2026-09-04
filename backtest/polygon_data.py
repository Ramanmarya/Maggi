"""
Historical market data from Polygon (now trading as Massive), cached to disk.

A second source alongside `data.py`'s Alpaca backend, for one reason: Alpaca's
option history begins in **February 2024**. Contracts expiring 2021-09,
2022-09 and 2023-09 all return zero bars, so the deepest backtest Alpaca can
support is "since Feb 2024" — a window containing no bear market. Polygon's
Options Advanced tier carries 5+ years and, uniquely among the tiers, the
historical NBBO quotes that let `costs.py` measure the bid/ask spread instead
of modelling it.

Two things are better here than in the Alpaca path, both consequences of
Polygon exposing a contract *reference* endpoint:

  - **Expiries and strikes are looked up, not guessed.** `data.py` builds a
    strike grid from a percentage band, generates OCC symbols, requests them
    all, and infers "this contract existed" from whether bars came back. That
    is why the cache holds 84k symbols for 527k bars — most requests are for
    contracts that never listed. Here `/v3/reference/options/contracts` says
    what actually existed on the day, so a chain walk requests only real
    contracts.
  - **The reference endpoint is not entitlement-gated**, so expiry and strike
    discovery works on the free tier. Only the aggregates and quotes are
    gated, which means most of this module can be tested before paying.

Entitlement failures are raised, never swallowed. A 403 that returned an
empty chain would render as "the strategy chose not to trade" in the equity
curve, which is the most expensive kind of silent failure: it looks like a
result. `PolygonEntitlementError` names the tier needed instead.

Cache keys are bare OCC symbols (`QQQ240315P00430000`), matching `data.py`,
so the two backends share one cache and a run can be started on one and
finished on the other. The `O:` prefix Polygon wants exists only at the HTTP
boundary.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from qqq.black_scholes import bs_delta, implied_vol
from qqq.broker_adapter import OptionContract

from .cache import Bar, BarCache
from .data import occ_symbol, parse_occ  # OCC format is source-independent

BASE_URL = "https://api.polygon.io"
USER_AGENT = "maggi-backtest/1.0"

# Polygon caps a single aggregates response at 50k rows; a daily series for one
# contract over five years is ~1,250, so this is never the binding limit.
AGG_LIMIT = 50_000
CONTRACTS_PAGE = 1000


class PolygonEntitlementError(RuntimeError):
    """The plan does not cover the requested data. Raised, not swallowed."""


class PolygonRateLimitError(RuntimeError):
    """429 survived the retry budget — the run should stop rather than
    silently produce a chain with holes in it."""


def to_polygon(occ: str) -> str:
    """`QQQ240315P00430000` -> `O:QQQ240315P00430000`."""
    return occ if occ.startswith("O:") else f"O:{occ}"


def from_polygon(ticker: str) -> str:
    """`O:QQQ240315P00430000` -> `QQQ240315P00430000`."""
    return ticker[2:] if ticker.startswith("O:") else ticker


class PolygonHistoricalData:
    """Drop-in replacement for `data.HistoricalData`.

    Same surface the backtest adapter depends on: `load_underlying`,
    `load_underlying_symbol`, `load_option_bars`, `discover_expiries`,
    `chain`, `strike_grid`, plus `.cache` and `.requests`.
    """

    def __init__(self, config, cache: BarCache | None = None, verbose: bool = False,
                 use_quotes: bool | None = None):
        self._config = config
        self.cache = cache or BarCache()
        self.verbose = verbose
        self.requests = 0
        self.quote_requests = 0
        self._last_request_at = 0.0
        self._valid_expiries: dict[tuple[date, int, int], list[date]] = {}
        self._contracts: dict[tuple[date, date, date], list[dict]] = {}
        # Quotes are opt-in: they are one request per contract-day and only
        # exist on Advanced. Default follows config so an upgrade is a rules
        # change rather than a code change.
        self.use_quotes = (
            bool(getattr(config, "polygon_use_quotes", False))
            if use_quotes is None else use_quotes
        )
        self._quotes_unavailable = False

    # ---- plumbing --------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"    [polygon] {msg}")

    def _throttle(self) -> None:
        """Space requests by the configured minimum interval.

        Paid tiers are unlimited, so this exists for the free tier's ~5/min
        and costs nothing once `polygon_min_interval_seconds` is set to 0.
        """
        interval = float(getattr(self._config, "polygon_min_interval_seconds", 0.0) or 0.0)
        if interval <= 0:
            return
        wait = interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: dict | None = None, retries: int = 5) -> dict:
        key = getattr(self._config, "polygon_api_key", "")
        if not key:
            raise PolygonEntitlementError(
                "POLYGON_API_KEY is not set — the Polygon backend cannot fetch "
                "anything. Add it to .env, or run with --source alpaca."
            )
        p = dict(params or {})
        p["apiKey"] = key
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(p)}"

        for attempt in range(retries):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=45) as r:
                    self.requests += 1
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # The rate-limit window is a MINUTE, not a second, so a
                    # 1-2-4-8 backoff expires the retry budget inside a single
                    # window and reports failure while the limit is still in
                    # force. Climb to a full minute instead.
                    time.sleep(min(60.0, 5.0 * (3 ** attempt)))
                    continue
                if e.code in (401, 403):
                    raise PolygonEntitlementError(
                        self._entitlement_message(path, e)
                    ) from e
                raise
            except urllib.error.URLError as e:
                if attempt == retries - 1:
                    raise
                time.sleep(2.0 ** attempt)
        raise PolygonRateLimitError(
            f"429 from Polygon after {retries} attempts on {path}. The free tier "
            "allows ~5 requests a minute; a chain walk needs far more. Either "
            "raise polygon_min_interval_seconds or upgrade the plan."
        )

    @staticmethod
    def _entitlement_message(path: str, err: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(err.read().decode())
            detail = body.get("message") or body.get("error") or ""
        except Exception:
            detail = ""
        want = "Options Advanced ($199/mo)" if "/quotes/" in path else \
               "a plan whose history covers the requested dates"
        return (
            f"Polygon refused {path} with HTTP {err.code}. {detail}\n"
            f"This backtest needs {want}. Nothing has been written to the cache; "
            f"re-run once the plan covers it. Refusing to continue rather than "
            f"return an empty chain, which would look like a strategy that "
            f"chose not to trade."
        )

    # ---- underlying ------------------------------------------------------
    def _fetch_equity_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        resp = self._get(
            f"/v2/aggs/ticker/{urllib.parse.quote(symbol)}/range/1/day/"
            f"{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": str(AGG_LIMIT)},
        )
        return _bars_from_aggs(resp)

    def load_underlying(self, start: date, end: date) -> list[Bar]:
        sym = self._config.symbol
        if not self.cache.have_range(sym, start, end):
            self._log(f"fetching {sym} bars {start}..{end}")
            bars = self._fetch_equity_bars(sym, start, end)
            self.cache.put_bars(sym, bars)
            self.cache.mark_range(sym, start, end)
            self.cache.commit()
        return self.cache.bars(sym, start, end)

    def load_underlying_symbol(self, symbol: str, as_of: date) -> list[Bar]:
        """Daily bars for any equity, cached. Used by the cash sweep, which
        trades an instrument other than the arm's own underlying."""
        start = as_of - timedelta(days=400)
        if not self.cache.have_range(symbol, start, as_of):
            try:
                bars = self._fetch_equity_bars(symbol, start, as_of)
            except PolygonEntitlementError:
                raise
            except Exception:
                # One optional instrument must not abort a run; the sweep
                # simply sees no price and stays in cash.
                bars = []
            self.cache.put_bars(symbol, bars)
            self.cache.mark_range(symbol, start, as_of)
            self.cache.commit()
        return self.cache.bars(symbol, None, as_of)

    # ---- contract reference ---------------------------------------------
    def list_contracts(self, as_of: date, expiry_gte: date, expiry_lte: date,
                       contract_type: str | None = None) -> list[dict]:
        """Contracts expiring in the window, as listed by the reference API.

        **On look-ahead.** The obvious guard is the endpoint's own `as_of`
        parameter, and it does not work for historical dates: measured
        2026-09-03, `as_of=2022-03-02` over a March-2022 expiry window returns
        0 rows where the same query without it returns the full chain. The
        response carries no listing date either, so the contract list alone
        cannot say what existed on the simulated day.

        Look-ahead is prevented one layer down instead, and more reliably: the
        bars request in `chain` is bounded at `as_of`, and `chain` drops any
        contract with no bar on that date. A contract listed after the
        simulated day has no bar on it and therefore cannot be selected. The
        reference list is a superset; the bar filter is the actual gate.
        """
        key = (as_of, expiry_gte, expiry_lte)
        if key in self._contracts:
            rows = self._contracts[key]
        else:
            rows = []
            params = {
                "underlying_ticker": self._config.symbol,
                "expiration_date.gte": expiry_gte.isoformat(),
                "expiration_date.lte": expiry_lte.isoformat(),
                "expired": "true",
                "limit": str(CONTRACTS_PAGE),
            }
            resp = self._get("/v3/reference/options/contracts", params)
            while True:
                rows.extend(resp.get("results") or [])
                nxt = resp.get("next_url")
                if not nxt:
                    break
                path = nxt[len(BASE_URL):] if nxt.startswith(BASE_URL) else nxt
                base, _, query = path.partition("?")
                resp = self._get(base, dict(urllib.parse.parse_qsl(query)))
            self._contracts[key] = rows
        if contract_type:
            rows = [r for r in rows if r.get("contract_type") == contract_type]
        return rows

    def discover_expiries(self, as_of: date, dte_range: tuple[int, int], spot: float) -> list[date]:
        """Real expiries in the DTE window.

        A reference lookup rather than `data.py`'s probe-and-see. One request
        covers every expiry and strike, where the Alpaca path needs a batched
        bars request per candidate date and infers listing from a non-empty
        response.
        """
        key = (as_of, dte_range[0], dte_range[1])
        if key in self._valid_expiries:
            return self._valid_expiries[key]
        lo = as_of + timedelta(days=dte_range[0])
        hi = as_of + timedelta(days=dte_range[1])
        rows = self.list_contracts(as_of, lo, hi)
        found = sorted({
            date.fromisoformat(r["expiration_date"])
            for r in rows if r.get("expiration_date")
        })
        self._valid_expiries[key] = found
        return found

    # ---- options ---------------------------------------------------------
    def load_option_bars(self, symbols: Iterable[str], start: date, end: date) -> None:
        """Fetch and cache daily bars for contracts, skipping cached ones.

        Polygon has no multi-symbol aggregates endpoint, so this is one
        request per contract where Alpaca batches 100. That is the cost of the
        deeper history, and it is why the cache matters more here: the second
        run of a window is free.
        """
        todo = [s for s in symbols if not self.cache.have_range(s, start, end)]
        if not todo:
            return
        self._log(f"fetching {len(todo)} option contracts {start}..{end}")
        for occ in todo:
            try:
                resp = self._get(
                    f"/v2/aggs/ticker/{urllib.parse.quote(to_polygon(occ), safe=':')}"
                    f"/range/1/day/{start.isoformat()}/{end.isoformat()}",
                    {"adjusted": "true", "sort": "asc", "limit": str(AGG_LIMIT)},
                )
                bars = _bars_from_aggs(resp)
            except PolygonEntitlementError:
                raise
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
                # A contract that never traded 404s. That is data, not failure.
                self._log(f"{occ}: {type(e).__name__} — recording empty")
                bars = []
            self.cache.put_bars(occ, bars)
            # Marked whether or not bars came back, so a contract that never
            # listed is remembered as empty instead of refetched every run.
            self.cache.mark_range(occ, start, end)
        self.cache.commit()

    def load_option_quotes(self, symbols: Iterable[str], day: date) -> None:
        """Cache the closing NBBO for each contract on `day`.

        The last quote of the session is the right one to pair with the daily
        close the marks are built from. Skipped entirely when quotes are off
        or the plan lacks them — the caller then falls back to `costs.py`'s
        modelled spread, which is the pre-existing behaviour.
        """
        if not self.use_quotes or self._quotes_unavailable:
            return
        todo = [s for s in symbols if not self.cache.have_quote(s, day)]
        if not todo:
            return
        start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        for occ in todo:
            try:
                resp = self._get(
                    f"/v3/quotes/{urllib.parse.quote(to_polygon(occ), safe=':')}",
                    {"timestamp.gte": start.isoformat().replace("+00:00", "Z"),
                     "timestamp.lt": end.isoformat().replace("+00:00", "Z"),
                     "order": "desc", "sort": "timestamp", "limit": "1"},
                )
                self.quote_requests += 1
            except PolygonEntitlementError:
                # Downgrade once, loudly, and finish the run on modelled
                # spreads rather than aborting a multi-hour backtest.
                self._quotes_unavailable = True
                self._log(
                    "NBBO quotes are not on this plan (Options Advanced carries "
                    "them). Continuing with the modelled spread from costs.py."
                )
                return
            rows = resp.get("results") or []
            if rows:
                bid = rows[0].get("bid_price")
                ask = rows[0].get("ask_price")
                ok = (bid is not None and ask is not None and ask >= bid > 0)
                self.cache.put_quote(occ, day, bid if ok else None, ask if ok else None)
            else:
                self.cache.put_quote(occ, day, None, None)
        self.cache.commit()

    @staticmethod
    def _contract_life_window(expiry: date) -> tuple[date, date]:
        """The span to fetch for one contract, once.

        Wide enough to cover every session the strategy could evaluate it on —
        the DTE window tops out well inside 120 days — so `have_range` reports
        a hit for every subsequent session and the contract is never
        refetched. Fetching through expiry pulls bars dated after the session
        being simulated, which is safe because nothing ever reads a bar later
        than `as_of`: `chain`, `option_mark` and `close_position` all index
        the cache by the current session.
        """
        return expiry - timedelta(days=120), expiry

    def strike_grid(self, spot: float, low_pct: float, high_pct: float, step: float = 1.0) -> list[float]:
        """Kept for interface parity with the Alpaca backend. `chain` does not
        use it — real strikes come from the reference endpoint."""
        lo = int(spot * (1 - low_pct) / step) * step
        hi = int(spot * (1 + high_pct) / step + 1) * step
        out, k = [], lo
        while k <= hi:
            out.append(round(k, 2))
            k += step
        return out

    def chain(
        self,
        as_of: date,
        spot: float,
        dte_range: tuple[int, int],
        put_band: tuple[float, float] = (0.15, 0.02),
        call_band: tuple[float, float] = (0.02, 0.15),
    ) -> list[OptionContract]:
        """Reconstruct the option chain as it stood on `as_of`.

        Strikes come from the contracts that actually listed, filtered to the
        bands the strategy trades. Greeks are still Black-Scholes with IV
        backed out of the mark — Polygon's historical endpoints carry no
        greeks either — but the mark itself is the real NBBO mid when quotes
        are on, rather than the daily close standing in for one.
        """
        lo = as_of + timedelta(days=dte_range[0])
        hi = as_of + timedelta(days=dte_range[1])
        rows = self.list_contracts(as_of, lo, hi)
        if not rows:
            return []

        put_lo, put_hi = spot * (1 - put_band[0]), spot * (1 + put_band[1])
        call_lo, call_hi = spot * (1 - call_band[0]), spot * (1 + call_band[1])

        wanted: list[tuple[str, date, str, float]] = []
        for r in rows:
            try:
                kind = r["contract_type"]
                strike = float(r["strike_price"])
                expiry = date.fromisoformat(r["expiration_date"])
                occ = from_polygon(r["ticker"])
            except (KeyError, ValueError, TypeError):
                continue
            band = (put_lo, put_hi) if kind == "put" else (call_lo, call_hi)
            if band[0] <= strike <= band[1]:
                wanted.append((occ, expiry, kind, strike))

        if not wanted:
            return []

        # Fetch each contract's whole life in one request rather than a 3-day
        # window per session. Polygon bills one request per contract where
        # Alpaca batches 100, so the naive per-session window costs a request
        # per contract PER DAY: ~943 in-band contracts x ~1,258 sessions is
        # 1.19M requests for a five-year run. Keyed to the expiry instead,
        # `have_range` turns every later session into a cache hit and the same
        # run costs one request per distinct contract — roughly 12x less.
        symbols = [w[0] for w in wanted]
        by_expiry: dict[date, list[str]] = {}
        for occ, expiry, _, _ in wanted:
            by_expiry.setdefault(expiry, []).append(occ)
        for expiry, syms in by_expiry.items():
            self.load_option_bars(syms, *self._contract_life_window(expiry))
        # Quotes cost one request per contract-day, so ask only about contracts
        # that actually traded on the day. This is also the look-ahead gate:
        # a contract listed after `as_of` has no bar on it and is dropped here
        # before any quote is requested for it.
        live = [s for s in symbols if (self.cache.bar_on(s, as_of) or None) is not None]
        self.load_option_quotes(live, as_of)

        r_f = self._config.backtest_risk_free_rate
        q_y = self._config.backtest_dividend_yield_estimate
        root = self._config.symbol

        out: list[OptionContract] = []
        for occ, expiry, kind, strike in wanted:
            bar = self.cache.bar_on(occ, as_of)
            if bar is None or bar.close <= 0:
                continue  # never listed, or not yet listed on this session
            quote = self.cache.quote_on(occ, as_of)
            if quote is not None:
                mark, bid, ask = quote.mid, quote.bid, quote.ask
            else:
                # No measured spread: report the close in both fields and let
                # the adapter widen it with the modelled half-spread, exactly
                # as the Alpaca path does.
                mark = bid = ask = bar.close
            if mark <= 0:
                continue
            years = max((expiry - as_of).days, 1) / 365.0
            iv = implied_vol(mark, spot, strike, years, r_f, q_y, kind)
            if iv is None:
                continue
            out.append(
                OptionContract(
                    symbol=occ, underlying=root, expiry=expiry, strike=strike,
                    option_type=kind, bid=bid, ask=ask,
                    delta=bs_delta(spot, strike, years, r_f, q_y, iv, kind),
                    implied_vol=iv, open_interest=None,
                )
            )
        return out

    # ---- diagnostics -----------------------------------------------------
    def earliest_option_data(self, probe: date | None = None) -> date | None:
        """Smallest date this key can actually read option aggregates for.

        Entitlement is a date window, and the window moves. Reporting it up
        front turns "the strategy did nothing before 2024" into "the plan
        starts in 2024", which are very different bugs.
        """
        probe = probe or date.today()
        for years_back in range(0, 11):
            day = date(probe.year - years_back, 3, 15)
            rows = self.list_contracts(day, day, day + timedelta(days=45), "put")
            if not rows:
                continue
            occ = from_polygon(rows[len(rows) // 2]["ticker"])
            try:
                resp = self._get(
                    f"/v2/aggs/ticker/{urllib.parse.quote(to_polygon(occ), safe=':')}"
                    f"/range/1/day/{day.isoformat()}/{(day + timedelta(days=10)).isoformat()}",
                    {"adjusted": "true", "sort": "asc", "limit": "10"},
                )
            except PolygonEntitlementError:
                return date(probe.year - years_back + 1, 1, 1) if years_back else None
            if not (resp.get("results") or []):
                continue
        return None


def _bars_from_aggs(resp: dict) -> list[Bar]:
    out: list[Bar] = []
    for r in resp.get("results") or []:
        try:
            day = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date()
            out.append(Bar(day, r["o"], r["h"], r["l"], r["c"], r.get("v", 0.0)))
        except (KeyError, TypeError, ValueError):
            continue
    return out
