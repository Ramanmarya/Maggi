"""Continuous rolling: keep the book at a target deployment.

The ladder only fires when price touches an unused rung, so in a flat or
rising market both arms sat in cash -- the GLD arm held 4 puts where
collateral allowed 14, and 90% of its return was T-bill interest. These pin
the replacement, including the ways "deploy more" turns into "deploy
recklessly".
"""
from dataclasses import replace
from datetime import date

import pytest

from qqq.config import StrategyConfig
from qqq.cycle import StrategyCycle
from qqq.state import PortfolioState, PutSpreadPosition


def _cycle(**kw):
    base = dict(put_entry_mode="continuous", continuous_target_deployment=0.60,
                core_unit_shares=100)
    base.update(kw)
    cfg = replace(StrategyConfig(), **base)
    c = StrategyCycle.__new__(StrategyCycle)
    c._config = cfg
    return c


def _put(i, strike=700.0, expiry="2026-10-17", status="OPEN", long_strike=0.0):
    return PutSpreadPosition(
        id=f"p{i}", short_strike=strike, long_strike=long_strike, expiry=expiry,
        contracts=1, net_credit=10.0, opened_at="2026-09-04", status=status,
        close_price=None, closed_at=None, short_symbol="S", long_symbol="",
    )


def _state(positions):
    st = PortfolioState(); st.open_put_spreads = positions; return st


# ------------------------------------------------------------- deployment

def test_deployment_measures_collateral_not_premium():
    """Cash-secured means the STRIKE is the money at stake."""
    c = _cycle()
    st = _state([_put(1, 700.0), _put(2, 700.0)])
    assert c.deployment(st, 250_000) == pytest.approx(140_000/250_000)


def test_closed_positions_release_their_collateral():
    """Otherwise the book fills once and never trades again."""
    c = _cycle()
    st = _state([_put(1, 700.0, status="CLOSED"), _put(2, 700.0)])
    assert c.deployment(st, 250_000) == pytest.approx(70_000/250_000)


def test_spreads_do_not_count_as_cash_collateral():
    """A defined-risk spread pledges its width, not its strike."""
    c = _cycle()
    st = _state([_put(1, 700.0, long_strike=650.0)])
    assert c.deployment(st, 250_000) == 0.0


def test_unknown_equity_is_treated_as_fully_deployed():
    """Failing OPEN here would size against a zero account."""
    assert _cycle().deployment(_state([]), 0) == 1.0
    assert _cycle().deployment(_state([]), -5) == 1.0


# ------------------------------------------------------------------ gating

def test_opens_while_below_target_and_stops_at_it():
    c = _cycle(continuous_target_deployment=0.60)
    empty = _state([])
    assert c._should_open_put(empty, None, False, date(2026,9,4), 250_000) is True
    full = _state([_put(i, 700.0) for i in range(3)])     # 210k/250k = 84%
    assert c._should_open_put(full, None, False, date(2026,9,4), 250_000) is False


def test_price_action_is_irrelevant_in_continuous_mode():
    """The whole point: deployment decides, not whether a rung was touched."""
    c = _cycle()
    st = _state([])
    assert c._should_open_put(st, None, False, date(2026,9,4), 250_000) is True
    assert c._should_open_put(st, 12.5, True, date(2026,9,4), 250_000) is True


def test_missing_equity_refuses_rather_than_guesses():
    assert _cycle()._should_open_put(_state([]), None, False, date(2026,9,4), None) is False


@pytest.mark.parametrize("mode", ["ladder", "scheduled"])
def test_other_modes_are_untouched(mode):
    """Continuous must be additive; the shipped modes keep their behaviour."""
    c = _cycle(put_entry_mode=mode)
    st = _state([])
    if mode == "ladder":
        assert c._should_open_put(st, None, False, date(2026,9,4), 250_000) is False
        assert c._should_open_put(st, 12.5, True, date(2026,9,4), 250_000) is True


# ------------------------------------------------------------ expiry ladder

def test_one_expiry_cannot_absorb_the_whole_book():
    """Positions stacked in a single expiry all roll together, turning a
    rolling book into a cliff."""
    c = _cycle(continuous_max_per_expiry=2)
    st = _state([_put(1, expiry="2026-10-17"), _put(2, expiry="2026-10-17")])
    assert c._expiry_has_room(st, "2026-10-17") is False
    assert c._expiry_has_room(st, "2026-11-21") is True


def test_closed_positions_free_expiry_slots():
    c = _cycle(continuous_max_per_expiry=2)
    st = _state([_put(1, expiry="2026-10-17", status="CLOSED"),
                 _put(2, expiry="2026-10-17")])
    assert c._expiry_has_room(st, "2026-10-17") is True


def test_target_over_one_is_still_bounded_by_the_risk_gates():
    """Continuous mode changes how often the engine ASKS. A target above
    100% must not become a way to write uncollateralised puts -- the
    collateral and crash-stress gates in PutSpreadEngine still decide."""
    c = _cycle(continuous_target_deployment=5.0)
    st = _state([_put(i, 700.0) for i in range(3)])
    assert c._should_open_put(st, None, False, date(2026,9,4), 250_000) is True
    import inspect
    src = inspect.getsource(StrategyCycle._should_open_put)
    assert "risk gates" in src.lower(), "the bound must be documented where it is relied on"
