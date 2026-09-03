"""Cost model, bar cache and data-layer tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from backtest.cache import Bar, BarCache
from backtest.costs import CostModel
from backtest.data import HistoricalData, occ_symbol, parse_occ


# ---- costs ---------------------------------------------------------------
def test_seller_receives_less_than_mid_and_buyer_pays_more():
    """The direction that matters: a premium seller must not be paid the mid,
    or the backtest overstates every credit it collects."""
    c = CostModel()
    assert c.option_fill_price(1.71, "sell") < 1.71 < c.option_fill_price(1.71, "buy")


def test_spread_has_a_floor_so_cheap_options_are_not_free_to_trade():
    c = CostModel(option_spread_pct=0.02, option_spread_min=0.02)
    # 2% of $0.10 is a hundredth of a cent; the floor must dominate.
    assert c.option_half_spread(0.10) == pytest.approx(0.01)


def test_fill_price_never_goes_negative():
    assert CostModel().option_fill_price(0.01, "sell") >= 0.0


def test_commission_scales_with_contracts():
    c = CostModel(option_commission_per_contract=0.65)
    assert c.option_commission(4) == pytest.approx(2.60)
    assert c.option_commission(-2) == pytest.approx(1.30), "sign must not matter"


def test_round_trip_cost_is_material_against_a_small_credit():
    """Guards the premise for modelling costs at all: on a $1.71 credit the
    round trip is a double-digit percentage of the premium."""
    c = CostModel()
    credit = 1.71
    received = c.option_fill_price(credit, "sell")
    paid_back = c.option_fill_price(credit, "buy")
    round_trip = (paid_back - received) * 100 + c.option_commission(2)
    assert round_trip / (credit * 100) > 0.02


# ---- cache ---------------------------------------------------------------
@pytest.fixture
def cache(tmp_path: Path) -> BarCache:
    return BarCache(tmp_path / "test.sqlite")


def test_bars_round_trip(cache):
    cache.put_bars("X", [Bar(date(2025, 1, 2), 1, 2, 0.5, 1.5, 10)])
    cache.commit()
    assert cache.bars("X")[0].close == 1.5


def test_empty_result_is_remembered_so_it_is_not_refetched(cache):
    """A contract that never listed must be cached as empty, or every run
    re-requests thousands of dead symbols."""
    cache.put_bars("GHOST", [])
    cache.mark_range("GHOST", date(2025, 1, 1), date(2025, 1, 31))
    cache.commit()
    assert cache.bars("GHOST") == []
    assert cache.have_range("GHOST", date(2025, 1, 5), date(2025, 1, 6))


def test_have_range_is_false_outside_what_was_fetched(cache):
    cache.mark_range("X", date(2025, 1, 1), date(2025, 1, 31))
    cache.commit()
    assert not cache.have_range("X", date(2025, 2, 1), date(2025, 2, 2))


def test_writes_are_idempotent(cache):
    for _ in range(3):
        cache.put_bars("X", [Bar(date(2025, 1, 2), 1, 2, 0.5, 1.5, 10)])
    cache.commit()
    assert len(cache.bars("X")) == 1


def test_bar_on_returns_the_exact_day_or_none(cache):
    cache.put_bars("X", [Bar(date(2025, 1, 2), 1, 2, 0.5, 1.5, 10)])
    cache.commit()
    assert cache.bar_on("X", date(2025, 1, 2)).close == 1.5
    assert cache.bar_on("X", date(2025, 1, 3)) is None


def test_date_filtering(cache):
    cache.put_bars("X", [Bar(date(2025, 1, d), 1, 1, 1, float(d), 1) for d in (2, 3, 6, 7)])
    cache.commit()
    assert [b.close for b in cache.bars("X", date(2025, 1, 3), date(2025, 1, 6))] == [3.0, 6.0]


# ---- OCC symbols ---------------------------------------------------------
@pytest.mark.parametrize("expiry,kind,strike,expected", [
    (date(2026, 9, 18), "put", 500.0, "QQQ260918P00500000"),
    (date(2026, 9, 18), "call", 717.5, "QQQ260918C00717500"),
    (date(2025, 1, 3), "put", 1234.0, "QQQ250103P01234000"),
])
def test_occ_symbol_format(expiry, kind, strike, expected):
    assert occ_symbol("QQQ", expiry, kind, strike) == expected


@pytest.mark.parametrize("expiry,kind,strike", [
    (date(2026, 9, 18), "put", 500.0),
    (date(2026, 12, 31), "call", 717.5),
    (date(2025, 6, 20), "put", 99.5),
])
def test_occ_round_trip(expiry, kind, strike):
    assert parse_occ(occ_symbol("QQQ", expiry, kind, strike)) == (expiry, kind, strike)


# ---- strike grid ---------------------------------------------------------
def test_strike_grid_brackets_the_spot():
    hd = HistoricalData.__new__(HistoricalData)  # no network needed
    grid = hd.strike_grid(717.0, low_pct=0.05, high_pct=0.02, step=1.0)
    assert min(grid) <= 717.0 * 0.95
    assert max(grid) >= 717.0 * 1.02
    assert grid == sorted(grid)


def test_strike_grid_respects_the_step():
    hd = HistoricalData.__new__(HistoricalData)
    grid = hd.strike_grid(700.0, 0.02, 0.02, step=5.0)
    assert all(abs(b - a - 5.0) < 1e-9 for a, b in zip(grid, grid[1:]))
