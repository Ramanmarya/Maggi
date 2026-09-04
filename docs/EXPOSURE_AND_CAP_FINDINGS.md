# What the cap raise and the hybrid actually did

Measured 2026-09-03. Polygon options data, modelled spread, QQQ.

## The setup

One QQQ contract is **28.7% of a $250,000 account** at $717. A backtest only
presses the risk caps if it reproduces that ratio. At $250k the 2022 window
runs at 16% per contract and 2023 at 11% — a third of the pressure — so those
runs test a machine that is never squeezed. Both windows are therefore run
twice: at $250k, and at the equity that restores today's tightness
($140,009 for 2022, $92,193 for 2023).

## At $250k — both changes inert or harmful

```
2022 CRASH  QQQ -30.2%          return   premium  overlay P&L  puts
A  cash-sec /15%                -4.68%     8,085       -4,179    11
B  cash-sec /25%                -4.68%     8,085       -4,179    11
C  hybrid   /25%                -5.71%     7,338       -6,715    11

2023 BULL   QQQ +38.9%
A  cash-sec /15%                 8.90%    10,983        6,697    25
B  cash-sec /25%                 8.90%    10,983        6,697    25
C  hybrid   /25%                 8.49%     9,218        5,693    21
```

A and B match to the dollar: at these prices the **ladder**, not any risk cap,
decides how much the book trades. Raising a non-binding cap does nothing.

## At today's tightness — the cap raise is the change that works

```
config                crash     bull  combined  overlay crash     bull       net   puts
A  cash-sec /15%    -12.08%   17.30%     3.13%         -6,928    3,783    -3,145     26
B  cash-sec /25%    -11.59%   20.31%     6.37%         -6,214    6,697      +483     36
C  hybrid   /25%    -11.95%   19.21%     4.96%         -6,715    5,693    -1,022     32

  B vs A: +3.24 pts over the pair | overlay +3,628 | puts +10
  C vs B: -1.40 pts               | overlay -1,505 | puts  -4
```

**B is the only config whose overlay is positive across both regimes.**

## Verdict

- **Crash-stress cap 15% → 25%: KEPT.** Inert at $250k, decisive at today's
  ratio. If every position were assigned the book owns ~116% of equity, on
  margin — the accepted risk, and why it stops at 25%.
- **Hybrid: BUILT, TESTED, OFF.** It lost to pure cash-secured at the same cap
  in **all four** windows. It opened *fewer* positions (32 vs 36), not more: a
  spread must clear the R:R and max-loss gates an unspread put skips, so on
  days a cash-secured put would have been written the spread version found no
  qualifying long leg and wrote nothing.

The arithmetic that motivated the hybrid — 17.9% premium per dollar of shock
charge versus 6.0% — is correct, and irrelevant here. It only pays when a risk
cap is what stops you trading. Re-enable with `put_engine.structure: "hybrid"`
if a future price/account ratio makes it bind.

## The ceiling

Baseline config, $250k, both windows: the overlay netted **+$2,518 across a
full year — 1.01% on capital**, from $19,068 of gross credit. It lost $4,179
in the fall and made $6,697 in the rally. Selling puts is a long-beta position
wearing an income costume; the credit is not a yield.

For scale: $92,000 on $250k is 37%/yr. The CBOE PutWrite index — 32 years of
exactly this trade — returned 10.1%/yr *total*, underlying included.
