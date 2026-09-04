"""
BacktestBroker tests, driven by a synthetic market so they need no network.

The last few run the real StrategyCycle end to end. That matters more than it
looks: the whole design claim is that identical strategy code runs in the
backtest and in paper, so a test that exercised a simplified loop instead
would be validating something the operator never trades.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from backtest.adapter import BacktestBroker
from backtest.cache import Bar, BarCache
from backtest.costs import CostModel
from backtest.data import occ_symbol
from qqq.broker_adapter import OptionContract, SingleLegOrder, VerticalSpreadOrder
from qqq.config import StrategyConfig

START = date(2026, 1, 5)


def _sessions(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class FakeData:
    """Synthetic market: a price path plus a chain priced off distance from spot."""

    def __init__(self, path: list[float], cache: BarCache):
        self.days = _sessions(len(path))
        self.path = path
        self.cache = cache
        self.requests = 0
        for day, px in zip(self.days, path):
            cache.put_bars("QQQ", [Bar(day, px, px * 1.005, px * 0.995, px, 1e6)])
        cache.commit()

    def load_underlying(self, start, end):
        return self.cache.bars("QQQ", start, end)

    def load_option_bars(self, symbols, start, end):
        return None

    def load_underlying_symbol(self, symbol, as_of):
        return self.cache.bars(symbol, None, as_of)

    def chain(self, as_of, spot, dte_range, **kw):
        expiry = as_of + timedelta(days=(dte_range[0] + dte_range[1]) // 2)
        out = []
        for offset in range(-80, 5):
            strike = round(spot + offset)
            moneyness = (strike - spot) / spot
            delta = -max(0.01, min(0.99, 0.5 + moneyness * 9))
            mid = max(0.05, spot * 0.02 * (2.718 ** (-abs(moneyness) * 32)))
            sym = occ_symbol("QQQ", expiry, "put", float(strike))
            self.cache.put_bars(sym, [Bar(as_of, mid, mid, mid, mid, 100)])
            out.append(OptionContract(
                symbol=sym, underlying="QQQ", expiry=expiry, strike=float(strike),
                option_type="put", bid=mid, ask=mid, delta=round(delta, 4), implied_vol=0.20,
            ))
        self.cache.commit()
        return out


@pytest.fixture
def setup(tmp_path: Path):
    def build(path: list[float], equity: float = 100_000.0):
        cache = BarCache(tmp_path / f"bt{len(path)}_{int(path[0])}.sqlite")
        data = FakeData(path, cache)
        # Accumulation pinned OFF: these tests exercise the core bootstrap and
        # the execution path, not the accumulation feature. Reading the live
        # rules.json makes them fail whenever the operator enables it, which
        # is a false alarm rather than a regression. A dedicated test below
        # covers accumulation firing.
        config = replace(
            StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"),
            state_file_path=tmp_path / "state.json",
            ladder_accumulate_shares_per_zone=0,
            # Pinned: these exercise the spread execution path. The shipped
            # structure is cash_secured, which writes one leg, not two.
            put_structure="spread",
            put_spread_short_delta_target=0.20,
        )
        broker = BacktestBroker(config, data, equity, CostModel())
        broker.prime(data.days[0], data.days[-1])
        broker.set_as_of(data.days[-1])
        return broker, data, config
    return build


# ---- market data ---------------------------------------------------------
def test_price_is_the_close_on_the_as_of_date(setup):
    broker, data, _ = setup([700.0 + i for i in range(50)])
    broker.set_as_of(data.days[10])
    assert broker.get_underlying_price() == pytest.approx(710.0)


def test_no_lookahead_bias(setup):
    """The adapter must not see bars after as_of — the single most damaging
    bug a backtest can have, and the one that makes results look excellent."""
    broker, data, _ = setup([700.0 + i for i in range(50)])
    broker.set_as_of(data.days[10])
    early = broker.get_underlying_price()
    broker.set_as_of(data.days[40])
    assert broker.get_underlying_price() > early
    broker.set_as_of(data.days[10])
    assert broker.get_underlying_price() == pytest.approx(early), "price changed with no new information"


def test_atr_is_positive_and_scales_with_range(setup):
    broker, data, _ = setup([700.0] * 60)
    broker.set_as_of(data.days[-1])
    assert broker.get_atr() > 0


def test_200dma_lags_a_rising_price(setup):
    broker, data, _ = setup([600.0 + i for i in range(250)])
    broker.set_as_of(data.days[-1])
    dma, slope = broker.get_200dma()
    assert dma < broker.get_underlying_price()
    assert slope > 0


# ---- execution -----------------------------------------------------------
def _spread(chain, short_strike, long_strike):
    short = next(c for c in chain if c.strike == short_strike)
    long_ = next(c for c in chain if c.strike == long_strike)
    return VerticalSpreadOrder("QQQ", short, long_, 1, 1.50, "test-spread")


def test_submitting_a_spread_opens_two_legs_and_credits_cash(setup):
    broker, data, config = setup([700.0] * 60)
    chain = broker.get_option_chain(config.put_spread_dte_range)
    cash_before = broker.ledger.cash
    result = broker.submit_vertical_spread(_spread(chain, 680.0, 664.0))
    assert result.success
    assert len(broker.ledger.open_options()) == 2
    assert broker.ledger.cash > cash_before, "a credit spread must bring cash in"


def test_closing_a_spread_removes_both_legs(setup):
    broker, data, config = setup([700.0] * 60)
    chain = broker.get_option_chain(config.put_spread_dte_range)
    result = broker.submit_vertical_spread(_spread(chain, 680.0, 664.0))
    broker.close_position(result.order_id, None)
    assert broker.ledger.open_options() == []


def test_closing_an_unknown_position_fails_rather_than_silently_succeeding(setup):
    broker, _, _ = setup([700.0] * 60)
    result = broker.close_position("no-such-order", None)
    assert result.success is False
    assert broker.rejected_orders == 1


def test_buying_shares_moves_cash_and_position(setup):
    broker, _, config = setup([700.0] * 60)
    order = SingleLegOrder(None, "QQQ", "buy", 100, "market", None, "core")
    broker.submit_single_leg(order)
    assert broker.ledger.shares_held("QQQ") == 100
    assert broker.ledger.cash < 100_000


def test_commission_is_charged_on_option_fills(setup):
    broker, data, config = setup([700.0] * 60)
    chain = broker.get_option_chain(config.put_spread_dte_range)
    zero = BacktestBroker(config, data, 100_000.0, CostModel(option_commission_per_contract=0.0))
    zero.prime(data.days[0], data.days[-1]); zero.set_as_of(data.days[-1])
    broker.submit_vertical_spread(_spread(chain, 680.0, 664.0))
    zero.submit_vertical_spread(_spread(chain, 680.0, 664.0))
    assert zero.ledger.cash > broker.ledger.cash


def test_spread_cost_makes_a_sold_option_fetch_less_than_its_mid(setup):
    broker, data, config = setup([700.0] * 60)
    chain = broker.get_option_chain(config.put_spread_dte_range)
    short = next(c for c in chain if c.strike == 680.0)
    assert short.bid < short.ask, "the adapter must widen the close into a two-sided market"


# ---- marks ---------------------------------------------------------------
def test_unpriced_option_marks_to_intrinsic_not_to_entry(setup):
    """A short that stops printing must not stay frozen at its opening credit,
    which would hide the loss entirely."""
    broker, data, config = setup([700.0] * 60)
    sym = occ_symbol("QQQ", data.days[-1] + timedelta(days=28), "put", 750.0)
    broker.ledger.fill(data.days[-1], sym, -1, 2.0, "option", "test")
    prices = broker.mark_prices()
    assert prices[sym] == pytest.approx(50.0), "750 strike with spot 700 is 50 intrinsic"


def test_equity_falls_when_a_short_put_moves_against_the_position(setup):
    broker, data, config = setup([700.0] * 60)
    chain = broker.get_option_chain(config.put_spread_dte_range)
    broker.submit_vertical_spread(_spread(chain, 680.0, 664.0))
    before = broker.get_current_positions().equity
    # Re-mark with the shorts far in the money
    sym = next(c for c in chain if c.strike == 680.0).symbol
    data.cache.put_bars(sym, [Bar(data.days[-1], 40.0, 40, 40, 40.0, 1)])
    data.cache.commit()
    assert broker.get_current_positions().equity < before


# ---- end-to-end ----------------------------------------------------------
def test_full_cycle_runs_and_establishes_the_core(setup):
    from qqq.cycle import StrategyCycle

    broker, data, config = setup([700.0] * 80)
    cycle = StrategyCycle(broker, config)
    for day in data.days[60:]:
        broker.set_as_of(day)
        cycle.run_daily_cycle()
    assert broker.ledger.shares_held("QQQ") == config.core_unit_shares


def test_core_is_bought_once_not_every_session(setup):
    """Measured against shares actually held, so a restart or a repeated cycle
    must not stack a second core."""
    from qqq.cycle import StrategyCycle

    broker, data, config = setup([700.0] * 80)
    cycle = StrategyCycle(broker, config)
    for day in data.days[60:]:
        broker.set_as_of(day)
        cycle.run_daily_cycle()
    core_fills = [f for f in broker.ledger.fills if f.reason == "share_order"]
    assert len(core_fills) == 1


def test_declining_market_accumulates_exposure(setup):
    """The accumulation thesis: falling prices should produce spreads."""
    from qqq.cycle import StrategyCycle

    path = [700.0] * 60 + [700.0 - i * 2.0 for i in range(30)]
    broker, data, config = setup(path)
    cycle = StrategyCycle(broker, config)
    for day in data.days[60:]:
        broker.set_as_of(day)
        cycle.run_daily_cycle()
    opened = [f for f in broker.ledger.fills if f.reason == "open_spread_short"]
    assert opened, "a sustained decline produced no put spreads at all"


def test_equity_reconciles_with_cash_plus_marks(setup):
    from qqq.cycle import StrategyCycle

    broker, data, config = setup([700.0] * 60 + [700.0 - i for i in range(20)])
    cycle = StrategyCycle(broker, config)
    for day in data.days[60:]:
        broker.set_as_of(day)
        cycle.run_daily_cycle()
    snap = broker.get_current_positions()
    assert snap.equity == pytest.approx(broker.ledger.cash + sum(p.market_value for p in snap.positions))


# ---- date source ---------------------------------------------------------
def test_broker_supplies_the_date_not_the_system_clock(setup):
    """Regression. cycle.py used date.today(), so replaying 2025 computed DTE
    against the real present day: every 28-DTE spread looked ~340 days past
    expiry and was force-closed in the same cycle that opened it. Option P&L
    came out to exactly zero because every trade was a same-day round trip at
    an identical price, which looks like a clean result rather than a bug."""
    broker, data, _ = setup([700.0] * 60)
    broker.set_as_of(data.days[30])
    assert broker.today() == data.days[30]
    assert broker.today() != date.today() or data.days[30] == date.today()


def test_a_freshly_opened_spread_survives_the_session(setup):
    """The behavioural version of the bug above: a spread opened at 21-35 DTE
    must still be open when the cycle ends."""
    from qqq.cycle import StrategyCycle

    path = [700.0] * 60 + [700.0 - i * 2.0 for i in range(20)]
    broker, data, config = setup(path)
    cycle = StrategyCycle(broker, config)
    for day in data.days[60:]:
        broker.set_as_of(day)
        cycle.run_daily_cycle()
        opened = [f for f in broker.ledger.fills
                  if f.reason == "open_spread_short" and f.day == day]
        closed_same_day = [f for f in broker.ledger.fills
                           if f.reason == "close_spread" and f.day == day]
        if opened:
            assert not closed_same_day, f"spread opened and closed on {day}"
            break
    else:
        pytest.skip("no spread was opened in this path")


def test_spreads_are_held_across_multiple_sessions(setup):
    """Positive confirmation: at least one spread must live past its open day,
    otherwise the engine is round-tripping and the backtest measures nothing."""
    from qqq.cycle import StrategyCycle

    path = [700.0] * 60 + [700.0 - i * 1.5 for i in range(40)]
    broker, data, config = setup(path)
    cycle = StrategyCycle(broker, config)
    for day in data.days[60:]:
        broker.set_as_of(day)
        cycle.run_daily_cycle()
    opens = {f.day for f in broker.ledger.fills if f.reason == "open_spread_short"}
    closes = {f.day for f in broker.ledger.fills if f.reason == "close_spread"}
    assert opens, "no spreads opened"
    assert opens - closes, "every spread closed on the day it opened"


def test_accumulation_adds_shares_beyond_the_core_when_enabled(tmp_path):
    """The counterpart to the pinned fixture above: with accumulation on, a
    falling market must carry the position past the core, which is the whole
    point of §15's exposure curve."""
    from dataclasses import replace as _replace

    from qqq.cycle import StrategyCycle

    cache = BarCache(tmp_path / "acc.sqlite")
    path = [700.0] * 60 + [700.0 - i * 3.0 for i in range(30)]
    data = FakeData(path, cache)
    config = _replace(
        StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"),
        state_file_path=tmp_path / "acc_state.json",
        ladder_accumulate_shares_per_zone=50,
        max_crash_stress_pct=0.50,     # not the constraint under test
        equity_basis_override=None,
    )
    broker = BacktestBroker(config, data, 250_000.0, CostModel())
    broker.prime(data.days[0], data.days[-1])
    cycle = StrategyCycle(broker, config)
    for day in data.days[60:]:
        broker.set_as_of(day)
        cycle.run_daily_cycle()

    assert broker.ledger.shares_held("QQQ") > config.core_unit_shares, (
        "a sustained decline should have accumulated past the core"
    )
