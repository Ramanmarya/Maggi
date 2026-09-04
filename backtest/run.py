"""
Backtest runner.

    python3 -m backtest.run --start 2025-06-02 --end 2026-08-28
    python3 -m backtest.run --start 2026-06-01 --end 2026-08-28 --verbose

Steps the real StrategyCycle over real NYSE sessions with a real ledger, so
the equity curve reflects fills, marks, commissions, modelled spread cost and
assignment. The strategy code is untouched: the same cycle.py and engines run
here as in paper.

The order gate is deliberately bypassed — the backtest asks what the strategy
would have done, and routing it through the live kill switch and phase would
make results depend on the operator's current settings.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.adapter import BacktestBroker  # noqa: E402
from backtest.calendar import trading_days  # noqa: E402
from backtest.costs import CostModel  # noqa: E402
from backtest.data import HistoricalData  # noqa: E402
from backtest.metrics import compute  # noqa: E402
from qqq.config import StrategyConfig  # noqa: E402
from qqq.cycle import StrategyCycle  # noqa: E402
from qqq.state import PortfolioState, save_state  # noqa: E402


def _make_data(config, source: str, verbose: bool, use_quotes: bool | None):
    """Pick the historical data backend.

    Alpaca is the default and reaches back to 2024-02. Polygon reaches five
    years on Options Advanced and is the only source carrying historical NBBO.
    Both write to the same cache under bare OCC symbols, so a window fetched
    by one is readable by the other.
    """
    if source == "polygon":
        from backtest.polygon_data import PolygonHistoricalData

        if not config.polygon_api_key:
            raise SystemExit("POLYGON_API_KEY required for --source polygon.")
        return PolygonHistoricalData(config, verbose=verbose, use_quotes=use_quotes)
    if source != "alpaca":
        raise SystemExit(f"unknown --source {source!r}; expected alpaca or polygon")
    if not config.alpaca_api_key:
        raise SystemExit("Alpaca credentials required (historical data source).")
    return HistoricalData(config, verbose=verbose)


def run(start: date, end: date, starting_equity: float, verbose: bool,
        state_path: Path | None = None, source: str | None = None,
        use_quotes: bool | None = None) -> tuple:
    config = StrategyConfig()
    source = source or config.backtest_source

    # Isolated state file: a backtest must never touch live trading state.
    state_file = state_path or (ROOT / "backtest" / "state" / "backtest_state.json")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    save_state(PortfolioState(), state_file)
    config = _with_state(config, state_file)

    data = _make_data(config, source, verbose, use_quotes)
    broker = BacktestBroker(config, data, starting_equity, CostModel())
    broker.prime(start, end)
    cycle = StrategyCycle(broker, config)

    sessions = trading_days(config, start, end)
    if not sessions:
        raise SystemExit(f"no NYSE sessions between {start} and {end}")

    quotes_on = getattr(data, "use_quotes", False)
    print(f"Backtest {start} .. {end}  ({len(sessions)} sessions)")
    print(f"  data source     {source}"
          f"{'  (measured NBBO)' if quotes_on else '  (modelled spread)'}")
    print(f"  starting equity ${starting_equity:,.2f}")
    basis = config.equity_basis_override
    basis_text = f"${basis:,.2f} (override)" if basis else "live equity"
    print(f"  risk basis      {basis_text}")
    logging.getLogger("qqq.cycle").setLevel(logging.ERROR)  # the override is reported once, above
    print()

    # Seed the curve with the pre-trade balance on the session before the
    # first, so day one's own fills are inside the measured return rather
    # than silently forming the baseline.
    curve: list[tuple[date, float]] = [(sessions[0] - timedelta(days=1), starting_equity)]
    for i, day in enumerate(sessions):
        broker.set_as_of(day)
        try:
            cycle.run_daily_cycle()
        except Exception as e:
            print(f"  {day}  CYCLE ERROR {type(e).__name__}: {e}")
        for ev in broker.settle():
            print(f"  {day}  {ev}")
        equity = broker.get_current_positions().equity
        curve.append((day, equity))
        if verbose or i % 21 == 0 or i == len(sessions) - 1:
            spot = broker.get_underlying_price()
            print(f"  {day}  QQQ {spot:>7.2f}  equity ${equity:>12,.2f}  "
                  f"cash ${broker.ledger.cash:>11,.2f}  pos {len(broker.ledger.positions)}")

    commission = sum(
        CostModel().option_commission(abs(f.qty)) for f in broker.ledger.fills if f.kind == "option"
    )
    metrics = compute(curve, broker.ledger, commission)
    print(f"\n{'='*62}\n  RESULTS\n{'='*62}")
    print(metrics.render())
    print(f"\n  data requests: {data.requests}   cache: {data.cache.stats()}")
    return metrics, curve, broker


def _with_state(config: StrategyConfig, path: Path) -> StrategyConfig:
    from dataclasses import replace

    return replace(config, state_file_path=path)


def main() -> int:
    ap = argparse.ArgumentParser(description="QQQ drift-harvest backtest")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--starting-equity", type=float, default=100_000.0)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--source", choices=("alpaca", "polygon"), default=None,
                    help="historical data backend (default: rules.json backtest.source). "
                         "alpaca reaches 2024-02; polygon reaches 5y on Options Advanced")
    ap.add_argument("--quotes", dest="quotes", action="store_true", default=None,
                    help="use measured NBBO instead of the modelled spread "
                         "(polygon + Options Advanced only)")
    ap.add_argument("--no-quotes", dest="quotes", action="store_false",
                    help="force the modelled spread even if quotes are configured")
    a = ap.parse_args()
    run(datetime.strptime(a.start, "%Y-%m-%d").date(),
        datetime.strptime(a.end, "%Y-%m-%d").date(),
        a.starting_equity, a.verbose, source=a.source, use_quotes=a.quotes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
