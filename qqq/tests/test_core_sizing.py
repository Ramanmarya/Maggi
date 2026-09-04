"""The core's size must be a CHOSEN risk, not one that drifts with price.

A fixed unit count means the exposure it represents changes as the market
moves: 100 QQQ was 16% of a $250k account at 2022's $402 and is 29% at
today's $717. These pin the percentage target that fixes that.
"""
from dataclasses import replace

import pytest

from qqq.config import StrategyConfig
from qqq.cycle import StrategyCycle


def _cycle(pct, units=1.0, shares=100):
    cfg = replace(
        StrategyConfig(),
        core_target_pct=pct,
        core_units_target=units,
        core_unit_shares=shares,
    )
    c = StrategyCycle.__new__(StrategyCycle)
    c._config = cfg
    return c


def test_zero_pct_keeps_the_fixed_unit_target():
    """0 must be inert — it is the shipped default and V5 2's literal reading."""
    assert _cycle(0.0, units=1.4).\
        _core_units_target(equity=250_000, price=717.0) == 1.4


def test_pct_holds_exposure_constant_across_a_price_collapse():
    """The whole point: same dollar risk at $402 and at $717."""
    c = _cycle(0.30)
    for price in (401.67, 717.0, 264.49):
        units = c._core_units_target(equity=250_000, price=price)
        exposure = units * 100 * price
        assert exposure == pytest.approx(75_000, rel=1e-9), price


def test_pct_buys_more_shares_when_qqq_is_cheaper():
    """Sizing on dollars is implicitly counter-cyclical; that is a feature."""
    c = _cycle(0.30)
    cheap = c._core_units_target(equity=250_000, price=264.49)
    rich = c._core_units_target(equity=250_000, price=717.0)
    assert cheap > rich * 2.5


def test_pct_shrinks_the_core_as_the_account_shrinks():
    """A drawdown must reduce the target, or losses compound into more risk."""
    c = _cycle(0.30)
    assert c._core_units_target(150_000, 400.0) < c._core_units_target(250_000, 400.0)


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_nonpositive_price_falls_back_rather_than_dividing_by_zero(price):
    """A bad tick must not raise inside the cycle or size an infinite core."""
    assert _cycle(0.30, units=1.0)._core_units_target(250_000, price) == 1.0
