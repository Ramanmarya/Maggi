"""The liquidity floor: refuse contracts the arm could not get back out of.

Selling is always easy. The wheel's problem is CLOSING -- at 50% profit, or
rolling at 3 DTE. A strike with no open interest leaves only a punitive
spread or holding to expiry, which converts a managed position into an
assignment nobody chose. QQQ's chain hides this (median OI 289); XLE's does
not (median 62, median bid-ask 18.4% of mid).
"""
from dataclasses import replace
from datetime import date

import pytest

from qqq.broker_adapter import OptionContract
from qqq.config import StrategyConfig
from qqq.put_engine import PutSpreadEngine


def _leg(strike=62.0, bid=0.80, ask=0.90, oi=500, delta=-0.30):
    return OptionContract(symbol=f"XLE_{strike}", underlying="XLE",
                          expiry=date(2026, 10, 16), strike=strike, option_type="put",
                          bid=bid, ask=ask, delta=delta, implied_vol=0.30, open_interest=oi)


def _engine(**kw):
    e = PutSpreadEngine.__new__(PutSpreadEngine)
    e._config = replace(StrategyConfig(), **kw)
    return e


def test_off_by_default_so_existing_arms_are_untouched():
    """QQQ and GLD ship with both floors at 0 and must see every contract."""
    legs = [_leg(oi=0), _leg(oi=3), _leg(bid=0.10, ask=1.90)]
    assert _engine(min_open_interest=0, max_spread_pct_of_mid=0.0)._liquid_only(legs) == legs


def test_thin_open_interest_is_refused():
    e = _engine(min_open_interest=100, max_spread_pct_of_mid=0.0)
    kept = e._liquid_only([_leg(strike=60, oi=5), _leg(strike=62, oi=500)])
    assert [c.strike for c in kept] == [62.0]


def test_unknown_open_interest_fails_the_floor():
    """Absent data is not proof of liquidity. An arm that asked for a floor
    must not be handed contracts whose depth is simply unknown."""
    e = _engine(min_open_interest=100, max_spread_pct_of_mid=0.0)
    assert e._liquid_only([_leg(oi=None)]) == []


def test_a_punitive_spread_is_refused():
    """Round-tripping an 18% spread twice can exceed the whole premium."""
    e = _engine(min_open_interest=0, max_spread_pct_of_mid=0.25)
    wide = _leg(strike=60, bid=0.50, ask=1.50)      # 100% of mid
    tight = _leg(strike=62, bid=0.85, ask=0.95)     # 11% of mid
    assert [c.strike for c in e._liquid_only([wide, tight])] == [62.0]


def test_a_zero_or_negative_mid_is_refused():
    """A contract with no bid has no closable price at all."""
    e = _engine(min_open_interest=0, max_spread_pct_of_mid=0.25)
    assert e._liquid_only([_leg(bid=0.0, ask=0.0)]) == []


def test_both_filters_apply_together():
    e = _engine(min_open_interest=100, max_spread_pct_of_mid=0.25)
    legs = [_leg(strike=59, oi=5, bid=0.85, ask=0.95),      # fails OI
            _leg(strike=60, oi=500, bid=0.50, ask=1.50),    # fails spread
            _leg(strike=62, oi=500, bid=0.85, ask=0.95)]    # passes
    assert [c.strike for c in e._liquid_only(legs)] == [62.0]


def test_an_empty_chain_after_filtering_returns_no_leg_rather_than_raising():
    """The engine must decline to trade, not crash, when nothing is liquid."""
    e = _engine(min_open_interest=100, max_spread_pct_of_mid=0.25)
    assert e._select_short_leg([_leg(oi=1)], "BULL") is None


def test_the_xle_arm_actually_asks_for_the_floor():
    """The filter is worthless if the arm that needs it ships with it off."""
    x = StrategyConfig.for_arm("xle")
    assert x.min_open_interest >= 100
    assert 0 < x.max_spread_pct_of_mid <= 0.30


@pytest.mark.parametrize("arm", ["qqq", "gld"])
def test_deep_chain_arms_keep_the_floor_off(arm):
    c = StrategyConfig.for_arm(arm)
    assert c.min_open_interest == 0 and c.max_spread_pct_of_mid == 0.0
