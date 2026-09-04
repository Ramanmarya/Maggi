"""
Scheduled put entry (#1) and core-covering calls (#2).

Both are deliberate deviations from V5, both default off, and both change how
much premium the arm can collect — so the tests pin the *trigger* logic rather
than any particular income number.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from qqq.config import StrategyConfig
from qqq.cycle import StrategyCycle
from qqq.state import PortfolioState, PutSpreadPosition

TODAY = date(2026, 9, 4)


def _cfg(**kw):
    fields = {"regime_filter_enabled": False, **kw}
    return replace(StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"), **fields)


def _cycle(**kw):
    return StrategyCycle(broker=None, config=_cfg(**kw))


def _open_put(i=0):
    return PutSpreadPosition(
        id=f"p{i}", short_strike=700.0, long_strike=0.0, expiry="2026-10-02",
        contracts=1, net_credit=11.0, opened_at="2026-09-01T00:00:00Z",
    )


# ---- #1 entry mode -------------------------------------------------------
def test_ladder_mode_needs_both_a_zone_and_appetite():
    c = _cycle(put_entry_mode="ladder")
    st = PortfolioState()
    assert c._should_open_put(st, 700.0, True, TODAY) is True
    assert c._should_open_put(st, None, True, TODAY) is False, "no zone touched"
    assert c._should_open_put(st, 700.0, False, TODAY) is False, "already at target exposure"


def test_scheduled_mode_writes_without_a_zone():
    """The whole point: a premium program harvests continuously, not only when
    price happens to touch a rung."""
    c = _cycle(put_entry_mode="scheduled")
    assert c._should_open_put(PortfolioState(), None, False, TODAY) is True


def test_scheduled_mode_stops_at_the_target_position_count():
    c = _cycle(put_entry_mode="scheduled", put_target_open_positions=3)
    st = PortfolioState()
    st.open_put_spreads = [_open_put(i) for i in range(3)]
    assert c._should_open_put(st, None, True, TODAY) is False


def test_scheduled_mode_respects_the_cadence():
    c = _cycle(put_entry_mode="scheduled", put_min_days_between_entries=7)
    st = PortfolioState()
    st.last_put_entry = (TODAY - timedelta(days=3)).isoformat()
    assert c._should_open_put(st, None, True, TODAY) is False, "too soon"
    st.last_put_entry = (TODAY - timedelta(days=7)).isoformat()
    assert c._should_open_put(st, None, True, TODAY) is True


def test_closed_positions_do_not_count_toward_the_target():
    c = _cycle(put_entry_mode="scheduled", put_target_open_positions=1)
    st = PortfolioState()
    closed = _open_put(0)
    closed.status = "CLOSED"
    st.open_put_spreads = [closed]
    assert c._should_open_put(st, None, True, TODAY) is True


def test_both_mode_takes_either_trigger():
    c = _cycle(put_entry_mode="both", put_min_days_between_entries=7)
    st = PortfolioState()
    st.last_put_entry = TODAY.isoformat()          # cadence blocks the schedule
    assert c._should_open_put(st, 700.0, True, TODAY) is True, "ladder should still fire"
    assert c._should_open_put(st, None, True, TODAY) is False, "neither trigger"


def test_a_corrupt_last_entry_date_does_not_block_forever():
    c = _cycle(put_entry_mode="scheduled")
    st = PortfolioState()
    st.last_put_entry = "not-a-date"
    assert c._should_open_put(st, None, True, TODAY) is True


# ---- #2 core coverage ----------------------------------------------------
@pytest.mark.parametrize("cover,shares,core,expected", [
    (False, 100, 1.0, 0.0),    # §2: core uncapped, nothing callable
    (False, 250, 1.0, 1.5),    # only the excess above the core
    (True,  100, 1.0, 1.0),    # the core itself becomes callable
    (True,  250, 1.0, 2.5),    # everything is callable
])
def test_callable_inventory_depends_on_cover_core(cover, shares, core, expected):
    from qqq.broker_adapter import PortfolioSnapshot, PositionSnapshot

    cfg = _cfg(call_cover_core=cover)
    held = shares / cfg.core_unit_shares
    got = held if cover else max(0.0, held - core)
    assert got == pytest.approx(expected)


def test_covering_the_core_never_creates_a_naked_call():
    """Whatever the coverage base, §9 gate 4 still binds: contracts must not
    exceed callable inventory."""
    from qqq.risk import RiskManager

    risk = RiskManager(_cfg(call_cover_core=True))
    assert risk.check_call_coverage([], excess_units=1.0, proposing=1).passed is True
    assert risk.check_call_coverage([], excess_units=1.0, proposing=2).passed is False
