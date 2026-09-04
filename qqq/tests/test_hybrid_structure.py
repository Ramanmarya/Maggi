"""Hybrid: cash-secured slots for inventory, spreads for the rest.

A defined-risk spread can never deliver shares — assignment of the short and
exercise of the long net to zero — so a pure-spread book never acquires the
core. An unspread put is charged its WHOLE STRIKE under §31's shock, so a
pure cash-secured book fits one position on this account. The hybrid keeps
the first N unspread for inventory and spreads the rest for count.
"""
from dataclasses import replace

import pytest

from qqq.config import StrategyConfig
from qqq.put_engine import PutSpreadEngine
from qqq.state import PortfolioState, PutSpreadPosition


def _pos(i, long_strike, status="OPEN"):
    return PutSpreadPosition(
        id=f"p{i}", short_strike=700.0, long_strike=long_strike, expiry="2026-10-17",
        contracts=1, net_credit=10.0, opened_at="2026-09-03", status=status,
        close_price=None, closed_at=None, short_symbol="S", long_symbol="L",
    )


def _engine(structure, slots=2, protective=True):
    cfg = replace(
        StrategyConfig(), put_structure=structure,
        hybrid_cash_secured_slots=slots, put_protective_leg=protective,
    )
    e = PutSpreadEngine.__new__(PutSpreadEngine)
    e._config = cfg
    return e


def _state(positions):
    st = PortfolioState()
    st.open_put_spreads = positions
    return st


@pytest.mark.parametrize("n,expected", [(0, True), (1, True), (2, False), (5, False)])
def test_slots_fill_with_cash_secured_then_switch_to_spreads(n, expected):
    e = _engine("hybrid", slots=2)
    assert e._structure_for_next_put(_state([_pos(i, 0.0) for i in range(n)])) is expected


def test_closed_positions_do_not_consume_a_slot():
    """Otherwise the book permanently stops writing cash-secured puts."""
    e = _engine("hybrid", slots=2)
    closed = [_pos(i, 0.0, status="CLOSED") for i in range(9)]
    assert e._structure_for_next_put(_state(closed)) is True


def test_spreads_do_not_consume_a_cash_secured_slot():
    """Slots exist to guarantee INVENTORY; a spread can never deliver shares."""
    e = _engine("hybrid", slots=2)
    st = _state([_pos(i, 650.0) for i in range(6)] + [_pos(99, 0.0)])
    assert e._structure_for_next_put(st) is True


def test_zero_slots_is_pure_spreads():
    assert _engine("hybrid", slots=0)._structure_for_next_put(_state([])) is False


@pytest.mark.parametrize("structure,expected", [("cash_secured", True), ("spread", False)])
def test_existing_structures_are_untouched_by_the_hybrid_branch(structure, expected):
    e = _engine(structure)
    for n in (0, 3, 10):
        st = _state([_pos(i, 0.0) for i in range(n)])
        assert e._structure_for_next_put(st) is expected


def test_hybrid_overflow_is_never_naked_even_with_protective_leg_off():
    """§39 Q1 testing disables the protective leg. That must not turn the
    hybrid's overflow into unlimited-risk naked puts."""
    import inspect
    src = inspect.getsource(PutSpreadEngine.propose_spread)
    assert 'or self._config.put_structure == "hybrid"' in src, (
        "hybrid overflow must force a long leg regardless of put_protective_leg"
    )


def test_live_adapter_accepts_a_hybrid_cash_secured_put():
    """The adapter refuses unspread puts unless the structure permits them.
    Whitelisting only 'cash_secured' would make every hybrid slot order fail
    live while the backtest, which uses a different adapter, stayed green."""
    import inspect
    from qqq.alpaca_adapter import AlpacaAdapter
    src = inspect.getsource(AlpacaAdapter.submit_vertical_spread)
    assert '("cash_secured", "hybrid")' in src
