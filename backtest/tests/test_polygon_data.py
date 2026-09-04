"""
Polygon backend tests, driven by a stubbed HTTP layer so they need no network
and no subscription.

The cases that matter most are the failure ones. A backtest that silently
returns an empty chain because the plan does not cover 2022 renders as "the
strategy chose not to trade" in the equity curve — a wrong answer that looks
like a right one. So entitlement errors must raise, and the one place they are
allowed to degrade quietly (quotes) must be explicitly proven to degrade to
the modelled spread rather than to no data.
"""

from __future__ import annotations

import urllib.error
from dataclasses import replace
from datetime import date

import pytest

from backtest.cache import Bar, BarCache
from backtest.costs import CostModel
from backtest.polygon_data import (
    PolygonEntitlementError,
    PolygonHistoricalData,
    from_polygon,
    to_polygon,
)
from qqq.config import StrategyConfig

AS_OF = date(2026, 3, 2)
SPOT = 500.0


def _config(tmp_path):
    return replace(
        StrategyConfig(),
        symbol="QQQ",
        polygon_api_key="test-key",
        polygon_min_interval_seconds=0.0,   # no sleeping in tests
        backtest_risk_free_rate=0.045,
        backtest_dividend_yield_estimate=0.006,
    )


def _contract_row(strike: float, kind: str, expiry: date):
    yy = f"{expiry:%y%m%d}"
    letter = "P" if kind == "put" else "C"
    return {
        "ticker": f"O:QQQ{yy}{letter}{int(strike * 1000):08d}",
        "contract_type": kind,
        "strike_price": strike,
        "expiration_date": expiry.isoformat(),
        "underlying_ticker": "QQQ",
    }


class StubPolygon(PolygonHistoricalData):
    """Records calls and serves canned responses instead of hitting the API."""

    def __init__(self, *a, contracts=None, bar_close=5.0, quote=None,
                 fail_quotes=False, fail_aggs=False, **kw):
        super().__init__(*a, **kw)
        self._stub_contracts = contracts or []
        self._stub_close = bar_close
        self._stub_quote = quote
        self._fail_quotes = fail_quotes
        self._fail_aggs = fail_aggs
        self.calls: list[str] = []

    def _get(self, path, params=None, retries=4):
        self.calls.append(path)
        self.requests += 1
        if "/reference/options/contracts" in path:
            return {"results": self._stub_contracts}
        if "/quotes/" in path:
            if self._fail_quotes:
                raise PolygonEntitlementError("quotes need Options Advanced")
            if self._stub_quote is None:
                return {"results": []}
            bid, ask = self._stub_quote
            return {"results": [{"bid_price": bid, "ask_price": ask}]}
        if "/aggs/" in path:
            if self._fail_aggs:
                raise PolygonEntitlementError("plan does not cover this timeframe")
            ts = int(
                __import__("datetime").datetime(
                    AS_OF.year, AS_OF.month, AS_OF.day,
                    tzinfo=__import__("datetime").timezone.utc,
                ).timestamp() * 1000
            )
            c = self._stub_close
            return {"results": [{"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 10}]}
        raise AssertionError(f"unexpected path {path}")


# ---- symbol translation -------------------------------------------------
def test_occ_and_polygon_tickers_round_trip():
    occ = "QQQ260320P00500000"
    assert to_polygon(occ) == "O:QQQ260320P00500000"
    assert from_polygon(to_polygon(occ)) == occ
    # Already-prefixed input must not gain a second prefix.
    assert to_polygon(to_polygon(occ)) == "O:QQQ260320P00500000"


def test_cache_keys_are_bare_occ_so_both_backends_share_one_cache(tmp_path):
    """The Alpaca backend writes bare OCC symbols. If Polygon wrote `O:`-
    prefixed keys the two would silently miss each other's cached bars."""
    cache = BarCache(tmp_path / "c.sqlite")
    d = StubPolygon(_config(tmp_path), cache=cache,
                    contracts=[_contract_row(500, "put", date(2026, 3, 20))])
    d.load_option_bars(["QQQ260320P00500000"], AS_OF, AS_OF)
    assert cache.bar_on("QQQ260320P00500000", AS_OF) is not None
    assert cache.bar_on("O:QQQ260320P00500000", AS_OF) is None


# ---- discovery ----------------------------------------------------------
def test_discover_expiries_uses_the_reference_endpoint_not_bar_probing(tmp_path):
    rows = [_contract_row(500, "put", date(2026, 3, 20)),
            _contract_row(505, "put", date(2026, 3, 20)),
            _contract_row(500, "put", date(2026, 4, 17))]
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=rows)
    found = d.discover_expiries(AS_OF, (10, 60), SPOT)
    assert found == [date(2026, 3, 20), date(2026, 4, 17)]
    # The whole point: no aggregates requests were needed to learn this.
    assert all("/aggs/" not in c for c in d.calls)


def test_contract_listing_is_cached_across_calls(tmp_path):
    """Two reference calls on the first listing — one per half of the `expired`
    partition — and none at all on the second, because the result is cached."""
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=[_contract_row(500, "put", date(2026, 3, 20))])
    d.discover_expiries(AS_OF, (10, 60), SPOT)
    first = sum("/reference/" in c for c in d.calls)
    d.discover_expiries(AS_OF, (10, 60), SPOT)
    second = sum("/reference/" in c for c in d.calls) - first
    assert first == 2, "expired=true and expired=false are both required"
    assert second == 0, "the second listing must be served from cache"


# ---- chain --------------------------------------------------------------
def test_chain_filters_to_the_traded_bands(tmp_path):
    expiry = date(2026, 3, 20)
    rows = [
        _contract_row(300, "put", expiry),   # 40% OTM — outside the 15% band
        _contract_row(460, "put", expiry),   # inside
        _contract_row(505, "call", expiry),  # inside the call band
        _contract_row(700, "call", expiry),  # far OTM — outside
    ]
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=rows, bar_close=3.0)
    chain = d.chain(AS_OF, SPOT, (10, 60))
    strikes = sorted(c.strike for c in chain)
    assert 460.0 in strikes and 505.0 in strikes
    assert 300.0 not in strikes and 700.0 not in strikes


