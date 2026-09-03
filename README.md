# Maggi

Single-arm paper-trading platform on Alpaca. The arm is `qqq`: a
drift-harvest strategy that stays structurally long Nasdaq, gets paid to wait
via put credit spreads, accumulates into corrections on an ATR ladder, and
sells calls against excess inventory only.

- **What it does:** [ALGORITHM.md](ALGORITHM.md)
- **How to run it:** [RUNBOOK.md](RUNBOOK.md)
- **Rules for changing it:** [CLAUDE.md](CLAUDE.md)

## Quick start

```bash
cp .env.example .env      # add Alpaca paper + Polygon keys
chmod 600 .env
python3 -m pytest qqq/tests/ -q
python3 -m qqq.orchestrator --mode preflight
python3 -m dashboard.server --demo
```

Phase is `design`: the orchestrator runs the full path, and the order gate
blocks every submission. Nothing reaches the broker until `allocator.json`
says otherwise.
