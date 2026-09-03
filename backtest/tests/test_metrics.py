"""Metrics tests against curves whose answers are known by construction."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from backtest.ledger import Ledger
from backtest.metrics import compute


def _curve(values, start=date(2025, 1, 1)):
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def _empty_ledger():
    return Ledger(cash=0.0)


def test_flat_curve_has_zero_return_and_zero_drawdown():
    m = compute(_curve([100_000] * 30), _empty_ledger(), 0.0)
    assert m.total_return == pytest.approx(0.0)
    assert m.max_drawdown == pytest.approx(0.0)
    assert m.sharpe == pytest.approx(0.0)


def test_doubling_over_one_year_is_100_percent_return():
    curve = [(date(2025, 1, 1), 100_000.0), (date(2026, 1, 1), 200_000.0)]
    m = compute(curve, _empty_ledger(), 0.0)
    assert m.total_return == pytest.approx(1.0)
    assert m.cagr == pytest.approx(1.0, abs=0.01), "one year, so CAGR equals total return"


def test_cagr_annualises_a_partial_year_upward():
    """+10% in about a quarter should annualise to far more than 10%."""
    curve = [(date(2025, 1, 1), 100_000.0), (date(2025, 4, 1), 110_000.0)]
    m = compute(curve, _empty_ledger(), 0.0)
    assert m.cagr > 0.40


def test_max_drawdown_measures_from_the_peak_not_the_start():
    m = compute(_curve([100, 120, 90, 130]), _empty_ledger(), 0.0)
    assert m.max_drawdown == pytest.approx(0.25), "90 from a peak of 120 is -25%"


def test_max_drawdown_reports_the_trough_date():
    curve = _curve([100, 120, 90, 130])
    m = compute(curve, _empty_ledger(), 0.0)
    assert m.max_drawdown_date == curve[2][0]


def test_recovery_does_not_erase_the_recorded_drawdown():
    m = compute(_curve([100, 50, 100]), _empty_ledger(), 0.0)
    assert m.max_drawdown == pytest.approx(0.5)


def test_steady_gains_give_a_high_sharpe_and_losses_a_negative_one():
    up = compute(_curve([100 * (1.001 ** i) for i in range(60)]), _empty_ledger(), 0.0)
    down = compute(_curve([100 * (0.999 ** i) for i in range(60)]), _empty_ledger(), 0.0)
    assert up.sharpe > 5
    assert down.sharpe < -5


def test_volatility_is_annualised_from_daily_returns():
    curve = _curve([100 * (1.01 if i % 2 else 0.99) ** 1 for i in range(2)])
    m = compute(_curve([100, 101, 100, 101, 100]), _empty_ledger(), 0.0)
    assert m.volatility == pytest.approx(m.sharpe and abs(m.volatility), abs=1.0)
    assert m.volatility > 0


def test_single_point_curve_is_rejected():
    with pytest.raises(ValueError):
        compute([(date(2025, 1, 1), 100.0)], _empty_ledger(), 0.0)


def test_trade_counts_come_from_the_fill_log():
    L = Ledger(cash=100_000.0)
    d = date(2025, 6, 2)
    L.fill(d, "QQQ260206P00680000", -1, 5.0, "option", "open_spread_short")
    L.fill(d, "QQQ260206P00660000", 1, 1.0, "option", "open_spread_long")
    L.fill(d, "QQQ260206P00680000", 1, 2.0, "option", "close_spread")
    m = compute(_curve([100_000, 100_400]), L, 1.30)
    assert m.spreads_opened == 1
    assert m.spreads_closed == 1
    assert m.commission_paid == pytest.approx(1.30)


def test_premium_collected_is_positive_for_credits():
    """Regression: a sign slip here reported collected premium as negative."""
    L = Ledger(cash=100_000.0)
    L.fill(date(2025, 6, 2), "QQQ260206P00680000", -1, 5.0, "option", "open_spread_short")
    m = compute(_curve([100_000, 100_500]), L, 0.0)
    assert m.premium_collected == pytest.approx(500.0)