def test_chain_without_quotes_reports_close_in_both_fields(tmp_path):
    """That equality is the signal the adapter uses to decide whether to apply
    the modelled spread, so it is load-bearing rather than cosmetic."""
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=[_contract_row(460, "put", date(2026, 3, 20))],
                    bar_close=3.0, use_quotes=False)
    chain = d.chain(AS_OF, SPOT, (10, 60))
    assert chain and all(c.bid == c.ask == 3.0 for c in chain)


def test_chain_with_quotes_reports_the_measured_spread(tmp_path):
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=[_contract_row(460, "put", date(2026, 3, 20))],
                    bar_close=3.0, quote=(2.90, 3.10), use_quotes=True)
    chain = d.chain(AS_OF, SPOT, (10, 60))
    assert chain
    c = chain[0]
    assert (c.bid, c.ask) == (2.90, 3.10)
    assert c.delta is not None and c.implied_vol is not None


def test_chain_greeks_come_off_the_quote_mid_when_quotes_are_on(tmp_path):
    """A wide quote whose mid differs from the close must price off the mid,
    or the measured spread would improve fills while leaving selection on the
    unmeasured number."""
    cheap = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "a.sqlite"),
                        contracts=[_contract_row(460, "put", date(2026, 3, 20))],
                        bar_close=3.0, quote=(1.00, 1.20), use_quotes=True)
    dear = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "b.sqlite"),
                       contracts=[_contract_row(460, "put", date(2026, 3, 20))],
                       bar_close=3.0, quote=(6.00, 6.20), use_quotes=True)
    iv_cheap = cheap.chain(AS_OF, SPOT, (10, 60))[0].implied_vol
    iv_dear = dear.chain(AS_OF, SPOT, (10, 60))[0].implied_vol
    assert iv_dear > iv_cheap


# ---- failure behaviour --------------------------------------------------
def test_entitlement_error_on_bars_raises_rather_than_emptying_the_chain(tmp_path):
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=[_contract_row(460, "put", date(2026, 3, 20))],
                    fail_aggs=True)
    with pytest.raises(PolygonEntitlementError):
        d.chain(AS_OF, SPOT, (10, 60))


def test_missing_quotes_entitlement_degrades_to_the_modelled_spread(tmp_path):
    """Quotes are the one thing allowed to fail soft: losing them costs
    precision, and aborting a multi-hour run would cost the whole run."""
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=[_contract_row(460, "put", date(2026, 3, 20))],
                    bar_close=3.0, fail_quotes=True, use_quotes=True)
    chain = d.chain(AS_OF, SPOT, (10, 60))
    assert chain and chain[0].bid == chain[0].ask == 3.0
    assert d._quotes_unavailable is True


