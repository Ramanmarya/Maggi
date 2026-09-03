"""
QQQ arm orchestrator — the single entry point, invoked one-shot by launchd.

    python3 -m qqq.orchestrator --mode daily
    python3 -m qqq.orchestrator --mode intraday
    python3 -m qqq.orchestrator --mode preflight

Why one-shot instead of a resident scheduler: a long-lived process that dies
silently at 03:00 leaves no trace and stops trading without telling anyone.
launchd restarts a one-shot on every trigger, logs its exit status, and makes
"did it run?" answerable from the filesystem.

Every mode runs the FULL execution path regardless of phase — the order gate
blocks submission at the broker boundary rather than short-circuiting earlier.
That is deliberate: a phase check that returns early hides NameError- and
ImportError-class bugs in the code that only ever runs when real orders are
being placed, so the first live cycle becomes the first real test.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import circuit_breaker  # noqa: E402
from core.atomic_io import atomic_write_text  # noqa: E402
from core.gated_broker import GatedBroker  # noqa: E402
from core.kill_switch import check_order_gate  # noqa: E402
from core.structured_log import configure, event  # noqa: E402
from qqq.config import StrategyConfig  # noqa: E402
from qqq.cycle import StrategyCycle  # noqa: E402
from qqq.metrics import format_metrics_line, snapshot_metrics  # noqa: E402

LOG_PATH = PROJECT_ROOT / "logs" / "qqq_orchestrator.log"
logger = configure("qqq.orchestrator", LOG_PATH)


def _build_broker(config: StrategyConfig):
    """Construct the live adapter and wrap it in the order gate.

    The engines only ever see the GatedBroker, so there is no code path from
    strategy logic to Alpaca that skips the gate.
    """
    from qqq.alpaca_adapter import AlpacaAdapter

    return GatedBroker(AlpacaAdapter(config), arm="qqq")


def _write_digest(mode: str, lines: list[str]) -> Path:
    path = PROJECT_ROOT / "digests" / f"{date.today().isoformat()}_{mode}.md"
    header = [
        f"# QQQ {mode} cycle — {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    atomic_write_text(path, "\n".join(header + lines) + "\n")
    return path


def run(mode: str) -> int:
    config = StrategyConfig()
    problems = config.validate()
    if problems:
        # Preflight is offline by design — it runs against a stub broker and
        # never needs credentials, so config problems are reported there
        # rather than blocking the very test meant to find problems.
        level = logger.warning if mode == "preflight" else logger.error
        for p in problems:
            level("Config problem: %s", p)
        if mode != "preflight":
            event("config_invalid", problems=problems, mode=mode)
            return 1

    gate = check_order_gate("qqq")
    logger.info(
        "Order gate: %s (%s)", "OPEN" if gate.allowed else "CLOSED", gate.reason
    )
    if not gate.allowed:
        logger.info(
            "Running the full execution path with orders blocked at the broker "
            "boundary. Signals, sizing and risk gates all still execute."
        )

    if mode == "preflight":
        return _preflight(config, gate)

    broker = _build_broker(config)

    if not broker.is_market_open():
        logger.info("Market is closed — skipping %s cycle.", mode)
        event("cycle_skipped", mode=mode, reason="market_closed")
        return 0

    snapshot = broker.get_current_positions()

    breaker = circuit_breaker.check(
        equity=snapshot.equity,
        daily_loss_limit_pct=config.daily_loss_limit_pct,
        max_drawdown_pct=config.max_drawdown_pct,
    )
    logger.info(
        "Breaker: %s | equity=$%s daily=%.2f%% dd=%.2f%%",
        "TRIPPED" if breaker.tripped else "ok",
        f"{snapshot.equity:,.2f}",
        breaker.daily_pnl_pct * 100,
        breaker.drawdown_pct * 100,
    )
    if breaker.tripped:
        logger.error("Circuit breaker tripped: %s", breaker.reason)

    cycle = StrategyCycle(broker, config)
    if mode == "daily":
        state = cycle.run_daily_cycle()
    elif mode == "intraday":
        state = cycle.run_intraday_check()
    else:
        logger.error("Unknown mode %r", mode)
        return 2

    metrics = snapshot_metrics(state, broker.get_current_positions())
    line = format_metrics_line(metrics)
    logger.info("%s cycle complete: %s", mode, line)
    event(
        "cycle_complete",
        mode=mode,
        phase=gate.phase,
        orders_allowed=gate.allowed,
        equity=metrics.equity,
        regime=metrics.regime,
        core_units=metrics.core_units,
        excess_units=metrics.excess_units,
        open_puts=metrics.open_put_spread_count,
        open_calls=metrics.open_call_count,
        reference_price=metrics.reference_price,
    )

    digest = _write_digest(
        mode,
        [
            f"- Phase: `{gate.phase}` — orders {'ALLOWED' if gate.allowed else 'BLOCKED'} ({gate.reason})",
            f"- Breaker: {'TRIPPED — ' + breaker.reason if breaker.tripped else 'ok'}",
            f"- Regime: **{metrics.regime}**",
            f"- Equity: ${metrics.equity:,.2f}",
            f"- Reference price: {metrics.reference_price}",
            f"- Units: core {metrics.core_units:.2f} / excess {metrics.excess_units:.2f}",
            f"- Open: {metrics.open_put_spread_count} put spread(s), {metrics.open_call_count} call(s)",
        ],
    )
    logger.info("Digest: %s", digest)
    return 0


def _preflight(config: StrategyConfig, gate) -> int:
    """Static fire test: import every module, construct every engine, and
    exercise the pure-logic paths against a stub broker. Catches the
    NameError/ImportError class of bug without touching the network.
    """
    from qqq.preflight import run_preflight

    ok, findings = run_preflight(config, gate)
    for f in findings:
        logger.info("preflight: %s", f)
    event("preflight", passed=ok, findings=findings)
    logger.info("PREFLIGHT %s", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="QQQ drift-harvest arm orchestrator")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["daily", "intraday", "preflight"],
        help="daily = full decision cycle; intraday = defensive checks only; "
             "preflight = offline fire test, no network",
    )
    args = parser.parse_args()

    try:
        return run(args.mode)
    except Exception:
        trace = traceback.format_exc()
        logger.error("FATAL in %s cycle:\n%s", args.mode, trace)
        # The event stream is one line per event; the full trace is in the log.
        # Keep just the exception line so the stream stays scannable.
        event("fatal", mode=args.mode, error=trace.strip().splitlines()[-1][:300])
        return 3


if __name__ == "__main__":
    sys.exit(main())
