"""
Regime filter tests (§4, §20, §24).

The filter was specified from the start and never connected: the regime was
computed, logged and displayed while nothing branched on it. These tests pin
the property that makes it safe to enable — it only ever WITHDRAWS risk
relative to the tested bull-case behaviour.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from qqq.config import StrategyConfig
from qqq.ladder import AcquisitionLadder
from qqq.regime_policy import DEFAULTS, RegimePolicy
from qqq.state import PortfolioState


def _cfg(enabled=True, **kw):
    fields = {
        "regime_filter_enabled": enabled,
        "regime_adjustments": DEFAULTS,
        "ladder_atr_multipliers": (0.0, 0.5, 1.5, 3.0, 5.0),
        **kw,          # callers may override any of the above
    }
    return replace(StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"), **fields)


def test_disabled_filter_is_a_no_op():
    """Off must reproduce today's behaviour exactly, so the A/B is honest."""
    p = RegimePolicy.for_regime(_cfg(enabled=False), "DEFENSIVE")
    assert (p.accumulate, p.spacing, p.put_delta, p.call_delta) == (1.0, 1.0, 1.0, 1.0)


def test_bull_is_neutral_so_the_filter_only_ever_withdraws_risk():
    """BULL is the regime the strategy was tested in. If the filter changed
    behaviour there it would invalidate every backtest already run."""
    p = RegimePolicy.for_regime(_cfg(), "BULL")
    assert (p.accumulate, p.spacing, p.put_delta) == (1.0, 1.0, 1.0)


@pytest.mark.parametrize("field", ["accumulate", "put_delta"])
def test_risk_falls_monotonically_from_bull_to_defensive(field):
    cfg = _cfg()
    bull = getattr(RegimePolicy.for_regime(cfg, "BULL"), field)
    neutral = getattr(RegimePolicy.for_regime(cfg, "NEUTRAL"), field)
    defensive = getattr(RegimePolicy.for_regime(cfg, "DEFENSIVE"), field)
    assert bull >= neutral >= defensive
    assert defensive < bull, "DEFENSIVE must take less risk than BULL"


def test_spacing_widens_as_the_regime_deteriorates():
    """§4: wider ATR spacing in a downtrend means fewer, deeper entries."""
    cfg = _cfg()
    assert (RegimePolicy.for_regime(cfg, "BULL").spacing
            < RegimePolicy.for_regime(cfg, "NEUTRAL").spacing
            < RegimePolicy.for_regime(cfg, "DEFENSIVE").spacing)


def test_call_aggressiveness_rises_in_a_downtrend():
    """§24: more willingness to sell calls when delta is unwanted."""
    cfg = _cfg()
    assert (RegimePolicy.for_regime(cfg, "DEFENSIVE").call_delta
            > RegimePolicy.for_regime(cfg, "BULL").call_delta)


def test_reference_level_never_moves_with_spacing():
    """The 0.0 multiplier is the reference itself, not a zone below it."""
    p = RegimePolicy.for_regime(_cfg(), "DEFENSIVE")
    assert p.scaled_multipliers((0.0, 1.5, 3.0))[0] == 0.0


def test_defensive_ladder_is_strictly_deeper_than_bull():
    cfg = _cfg()
    ladder = AcquisitionLadder(broker=None, config=cfg)
    bull = ladder.build_zones(600.0, 6.0, "BULL")
    defensive = ladder.build_zones(600.0, 6.0, "DEFENSIVE")
    assert bull[0] == defensive[0] == 600.0, "the reference is unchanged"
    for b, d in zip(bull[1:], defensive[1:]):
        assert d < b, "every acquisition zone must sit deeper in DEFENSIVE"


def test_defensive_accumulates_less_per_zone():
    cfg = _cfg()
    per_fire = 50
    bull = int(per_fire * RegimePolicy.for_regime(cfg, "BULL").accumulate)
    defensive = int(per_fire * RegimePolicy.for_regime(cfg, "DEFENSIVE").accumulate)
    assert defensive < bull
    assert defensive == 20, "§14: the rate of increase must fall during severe declines"


def test_unknown_regime_falls_back_to_neutral_not_bull():
    """An unrecognised label must not silently grant full bull-case risk."""
    p = RegimePolicy.for_regime(_cfg(), "SOMETHING_ELSE")
    assert p.accumulate == DEFAULTS["NEUTRAL"]["accumulate"]


