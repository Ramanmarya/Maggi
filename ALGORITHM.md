# QQQ Drift-Harvest Algorithm

Translated from the "MNQ Positive-Drift + Mean-Reversion Premium Strategy V5"
doc to QQQ shares + QQQ equity options on Alpaca.

## 1. Purpose

Stay structurally long Nasdaq (via QQQ) while:

1. Getting paid (put credit spread premium) while waiting to buy lower.
2. Accumulating more QQQ during meaningful corrections, at a *decreasing*
   rate as declines get more severe.
3. Getting paid (call premium) to gradually sell excess inventory higher.
4. Never taking uncapped downside risk — every option position is
   defined-risk, and a hard portfolio crash-stress cap sits in front of
   every new position.

The strategy does not try to eliminate drawdowns or time the market. It stays
long in every regime; regime only changes *how aggressively* it accumulates
or sells premium.

## 2. Account & instrument

| | MNQ (original) | QQQ (this implementation) |
|---|---|---|
| Core position | 1 MNQ future | 100 shares (1 "unit") |
| Multiplier | $2/point | $1/share (none) |
| Options | Futures options | American equity options |
| Dividend | None | ~0.5–0.6%/yr, quarterly — creates early-assignment risk on short calls (§8) |

Starting equity: $100,000 (paper). All risk caps are a % of **current**
equity, not the starting balance.

## 3. Three return engines

- **Engine A — Core/excess QQQ inventory.** 1 unit (100 shares) is the
  permanent core position and is never capped with calls. Additional units
  are accumulated only during meaningful corrections, at pre-defined ATR zones.
- **Engine B — Put credit spreads.** Sold below the market to collect premium
  and express willingness to buy more QQQ lower, with defined (capped) downside.
- **Engine C — Hybrid call engine.** Calls sold primarily against *excess*
  inventory (units beyond the core 1) to monetize recoveries, not against the
  core position.

## 4. Market regime

Computed from price vs. 200-day moving average and the DMA's slope:

```
BULL:       price > 200DMA  AND  200DMA slope > 0
DEFENSIVE:  price < 200DMA  AND  200DMA slope < 0
NEUTRAL:    everything else (e.g. price above DMA but DMA flattening)
```

Regime does **not** flip the strategy short — it only adjusts accumulation
aggressiveness, put/call strike selection, and call willingness.

Implementation: `qqq/regime.py`. Tunables: `rules.json:regime`.

## 5. Acquisition ladder

- **20-day ATR** is computed daily.
- **Zones**: `Reference`, `Reference − 1.5×ATR`, `Reference − 3×ATR`,
  `Reference − 5×ATR`. The reference level itself is not a tradeable zone —
  it just marks "at the highs" (Phase A: avoid aggressive selling here).
