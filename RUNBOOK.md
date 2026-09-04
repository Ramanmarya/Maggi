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
python3 -m backtest.run --start 2021-09-01 --end 2026-08-28 --source polygon --quotes
```

Runs the real `StrategyCycle` over real NYSE sessions with a real ledger:
fills move cash, positions are marked daily, commissions and a modelled
spread are charged, and expiring options settle physically — an in-the-money
short put delivers shares, which is how Engine A accumulates.

Data comes from **Alpaca by default**. Alpaca returns 100 contracts per call
and does not throttle, where Polygon's current plan throttles at 5 requests a
minute and returns one contract per call, so Alpaca is much the faster source
for any window it can serve.

**How far back each source reaches** (measured 2026-09-03, not read off a
docs page):

| source | option history | NBBO quotes |
|---|---|---|
| Alpaca | starts **2024-02** — 2021/2022/2023 return zero bars | no |
| Polygon, current key | rolling ~2y; 403 before ~2024-09 | no |
| Polygon Options Advanced ($199/mo) | 5+ years | **yes** |

So the deepest window Alpaca can support contains no bear market. A five-year
run — the one that puts the 2022 decline inside the sample — needs
`--source polygon` on Options Advanced. `--quotes` then replaces `costs.py`'s
modelled spread with the measured one; it is inert without the subscription,
and the backend logs that it is falling back rather than returning empty data.

Budget for a cold five-year backfill: **~220k requests**, a few hours on an
unlimited plan. Each contract is fetched once over its whole life rather than
once per session — the naive alternative is ~3.7M requests. Backfill **wider
strike bands than the strategy currently trades**: the cache is permanent, but
a later config change against narrow cached bands means re-subscribing to
re-pull.

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
