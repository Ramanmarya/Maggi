"""
Ledger tests.

Weighted heavily toward sign conventions and settlement, because those fail
silently: a sign error does not raise, it just produces a plausible-looking
equity curve that is wrong, which is worse than no backtest at all.
"""

from __future__ import annotations

from datetime import date

import pytest

from backtest.ledger import OPTION_MULTIPLIER, Ledger

DAY = date(2026, 1, 5)
PUT = "QQQ260206P00680000"   # expiry 2026-02-06, strike 680
CALL = "QQQ260206C00720000"  # expiry 2026-02-06, strike 720


def _ledger(cash: float = 100_000.0) -> Ledger:
    return Ledger(cash=cash)


# ---- cash direction ------------------------------------------------------
def test_buying_shares_reduces_cash():
    L = _ledger()
    L.fill(DAY, "QQQ", 100, 700.0, "equity")
    assert L.cash == pytest.approx(30_000.0)
    assert L.shares_held("QQQ") == 100


def test_selling_an_option_credits_cash_with_the_multiplier():
    L = _ledger()
    L.fill(DAY, PUT, -1, 5.00, "option")
    assert L.cash == pytest.approx(100_500.0), "a $5.00 credit on one contract is $500"


def test_buying_an_option_debits_cash_with_the_multiplier():
    L = _ledger()
    L.fill(DAY, PUT, 2, 1.25, "option")
    assert L.cash == pytest.approx(100_000.0 - 250.0)


def test_equity_is_unchanged_by_a_fill_at_the_mark():
    """Trading at the current price moves cash and market value equally."""
    L = _ledger()
    before = L.equity({"QQQ": 700.0})
    L.fill(DAY, "QQQ", 100, 700.0, "equity")
    assert L.equity({"QQQ": 700.0}) == pytest.approx(before)


# ---- realised P&L --------------------------------------------------------
def test_short_option_closed_lower_is_a_profit():
    L = _ledger()
    L.fill(DAY, PUT, -1, 5.00, "option")
    L.fill(DAY, PUT, 1, 2.00, "option")
    assert L.realized_pnl == pytest.approx(300.0)
    assert PUT not in L.positions


def test_short_option_closed_higher_is_a_loss():
    L = _ledger()
    L.fill(DAY, PUT, -1, 2.00, "option")
    L.fill(DAY, PUT, 1, 5.00, "option")
    assert L.realized_pnl == pytest.approx(-300.0)


def test_long_position_closed_higher_is_a_profit():
    L = _ledger()
    L.fill(DAY, "QQQ", 100, 700.0, "equity")
    L.fill(DAY, "QQQ", -100, 710.0, "equity")
    assert L.realized_pnl == pytest.approx(1_000.0)


def test_partial_close_books_only_the_closed_portion():
    L = _ledger()
    L.fill(DAY, PUT, -4, 5.00, "option")
    L.fill(DAY, PUT, 1, 3.00, "option")
    assert L.realized_pnl == pytest.approx(200.0)
    assert L.positions[PUT].qty == -3
    assert L.positions[PUT].avg_price == pytest.approx(5.00), "entry price must survive a partial close"


def test_adding_to_a_position_averages_the_entry_price():
    L = _ledger()
    L.fill(DAY, "QQQ", 100, 700.0, "equity")
    L.fill(DAY, "QQQ", 100, 720.0, "equity")
    assert L.positions["QQQ"].qty == 200
    assert L.positions["QQQ"].avg_price == pytest.approx(710.0)


def test_flipping_through_zero_books_the_close_and_reopens_at_the_new_price():
    L = _ledger()
    L.fill(DAY, "QQQ", 100, 700.0, "equity")
    L.fill(DAY, "QQQ", -150, 710.0, "equity")
    assert L.realized_pnl == pytest.approx(1_000.0), "only the 100 that closed should book P&L"
    assert L.positions["QQQ"].qty == -50
    assert L.positions["QQQ"].avg_price == pytest.approx(710.0)


def test_zero_quantity_fill_is_rejected():
    with pytest.raises(ValueError):
        _ledger().fill(DAY, "QQQ", 0, 700.0, "equity")


# ---- valuation -----------------------------------------------------------
def test_short_option_shows_negative_market_value():
    L = _ledger()
    L.fill(DAY, PUT, -1, 5.00, "option")
    assert L.market_value({PUT: 6.00}) == pytest.approx(-600.0)
    assert L.equity({PUT: 6.00}) == pytest.approx(100_500.0 - 600.0)


