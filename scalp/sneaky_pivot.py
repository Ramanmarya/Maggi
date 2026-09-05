"""Sneaky Pivot: 15-minute range / swing mean reversion.

THE LOOK-AHEAD TRAP, handled explicitly.

A swing low is defined as a bar whose low is below the previous N bars AND
the next N bars. That definition can only be evaluated N bars AFTER the swing
prints. A backtest that marks the swing at its own index is reading the
future: it buys the exact low of a move it could not have known was a low,
and produces a spectacular equity curve that cannot be traded.

Every swing here is stamped with confirmed_at = index + N, and no decision
made at bar i may consult a swing whose confirmed_at exceeds i.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Swing:
    idx: int
    price: float
    confirmed_at: int          # the bar at which this became knowable
    kind: str                  # "low" | "high"


@dataclass
class Params:
    swing_bars: int = 2        # bars either side that define a swing
    range_days: int = 2        # lookback for the active range
    buy_zone: float = 0.25     # range position at or below which longs are considered
    sell_zone: float = 0.75    # range position at or above which longs are trimmed
    min_range_atr: float = 1.5 # refuse ranges narrower than this many ATR
    atr_period: int = 14
    stop_mode: str = "swing"   # swing | range | range_atr
    stop_atr_buffer: float = 0.25
    trend_filter: str = "none" # none | ema200
    ema_period: int = 200
    min_rr: float = 1.0        # refuse setups whose reward/risk is below this
    bars_per_day: int = 26     # RTH 15-minute bars
    risk_per_trade: float = 0.01
    cost_per_share: float = 0.01   # half-spread each way, in dollars
    max_position_pct: float = 0.95  # ceiling on cash committed to one trade


def find_swings(bars, n):
    """Confirmed swing points only. confirmed_at = idx + n."""
    out = []
    for i in range(n, len(bars) - n):
        lo = bars[i]["l"]
        if all(lo < bars[i - k]["l"] for k in range(1, n + 1)) and \
           all(lo < bars[i + k]["l"] for k in range(1, n + 1)):
            out.append(Swing(i, lo, i + n, "low"))
        hi = bars[i]["h"]
        if all(hi > bars[i - k]["h"] for k in range(1, n + 1)) and \
           all(hi > bars[i + k]["h"] for k in range(1, n + 1)):
            out.append(Swing(i, hi, i + n, "high"))
    return out


def atr(bars, i, period):
    if i < period + 1:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        pc = bars[j - 1]["c"]
        trs.append(max(bars[j]["h"] - bars[j]["l"],
                       abs(bars[j]["h"] - pc), abs(bars[j]["l"] - pc)))
    return sum(trs) / len(trs)


def ema_series(bars, period):
    k = 2 / (period + 1)
    out = [bars[0]["c"]]
    for b in bars[1:]:
        out.append(b["c"] * k + out[-1] * (1 - k))
    return out


def active_range(bars, i, swings_by_conf, p):
    """Range from CONFIRMED swings inside the lookback window.

    Falls back to the window's raw high/low when too few swings have
    confirmed -- but never uses a swing the bar could not know about.
    """
    lo_i = max(0, i - p.range_days * p.bars_per_day)
    lows = [s.price for s in swings_by_conf if s.kind == "low" and lo_i <= s.idx <= i and s.confirmed_at <= i]
    highs = [s.price for s in swings_by_conf if s.kind == "high" and lo_i <= s.idx <= i and s.confirmed_at <= i]
    if len(lows) < 2 or len(highs) < 2:
        return None, None
    return min(lows), max(highs)


@dataclass
class Trade:
    entry_i: int
    entry: float
    stop: float
    qty: int
    t1: float
    t2: float
    t3: float
    open_qty: int
    realized: float = 0.0
    exits: list = field(default_factory=list)


def run(bars, p: Params, equity=250_000.0, verbose=False):
    """§26/§27 as written, with no bar allowed to see its own future."""
    swings = find_swings(bars, p.swing_bars)
    ema = ema_series(bars, p.ema_period) if p.trend_filter == "ema200" else None
    cash = equity
    trades, open_t = [], None
    curve = []
    start = max(p.atr_period + 2, p.swing_bars * 2 + 2, p.range_days * p.bars_per_day)

    for i in range(start, len(bars)):
        b = bars[i]
        a = atr(bars, i, p.atr_period)
        mark = cash + (open_t.open_qty * b["c"] if open_t else 0.0)
        curve.append(mark)
        if a is None:
            continue

        # ---- manage an open position first (§27) --------------------------
        if open_t:
            if b["l"] <= open_t.stop:                      # §9 invalidation
                fill = open_t.stop - p.cost_per_share
                cash += open_t.open_qty * fill
                open_t.realized += open_t.open_qty * (fill - open_t.entry)
                open_t.exits.append(("stop", i, fill))
                open_t.open_qty = 0
                trades.append(open_t); open_t = None
            else:
                for lvl, tag, frac in ((open_t.t1, "t1", 1/3), (open_t.t2, "t2", 1/2),
                                       (open_t.t3, "t3", 1.0)):
                    if open_t.open_qty > 0 and b["h"] >= lvl and tag not in [e[0] for e in open_t.exits]:
                        q = open_t.open_qty if tag == "t3" else max(1, int(open_t.open_qty * frac))
                        q = min(q, open_t.open_qty)
                        fill = lvl - p.cost_per_share
                        cash += q * fill
                        open_t.realized += q * (fill - open_t.entry)
                        open_t.open_qty -= q
                        open_t.exits.append((tag, i, fill))
                if open_t and open_t.open_qty == 0:
                    trades.append(open_t); open_t = None
            if open_t:
                continue

        # ---- look for an entry (§7, §8, §26) -----------------------------
        rl, rh = active_range(bars, i, swings, p)
        if rl is None or rh <= rl:
            continue
        width = rh - rl
        if width < p.min_range_atr * a:                    # §21 volatility filter
            continue
        pos = (b["c"] - rl) / width                        # §5 range position
        if pos > p.buy_zone:                               # §6 buy zone
            continue
        if p.trend_filter == "ema200" and b["c"] < ema[i]:  # §19
            continue

        # §7: near Range Low OR a confirmed Swing Low
        recent_lows = [s for s in swings if s.kind == "low" and s.confirmed_at <= i
                       and i - s.idx <= p.range_days * p.bars_per_day]
        near_swing = any(abs(b["l"] - s.price) <= 0.5 * a for s in recent_lows)
        near_rl = (b["l"] - rl) <= 0.5 * a
        if not (near_swing or near_rl):
            continue

        # §7 confirmation: bullish reversal -- close in the upper half of the
        # bar's range, above the open, with a lower wick. Evaluated on the
        # CLOSED bar, so no future information is used.
        rng = b["h"] - b["l"]
        if rng <= 0:
            continue
        wick = min(b["o"], b["c"]) - b["l"]
        bullish = b["c"] > b["o"] and (b["c"] - b["l"]) / rng > 0.5 and wick > 0.2 * rng
        if not bullish:
            continue

        # §8 trigger: buy the break of the reversal bar's high, next bar
        if i + 1 >= len(bars):
            continue
        nxt = bars[i + 1]
        if nxt["h"] < b["h"]:
            continue
        entry = b["h"] + p.cost_per_share

        # §9 stop
        if p.stop_mode == "swing" and recent_lows:
            stop = min(s.price for s in recent_lows[-3:]) - p.stop_atr_buffer * a
        elif p.stop_mode == "range_atr":
            stop = rl - p.stop_atr_buffer * a
        else:
            stop = rl
        if stop >= entry:
            continue

        t1, t2, t3 = rl + width / 2, rl + width * 0.75, rh
        rr = (t3 - entry) / (entry - stop)
        if rr < p.min_rr:                                   # §12 reward/risk
            continue

        # §23 sizes by stop distance, which was written for MNQ futures on
        # margin. On cash equities a tight stop implies a share count worth
        # several times the account, so the risk-sized quantity is CAPPED by
        # what the cash can actually buy. Without this the strategy refuses
        # 446 of its own 453 signals and reports on a sample of 7.
        risk_qty = int((equity * p.risk_per_trade) / (entry - stop))
        cash_qty = int((cash * p.max_position_pct) / entry)
        qty = min(risk_qty, cash_qty)
        if qty < 1:
            continue
        cash -= qty * entry
        open_t = Trade(i + 1, entry, stop, qty, t1, t2, t3, qty)

    if open_t:
        cash += open_t.open_qty * bars[-1]["c"]
        open_t.realized += open_t.open_qty * (bars[-1]["c"] - open_t.entry)
        trades.append(open_t)
    return dict(final=cash, trades=trades, curve=curve, start_equity=equity)