def test_a_partial_override_keeps_the_remaining_defaults():
    cfg = _cfg(regime_adjustments={"DEFENSIVE": {"accumulate": 0.1}})
    p = RegimePolicy.for_regime(cfg, "DEFENSIVE")
    assert p.accumulate == 0.1
    assert p.spacing == DEFAULTS["DEFENSIVE"]["spacing"]


def test_accumulation_is_scaled_by_the_live_regime_end_to_end():
    """The counterpart: with the filter on, a NEUTRAL regime must actually
    reduce the size bought, not just the multiplier in isolation."""
    from qqq.broker_adapter import PortfolioSnapshot, PositionSnapshot
    from qqq.cycle import StrategyCycle

    class _B:
        def __init__(self): self.orders = []
        def option_delta(self, s): return None
        def get_underlying_price(self): return 700.0
        def today(self): return __import__("datetime").date(2026, 1, 5)
        def submit_single_leg(self, order):
            from qqq.broker_adapter import OrderResult
            self.orders.append(order)
            return OrderResult(True, "ok", 700.0, "filled")

    for regime, expected in (("BULL", 50), ("NEUTRAL", 37), ("DEFENSIVE", 20)):
        broker = _B()
        cfg = _cfg(ladder_accumulate_shares_per_zone=50, equity_basis_override=None,
                   max_crash_stress_pct=0.50)
        state = PortfolioState()
        state.current_regime = regime
        snap = PortfolioSnapshot(
            equity=1_000_000.0, cash=1_000_000.0, buying_power=1_000_000.0,
            positions=[PositionSnapshot("QQQ", 100, 700.0, 700.0, 70_000.0, 0.0, "equity")],
        )
        StrategyCycle(broker, cfg)._accumulate_shares(state, snap, 700.0, 5.0, 1.0)
        assert broker.orders[0].qty == expected, f"{regime} bought {broker.orders[0].qty}"


# ---- ex-dividend safety (§8) --------------------------------------------
def test_ex_div_guard_refuses_a_thin_call_across_the_dividend():
    """§8's QQQ-specific risk: a short call held across an ex-dividend date can
    be exercised early to capture the payout. The guard refuses when extrinsic
    value is below 1.25x the dividend. This test exists because the live
    calendar returned an empty list until 2026-09-03, which made the guard
    pass trivially and silently."""
    from datetime import date, timedelta

    from qqq.broker_adapter import DividendEvent, OptionContract
    from qqq.call_engine import HybridCallEngine
    from qqq.risk import RiskManager

    ex = date.today() + timedelta(days=10)

    class _B:
        def get_underlying_price(self): return 700.0
        def get_dividend_calendar(self):
            return [DividendEvent(ex_date=ex, pay_date=ex, amount_per_share=0.81)]
        def today(self): return date.today()

    cfg = _cfg()
    engine = HybridCallEngine(_B(), cfg, RiskManager(cfg))

    # Deep ITM call expiring after the ex-div: almost no extrinsic left, so
    # early exercise to capture the dividend is rational for the holder.
    thin = OptionContract(
        symbol="QQQ261002C00650000", underlying="QQQ",
        expiry=ex + timedelta(days=11), strike=650.0, option_type="call",
        bid=50.2, ask=50.4, delta=0.95, implied_vol=0.18,
    )
    assert engine._fails_exdiv_safety(thin, _B().get_dividend_calendar()) is True

    # Well out of the money: plenty of extrinsic, early exercise is irrational.
    fat = OptionContract(
        symbol="QQQ261002C00730000", underlying="QQQ",
        expiry=ex + timedelta(days=11), strike=730.0, option_type="call",
        bid=6.0, ask=6.2, delta=0.25, implied_vol=0.18,
    )
    assert engine._fails_exdiv_safety(fat, _B().get_dividend_calendar()) is False


def test_ex_div_guard_ignores_calls_expiring_before_the_dividend():
    from datetime import date, timedelta

    from qqq.broker_adapter import DividendEvent, OptionContract
    from qqq.call_engine import HybridCallEngine
    from qqq.risk import RiskManager

    ex = date.today() + timedelta(days=20)

    class _B:
        def get_underlying_price(self): return 700.0
        def today(self): return date.today()

    cfg = _cfg()
    engine = HybridCallEngine(_B(), cfg, RiskManager(cfg))
    early = OptionContract(
        symbol="QQQ260918C00650000", underlying="QQQ",
        expiry=ex - timedelta(days=5), strike=650.0, option_type="call",
        bid=50.2, ask=50.4, delta=0.95, implied_vol=0.18,
    )
    divs = [DividendEvent(ex_date=ex, pay_date=ex, amount_per_share=0.81)]
    assert engine._fails_exdiv_safety(early, divs) is False
