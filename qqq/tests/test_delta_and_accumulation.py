"""
Delta aggregation and share accumulation.

Both fix defects that were invisible in results rather than loud: the
aggregator's sign error cancelled out because the strategy only ever traded
spreads, and accumulation was impossible because a defined-risk spread nets
to zero shares at assignment.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from qqq.broker_adapter import PortfolioSnapshot, PositionSnapshot
from qqq.config import StrategyConfig
from qqq.delta import DeltaAggregator

SHORT_PUT = "QQQ260206P00680000"
LONG_PUT = "QQQ260206P00660000"
SHORT_CALL = "QQQ260206C00720000"


class _Broker:
    def __init__(self, deltas: dict[str, float | None]):
        self.deltas = deltas
        self.orders: list = []

    def option_delta(self, symbol):
        return self.deltas.get(symbol)

    def get_underlying_price(self):
        return 700.0

    def submit_single_leg(self, order):
        from qqq.broker_adapter import OrderResult

        self.orders.append(order)
        return OrderResult(True, "ok", 700.0, "filled")

    def today(self):
        return date(2026, 1, 5)


def _cfg(**kw):
    return replace(StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"), **kw)


def _snap(positions, cash=100_000.0, equity=100_000.0):
    return PortfolioSnapshot(equity=equity, cash=cash, buying_power=cash, positions=positions)


def _eq(qty):
    return PositionSnapshot("QQQ", qty, 700.0, 700.0, qty * 700.0, 0.0, "equity")


def _opt(sym, qty):
    return PositionSnapshot(sym, qty, 5.0, 5.0, qty * 500.0, 0.0, "option")


# ---- delta aggregation ---------------------------------------------------
def test_shares_count_as_units():
    agg = DeltaAggregator(_Broker({}), _cfg())
    assert agg.total_unit_delta(_snap([_eq(250)])) == pytest.approx(2.5)


def test_a_short_put_is_bullish_not_bearish():
    """The core of the old bug: qty was used directly, scoring a short
    20-delta put as -1.00 units — short 100 shares — when it is about +0.20."""
    agg = DeltaAggregator(_Broker({SHORT_PUT: -0.20}), _cfg())
    assert agg.total_unit_delta(_snap([_opt(SHORT_PUT, -1)])) == pytest.approx(0.20)


def test_a_long_put_is_bearish():
    agg = DeltaAggregator(_Broker({LONG_PUT: -0.05}), _cfg())
    assert agg.total_unit_delta(_snap([_opt(LONG_PUT, 1)])) == pytest.approx(-0.05)


def test_a_short_call_is_bearish():
    agg = DeltaAggregator(_Broker({SHORT_CALL: 0.20}), _cfg())
    assert agg.total_unit_delta(_snap([_opt(SHORT_CALL, -1)])) == pytest.approx(-0.20)


def test_a_put_credit_spread_has_positive_net_delta():
    """Previously this summed to exactly zero, which is why the sign error
    never surfaced — spreads were the only thing the strategy traded."""
    agg = DeltaAggregator(_Broker({SHORT_PUT: -0.20, LONG_PUT: -0.05}), _cfg())
    total = agg.total_unit_delta(_snap([_opt(SHORT_PUT, -1), _opt(LONG_PUT, 1)]))
    assert total == pytest.approx(0.15)
    assert total > 0, "a put credit spread profits when the underlying rises"


def test_shares_and_options_combine():
    agg = DeltaAggregator(_Broker({SHORT_PUT: -0.20, LONG_PUT: -0.05}), _cfg())
    total = agg.total_unit_delta(_snap([_eq(100), _opt(SHORT_PUT, -1), _opt(LONG_PUT, 1)]))
    assert total == pytest.approx(1.15)


def test_an_unknown_greek_contributes_nothing_rather_than_a_guess():
    agg = DeltaAggregator(_Broker({SHORT_PUT: None}), _cfg())
    assert agg.total_unit_delta(_snap([_eq(100), _opt(SHORT_PUT, -1)])) == pytest.approx(1.0)


def test_multiple_contracts_scale_the_delta():
    agg = DeltaAggregator(_Broker({SHORT_PUT: -0.20}), _cfg())
    assert agg.total_unit_delta(_snap([_opt(SHORT_PUT, -3)])) == pytest.approx(0.60)


# ---- share accumulation --------------------------------------------------
def _cycle(broker, **cfg_kw):
    from qqq.cycle import StrategyCycle

    return StrategyCycle(broker, _cfg(**cfg_kw))


def test_accumulation_is_off_by_default():
    broker = _Broker({})
    cyc = _cycle(broker, ladder_accumulate_shares_per_zone=0)
    from qqq.state import PortfolioState

    cyc._accumulate_shares(PortfolioState(), _snap([_eq(100)]), 700.0, 3.0, 1.0)
    assert broker.orders == []


def test_accumulation_buys_toward_the_target():
    broker = _Broker({})
    cyc = _cycle(broker, ladder_accumulate_shares_per_zone=25, equity_basis_override=None)
    from qqq.state import PortfolioState

    cyc._accumulate_shares(
        PortfolioState(), _snap([_eq(100)], equity=1_000_000.0), 700.0, 3.0, 1.0
    )
    assert len(broker.orders) == 1
    assert broker.orders[0].side == "buy"
    assert broker.orders[0].qty == 25


def test_accumulation_never_overshoots_the_curve():
    """Shortfall is 10 shares; the per-fire cap of 25 must not be taken."""
    broker = _Broker({})
    cyc = _cycle(broker, ladder_accumulate_shares_per_zone=25, equity_basis_override=None)
    from qqq.state import PortfolioState

    cyc._accumulate_shares(
        PortfolioState(), _snap([_eq(100)], equity=1_000_000.0), 700.0, 1.10, 1.0
    )
    assert broker.orders[0].qty == 10


def test_accumulation_is_bounded_by_settled_cash():
    broker = _Broker({})
    cyc = _cycle(broker, ladder_accumulate_shares_per_zone=100, equity_basis_override=None)
    from qqq.state import PortfolioState

    cyc._accumulate_shares(
        PortfolioState(), _snap([_eq(100)], cash=7_000.0, equity=1_000_000.0), 700.0, 3.0, 1.0
    )
    assert broker.orders[0].qty == 10, "only 10 shares are affordable at 700 with 7,000 cash"


def test_accumulation_does_nothing_when_already_at_target():
    broker = _Broker({})
    cyc = _cycle(broker, ladder_accumulate_shares_per_zone=25, equity_basis_override=None)
    from qqq.state import PortfolioState

    cyc._accumulate_shares(PortfolioState(), _snap([_eq(100)]), 700.0, 1.0, 1.0)
    assert broker.orders == []


def test_accumulation_is_refused_when_it_would_breach_crash_stress():
    """Shares carry uncapped downside, so this is the gate that sees the real
    risk of accumulating — and it must be able to say no."""
    broker = _Broker({})
    cyc = _cycle(broker, ladder_accumulate_shares_per_zone=100,
                 equity_basis_override=None, max_crash_stress_pct=0.01)
    from qqq.state import PortfolioState

    cyc._accumulate_shares(PortfolioState(), _snap([_eq(100)]), 700.0, 3.0, 1.0)
    assert broker.orders == []


# ---- no naked calls (§8, §9 gate 4) --------------------------------------
def test_coverage_gate_counts_the_contract_being_proposed():
    """Counting only already-open calls always permits one more than coverage
    allows — a naked call whenever excess inventory is under one unit."""
    from qqq.risk import RiskManager

    risk = RiskManager(_cfg())
    assert risk.check_call_coverage([], excess_units=0.45, proposing=1).passed is False
    assert risk.check_call_coverage([], excess_units=1.0, proposing=1).passed is True


def test_coverage_gate_without_a_proposal_still_reads_the_book():
    from qqq.risk import RiskManager
    from qqq.state import CallPosition

    risk = RiskManager(_cfg())
    calls = [CallPosition(id="c1", short_strike=720.0, expiry="2026-02-06",
                          contracts=2, premium_received=3.0, opened_at="x")]
    assert risk.check_call_coverage(calls, excess_units=1.0, proposing=0).passed is False
    assert risk.check_call_coverage(calls, excess_units=2.0, proposing=0).passed is True


def test_excess_inventory_is_shares_never_option_delta():
    """A call is covered by stock alone. Put-spread delta cannot deliver
    shares if the call is exercised, so it must not read as coverage."""
    from qqq.cycle import StrategyCycle
    from qqq.state import PortfolioState

    broker = _Broker({SHORT_PUT: -0.20, LONG_PUT: -0.05})
    cyc = StrategyCycle(broker, _cfg())
    state = PortfolioState()
    snap = _snap([_eq(100), _opt(SHORT_PUT, -3), _opt(LONG_PUT, 3)])

    held = sum(p.qty for p in snap.positions if p.asset_class == "equity")
    excess = max(0.0, held / 100 - state.core_units)
    assert excess == 0.0, "100 shares against a 1.00-unit core is zero excess inventory"

    agg = DeltaAggregator(broker, _cfg())
    assert agg.total_unit_delta(snap) > 1.0, "delta does exceed the core — which is the trap"


def test_excess_inventory_appears_once_real_shares_are_accumulated():
    from qqq.state import PortfolioState

    state = PortfolioState()
    snap = _snap([_eq(250)])
    held = sum(p.qty for p in snap.positions if p.asset_class == "equity")
    assert max(0.0, held / 100 - state.core_units) == pytest.approx(1.5)


# ---- gates must judge the resulting book, not the prior one --------------
def _spread(i, width=16.0, credit=1.71):
    from qqq.state import PutSpreadPosition

    return PutSpreadPosition(
        id=f"s{i}", short_strike=700.0, long_strike=700.0 - width, expiry="2026-12-18",
        contracts=1, net_credit=credit, opened_at="x",
    )


def test_crash_stress_counts_the_spread_being_proposed():
    """Checking the book before the trade approves anything whose predecessors
    were within limits, permitting exactly one spread more than the cap allows.
    The symptom was a 'cap breached at end of cycle' warning on every run."""
    from qqq.risk import RiskManager

    # Pins its own 15% cap: this test is about the off-by-one mechanic, not
    # about whatever the strategy is currently tuned to. Reading the live
    # value makes it fail on a retune, which is a false alarm.
    cfg = _cfg(equity_basis_override=None, max_crash_stress_pct=0.15,
               crash_stress_binding_shock=-0.20)
    risk = RiskManager(cfg)
    # Sized so the core alone fits under the -20% shock (§31's binding case)
    # and the core plus one spread does not — which is exactly the boundary
    # the off-by-one used to slip through.
    basis, price = 100_000.0, 717.59

    # Core alone fits; core plus one spread does not.
    assert risk.check_crash_stress(price, [], [], 1.0, basis).passed is True
    assert risk.check_crash_stress(price, [_spread(0)], [], 1.0, basis).passed is False

    # So proposing that first spread must be refused.
    result = risk.check_all_for_new_put_spread(
        short_strike=700.0, long_strike=684.0, net_credit=1.71, contracts=1,
        equity=basis, existing_open_spreads=[], underlying_price=price,
        core_units=1.0, open_calls=[],
    )
    assert result.passed is False, "gate approved a trade that puts the book over the cap"


def test_aggregate_gate_also_counts_the_proposed_spread():
    from qqq.risk import RiskManager

    cfg = _cfg(equity_basis_override=None)
    risk = RiskManager(cfg)
    # Aggregate cap is 5% — at a 20,000 basis that is 1,000, under one spread.
    result = risk.check_all_for_new_put_spread(
        short_strike=700.0, long_strike=684.0, net_credit=1.71, contracts=1,
        equity=20_000.0, existing_open_spreads=[], underlying_price=300.0,
        core_units=0.0, open_calls=[],
    )
    assert result.passed is False


def test_a_spread_is_still_allowed_when_the_resulting_book_fits():
    from qqq.risk import RiskManager

    cfg = _cfg(equity_basis_override=None)
    risk = RiskManager(cfg)
    result = risk.check_all_for_new_put_spread(
        short_strike=450.0, long_strike=434.0, net_credit=1.71, contracts=1,
        equity=150_000.0, existing_open_spreads=[], underlying_price=445.0,
        core_units=1.0, open_calls=[],
    )
    assert result.passed is True, "gate refused a trade the caps comfortably allow"
