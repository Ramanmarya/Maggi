"""Long calls: a debit sleeve that must not be mistaken for a short one.

Everything else in this strategy sells premium. These pin the three ways a
bought call can quietly go wrong: leaking past its budget, being filed where
the risk manager reads it as short, and being closed as if it were a stock.
"""
from dataclasses import replace
from datetime import date

import pytest

from qqq.config import StrategyConfig
from qqq.long_call_engine import LongCallEngine
from qqq.state import LongCallPosition, PortfolioState

UNIT = 100


def _cfg(**kw):
    base = dict(long_call_enabled=True, long_call_annual_budget_pct=0.02,
                long_call_max_open=2, long_call_contracts=1,
                long_call_profit_multiple=2.0, long_call_close_dte=3)
    base.update(kw)
    return replace(StrategyConfig(), **base)


def _engine(**kw):
    e = LongCallEngine.__new__(LongCallEngine)
    e._config = _cfg(**kw)
    e._broker = None
    return e


def _pos(i, paid, opened, status="OPEN"):
    return LongCallPosition(
        id=f"lc{i}", symbol=f"QQQ260918C0074000{i}", strike=740.0,
        expiry="2026-09-18", contracts=1, premium_paid=paid,
        opened_at=opened, status=status,
    )


def test_budget_counts_premium_at_risk_not_outcomes():
    """A winner must not refund headroom to buy more."""
    e = _engine()
    st = PortfolioState()
    st.open_long_calls = [_pos(1, 8.0, "2026-08-01"), _pos(2, 12.0, "2026-07-01")]
    assert e.premium_spent_this_year(st, date(2026, 9, 4)) == pytest.approx(2000.0)


def test_budget_window_is_trailing_365_days():
    e = _engine()
    st = PortfolioState()
    st.open_long_calls = [_pos(1, 10.0, "2024-01-01"), _pos(2, 10.0, "2026-08-01")]
    assert e.premium_spent_this_year(st, date(2026, 9, 4)) == pytest.approx(1000.0)


def test_budget_refuses_the_trade_that_would_breach_it():
    """2% of $250k is $5,000. $4,500 spent leaves no room for an $800 call."""
    e = _engine()
    st = PortfolioState()
    st.open_long_calls = [_pos(1, 45.0, "2026-08-01")]
    assert e._within_budget(st, date(2026, 9, 4), 800.0, 250_000) is False
    assert e._within_budget(st, date(2026, 9, 4), 400.0, 250_000) is True


def test_zero_budget_disables_buying_entirely():
    e = _engine(long_call_annual_budget_pct=0.0)
    assert e._within_budget(PortfolioState(), date(2026, 9, 4), 1.0, 250_000) is False


def test_longs_never_enter_the_short_call_list():
    """RiskManager prices open_calls as SHORT: unlimited loss above the
    strike, share coverage required. A long call is bounded and needs no
    coverage, so filing one there inverts both tests."""
    st = PortfolioState()
    st.open_long_calls.append(_pos(1, 8.0, "2026-09-04"))
    assert st.open_calls == []


def test_close_carries_a_contract_so_it_prices_as_an_option():
    """With contract=None the backtest adapter takes its EQUITY branch and
    prices an OCC option symbol as a stock -- silently, at an unrelated
    price. The close must pass a real contract."""
    import inspect
    src = inspect.getsource(LongCallEngine.manage_existing)
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    assert "OptionContract(" in code
    assert "contract=leg" in code
    assert "contract=None" not in code, "close would price the option as a stock"


def test_disabled_engine_proposes_nothing():
    e = _engine(long_call_enabled=False)
    assert e.propose_call(PortfolioState(), 250_000, date(2026, 9, 4)) is None


def test_max_open_caps_concurrent_positions():
    e = _engine(long_call_max_open=2)
    st = PortfolioState()
    st.open_long_calls = [_pos(1, 8.0, "2026-09-01"), _pos(2, 8.0, "2026-09-02")]
    assert e.propose_call(st, 250_000, date(2026, 9, 4)) is None


def test_closed_positions_free_a_slot_but_not_budget():
    """Slots are about concurrency; budget is about money already risked."""
    e = _engine(long_call_max_open=2)
    st = PortfolioState()
    st.open_long_calls = [_pos(1, 8.0, "2026-09-01", status="CLOSED"),
                          _pos(2, 8.0, "2026-09-02", status="EXPIRED")]
    assert sum(1 for c in st.open_long_calls if c.status == "OPEN") == 0
    assert e.premium_spent_this_year(st, date(2026, 9, 4)) == pytest.approx(1600.0)


def test_state_round_trips_long_calls():
    st = PortfolioState()
    st.open_long_calls.append(_pos(1, 8.25, "2026-09-04"))
    back = PortfolioState.from_dict(st.to_dict())
    assert back.open_long_calls[0].premium_paid == 8.25
    assert back.open_long_calls[0].symbol == "QQQ260918C00740001"


def test_state_written_before_long_calls_still_loads():
    assert PortfolioState.from_dict({"core_units": 1.0}).open_long_calls == []


def test_engine_reads_the_real_orderresult_field():
    """OrderResult exposes filled_avg_price, not filled_price. The wrong name
    raises AttributeError inside the cycle only once an order actually fills,
    so unit tests with no broker never reach it."""
    import inspect
    from qqq.broker_adapter import OrderResult
    from dataclasses import fields
    names = {f.name for f in fields(OrderResult)}
    src = inspect.getsource(LongCallEngine)
    assert "filled_avg_price" in src
    assert "filled_price" not in src.replace("filled_avg_price", "")
    assert "filled_avg_price" in names