def test_unpriced_position_is_held_at_entry_not_zero():
    """Dropping an unpriced leg would silently mark a short to zero — i.e.
    book the maximum possible profit on it."""
    L = _ledger()
    L.fill(DAY, PUT, -1, 5.00, "option")
    assert L.market_value({}) == pytest.approx(-500.0)


# ---- expiry settlement ---------------------------------------------------
def test_otm_short_put_expires_worthless_and_keeps_the_premium():
    L = _ledger()
    L.fill(DAY, PUT, -1, 5.00, "option")
    events = L.settle_expiries(date(2026, 2, 6), spot=700.0, root="QQQ")  # 700 > 680 strike
    assert "worthless" in events[0]
    assert L.realized_pnl == pytest.approx(500.0)
    assert not L.positions


def test_itm_short_put_assigns_shares():
    """This is how Engine A accumulates. A cash-settled backtest never tests it."""
    L = _ledger(cash=100_000.0)
    L.fill(DAY, PUT, -1, 5.00, "option")          # +$500
    L.settle_expiries(date(2026, 2, 6), spot=660.0, root="QQQ")  # 20 ITM
    assert L.shares_held("QQQ") == 100
    # net cash: +500 premium, -2000 settling the option, -66,000 buying shares
    assert L.cash == pytest.approx(100_000 + 500 - 2_000 - 66_000)


def test_assignment_costs_the_strike_not_the_spot():
    """The economic test of assignment: shares are effectively acquired at the
    strike, whatever route the accounting takes."""
    L = _ledger(cash=100_000.0)
    L.fill(DAY, PUT, -1, 0.00, "option")  # zero premium isolates the settlement
    L.settle_expiries(date(2026, 2, 6), spot=660.0, root="QQQ")
    assert L.cash == pytest.approx(100_000 - 680 * 100), "effective cost must be the 680 strike"


def test_itm_short_call_delivers_shares_away():
    L = _ledger()
    L.fill(DAY, "QQQ", 100, 700.0, "equity")
    L.fill(DAY, CALL, -1, 3.00, "option")
    L.settle_expiries(date(2026, 2, 6), spot=740.0, root="QQQ")  # 740 > 720 strike
    assert L.shares_held("QQQ") == 0, "shares should be called away"


def test_put_spread_both_legs_itm_loses_at_most_the_width():
    """The defining property of a defined-risk spread — if this is wrong, the
    whole 'never uncapped downside' claim in §9 is untested."""
    short, long_ = "QQQ260206P00680000", "QQQ260206P00660000"  # 20 wide
    L = _ledger(cash=100_000.0)
    L.fill(DAY, short, -1, 5.00, "option")
    L.fill(DAY, long_, 1, 1.00, "option")
    start_equity = L.equity({})
    L.settle_expiries(date(2026, 2, 6), spot=600.0, root="QQQ")  # far below both
    # Short assigned (+100 sh), long exercised (-100 sh) -> flat
    assert L.shares_held("QQQ") == 0
    loss = start_equity - L.equity({})
    assert loss == pytest.approx(20 * 100 - 400), "width minus net credit"


def test_settlement_ignores_options_expiring_on_other_days():
    L = _ledger()
    L.fill(DAY, PUT, -1, 5.00, "option")
    assert L.settle_expiries(date(2026, 2, 5), spot=600.0, root="QQQ") == []
    assert PUT in L.positions


def test_every_fill_is_recorded_for_audit():
    L = _ledger()
    L.fill(DAY, "QQQ", 100, 700.0, "equity", reason="core")
    L.fill(DAY, PUT, -1, 5.00, "option", reason="open_spread_short")
    assert [f.reason for f in L.fills] == ["core", "open_spread_short"]
    assert L.fills[0].cash_delta == pytest.approx(-70_000.0)


def test_cash_reconciles_against_the_sum_of_fills():
    """Invariant: cash is exactly the starting balance plus every cash delta."""
    L = _ledger(cash=100_000.0)
    L.fill(DAY, "QQQ", 100, 700.0, "equity")
    L.fill(DAY, PUT, -2, 5.00, "option")
    L.fill(DAY, PUT, 1, 3.00, "option")
    L.settle_expiries(date(2026, 2, 6), spot=670.0, root="QQQ")
    assert L.cash == pytest.approx(100_000.0 + sum(f.cash_delta for f in L.fills))
