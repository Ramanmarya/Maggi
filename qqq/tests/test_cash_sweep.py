"""
Cash-sweep tests.

The sweep exists because idle cash earning nothing costs more than the whole
options overlay produces. Its one hard requirement is that it must never
starve the options program: the reserve has to survive every open spread
losing its maximum at once, plus an assignment, or the arm ends up force-
selling Treasuries at the worst possible moment.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from qqq.broker_adapter import PortfolioSnapshot, PositionSnapshot
from qqq.cash_sweep import execute, plan, required_reserve
from qqq.config import StrategyConfig

PRICE = 100.25


def _cfg(**kw):
    base = replace(
        StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"),
        cash_sweep_enabled=True, cash_sweep_symbol="SGOV",
        cash_sweep_buffer=5_000.0, cash_sweep_min_trade=1_000.0,
        equity_basis_override=150_000.0, max_aggregate_put_risk_pct=0.05,
    )
    return replace(base, **kw) if kw else base


# ---- the reserve ---------------------------------------------------------
def test_reserve_covers_the_aggregate_put_cap_plus_buffer():
    """5% of the 150k basis is 7,500; plus a 5,000 buffer is 12,500."""
    assert required_reserve(_cfg(), 100_000.0) == pytest.approx(12_500.0)


def test_reserve_falls_back_to_real_equity_when_no_basis_override():
    cfg = _cfg(equity_basis_override=None)
    assert required_reserve(cfg, 100_000.0) == pytest.approx(10_000.0)


# ---- the decision --------------------------------------------------------
def test_sweeps_only_the_cash_above_the_reserve():
    r = plan(_cfg(), cash=28_500.0, equity=100_000.0, sweep_price=PRICE, sweep_held=0)
    assert r.action == "buy"
    assert r.shares == int((28_500.0 - 12_500.0) // PRICE)


def test_holds_when_cash_is_inside_the_reserve():
    r = plan(_cfg(), cash=12_000.0, equity=100_000.0, sweep_price=PRICE, sweep_held=0)
    assert r.action == "hold"


def test_sells_back_when_cash_dips_under_the_reserve():
    """A spread loss or an assignment can pull cash below the line; the sweep
    must restore it rather than leave the arm unable to trade."""
    r = plan(_cfg(), cash=9_000.0, equity=100_000.0, sweep_price=PRICE, sweep_held=160)
    assert r.action == "sell"
    assert r.shares >= (12_500.0 - 9_000.0) / PRICE


def test_cannot_sell_more_than_it_holds():
    r = plan(_cfg(), cash=0.0, equity=100_000.0, sweep_price=PRICE, sweep_held=5)
    assert r.action == "sell"
    assert r.shares <= 5


def test_ignores_trivial_rebalances():
    """Churn has no upside at this yield. $13,000 is only $500 over the
    $12,500 reserve, inside the dead band."""
    r = plan(_cfg(), cash=13_000.0, equity=100_000.0, sweep_price=PRICE, sweep_held=0)
    assert r.action == "hold"


def test_does_not_thrash_around_the_reserve():
    """Regression. Without a dead band the sweep bought whenever cash was a
    dollar over the reserve and sold whenever it was a dollar under, which in
    backtest produced a buy and a sell of the same size on alternate days,
    every day, paying the spread each time for no yield at all."""
    cfg = _cfg()
    reserve = required_reserve(cfg, 100_000.0)
    band = cfg.cash_sweep_min_trade
    # Anywhere inside +/- one band of the reserve, do nothing.
    for cash in (reserve - band * 0.9, reserve, reserve + band * 0.9):
        assert plan(cfg, cash, 100_000.0, PRICE, sweep_held=200).action == "hold", cash
    # Outside it, act.
    assert plan(cfg, reserve + band * 3, 100_000.0, PRICE, 200).action == "buy"
    assert plan(cfg, reserve - band * 3, 100_000.0, PRICE, 200).action == "sell"


def test_disabled_by_configuration_does_nothing():
    r = plan(_cfg(cash_sweep_enabled=False), cash=90_000.0, equity=100_000.0,
             sweep_price=PRICE, sweep_held=0)
    assert r.action == "hold"


def test_missing_price_does_nothing_rather_than_guessing():
    for bad in (None, 0.0, -1.0):
        assert plan(_cfg(), cash=90_000.0, equity=100_000.0,
                    sweep_price=bad, sweep_held=0).action == "hold"


def test_a_larger_reserve_sweeps_less():
    small = plan(_cfg(cash_sweep_buffer=1_000.0), 28_500.0, 100_000.0, PRICE, 0)
    large = plan(_cfg(cash_sweep_buffer=15_000.0), 28_500.0, 100_000.0, PRICE, 0)
    assert small.shares > large.shares


# ---- execution -----------------------------------------------------------
class _Broker:
    def __init__(self, price=PRICE):
        self.price = price
        self.orders = []

    def equity_price(self, symbol):
        return self.price

    def submit_single_leg(self, order):
        from qqq.broker_adapter import OrderResult

        self.orders.append(order)
        return OrderResult(True, "ok", self.price, "filled")


def _snap(cash, held=0.0):
    pos = []
    if held:
        pos.append(PositionSnapshot("SGOV", held, PRICE, PRICE, held * PRICE, 0.0, "equity"))
    return PortfolioSnapshot(equity=100_000.0, cash=cash, buying_power=cash, positions=pos)


def test_execute_places_a_buy_for_the_excess():
    b = _Broker()
    execute(b, _cfg(), _snap(28_500.0))
    assert len(b.orders) == 1
    assert b.orders[0].side == "buy" and b.orders[0].symbol == "SGOV"


def test_execute_places_nothing_when_holding():
    b = _Broker()
    execute(b, _cfg(), _snap(12_000.0))
    assert b.orders == []


def test_execute_sees_its_own_holding_when_selling():
    b = _Broker()
    execute(b, _cfg(), _snap(5_000.0, held=200))
    assert b.orders and b.orders[0].side == "sell"


def test_yield_is_material_against_the_options_overlay():
    """Guards the premise for building this at all."""
    idle = 28_500.0 - required_reserve(_cfg(), 100_000.0)
    assert idle * 0.045 > 500, "if the swept yield were trivial this module would not earn its keep"


def test_sweep_holding_is_not_counted_as_nasdaq_exposure():
    """Regression. SGOV is a cash equivalent. Counted as equity units it read
    as 4.52 units of QQQ exposure, put total delta far above the target curve,
    and silently stopped the put engine writing anything at all."""
    from qqq.delta import DeltaAggregator

    class _B:
        def option_delta(self, s): return None

    cfg = _cfg()
    agg = DeltaAggregator(_B(), cfg)
    snap = PortfolioSnapshot(
        equity=100_000.0, cash=10_000.0, buying_power=10_000.0,
        positions=[
            PositionSnapshot("QQQ", 100, 445.0, 445.0, 44_500.0, 0.0, "equity"),
            PositionSnapshot("SGOV", 452, 100.33, 100.33, 45_349.0, 0.0, "equity"),
        ],
    )
    assert agg.total_unit_delta(snap) == pytest.approx(1.0)
