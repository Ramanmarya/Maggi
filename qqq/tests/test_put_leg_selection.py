"""
Regression tests for protective-leg selection (ALGORITHM.md §7).

The bug these pin: the engine chose the protective leg purely by delta. On
QQQ at ~$717 the 5-delta strike sits ~56 points below the 20-delta short, so
every proposal was a ~$5,600-wide spread whose max loss was several times the
per-spread cap, and the risk manager refused all of them. The arm could not
write a single spread and looked like it was simply choosing not to.

Delta-only selection ignores that spread width — and therefore max loss —
scales with the underlying's price. That is why the same rule worked on MNQ
and silently could not work on QQQ.

Chain geometry below mirrors the live QQQ chain observed 2026-09-03:
short 686p at -0.199 / $4.75 mid, 670p at -0.128 / $3.03, 630p at -0.048 / $1.20.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from qqq.broker_adapter import OptionContract
from qqq.config import StrategyConfig
from qqq.put_engine import PutSpreadEngine
from qqq.risk import RiskManager

EXPIRY = date.today() + timedelta(days=28)
SHORT_STRIKE = 686.0


def _put(strike: float, delta: float, mid: float) -> OptionContract:
    return OptionContract(
        symbol=f"QQQ{EXPIRY:%y%m%d}P{int(strike*1000):08d}",
        underlying="QQQ", expiry=EXPIRY, strike=strike, option_type="put",
        bid=round(mid * 0.985, 2), ask=round(mid * 1.015, 2),
        delta=delta, implied_vol=0.18,
    )


def _chain() -> list[OptionContract]:
    """Interpolated from the real QQQ chain quoted on 2026-09-03.

    Real anchors rather than a synthetic decay formula: a smooth formula
    understates how fast premium falls just below the money, which flatters
    risk/reward and makes the fixture disagree with the market it is meant to
    stand in for.
    """
    anchors = [  # (strike, delta, mid) as quoted
        (686, -0.199, 4.75), (685, -0.195, 4.66), (684, -0.189, 4.53),
        (683, -0.184, 4.40), (682, -0.178, 4.23), (681, -0.175, 4.19),
        (680, -0.169, 4.00), (679, -0.163, 3.85), (678, -0.160, 3.79),
        (677, -0.155, 3.66), (676, -0.151, 3.58), (675, -0.146, 3.44),
        (670, -0.128, 3.03), (665, -0.112, 2.66), (660, -0.099, 2.35),
        (655, -0.086, 2.04), (650, -0.076, 1.83), (630, -0.048, 1.20),
    ]
    by_strike = {k: (d, m) for k, d, m in anchors}
    known = sorted(by_strike)
    out = []
    for strike in range(686, 629, -1):
        if strike in by_strike:
            delta, mid = by_strike[strike]
        else:  # linear between the two surrounding anchors
            lo = max(k for k in known if k < strike)
            hi = min(k for k in known if k > strike)
            f = (strike - lo) / (hi - lo)
            delta = by_strike[lo][0] + f * (by_strike[hi][0] - by_strike[lo][0])
            mid = by_strike[lo][1] + f * (by_strike[hi][1] - by_strike[lo][1])
        out.append(_put(float(strike), round(delta, 3), round(mid, 2)))
    return out


def _config(**kw) -> StrategyConfig:
    base = StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y")
    return replace(base, **kw) if kw else base


def _engine(config: StrategyConfig) -> PutSpreadEngine:
    return PutSpreadEngine(broker=None, config=config, risk=RiskManager(config))


def _short_leg(chain):
    return next(c for c in chain if c.strike == SHORT_STRIKE)


def test_delta_only_leg_would_breach_the_cap():
    """Guards the premise: without the max-loss rule the pick is unusable."""
    config = _config()
    engine = _engine(config)
    chain = _chain()
    short = _short_leg(chain)

    by_delta = engine._select_long_leg(chain, short)  # equity omitted = delta only
    assert by_delta.delta == pytest.approx(-0.05, abs=0.02)
    econ = engine._spread_economics(short, by_delta)
    assert econ is not None
    _, max_loss, _ = econ
    assert max_loss > 150_000 * config.max_loss_per_spread_pct, (
        "premise broken: the delta-targeted leg should breach the per-spread cap"
    )


def test_falls_back_to_a_leg_that_fits_the_cap():
    config = _config()
    engine = _engine(config)
    chain = _chain()
    short = _short_leg(chain)

    leg = engine._select_long_leg(chain, short, equity=150_000)
    assert leg is not None, "engine refused instead of applying the max-loss rule"
    assert engine._within_caps(short, leg, 150_000)


def test_picks_the_widest_fitting_leg_not_the_narrowest():
    """Within a fixed max-loss budget, a wider spread collects more premium.
    Narrowing past what the cap allows gives up credit for nothing."""
    config = _config()
    engine = _engine(config)
    chain = _chain()
    short = _short_leg(chain)

    leg = engine._select_long_leg(chain, short, equity=150_000)
    fitting = [c for c in chain if c.strike < short.strike and engine._within_caps(short, c, 150_000)]
    assert leg.strike == min(c.strike for c in fitting)


def test_keeps_the_delta_target_when_it_already_fits():
    """The doc's primary intent must survive: a 5-delta leg that fits is used
    as-is rather than being widened to consume the whole budget.

    Both caps have to be lifted to reach that state, which is itself worth
    pinning: the risk/reward cap is a ratio, so unlike the max-loss cap it
    does NOT relax as the account grows. On this chain a 5-delta protective
    leg is ~14.8:1, so with the 10:1 filter on, no amount of equity brings
    the delta target back — only turning the filter off does.
    """
    config = _config(put_spread_max_risk_reward_ratio=None)
    engine = _engine(config)
    chain = _chain()
    short = _short_leg(chain)

    generous = 5_000_000  # max-loss cap far above any spread in this chain
    leg = engine._select_long_leg(chain, short, equity=generous)
    assert leg.delta == pytest.approx(-0.05, abs=0.02)


def test_risk_reward_cap_ceilings_the_width_however_large_the_account():
    """More equity relaxes the max-loss cap, so it does buy a wider spread —
    but the risk/reward cap is a ratio and therefore scale-free, so it sets a
    ceiling equity cannot pass. Worth pinning: it means a bigger account never
    restores the 5-delta protective leg on a chain shaped like this one."""
    config = _config()  # 10:1 filter active
    engine = _engine(config)
    chain = _chain()
    short = _short_leg(chain)

    at_150k = engine._select_long_leg(chain, short, equity=150_000)
    at_50m = engine._select_long_leg(chain, short, equity=50_000_000)

    # A larger account widens the spread...
    assert at_50m.strike < at_150k.strike
    # ...but never past the ratio ceiling, and so never to the 5-delta leg.
    _, _, rr = engine._spread_economics(short, at_50m)
    assert rr <= config.put_spread_max_risk_reward_ratio
    assert at_50m.delta < -0.05, "reached the 5-delta leg despite the R:R cap"


def test_refuses_when_no_leg_fits():
    """Rejecting is correct. Forcing a trade that breaches the cap is not."""
    config = _config()
    engine = _engine(config)
    chain = _chain()
    short = _short_leg(chain)

    assert engine._select_long_leg(chain, short, equity=1_000) is None


# ---- profit capture (§7) -------------------------------------------------
class _MarkBroker:
    """Stub that returns fixed marks so capture maths can be checked exactly."""

    def __init__(self, marks: dict[str, float | None]):
        self.marks = marks
        self.closed: list[str] = []

    def option_mark(self, symbol):
        return self.marks.get(symbol)

    def close_position(self, position_id, limit_pct):
        from qqq.broker_adapter import OrderResult

        self.closed.append(position_id)
        return OrderResult(True, position_id, None, "closed")

    def today(self):
        return date.today()


def _open_spread(credit: float = 1.50):
    from qqq.state import PutSpreadPosition

    return PutSpreadPosition(
        id="sp-1", short_strike=686.0, long_strike=670.0,
        expiry=(date.today() + timedelta(days=25)).isoformat(),
        contracts=1, net_credit=credit, opened_at="2026-09-03T00:00:00Z",
        short_symbol="SHORT", long_symbol="LONG",
    )


@pytest.mark.parametrize("short_mark,long_mark,expected", [
    (1.50, 0.00, 0.0),    # unchanged: worth what it was sold for
    (0.60, 0.00, 0.60),   # 60% of the credit decayed away
    (0.00, 0.00, 1.0),    # fully decayed
    (2.00, 0.00, -1.0/3), # moved against the position
])
def test_capture_fraction_maths(short_mark, long_mark, expected):
    config = _config()
    engine = PutSpreadEngine(_MarkBroker({"SHORT": short_mark, "LONG": long_mark}), config, RiskManager(config))
    assert engine._captured(_open_spread()) == pytest.approx(expected, abs=1e-6)


def test_spread_is_closed_once_the_capture_target_is_hit():
    config = _config()
    broker = _MarkBroker({"SHORT": 0.55, "LONG": 0.0})  # 63% captured, target 60%
    engine = PutSpreadEngine(broker, config, RiskManager(config))
    from qqq.state import PortfolioState

    state = PortfolioState()
    state.open_put_spreads = [_open_spread()]
    engine.manage_existing(state, date.today())
    assert broker.closed == ["sp-1"]
    assert state.open_put_spreads[0].status == "CLOSED"


def test_spread_is_held_below_the_capture_target():
    config = _config()
    broker = _MarkBroker({"SHORT": 0.90, "LONG": 0.0})  # 40% captured
    engine = PutSpreadEngine(broker, config, RiskManager(config))
    from qqq.state import PortfolioState

    state = PortfolioState()
    state.open_put_spreads = [_open_spread()]
    engine.manage_existing(state, date.today())
    assert broker.closed == []


def test_missing_marks_fall_back_to_the_time_exit_rather_than_guessing():
    """A data gap must not be read as 'no profit' or 'full profit'."""
    config = _config()
    broker = _MarkBroker({"SHORT": None, "LONG": None})
    engine = PutSpreadEngine(broker, config, RiskManager(config))
    assert engine._captured(_open_spread()) == 0.0
    from qqq.state import PortfolioState

    state = PortfolioState()
    state.open_put_spreads = [_open_spread()]
    engine.manage_existing(state, date.today())
    assert broker.closed == []


def test_time_exit_still_fires_inside_the_dte_window():
    config = _config()
    broker = _MarkBroker({"SHORT": 0.90, "LONG": 0.0})  # not at profit target
    engine = PutSpreadEngine(broker, config, RiskManager(config))
    from qqq.state import PortfolioState

    spread = _open_spread()
    spread.expiry = (date.today() + timedelta(days=2)).isoformat()  # inside 3 DTE
    state = PortfolioState()
    state.open_put_spreads = [spread]
    engine.manage_existing(state, date.today())
    assert broker.closed == ["sp-1"]
