"""Performance metrics from the daily equity curve and the fill log."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Metrics:
    start_equity: float
    end_equity: float
    total_return: float
    cagr: float
    max_drawdown: float
    max_drawdown_date: date | None
    sharpe: float
    volatility: float
    days: int
    spreads_opened: int
    spreads_closed: int
    assignments: int
    premium_collected: float
    commission_paid: float
    realized_pnl: float

    def render(self) -> str:
        def pct(v):
            return f"{v*100:+.2f}%"
        rows = [
            ("Start equity", f"${self.start_equity:,.2f}"),
            ("End equity", f"${self.end_equity:,.2f}"),
            ("Total return", pct(self.total_return)),
            ("CAGR", pct(self.cagr)),
            ("Max drawdown", f"{self.max_drawdown*100:.2f}%"
                             + (f" on {self.max_drawdown_date}" if self.max_drawdown_date else "")),
            ("Annualised vol", f"{self.volatility*100:.2f}%"),
            ("Sharpe (rf=0)", f"{self.sharpe:.2f}"),
            ("Trading days", str(self.days)),
            ("Spreads opened", str(self.spreads_opened)),
            ("Spreads closed", str(self.spreads_closed)),
            ("Assignments", str(self.assignments)),
            ("Net credit taken in", f"${self.premium_collected:,.2f}"),
            ("Commission paid", f"${self.commission_paid:,.2f}"),
            ("Realised P&L", f"${self.realized_pnl:,.2f}"),
        ]
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"  {k.ljust(width)}  {v}" for k, v in rows)


def compute(curve: list[tuple[date, float]], ledger, commission: float) -> Metrics:
    if len(curve) < 2:
        raise ValueError("need at least two equity points")

    start, end = curve[0][1], curve[-1][1]
    days = len(curve)
    years = max((curve[-1][0] - curve[0][0]).days, 1) / 365.25
    total_return = (end / start) - 1.0
    cagr = (end / start) ** (1 / years) - 1.0 if start > 0 and end > 0 else float("nan")

    peak, max_dd, max_dd_day = curve[0][1], 0.0, None
    for day, eq in curve:
        peak = max(peak, eq)
        dd = (eq - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd, max_dd_day = dd, day

    rets = [
        (curve[i][1] - curve[i - 1][1]) / curve[i - 1][1]
        for i in range(1, len(curve))
        if curve[i - 1][1] > 0
    ]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    sd = math.sqrt(var)
    vol = sd * math.sqrt(252)
    sharpe = (mean / sd * math.sqrt(252)) if sd > 0 else 0.0

    opened = sum(1 for f in ledger.fills if f.reason == "open_spread_short")
    closed = sum(1 for f in ledger.fills if f.reason == "close_spread" and f.qty > 0)
    assigned = sum(1 for f in ledger.fills if f.reason == "assignment")
    # NET credit, not gross short premium. Counting only the short leg
    # reported $32,857 collected on a year where the actual net credit taken
    # in was $5,894 — the long protective legs cost most of it back, and that
    # is the number that decides whether the engine makes money.
    premium = sum(
        f.cash_delta for f in ledger.fills
        if f.reason in ("open_spread_short", "open_spread_long", "single_leg")
    )

    return Metrics(
        start_equity=start, end_equity=end, total_return=total_return, cagr=cagr,
        max_drawdown=abs(max_dd), max_drawdown_date=max_dd_day,
        sharpe=sharpe, volatility=vol, days=days,
        spreads_opened=opened, spreads_closed=closed, assignments=assigned,
        premium_collected=premium, commission_paid=commission,
        realized_pnl=ledger.realized_pnl,
    )
