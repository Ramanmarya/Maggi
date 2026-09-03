"""
Entrypoint: backtest against Polygon.io historical data.

    python3 -m qqq.run_backtest --start 2023-01-01 --end 2025-01-01

NOTE: the historical option chain IS implemented (contract universe from
Polygon's reference endpoint, then a per-contract quote lookup, with greeks
recovered via Black-Scholes). What is NOT implemented is fill accounting:
submit_* in backtest_adapter.py does not update cash, equity or the position
list, so the equity curve stays flat at the starting balance. This entrypoint
exercises the decision path against real data; it does not yet produce
returns. See ALGORITHM.md 11.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta

from .backtest_adapter import BacktestBrokerAdapter
from .config import StrategyConfig
from .cycle import StrategyCycle
from .metrics import format_metrics_line, snapshot_metrics
from .state import PortfolioState, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("qqq_bot.run_backtest")


def daterange(start: date, end: date):
    d = start
    while d <= end:
        # Skip weekends; a real implementation should use a market calendar
        # (NYSE holidays) rather than just weekday filtering.
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--starting-equity", type=float, default=100_000.0)
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    config = StrategyConfig()
    if not config.polygon_api_key:
        logger.error("POLYGON_API_KEY not set — cannot backtest.")
        return

    # Fresh state for the backtest run, isolated from any live-trading state file.
    backtest_state_path = config.state_file_path.parent / "backtest_state.json"
    save_state(PortfolioState(), backtest_state_path)
    object.__setattr__(config, "state_file_path", backtest_state_path)

    adapter = BacktestBrokerAdapter(config, starting_equity=args.starting_equity)
    cycle = StrategyCycle(adapter, config)

    for day in daterange(start, end):
        adapter.set_as_of(day)
        try:
            state = cycle.run_daily_cycle()
        except NotImplementedError as e:
            logger.error("Backtest halted — %s", e)
            return
        snapshot = adapter.get_current_positions()
        logger.info("%s | %s", day.isoformat(), format_metrics_line(snapshot_metrics(state, snapshot)))

    logger.info("Backtest complete: %s to %s", start.isoformat(), end.isoformat())


if __name__ == "__main__":
    main()
