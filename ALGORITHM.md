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

### Risk/reward is U-shaped in width, and is close to the wrong metric here

Measured on the 2026-09-03 chain, short leg 686p:

| width | credit | max loss | R:R |
|---|---|---|---|
| 1 | $0.09 | $91 | 10.11:1 |
| 4 | $0.51 | $349 | 6.84:1 |
| **7** | $0.90 | $610 | **6.82:1** ← best |
| 16 | $1.71 | $1,428 | 8.33:1 |
| 26 | $2.39 | $2,361 | 9.88:1 |
| 36 | $2.91 | $3,309 | 11.37:1 |

Very narrow spreads collect too little against their width; very wide ones add
width faster than credit. So raising the per-spread cap does not improve
risk/reward — it *worsens* it, because the engine then selects a wider leg.

Choosing the best-ratio leg instead of the widest also loses in backtest
($130,326 vs $130,919). The reason is the 50% profit-capture exit: the
position is closed long before either end of the ratio is reached, so the
credit collected matters more than a max-loss-to-max-profit figure that is
almost never realised.

### Position limits in practice

The four gates interact to cap concurrent put spreads, and the binding one
changes with price because the core's notional — and therefore its share of
the crash-stress budget — moves with it:

| QQQ | core notional | −30% shock | headroom | max spreads | binding gate |
|---|---|---|---|---|---|
| 717 | $71,759 | $21,528 | $972 | **0** | crash-stress |
| 650 | $65,000 | $19,500 | $3,000 | 2 | crash-stress |
| 600 | $60,000 | $18,000 | $4,500 | 3 | crash-stress |
| 550 | $55,000 | $16,500 | $6,000 | 4 | crash-stress |
| ≤500 | $50,000 | $15,000 | $7,500 | **5** | aggregate + zones |

So the ceiling is **5 spreads** (5 short + 5 long contracts), set jointly by
the 5% aggregate cap and the five ladder zones — but only reachable at QQQ
around $500 or below. At $717 the core alone consumes all but $972 of the
crash-stress budget and no spread fits. This is the gate working as designed:
a $71,759 core against a $150,000 basis is a large position, and the strategy
writes premium only when it has room to.

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

## 12b. §15 and §31 are mutually incompatible on a $100k account

This is a contradiction inside the source document, not in the translation.

§31 caps portfolio loss under a −20% shock at 15–20% of equity. That is a
cap on *exposure*, and it resolves to a hard number:

```
0.20 × Exposure ≤ 0.15 × $100,000   ->   Exposure ≤ $75,000
```

§15's target-exposure curve asks for up to 3.25 units in a deep decline. In
MNQ that is 3.25 × $58,116 = **$188,877**, which loses $37,775 under a −20%
shock — **38% of equity**, roughly double what §31 permits.

**The exposure curve can never be reached without violating the crash-stress
cap.** This is true in MNQ and in QQQ; §31 caps by risk, not by capital, so
leverage does not relax it. $75,000 of exposure is 1.29 MNQ contracts or 105
QQQ shares — about one unit either way.

Everything observed in testing follows from this: the ladder gating an
accumulation that can never happen, Engine C starved of excess inventory,
and the exposure curve behaving as decoration. The two sections need
reconciling before either can be trusted — either §15's curve comes down, or
§31's cap goes up, or the account is larger than the one both were written for.

## 12a. Two errors in the MNQ → QQQ translation

Found 2026-09-03 by reading the original V5 document rather than the
translation. Both are material.

**MNQ is a future; the translation dropped the leverage.** V5 §2 sizes the
core as 1 MNQ — $58,116 of Nasdaq exposure (29,058 × $2) tying up roughly
$2,500 of margin. §15's exposure curve then asks for up to 3.25 units in a
deep decline: about $189,000 of exposure on ~$8,125 of margin, entirely
routine on a $100,000 account. Translating that to 100 QQQ *shares* preserved
the exposure and silently discarded the leverage the curve depends on. Shares
must be bought outright, so 3.25 units means $233,000 of cash. **The maximum
reachable on $100,000 in shares is 1.39 units.** §15's curve is unreachable
past a −5% decline, which is why accumulation testing showed it converting
alpha into beta: in shares it *is* just beta. Alpaca offers no futures, so
this cannot be fixed within this broker — it is a property of the instrument
choice, and it caps what the QQQ implementation can ever earn.

**§31's cap binds on the −20% shock, not the worst shock.** §30 says to
simulate −5/−10/−15/−20/−30%. §31 then caps the loss *"under an additional
instantaneous −20% shock"* at 15–20% of equity. The implementation took the
worst of all five and capped that — materially stricter than specified, and
strict enough that the core alone breached it at QQQ 717 and no spread could
be written at all. That breach is what prompted a $150,000 equity-basis
override and a 20% cap, both of which were solving a limit the specification
never set. Corrected: the cap binds on the −20% case and the deeper shocks are
still simulated and reported so the tail stays visible.

