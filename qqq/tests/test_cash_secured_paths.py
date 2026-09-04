"""
Adversarial tests for the cash-secured path — the newest code, and the least
exercised. Written to FAIL if the obvious shortcuts were taken.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from qqq.config import StrategyConfig
from qqq.put_engine import PutSpreadEngine
from qqq.risk import RiskManager
from qqq.state import PortfolioState, PutSpreadPosition


def _cfg(**kw):
    fields = {"regime_filter_enabled": False, "put_structure": "cash_secured", **kw}
    return replace(StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"), **fields)


class _Marks:
    def __init__(self, marks):
        self.marks = marks
        self.closed = []

    def option_mark(self, symbol):
        return self.marks.get(symbol)

    def close_position(self, pid, limit_pct):
        from qqq.broker_adapter import OrderResult
        self.closed.append(pid)
        return OrderResult(True, pid, None, "closed")

    def today(self):
        return date(2026, 9, 4)


def _csp(credit=11.0, days=25):
    """A cash-secured put as the engine records it: no long leg."""
    return PutSpreadPosition(
        id="csp-1", short_strike=700.0, long_strike=0.0,
        expiry=(date(2026, 9, 4) + timedelta(days=days)).isoformat(),
        contracts=1, net_credit=credit, opened_at="2026-09-04T00:00:00Z",
        short_symbol="SHORT", long_symbol="",     # <- empty, not None
    )


# ---- profit capture on an unspread position -----------------------------
def test_profit_capture_works_without_a_long_leg():
    """The shortcut this catches: _captured() asks for BOTH leg marks and
    returns 0.0 if either is missing. A cash-secured put has no long leg, so
    that would silently disable profit-taking entirely and leave the 3-DTE
    force-close as the only exit — quietly turning a 50%-capture strategy into
    a hold-to-expiry one."""
    cfg = _cfg()
    engine = PutSpreadEngine(_Marks({"SHORT": 4.0}), cfg, RiskManager(cfg))
    captured = engine._captured(_csp(credit=11.0))
    assert captured == pytest.approx((11.0 - 4.0) / 11.0), (
        "capture must be computed from the short leg alone when there is no long leg"
    )


def test_a_cash_secured_put_is_closed_at_the_capture_target():
    cfg = _cfg(put_spread_profit_capture_pct=0.50)
    broker = _Marks({"SHORT": 5.0})           # 11.0 -> 5.0 is 55% captured
    engine = PutSpreadEngine(broker, cfg, RiskManager(cfg))
    st = PortfolioState(); st.open_put_spreads = [_csp(credit=11.0)]
    engine.manage_existing(st, date(2026, 9, 4))
    assert broker.closed == ["csp-1"]


def test_a_cash_secured_put_is_held_below_the_capture_target():
    cfg = _cfg(put_spread_profit_capture_pct=0.50)
    broker = _Marks({"SHORT": 8.0})           # only 27% captured
    engine = PutSpreadEngine(broker, cfg, RiskManager(cfg))
    st = PortfolioState(); st.open_put_spreads = [_csp(credit=11.0)]
    engine.manage_existing(st, date(2026, 9, 4))
    assert broker.closed == []


def test_a_missing_short_mark_still_falls_back_to_the_time_exit():
    cfg = _cfg()
    broker = _Marks({"SHORT": None})
    engine = PutSpreadEngine(broker, cfg, RiskManager(cfg))
    assert engine._captured(_csp()) == 0.0
    st = PortfolioState(); st.open_put_spreads = [_csp(days=2)]   # inside 3 DTE
    engine.manage_existing(st, date(2026, 9, 4))
    assert broker.closed == ["csp-1"], "time exit must still fire without a mark"


# ---- settlement ----------------------------------------------------------
def test_an_assigned_cash_secured_put_delivers_shares():
    """The reason this structure was chosen at all. If assignment nets to zero
    the way a spread does, Engines A and C stay dead and the choice was
    pointless."""
    from qqq.state import PortfolioState as _PS  # noqa: F401
    from backtest.ledger import Ledger

    L = Ledger(cash=250_000.0)
    d = date(2026, 9, 4)
    L.fill(d, "QQQ261002P00700000", -1, 11.0, "option", "open_spread_short")
    L.settle_expiries(date(2026, 10, 2), spot=650.0, root="QQQ")
    assert L.shares_held("QQQ") == 100, "an ITM cash-secured put must deliver shares"


def test_assignment_costs_the_strike():
    from backtest.ledger import Ledger

    L = Ledger(cash=250_000.0)
    d = date(2026, 9, 4)
    L.fill(d, "QQQ261002P00700000", -1, 0.0, "option", "open_spread_short")
    L.settle_expiries(date(2026, 10, 2), spot=650.0, root="QQQ")
    assert L.cash == pytest.approx(250_000.0 - 700 * 100)


def test_an_out_of_the_money_put_keeps_the_premium_and_delivers_nothing():
    from backtest.ledger import Ledger

    L = Ledger(cash=250_000.0)
    L.fill(date(2026, 9, 4), "QQQ261002P00700000", -1, 11.0, "option", "open_spread_short")
    L.settle_expiries(date(2026, 10, 2), spot=750.0, root="QQQ")
    assert L.shares_held("QQQ") == 0
    assert L.cash == pytest.approx(250_000.0 + 1_100.0)


# ---- collateral ----------------------------------------------------------
def test_collateral_scales_with_contract_count():
    cfg = _cfg()
    risk = RiskManager(cfg)
    assert risk.check_cash_secured(700.0, 1, 70_000.0).passed is True
    assert risk.check_cash_secured(700.0, 2, 70_000.0).passed is False
    assert risk.check_cash_secured(700.0, 2, 140_000.0).passed is True


def test_collateral_is_checked_against_cash_not_equity():
    """Equity includes shares that cannot be spent on collateral. Checking the
    wrong one lets the engine promise money it does not have."""
    cfg = _cfg()
    risk = RiskManager(cfg)
    assert risk.check_cash_secured(700.0, 1, available_cash=10_000.0).passed is False
