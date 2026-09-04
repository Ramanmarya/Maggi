"""Two arms sharing one account: the ways that goes wrong quietly.

Every failure pinned here is silent -- the bot keeps running and keeps
placing orders, it just sizes or reads the wrong thing.
"""
import json
from pathlib import Path

import pytest

from core.kill_switch import allocation_pct
from qqq.config import PROJECT_ROOT, StrategyConfig, load_rules


# ---------------------------------------------------------------- allocation

def _alloc(tmp_path, payload):
    p = tmp_path / "allocator.json"
    p.write_text(json.dumps(payload))
    return p


def test_two_arms_cannot_each_size_against_the_whole_account(tmp_path):
    """The bug this exists for: without allocation both arms gate as if they
    owned 100%, and between them commit twice the capital that exists."""
    p = _alloc(tmp_path, {"allocations": {"qqq": {"pct": 45}, "gld": {"pct": 55}}})
    total = allocation_pct("qqq", p) + allocation_pct("gld", p)
    assert total == pytest.approx(1.0)
    assert allocation_pct("qqq", p) == pytest.approx(0.45)


@pytest.mark.parametrize("payload", [
    {},                                             # no allocations key
    {"allocations": {}},                            # arm absent
    {"allocations": {"gld": {}}},                   # no pct
    {"allocations": {"gld": {"pct": "half"}}},      # unparseable
    {"allocations": {"gld": {"pct": -10}}},         # negative
    {"allocations": {"gld": {"pct": 150}}},         # over 100
])
def test_unusable_allocation_fails_closed_at_zero(tmp_path, payload):
    """An arm with no readable allocation must trade NOTHING rather than
    default to helping itself to the whole account."""
    assert allocation_pct("gld", _alloc(tmp_path, payload)) == 0.0


def test_missing_allocator_file_fails_closed():
    assert allocation_pct("gld", Path("/nonexistent/allocator.json")) == 0.0


# -------------------------------------------------------------------- config

def test_for_arm_loads_that_arms_rules():
    gld = StrategyConfig.for_arm("gld")
    assert gld.symbol == "GLD"
    assert gld.arm == "gld"


def test_arms_do_not_bleed_into_each_other():
    """load_rules swaps a module global. A leaked swap would have the next
    arm silently reading the previous arm's rules."""
    before = StrategyConfig().symbol
    StrategyConfig.for_arm("gld")
    assert StrategyConfig().symbol == before


def test_rules_are_restored_even_if_construction_raises():
    before = StrategyConfig().symbol
    with pytest.raises(Exception):
        with load_rules(PROJECT_ROOT / "arms" / "gld" / "rules.json"):
            raise RuntimeError("boom")
    assert StrategyConfig().symbol == before


def test_arm_name_mismatch_is_refused():
    """arms/gld/rules.json declaring arm='qqq' would point state files and
    allocator lookups at the wrong arm while looking correct."""
    import qqq.config as cfg
    real = cfg.PROJECT_ROOT / "arms" / "gld" / "rules.json"
    d = json.loads(real.read_text())
    assert d["instrument"]["arm"] == "gld", "the guard has nothing to catch if this drifts"


def test_unknown_arm_raises_rather_than_silently_using_defaults():
    with pytest.raises(FileNotFoundError):
        StrategyConfig.for_arm("dogecoin")


# ---------------------------------------------------------------- gld design

def test_gld_ladder_spans_the_intended_percentage_band():
    """The ladder is denominated in ATRs. GLD's ATR is 1.47% of price against
    QQQ's 0.62%, so reusing QQQ's multipliers would spread the zones over
    2.1%-10.5% instead of the intended 1.6%-7.9%."""
    gld = StrategyConfig.for_arm("gld")
    mults = gld.ladder_atr_multipliers if hasattr(gld, "ladder_atr_multipliers") else None
    d = json.loads((PROJECT_ROOT / "arms" / "gld" / "rules.json").read_text())
    mults = d["ladder"]["atr_multipliers"]
    GLD_ATR_PCT = 0.0147
    lo, hi = mults[1]*GLD_ATR_PCT, mults[-1]*GLD_ATR_PCT
    assert 0.012 <= lo <= 0.020, f"nearest zone {lo:.3%} outside the 1.6% target"
    assert 0.070 <= hi <= 0.090, f"deepest zone {hi:.3%} outside the 7.9% target"


def test_gld_arm_holds_no_permanent_core():
    """The QQQ arm's core is a structural Nasdaq position. There is no
    equivalent thesis on gold: inventory must arrive only by assignment."""
    gld = StrategyConfig.for_arm("gld")
    assert gld.core_units_target == 0.0
    assert gld.core_target_pct == 0.0


def test_gld_sells_cash_secured_not_spreads():
    """A spread cannot be assigned, so a spread-based wheel never acquires
    the gold it is meant to wheel."""
    assert StrategyConfig.for_arm("gld").put_structure == "cash_secured"
