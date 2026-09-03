"""
RiskManager tests against the V5 doc's own worked examples (translated to
QQQ: no $2 multiplier, core_unit_shares=100 in place of it).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qqq.config import StrategyConfig
from qqq.risk import RiskManager
from qqq.state import CallPosition, PutSpreadPosition


def make_config(**overrides) -> StrategyConfig:
    base = StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y")
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return base


def test_spread_max_loss_within_cap_passes():
    config = make_config()
    risk = RiskManager(config)
    # width=5, credit=4 -> max loss = 5*100 - 4*100 = 100, well under 1% of 100k ($1000)
    result = risk.check_spread_max_loss(
        short_strike=400, long_strike=395, net_credit=4.0, contracts=1, equity=100_000
    )
    assert result.passed


def test_spread_max_loss_exceeds_cap_fails():
    config = make_config()
    risk = RiskManager(config)
    # width=10, credit=1 -> max loss = 10*100 - 1*100 = 900... still under $1000, bump width
    result = risk.check_spread_max_loss(
        short_strike=400, long_strike=385, net_credit=1.0, contracts=1, equity=100_000
    )
    # width=15 -> max loss = 15*100 - 100 = 1400 > 1000 cap
    assert not result.passed


def test_aggregate_put_risk_cap():
    config = make_config()
    risk = RiskManager(config)
    spreads = [
        PutSpreadPosition(
            id=f"s{i}", short_strike=400, long_strike=396, expiry="2026-12-31",
            contracts=1, net_credit=1.0, opened_at="now",
        )
        for i in range(20)  # each: (4*100 - 1*100) = 300 max loss -> 20*300=6000 > 5000 cap
    ]
    result = risk.check_aggregate_put_risk(spreads, equity=100_000)
    assert not result.passed


def test_call_coverage_blocks_naked_calls():
    config = make_config()
    risk = RiskManager(config)
    existing = [
        CallPosition(
            id="c1", short_strike=420, expiry="2026-12-31", contracts=1,
            premium_received=2.0, opened_at="now",
        )
    ]
    # 1 existing contract + trying to imply excess_units=0 -> should fail
    result = risk.check_call_coverage(existing, excess_units=0)
    assert not result.passed


def test_call_coverage_allows_covered_calls():
    config = make_config()
    risk = RiskManager(config)
    existing = []
    result = risk.check_call_coverage(existing, excess_units=1)
    assert result.passed
