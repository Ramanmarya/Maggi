# Runbook

## Daily

```bash
python3 scripts/status.py          # kill switch, phase, breaker, positions, verdict
python3 -m dashboard.server        # http://127.0.0.1:8787
```

## Control

```bash
python3 scripts/status.py --disable "reason"   # kill switch OFF
python3 scripts/status.py --enable             # kill switch ON (refuses if breaker tripped)
python3 scripts/status.py --reset-breaker      # reset a tripped breaker (prompts)
```

Changing **phase** is a manual edit of `allocator.json` — deliberately not
scriptable, so a phase promotion is always a reviewed diff.

## Scheduling

```bash
./scripts/install_launchd.sh          # install + load both jobs
./scripts/install_launchd.sh unload   # stop everything
launchctl list | grep com.maggi       # confirm loaded
```

- `com.maggi.qqq-daily` — 12:45 PT (15:45 ET) Mon–Fri, full decision cycle.
- `com.maggi.qqq-intraday` — every 15 min; exits immediately when Alpaca's
  clock says the market is closed.

## Running by hand

```bash
python3 -m qqq.orchestrator --mode preflight   # offline fire test, no network
python3 -m qqq.orchestrator --mode daily
python3 -m qqq.orchestrator --mode intraday
```

## Backtest

```bash
python3 -m qqq.run_backtest --start 2023-01-01 --end 2025-01-01
```

**Read ALGORITHM.md §11 first.** The backtest currently reports a flat equity
curve because fills do not update cash or positions. It exercises the decision
path against real historical chains; it does not yet produce returns.

Polygon's historical chain walk makes one quote request per contract. Workers
are pinned to 1 and requests spaced by `rules.json:backtest.polygon_min_interval_seconds`
to stay under the ~100 req/min plan cap.

## When something looks wrong

1. `python3 scripts/status.py` — is the gate closed, and why?
2. `tail -50 logs/qqq_orchestrator.log` — did the cycle run, did it throw?
3. `tail -20 logs/events.jsonl` — structured record of every decision.
4. `python3 -m qqq.orchestrator --mode preflight` — does the path still work offline?

**Never re-enable the kill switch without reading the status verdict first.**
A breaker that tripped on a real loss will trip again on the next cycle.
