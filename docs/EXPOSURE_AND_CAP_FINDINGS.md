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

---

# Continuous rolling vs the ATR ladder (2026-09-04)

Under-deployment looked like the binding constraint across three separate
findings: the GLD arm held 4 puts where collateral allowed 14, the QQQ arm
wrote 11 puts in six months of 2022, and raising the crash-stress cap was
inert at $250k because the ladder — not the caps — was what bound the book.

`entry_mode: "continuous"` targets a deployment fraction instead of waiting
for price to touch a rung. It was built, tested, and measured:

```
arm   mode          crash     bull   combined   avg vol   ret/vol    overlay   puts
qqq   ladder       -4.68%    8.90%      3.80%     6.39%      0.60     +2,518     36
qqq   cont60      -13.74%   14.13%     -1.55%    13.61%     -0.11     -6,942    119
qqq   cont80      -16.68%   14.87%     -4.29%    16.35%     -0.26    -12,391    140
gld   ladder        2.43%    2.70%      5.20%     1.02%      5.07     +1,687     63
gld   cont60        2.24%    3.26%      5.57%     2.39%      2.33     +3,249    147
gld   cont80        2.19%    3.29%      5.55%     2.48%      2.24     +3,390    148
```

It deployed exactly as designed. QQQ premium went from $18,878 to $78,618 —
4x — and the overlay from **+$2,518 to −$12,391**. Every extra dollar of
credit was given back, plus more.

`ret/vol` is the honest column: QQQ ladder 0.60, every continuous variant
NEGATIVE; GLD 5.07 → 2.33. **More trading bought worse risk-adjusted return
in both arms.**

## Verdict: ladder KEPT on both arms

The premise was wrong. The ladder was not costing return — it was declining
trades that lose money in a falling market, which is precisely what a
dip-buying ladder is for. A conservative design was misread as a defect.

GLD's mild improvement (5.20% → 5.57%) is not evidence for continuous mode:
gold was **flat** (+0.05%) in the crash window, so there was nothing to
amplify. Continuous deployment is leverage, and it multiplies whatever the
underlying did.

Continuous mode remains built, tested and OFF, one config line from use if a
future test justifies it.

## Open

GLD's deployment dial saturates — 60% and 80% produce 75 vs 76 puts (2022)
and 72 vs 72 (2023), while QQQ scales normally (48 → 63). A risk gate is
biting at gold's smaller contract size. Unidentified; irrelevant while the
ladder is in use, but it must be found before anyone trusts that dial.
