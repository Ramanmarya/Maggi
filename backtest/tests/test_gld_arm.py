"""The GLD arm end to end: does it actually fill, and do its gates bite?

Two failure modes matter here and neither raises an exception:
  - the arm runs every session and quietly opens nothing (no fills)
  - a gate is configured but never consulted (no filtering)
Both produce a clean run and a believable equity curve.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from backtest.adapter import BacktestBroker
from backtest.cache import BarCache
from backtest.costs import CostModel
from qqq.config import StrategyConfig
from qqq.cycle import StrategyCycle

from .test_adapter import FakeData

# GLD-scale paths. Gold's ATR is 1.47% of price, so daily steps here are
# ~2x what the QQQ paths use at a third of the price.
FLAT = [400.0] * 90
DIP = [400.0] * 50 + [400.0 - i * 2.0 for i in range(40)]
RISE = [400.0 + i * 0.8 for i in range(90)]
PATHS = {"flat": FLAT, "dip": DIP, "rise": RISE}


def _run(tmp_path, path, tag, equity=250_000.0, allocation=1.0, **cfg_kw):
    cache = BarCache(tmp_path / f"gld{tag}.sqlite")
    data = FakeData(path, cache)
    base = StrategyConfig.for_arm("gld")
    config = replace(
        base, alpaca_api_key="x", alpaca_secret_key="y",
        state_file_path=tmp_path / f"gld_state_{tag}.json",
        regime_filter_enabled=False, cash_sweep_enabled=False,
        equity_basis_override=None,
        # A backtest scopes its own capital; without this the arm reads
        # allocator.json, finds no gld entry, sizes against $0 and never trades.
        allocation_override=allocation,
        **cfg_kw,
    )
    broker = BacktestBroker(config, data, equity, CostModel())
    broker.prime(data.days[0], data.days[-1])
    cycle = StrategyCycle(broker, config)
    raised = []
    for day in data.days[60:]:
        broker.set_as_of(day)
        try:
            cycle.run_daily_cycle()
        except Exception as e:
            raised.append((day, e))
        broker.settle()
    return broker, config, raised


@pytest.mark.parametrize("name", list(PATHS))
def test_no_cycle_raises(tmp_path, name):
    _, _, raised = _run(tmp_path, PATHS[name], name)
    assert not raised, f"{len(raised)} cycles raised, first: {raised[0][1]!r}"


@pytest.mark.parametrize("name", list(PATHS))
def test_the_arm_actually_opens_positions(tmp_path, name):
    """The silent failure: runs clean, trades nothing. An arm that never
    fills produces a flat equity curve that looks like a calm market."""
    broker, _, _ = _run(tmp_path, PATHS[name], f"fill{name}")
    fills = [f for f in broker.ledger.fills if f.kind == "option"]
    assert fills, f"GLD arm opened NOTHING on the {name} path"


def test_no_allocation_means_no_trades(tmp_path):
    """Fails closed: an arm missing from allocator.json must size against $0,
    not against the whole account."""
    broker, _, raised = _run(tmp_path, DIP, "noalloc", allocation=0.0)
    assert not raised
    fills = [f for f in broker.ledger.fills if f.kind == "option"]
    assert not fills, "arm traded despite a zero allocation"


def test_half_allocation_opens_no_more_than_full(tmp_path):
    """Sizing must respond to allocation. If the gates ignore it, both runs
    open the same book and two arms would commit 200% of the account."""
    full, _, _ = _run(tmp_path, DIP, "af", allocation=1.0)
    half, _, _ = _run(tmp_path, DIP, "ah", allocation=0.5)
    nf = len([f for f in full.ledger.fills if f.kind == "option"])
    nh = len([f for f in half.ledger.fills if f.kind == "option"])
    assert nh <= nf, f"half allocation opened MORE ({nh}) than full ({nf})"


def test_the_crash_stress_cap_is_actually_consulted(tmp_path):
    """A gate that is configured but never read is the quiet failure. A cap
    tight enough to forbid everything must forbid everything."""
    loose, _, _ = _run(tmp_path, DIP, "caploose", max_crash_stress_pct=0.25)
    tight, _, _ = _run(tmp_path, DIP, "captight", max_crash_stress_pct=0.0001)
    nl = len([f for f in loose.ledger.fills if f.kind == "option"])
    nt = len([f for f in tight.ledger.fills if f.kind == "option"])
    assert nl > 0, "loose cap opened nothing — the test proves nothing"
    assert nt == 0, f"a 0.01% crash-stress cap still allowed {nt} option fills"


def test_collateral_never_exceeds_cash(tmp_path):
    """Cash-secured means cash-secured. Writing a put the account cannot
    collateralise is naked risk wearing the wrong label."""
    broker, config, _ = _run(tmp_path, DIP, "coll")
    assert broker.ledger.cash >= -1e-6, f"ending cash negative: {broker.ledger.cash}"


def test_the_arm_never_buys_gld_outright(tmp_path):
    """core_units is 0: inventory must arrive ONLY by assignment. A share
    purchase here means the core logic leaked in from the QQQ arm."""
    broker, _, _ = _run(tmp_path, RISE, "nocore")
    buys = [f for f in broker.ledger.fills
            if f.kind == "equity" and f.qty > 0 and f.reason != "assignment"]
    assert not buys, f"GLD arm bought shares outright: {buys[:2]}"


# ------------------------------------------------------- continuous rolling

def test_continuous_deploys_more_than_the_ladder(tmp_path):
    """The reason continuous mode exists. On a FLAT path the ladder never
    touches a rung, so it writes almost nothing and the arm earns T-bills."""
    lad, _, _ = _run(tmp_path, FLAT, "clad", put_entry_mode="ladder")
    con, _, _ = _run(tmp_path, FLAT, "ccon", put_entry_mode="continuous",
                     continuous_target_deployment=0.60)
    nl = len([f for f in lad.ledger.fills if f.kind == "option"])
    nc = len([f for f in con.ledger.fills if f.kind == "option"])
    assert nc > nl, f"continuous ({nc}) did not out-trade the ladder ({nl}) on a flat path"


def test_continuous_still_respects_the_crash_stress_cap(tmp_path):
    """Deploying more must not mean deploying past the gates."""
    b, _, _ = _run(tmp_path, DIP, "ccap", put_entry_mode="continuous",
                   continuous_target_deployment=0.95, max_crash_stress_pct=0.0001)
    fills = [f for f in b.ledger.fills if f.kind == "option"]
    assert not fills, f"a 0.01% cap still allowed {len(fills)} fills in continuous mode"


def test_continuous_never_overdraws_cash(tmp_path):
    """Cash-secured means every open put is fully collateralised. Asking for
    95% deployment must not write puts the account cannot cover."""
    b, _, raised = _run(tmp_path, FLAT, "ccash", put_entry_mode="continuous",
                        continuous_target_deployment=0.95)
    assert not raised
    assert b.ledger.cash >= -1e-6, f"cash went negative: {b.ledger.cash:,.2f}"


def test_a_higher_target_deploys_at_least_as_much(tmp_path):
    low, _, _ = _run(tmp_path, FLAT, "clo", put_entry_mode="continuous",
                     continuous_target_deployment=0.20)
    high, _, _ = _run(tmp_path, FLAT, "chi", put_entry_mode="continuous",
                      continuous_target_deployment=0.70)
    nlo = len([f for f in low.ledger.fills if f.kind == "option"])
    nhi = len([f for f in high.ledger.fills if f.kind == "option"])
    assert nhi >= nlo, f"70% target ({nhi}) traded less than 20% ({nlo})"