- **Reference price** = highest closing price since the last ladder reset.
  Recenters when a new close exceeds the prior reference by ≥ 0.5×ATR
  (agreed starting threshold; the source doc left the exact "meaningful new
  high" bar open).
- **Anti-clustering**: each zone can only be used once per reset. If price
  gaps down through multiple zones in one session, the engine fills the
  *shallowest* unfilled one first (gradual accumulation) rather than the deepest.

Implementation: `qqq/ladder.py`. Tunables: `rules.json:ladder`.

## 6. Target exposure curve

Reaching a zone does **not** automatically mean adding exposure — total
unit-equivalent delta (core units + short-put delta − protective-put delta
− short-call delta) is compared against a target curve keyed to decline
from reference:

| Decline from reference | Target exposure (units) |
|---|---|
| 0% (at highs) | 1.0 |
| 5% | 1.5 |
| 10% | 2.0 |
| 15% | 2.5 |
| 20% | 3.0 |
| 25% | ~3.125 |
| 30% | ~3.25 |

A new put spread is only proposed if current total delta is below the target
for the current decline — i.e. if options already provide the desired
exposure, no new position is opened. This table is explicitly a hypothesis
to backtest, not a tuned constant (`rules.json:exposure_curve.points`).

Implementation: `qqq/exposure_curve.py`.

## 7. Put engine (Engine B)

- **DTE window**: 21–35 days.
- **Short leg**: nearest available contract to 20-delta (15–25 delta range to test).
- **Protective leg**: nearest available contract to 5-delta, same expiry,
  strike below the short leg.
- **Quality filter**: reject if risk/reward (max loss ÷ max profit) exceeds
  10:1 (configurable; `null` to accept any credit).
- **Hard risk gates** (§9) run before every submission — reject rather than
  override if any cap would be breached.
- **Management**: close/roll inside 3 DTE. Target 60% premium capture
  (40–80% range to backtest); the live profit-capture check needs a
  per-position mark from the broker, **not yet wired** (§11).

Implementation: `qqq/put_engine.py`. Tunables: `rules.json:put_engine`.

## 8. Hybrid call engine (Engine C)

- Core unit (first 100 shares) is **never** capped with a short call.
- Calls are evaluated against **excess** units continuously — no rebound
  required to start selling (this is what makes it "hybrid" rather than
  "rebound-only").
- **DTE window**: 21–35 days. **Delta target**: 20 normally, shifting to 25
  after a confirmed rebound (50% retracement of the decline from reference).
- **Effective sale price check**: `strike + premium` must be at or above the
  current reference price — never agree to monetize excess inventory below
  the level that triggered accumulating it.
- **Coverage cap**: short call contracts can never exceed excess units —
  enforced as a hard gate, not just a targeting preference. No naked calls, ever.
- **Ex-dividend safety (QQQ-specific)**: a call is refused if it expires
  straddling an ex-dividend date with extrinsic value below 1.25× the dividend
  amount — the early-assignment risk MNQ never has.

Implementation: `qqq/call_engine.py`. Tunables: `rules.json:call_engine`, `rules.json:dividend`.

## 9. Risk manager — hard, non-negotiable gates

No order is submitted without clearing every relevant gate below. There is no
override path in the engines.

1. **Per-spread max loss** ≤ 1% of current equity.
2. **Aggregate open put-spread risk** ≤ 5% of current equity.
3. **Portfolio crash-stress test**: reprice the book under −5/−10/−15/−20/−30%
   instantaneous shocks; worst case must stay ≤ 15% of equity (doc allows up
   to 20%; 15% is the default here, configurable). This is what catches the
   fact that QQQ shares carry *uncapped* downside even though the put spreads
   are defined-risk.
4. **Call coverage**: open short call contracts ≤ excess units. No naked calls
   under any circumstance.

Implementation: `qqq/risk.py`. Tunables: `rules.json:risk`.

Above these sit two platform-level backstops that are **not** from the source
doc — see `core/circuit_breaker.py`: a daily loss limit and a max drawdown
limit, both of which flip the kill switch. They are set deliberately wide
because equity here includes the mark-to-market of a core position the
strategy is designed to hold through corrections.

## 10. Decision cycle

Runs **daily** (full cycle, near market close) plus **intraday** checks
(interval configurable) that are strictly defensive — they re-run the
crash-stress test and ex-dividend monitoring, but never open new positions.

**Daily cycle:**

1. Pull price, 20d ATR, 200DMA + slope, current positions, dividend calendar.
2. Update regime.
3. Recompute total portfolio unit-equivalent delta.
4. Check ladder for recenter / unused zone reached.
5. If a zone is reached *and* current delta is below the target-exposure curve
   → propose a put spread, run it through all risk gates, submit if clear.
   Manage existing spreads (close inside 3 DTE).
6. Recompute excess units; propose a call against excess inventory if
   economics/ex-div checks pass and risk gates clear. Manage existing calls
   (ex-div re-check).
7. Re-run crash-stress test defensively before persisting.
8. Persist state to `state/qqq_state.json` (atomic write).

**Intraday check:** crash-stress re-check + ex-div/assignment monitoring only.

Implementation: `qqq/cycle.py`, driven one-shot by `qqq/orchestrator.py`.

## 11. What's still a placeholder

- ~~**Backtest P&L accounting.**~~ **Built** (2026-09-03). `backtest/` now
  carries a real fill ledger, daily mark-to-market, physical expiry
  settlement, a NYSE calendar, a cost model and performance metrics. The old
  `qqq/backtest_adapter.py` is superseded by `backtest/adapter.py`.
  Remaining limitation: no historical bid/ask on either data plan, so the
  spread is modelled rather than measured.
- **Profit-capture closes** (60% target) need a live per-position mark from the
  broker adapter — only the DTE-based force-close is wired.
- **Roll logic** for calls threatened by ex-dividend assignment is flagged but
  not executed (logs a warning rather than rolling).
- **`get_dividend_calendar` on the live Alpaca adapter returns `[]`**, so the
  ex-div safety check passes trivially. Do not sell calls live until this is
  wired (Polygon's dividends endpoint already works in the backtest adapter).
- **The ladder exhausts itself in a sustained decline.** §5's anti-clustering
  rule allows each zone once per reset, and §5's recenter only fires on a new
  high. In a one-directional fall those combine badly: every zone is consumed
  in the first few percent, and the engine then does nothing for the rest of
  the decline. Measured at the trough of the Feb–Apr 2025 drawdown
  (2025-04-08, QQQ 416.36 against a reference of 538.19): all 5 zones filled,
  no zone available, target exposure 3.32 units, held 1.00. Across the whole
  Feb–May 2025 window the engine opened **zero** spreads — in precisely the
  conditions the strategy exists for. The rule assumes the reference recenters
  reasonably often; a bear market is exactly when it never does.
- **Over 2.5 years the option overlay contributed −$218.** Backtest 2024-03-01
  to 2026-08-28 (627 sessions, 83 spreads, including the −34.5% Feb–Apr 2025
  drawdown): strategy +26.86%, buy-and-hold 100 QQQ with idle cash +27.08%.
  As built, the strategy is economically indistinguishable from owning one
  unit of QQQ. This is the headline result and it should be resolved before
  any further tuning.
- **Engine A never actually accumulates.** In a twelve-month backtest the core
  stayed at exactly 1.00 units for all 251 sessions and there were zero
  assignments. Spreads are the only route to more shares, and the put engine
  force-closes at 3 DTE — before assignment can happen. So the ladder and the
  target-exposure curve gate *whether a spread is written*, but the mechanism
  that would carry exposure from 1.0 toward the curve's 3.25 never fires. The
  strategy as built is "long one unit plus a put-spread overlay", not the
  accumulating ladder §3 and §6 describe. Deciding whether that is a bug or an
  acceptable simplification is an open design question.
- **Position sizing beyond 1 contract per signal** is not implemented — the
  risk gates enforce the equity caps regardless of count, but the engines
  don't yet decide *when* to size up.
- **Option delta weighting** in `DeltaAggregator` treats each option leg as
  1 delta per contract rather than using per-contract greeks, so the
  target-exposure comparison in §6 is approximate.
- **Historical backtest greeks** are Black-Scholes approximations (IV backed
  out from quoted price), since Polygon's historical endpoints don't include
  greeks the way the live snapshot does.
