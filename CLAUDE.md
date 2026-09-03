# Maggi — operator + agent context

**Maggi** is a single-arm paper-trading platform on Alpaca. One arm: `qqq`,
the QQQ drift-harvest strategy. This file is the front door.

- Algorithm spec: [ALGORITHM.md](ALGORITHM.md)
- Day-to-day operations: [RUNBOOK.md](RUNBOOK.md)
- Tunables: [qqq/rules.json](qqq/rules.json)
- Capital + phase: [allocator.json](allocator.json)

## Bright lines (do not cross)

1. **Never auto-modify trading state.** `TRADING_ENABLED`, `allocator.json`,
   breaker state. Operator-only, through `scripts/status.py`. No code path in
   this repo writes `allocator.json`.
2. **Never auto-edit strategy code** in response to a losing run.
3. **The order gate is the only path to the broker.** Engines receive a
   `GatedBroker`, never a raw `AlpacaAdapter`. There is no runtime flag that
   disables it — bypassing it requires a visible code change in
   `qqq/orchestrator.py`.
4. **Closing is never gated.** A kill switch stops new risk; it must not trap
   the book in positions it cannot exit.
5. **Fail closed.** Missing file, bad JSON, unknown phase, unreachable market
   clock — all mean no orders.

## Layout

```
core/          platform: order gate, breaker, atomic IO, structured logging
qqq/           the arm: engines, cycle, orchestrator, rules.json
dashboard/     read-only localhost view (model + render + server)
scripts/       status.py (operator control), install_launchd.sh
launchd/       one-shot schedules: daily 12:45 PT, intraday every 15 min
```

## Phase ladder

`design` → `scanner_only` → `paper` → `tiny_live` → `live` (and `killed`).
Only `paper`, `tiny_live` and `live` permit orders. Current phase lives in
`allocator.json`. **Currently: `design`.**

Every mode runs the FULL execution path regardless of phase — the gate blocks
at the broker boundary rather than short-circuiting earlier. A phase check
that returns early leaves the order-placing code untested until the day it
first places an order.

## Before you promote a phase

```bash
python3 -m qqq.orchestrator --mode preflight   # must exit 0
python3 -m pytest qqq/tests/ -q                # must be green
python3 scripts/status.py                      # read the verdict
```

## Known gaps

See [ALGORITHM.md §11](ALGORITHM.md#11-whats-still-a-placeholder). The two
that block real use: backtest P&L accounting is not implemented, and the live
dividend calendar returns empty (so ex-div safety is untested — do not sell
calls live until it is wired).