def test_a_contract_is_fetched_once_not_once_per_session(tmp_path):
    """The cost difference between fetching a contract's whole life once and
    fetching a 3-day window per session is ~1.19M requests versus ~100k over a
    five-year run, so this is the test that decides whether a 5y backfill is
    hours or days."""
    expiry = date(2026, 3, 20)
    d = StubPolygon(_config(tmp_path), cache=BarCache(tmp_path / "c.sqlite"),
                    contracts=[_contract_row(460, "put", expiry),
                               _contract_row(470, "put", expiry)],
                    bar_close=3.0)
    d.chain(AS_OF, SPOT, (10, 60))
    after_first = len([c for c in d.calls if "/aggs/" in c])
    assert after_first == 2  # one per contract, not one per contract-day

    # A later session inside the same contracts' lives must add no bar traffic.
    d.chain(date(2026, 3, 3), SPOT, (10, 60))
    assert len([c for c in d.calls if "/aggs/" in c]) == after_first


def test_a_contract_that_never_traded_is_remembered_as_empty(tmp_path):
    class NoBars(StubPolygon):
        def _get(self, path, params=None, retries=4):
            if "/aggs/" in path:
                self.calls.append(path)
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return super()._get(path, params, retries)

    cache = BarCache(tmp_path / "c.sqlite")
    d = NoBars(_config(tmp_path), cache=cache, contracts=[])
    d.load_option_bars(["QQQ260320P00123000"], AS_OF, AS_OF)
    first = len([c for c in d.calls if "/aggs/" in c])
    d.load_option_bars(["QQQ260320P00123000"], AS_OF, AS_OF)
    assert len([c for c in d.calls if "/aggs/" in c]) == first  # not refetched


def test_no_api_key_is_a_clear_error_not_an_empty_result(tmp_path):
    cfg = replace(_config(tmp_path), polygon_api_key="")
    d = PolygonHistoricalData(cfg, cache=BarCache(tmp_path / "c.sqlite"))
    with pytest.raises(PolygonEntitlementError, match="POLYGON_API_KEY"):
        d.load_underlying(date(2026, 1, 2), AS_OF)


# ---- cost model ---------------------------------------------------------
def test_measured_half_spread_overrides_the_model():
    costs = CostModel()
    modelled = costs.option_fill_price(3.00, "sell")
    measured = costs.option_fill_price(3.00, "sell", half_spread=0.01)
    assert measured > modelled          # a 2c book is tighter than the 6c model
    assert measured == pytest.approx(2.99)


def test_measured_spread_can_be_worse_than_the_model_too():
    """The model is calibrated pessimistic, not maximal. A genuinely wide
    book must be allowed to price worse than it."""
    costs = CostModel()
    assert costs.option_fill_price(3.00, "sell", half_spread=0.25) == pytest.approx(2.75)


def test_quote_cache_negative_entries_are_not_refetched(tmp_path):
    cache = BarCache(tmp_path / "c.sqlite")
    cache.put_quote("QQQ260320P00460000", AS_OF, None, None)
    cache.commit()
    assert cache.have_quote("QQQ260320P00460000", AS_OF) is True
    assert cache.quote_on("QQQ260320P00460000", AS_OF) is None


def test_parallel_fetch_returns_the_same_bars_as_serial(tmp_path, monkeypatch):
    """Parallelism must be invisible in the output. Measured 3.6 req/sec serial
    against 40/sec at 16 workers — the difference between a 16-hour backfill and
    a 1.5-hour one — but only if the results are identical."""
    from dataclasses import replace as dc_replace

    from backtest.cache import BarCache
    from backtest.polygon_data import PolygonHistoricalData as PolygonData
    from qqq.config import StrategyConfig

    calls: list[str] = []

    def fake_get(self, path, params=None):
        calls.append(path)
        occ = path.split("/ticker/")[1].split("/range")[0]
        return {"results": [{"t": 1717200000000, "o": 1.0, "h": 1.2,
                             "l": 0.9, "c": 1.1, "v": 10}]}

    monkeypatch.setattr(PolygonData, "_get", fake_get, raising=False)
    base = StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y", polygon_api_key="k")
    syms = [f"QQQ260918P00{s}000" for s in range(400, 460, 5)]

    out = {}
    for workers in (1, 8):
        calls.clear()
        cache = BarCache(tmp_path / f"w{workers}.sqlite")
        d = PolygonData(dc_replace(base, polygon_max_workers=workers), cache=cache)
        d.load_option_bars(syms, date(2026, 6, 1), date(2026, 9, 18))
        out[workers] = {s: [(b.day, b.close) for b in cache.bars(s)] for s in syms}
        assert len(calls) == len(syms), "each contract fetched exactly once"

    assert out[1] == out[8], "parallel fetch produced different bars than serial"