With the correction, every override was removed — caps size against true
equity, crash-stress is 15% (§31's lower bound), per-spread loss is 1% (§10) —
and the backtest is unchanged: $130,714 versus $130,919, same Sharpe, same
drawdown, same 79 spreads. The spec's own rules also select a *better* trade:
a 7-wide spread at 6.82:1 risk/reward rather than the 26-wide at 9.88:1 the
loosened caps produced.

## 12. Additions beyond the source document

These are not in the V5 doc. They are recorded here so the difference between
"the strategy as specified" and "what actually runs" stays visible.

**Idle-cash sweep into short Treasuries** (`qqq/cash_sweep.py`). The strategy
holds ~$28,500 idle on a $100,000 account, because the core consumes the
capital and the options program risks only a few thousand at a time. That cash
earned nothing in every backtest — which quietly costs more than the whole
options overlay produces. Swept into SGOV above a reserve of the aggregate
put-risk cap plus a buffer, so the options program can always open, close or
take assignment without a forced sale. At current balances it moves ~$16,000
and collects ~$719/yr, against ~$1,620/yr from every spread the engine writes.

**Why the core is NOT being increased.** A sweep of core size on the 626-session
backtest suggested a 1.4-unit core (+$16,290/yr at Sharpe 1.04, versus
+$12,367 at 1.0). That recommendation does not survive contact with today's
price and has been withdrawn. QQQ has risen 61% since the backtest window
opened, so a "unit" now costs $71,722 rather than $44,564: the backtest's
1.4-unit core was 62% of the account, while today's 1.0-unit core is already
**72%**. Expressed as exposure — which is what actually matters — the live
account is already between the 1.4- and 1.8-unit configurations. Buying to a
literal 1.4 units would cost $100,411 against $100,077 of equity, and breach
crash-stress at $30,123 versus a $30,000 cap. Unit counts are a misleading way
to express position size across a large price move; percent of equity is not.

## 11. What's still a placeholder

- ~~**Backtest P&L accounting.**~~ **Built** (2026-09-03). `backtest/` now
  carries a real fill ledger, daily mark-to-market, physical expiry
  settlement, a NYSE calendar, a cost model and performance metrics. The old
  `qqq/backtest_adapter.py` is superseded by `backtest/adapter.py`.
  Remaining limitation: no historical bid/ask on either data plan, so the
  spread is modelled rather than measured.
- ~~**Profit-capture closes**~~ **Wired** (2026-09-03) via `option_mark()` on
  the broker protocol. Backtested: enabling it raised return 28.15% → 31.09%,
  *lowered* max drawdown 12.04% → 11.11% and raised Sharpe 0.90 → 1.07, with
  the share of collected credit actually kept going from 14.2% to 43.0%. Set
  to 50%.
- **Roll logic** for calls threatened by ex-dividend assignment is flagged but
  not executed (logs a warning rather than rolling).
- **`get_dividend_calendar` on the live Alpaca adapter returns `[]`**, so the
  ex-div safety check passes trivially. Do not sell calls live until this is
  wired (Polygon's dividends endpoint already works in the backtest adapter).
- **Zone exhaustion in a decline is a FEATURE, not a defect** (tested
  2026-09-03). The engine writes nothing through a sustained fall because §5
  spends each zone once per reset and only recenters on a new high. That
  looked like a bug — at the 2025-04-08 trough all 5 zones were filled, the
  curve wanted 3.32 units and it held 1.00. Letting zones re-arm on a cooldown
  fixes the *behaviour* (0 spreads in the Feb–May 2025 fall becomes 54 at a
  5-day cooldown) and makes the *results worse*: return falls 28.15% → 27.20%
  and max drawdown roughly doubles, 12.04% → 25.38%, Sharpe 0.90 → 0.61. A
  21-day cooldown recovers most of it but still trails. Selling puts into a
  falling market writes contracts that then go in-the-money. Kept at 0.
- **The real defect was zone ordering, now fixed.** `unused_zone_at_or_below`
  returned `min(candidates)` — zones are prices, so that is the *deepest*
  zone, contradicting its own docstring and §5's gradual intent. Correcting it
  to the shallowest zone moved the option overlay from **−$218 to +$1,283**
  over the same 626 sessions, and the strategy from behind buy-and-hold to
  modestly ahead ($128,148 vs $127,080).
- ~~**The ladder exhausts itself in a sustained decline.**~~ §5's anti-clustering
  rule allows each zone once per reset, and §5's recenter only fires on a new
  high. In a one-directional fall those combine badly: every zone is consumed
  in the first few percent, and the engine then does nothing for the rest of
  the decline. Measured at the trough of the Feb–Apr 2025 drawdown
  (2025-04-08, QQQ 416.36 against a reference of 538.19): all 5 zones filled,
  no zone available, target exposure 3.32 units, held 1.00. Across the whole
  Feb–May 2025 window the engine opened **zero** spreads — in precisely the
  conditions the strategy exists for. The rule assumes the reference recenters
  reasonably often; a bear market is exactly when it never does.
- **Where the edge stands after all fixes**, at the configuration actually
  shipped (R:R 5:1, 1 contract, 50% profit capture, accumulation off,
  trade-at-reference on). Backtest 2024-03-01 to 2026-08-28, 626 sessions,
  including the −34.5% Feb–Apr 2025 fall:

  | | equity | return | max DD | Sharpe |
  |---|---|---|---|---|
  | strategy | $130,721 | +30.72% | 11.17% | 1.06 |
  | buy & hold 1 unit | $127,080 | +27.08% | 11.26% | 0.99 |

  Edge **+$3,641** over its own beta, with slightly lower drawdown. Before the
  fixes this same window produced an overlay contribution of **−$218**.
  (An earlier note quoted $135,550 / +$8,471; those were measured at 3
  contracts, a setting since rejected as overfitting.)
- **The edge comes from a deviation, not from the doc.** With
  `trade_at_reference` false — §5 as written, where the reference marks "at
  the highs" and the strategy waits below it — the same 626 sessions return
  $128,369 / Sharpe 0.98 against buy-and-hold's 0.99. That is no edge at all.
  Turning it on ($130,721 / Sharpe 1.06 / DD 11.17%) is what produces one, and
  it nearly doubles spread count, 41 to 76. Worth stating plainly: the V5
  design as translated does not beat simply owning the underlying over this
  sample; the operator's deviation from it does.
- **Engines A and B are structurally incompatible.** Engine A accumulates
  inventory through assignment; Engine B is defined-risk, which means every
  short put is paired with a long put beneath it. When a spread goes deep
  in-the-money, the short leg is assigned (+100 shares) *and* the long leg is
  exercised (−100 shares): **net zero**. A defined-risk spread can never
  deliver inventory, at any price, ever. So §3's "accumulate more QQQ during
  meaningful corrections" cannot happen through §7's instrument. Verified by
  test (`test_put_spread_both_legs_itm_loses_at_most_the_width`). Accumulation
  requires cash-secured puts or outright share purchases; you can have defined
  risk or accumulation from this engine, not both. This is the deepest issue
  in the translation and it invalidates the exposure curve's whole purpose.
- **Engine C is unreachable as a consequence.** Calls are written only against
  excess units; excess units require shares beyond the core; shares beyond the
  core require assignment; assignment cannot happen (above). Zero calls have
  been written in any backtest.
- ~~**Engine A never actually accumulates.**~~ In a twelve-month backtest the core
  stayed at exactly 1.00 units for all 251 sessions and there were zero
  assignments. Spreads are the only route to more shares, and the put engine
  force-closes at 3 DTE — before assignment can happen. So the ladder and the
  target-exposure curve gate *whether a spread is written*, but the mechanism
  that would carry exposure from 1.0 toward the curve's 3.25 never fires. The
  strategy as built is "long one unit plus a put-spread overlay", not the
  accumulating ladder §3 and §6 describe. Deciding whether that is a bug or an
  acceptable simplification is an open design question.
- ~~**Position sizing beyond 1 contract**~~ **Implemented and swept**; kept at
  1. Edge over buy-and-hold ran $3,642 / $1,338 / $8,471 / $4,353 for 1/2/3/4
  contracts, Sharpe 1.06 / 0.90 / 1.04 / 1.00 — non-monotonic, with the swing
  between adjacent settings exceeding the total edge of most of them. That is
  path dependence, not a size effect. 3 looks best on this sample; choosing it
  would be overfitting.
- ~~**Per-contract delta weighting**~~ **Fixed.** The aggregator scored a
  short 20-delta put as −1.00 units — short 100 shares — when it is about
  +0.20. Spreads cancelled the error, so it never showed in results.
- **The risk/reward filter is inert above 5:1.** 8:1, 10:1 and unrestricted
  give byte-identical results, because max-loss-aware leg selection already
  keeps every proposal under 8:1. Only 5:1 binds. Set there.
- **Historical backtest greeks** are Black-Scholes approximations (IV backed
  out from quoted price), since Polygon's historical endpoints don't include
  greeks the way the live snapshot does.
