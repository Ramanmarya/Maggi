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
python3 -m backtest.run --start 2025-09-02 --end 2026-08-28
python3 -m backtest.run --start 2026-06-01 --end 2026-08-28 --verbose
```

Runs the real `StrategyCycle` over real NYSE sessions with a real ledger:
fills move cash, positions are marked daily, commissions and a modelled
spread are charged, and expiring options settle physically — an in-the-money
short put delivers shares, which is how Engine A accumulates.

Data comes from **Alpaca, not Polygon**. Both serve the same daily option
OHLCV about 18 months back, but Polygon's plan throttles at 5 requests per
minute and returns one contract per call; Alpaca returns 100 per call and
does not throttle. No plan upgrade is needed for this.

Everything is cached to `backtest/cache/bars.sqlite` on first fetch, so the
first run over a new window is network-bound and every rerun after it is
near-instant. Delete that file to force a refetch.

**What the backtest cannot tell you:** neither data plan carries historical
bid/ask, so the spread is a modelled assumption (`backtest/costs.py`), not a
measurement. It is set pessimistically on purpose — a premium seller is not
filled at mid. Treat the credit side of any result as the optimistic end.

## When something looks wrong

1. `python3 scripts/status.py` — is the gate closed, and why?
2. `tail -50 logs/qqq_orchestrator.log` — did the cycle run, did it throw?
3. `tail -20 logs/events.jsonl` — structured record of every decision.
4. `python3 -m qqq.orchestrator --mode preflight` — does the path still work offline?

**Never re-enable the kill switch without reading the status verdict first.**
A breaker that tripped on a real loss will trip again on the next cycle.
