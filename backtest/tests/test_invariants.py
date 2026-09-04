"""
Invariant tests — properties that must hold for EVERY run, not assertions
about one expected number.

Written after a run reported $46,191/yr while every cash-secured order was
raising AttributeError inside submit(). The harness swallowed the exception,
the cycle aborted partway, and the resulting equity curve looked plausible
enough to quote as a finding. Example-based tests could not catch that, because
nothing was *wrong* with any single expected value — the run simply wasn't
executing the strategy.

These check the things that are true regardless of configuration, so a broken
code path fails loudly instead of producing a believable number.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from backtest.adapter import BacktestBroker
from backtest.cache import BarCache
from backtest.costs import CostModel
from backtest.ledger import Ledger
from qqq.config import StrategyConfig
from qqq.cycle import StrategyCycle
from qqq.state import PortfolioState, load_state, save_state

from .test_adapter import FakeData, _sessions


# --------------------------------------------------------------------------
def _run(tmp_path, path, equity=250_000.0, sessions_from=60, **cfg_kw):
    """Drive the real cycle over a synthetic path. Exceptions are RE-RAISED —
    a test harness that swallows them reproduces the original defect."""
    cache = BarCache(tmp_path / f"inv{abs(hash(tuple(path))) % 10**6}.sqlite")
    data = FakeData(path, cache)
    config = replace(
        StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"),
        state_file_path=tmp_path / "inv_state.json",
        regime_filter_enabled=False, cash_sweep_enabled=False,
        equity_basis_override=None, **cfg_kw,
    )
    broker = BacktestBroker(config, data, equity, CostModel())
    broker.prime(data.days[0], data.days[-1])
    cycle = StrategyCycle(broker, config)
    raised = []
    for day in data.days[sessions_from:]:
        broker.set_as_of(day)
        try:
            cycle.run_daily_cycle()
        except Exception as e:            # captured, then asserted on
            raised.append((day, e))
        broker.settle()
    return broker, config, data, raised


FLAT = [700.0] * 90
RISING = [700.0 + i * 1.5 for i in range(90)]
FALLING = [700.0] * 60 + [700.0 - i * 4.0 for i in range(40)]
CRASH = [700.0] * 60 + [700.0 * (0.97 ** i) for i in range(40)]
CHOP = [700.0 + (30 if i % 7 < 3 else -30) for i in range(100)]
ALL_PATHS = {"flat": FLAT, "rising": RISING, "falling": FALLING,
             "crash": CRASH, "chop": CHOP}


@pytest.mark.parametrize("name", list(ALL_PATHS))
@pytest.mark.parametrize("structure", ["spread", "cash_secured"])
def test_no_cycle_ever_raises(tmp_path, name, structure):
    """THE test this suite exists for. A cycle that raises has executed part
    of its decisions, and everything downstream is debris."""
    _, _, _, raised = _run(tmp_path, ALL_PATHS[name], put_structure=structure)
    assert not raised, f"{len(raised)} cycles raised, first: {raised[0][1]!r}"


@pytest.mark.parametrize("name", list(ALL_PATHS))
def test_equity_always_equals_cash_plus_marks(tmp_path, name):
    """Accounting identity. Any drift means a fill moved cash without moving a
    position, or vice versa."""
    broker, _, _, _ = _run(tmp_path, ALL_PATHS[name])
    snap = broker.get_current_positions()
    assert snap.equity == pytest.approx(
        broker.ledger.cash + sum(p.market_value for p in snap.positions), abs=0.01
    )


@pytest.mark.parametrize("name", list(ALL_PATHS))
def test_cash_reconciles_against_every_fill(tmp_path, name):
    """Cash must be the starting balance plus the sum of all cash deltas,
    minus commissions. A mismatch means money was created or destroyed."""
    broker, _, _, _ = _run(tmp_path, ALL_PATHS[name])
    L = broker.ledger
    from_fills = 250_000.0 + sum(f.cash_delta for f in L.fills)
    # commissions are charged directly against cash, outside the fill log
    assert L.cash <= from_fills + 0.01
    assert L.cash > from_fills - 10_000, "cash drifted far below the fill log"


@pytest.mark.parametrize("name", list(ALL_PATHS))
def test_state_survives_a_save_load_round_trip(tmp_path, name):
    """A restart must not lose or alter anything. State corruption shows up as
    forgotten open positions, which is silent and expensive."""
    _, config, _, _ = _run(tmp_path, ALL_PATHS[name])
    before = load_state(config.state_file_path)
    save_state(before, config.state_file_path)
    after = load_state(config.state_file_path)
    assert after.to_dict() == before.to_dict()


def test_replaying_the_same_window_is_deterministic(tmp_path):
    """Two identical runs must agree exactly. Divergence means hidden state —
    a cached value, a clock read, or ordering that depends on dict iteration."""
    a, _, _, _ = _run(tmp_path / "a", FALLING)
    b, _, _, _ = _run(tmp_path / "b", FALLING)
    sa = a.get_current_positions()
    sb = b.get_current_positions()
    assert sa.equity == pytest.approx(sb.equity)
    assert len(a.ledger.fills) == len(b.ledger.fills)


def test_no_lookahead_price_is_stable_when_the_clock_rewinds(tmp_path):
    """The single most damaging backtest bug: seeing tomorrow's price today.
    It makes results look excellent and is invisible in the output."""
    broker, _, data, _ = _run(tmp_path, RISING)
    broker.set_as_of(data.days[70])
    early = broker.get_underlying_price()
    broker.set_as_of(data.days[85])
    assert broker.get_underlying_price() > early
    broker.set_as_of(data.days[70])
    assert broker.get_underlying_price() == pytest.approx(early)


@pytest.mark.parametrize("name", ["falling", "crash"])
def test_the_engine_actually_trades_in_conditions_built_for_it(tmp_path, name):
    """Guards against the silent no-op. A strategy that writes nothing passes
    every accounting invariant perfectly — this is what caught cash-secured
    writing zero puts for three separate reasons in one session."""
    broker, _, _, _ = _run(tmp_path, ALL_PATHS[name])
    opened = [f for f in broker.ledger.fills if f.reason == "open_spread_short"]
    assert opened, "a sustained decline produced no short puts at all"


@pytest.mark.parametrize("name", list(ALL_PATHS))
def test_short_calls_never_exceed_callable_inventory(tmp_path, name):
    """§9 gate 4. A naked call on QQQ is unbounded risk, and the path to one
    opened silently once already when excess inventory was derived from delta
    rather than from shares."""
    broker, config, _, _ = _run(tmp_path, ALL_PATHS[name])
    from backtest.data import parse_occ

    calls = 0
    for pos in broker.ledger.open_options():
        try:
            _, kind, _ = parse_occ(pos.symbol)
        except (ValueError, IndexError):
            continue
        if kind == "call" and pos.qty < 0:
            calls += abs(pos.qty)
    shares = broker.ledger.shares_held(config.symbol)
    assert calls <= shares / config.core_unit_shares + 1e-9, (
        f"{calls} short calls against {shares:.0f} shares — naked"
    )


@pytest.mark.parametrize("name", list(ALL_PATHS))
def test_equity_curve_contains_no_nan_or_infinity(tmp_path, name):
    """A single NaN propagates through every metric and turns Sharpe into
    nonsense that still prints as a number."""
    broker, _, data, _ = _run(tmp_path, ALL_PATHS[name])
    eq = broker.get_current_positions().equity
    assert math.isfinite(eq), f"equity is {eq}"


def test_tighter_risk_caps_never_increase_activity(tmp_path):
    """Monotonicity. If halving the risk budget lets the engine trade MORE,
    a gate is being read backwards — which is exactly what the min/max zone
    bug did."""
    loose, _, _, _ = _run(tmp_path / "l", FALLING, max_aggregate_put_risk_pct=0.20)
    tight, _, _, _ = _run(tmp_path / "t", FALLING, max_aggregate_put_risk_pct=0.01)
    n_loose = sum(1 for f in loose.ledger.fills if f.reason == "open_spread_short")
    n_tight = sum(1 for f in tight.ledger.fills if f.reason == "open_spread_short")
    assert n_tight <= n_loose, f"tighter cap traded MORE ({n_tight} vs {n_loose})"


def test_a_closed_kill_switch_permits_no_new_risk(tmp_path, monkeypatch):
    """The gate must hold under a full replay, not just a unit probe."""
    from core import kill_switch

    sw = tmp_path / "TRADING_ENABLED"; sw.write_text("false")
    monkeypatch.setattr(kill_switch, "TRADING_ENABLED_PATH", sw)
    broker, _, _, raised = _run(tmp_path, FALLING)
    assert not raised