def test_worker_count_of_one_takes_the_serial_path(tmp_path, monkeypatch):
    """A single worker must not spin up a pool — it is the debugging path."""
    from dataclasses import replace as dc_replace

    from backtest.cache import BarCache
    from backtest.polygon_data import PolygonHistoricalData as PolygonData
    from qqq.config import StrategyConfig

    monkeypatch.setattr(PolygonData, "_get",
                        lambda self, path, params=None: {"results": []}, raising=False)
    base = StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y", polygon_api_key="k")
    d = PolygonData(dc_replace(base, polygon_max_workers=1),
                    cache=BarCache(tmp_path / "serial.sqlite"))
    d.load_option_bars(["QQQ260918P00450000"], date(2026, 6, 1), date(2026, 9, 18))
    assert d.cache.have_range("QQQ260918P00450000", date(2026, 6, 1), date(2026, 9, 18))


def test_contract_listing_covers_both_halves_of_the_expired_partition(tmp_path, monkeypatch):
    """`expired` partitions the universe, it does not filter it. Sending only
    expired=true returned the full chain for historical windows and NOTHING for
    any window whose contracts had not expired yet — the chain came back empty,
    the engine wrote no spreads, and the run still printed a plausible equity
    curve. Both halves must be queried."""
    from backtest.cache import BarCache
    from backtest.polygon_data import PolygonHistoricalData
    from qqq.config import StrategyConfig

    asked: list[str] = []

    def fake_get(self, path, params=None):
        if "reference/options/contracts" not in path:
            return {"results": []}
        flag = (params or {}).get("expired")
        asked.append(flag)
        # Mimic the real API: each flag returns a disjoint half.
        if flag == "true":
            return {"results": [{"ticker": "O:QQQ220715P00300000",
                                 "expiration_date": "2022-07-15",
                                 "strike_price": 300.0, "contract_type": "put"}]}
        return {"results": [{"ticker": "O:QQQ260918P00450000",
                             "expiration_date": "2026-09-18",
                             "strike_price": 450.0, "contract_type": "put"}]}

    monkeypatch.setattr(PolygonHistoricalData, "_get", fake_get, raising=False)
    d = PolygonHistoricalData(
        StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y", polygon_api_key="k"),
        cache=BarCache(tmp_path / "part.sqlite"),
    )
    rows = d.list_contracts(date(2022, 6, 15), date(2022, 7, 1), date(2026, 9, 30))
    assert sorted(asked) == ["false", "true"], "both halves must be requested"
    assert len(rows) == 2, "results from both halves must be merged"


def test_merged_contract_listing_does_not_duplicate(tmp_path, monkeypatch):
    from backtest.cache import BarCache
    from backtest.polygon_data import PolygonHistoricalData
    from qqq.config import StrategyConfig

    row = {"ticker": "O:QQQ220715P00300000", "expiration_date": "2022-07-15",
           "strike_price": 300.0, "contract_type": "put"}
    monkeypatch.setattr(PolygonHistoricalData, "_get",
                        lambda self, path, params=None: {"results": [row]}, raising=False)
    d = PolygonHistoricalData(
        StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y", polygon_api_key="k"),
        cache=BarCache(tmp_path / "dup.sqlite"),
    )
    rows = d.list_contracts(date(2022, 6, 15), date(2022, 7, 1), date(2022, 7, 31))
    assert len(rows) == 1, "the same ticker returned by both halves must appear once"


def test_entitlement_gap_falls_back_but_a_missing_key_still_raises(tmp_path, monkeypatch):
    """Two different failures that look identical at the HTTP layer.

    An entitlement GAP is a fact about the data plan — the upgraded plan covers
    options to 2020 while stocks stayed at two years — and the equity leg can
    be served by Alpaca instead. A MISSING KEY is a configuration error, and
    falling back there would hide it: the run would appear to work while
    silently using a data source the operator did not choose.
    """
    from backtest.cache import BarCache
    from backtest.polygon_data import PolygonEntitlementError, PolygonHistoricalData
    from qqq.config import StrategyConfig

    def refuse(self, path, params=None):
        raise PolygonEntitlementError("plan does not cover this timeframe")

    monkeypatch.setattr(PolygonHistoricalData, "_get", refuse, raising=False)
    monkeypatch.setattr(PolygonHistoricalData, "_alpaca_equity_bars",
                        lambda self, sym, s, e: [], raising=False)

    with_key = StrategyConfig(alpaca_api_key="a", alpaca_secret_key="b", polygon_api_key="k")
    d = PolygonHistoricalData(with_key, cache=BarCache(tmp_path / "gap.sqlite"))
    assert d.load_underlying(date(2022, 1, 3), date(2022, 12, 30)) == []

    without = StrategyConfig(alpaca_api_key="a", alpaca_secret_key="b", polygon_api_key="")
    d2 = PolygonHistoricalData(without, cache=BarCache(tmp_path / "nokey.sqlite"))
    with pytest.raises(PolygonEntitlementError):
        d2.load_underlying(date(2022, 1, 3), date(2022, 12, 30))
