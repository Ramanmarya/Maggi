"""
Dashboard data model — turns the bot's state files into the rows the view
renders. Kept separate from rendering so the layout can be tested without
a browser and changed without touching data logic.

Everything here reads; nothing writes. The dashboard is a window on the
bot, never a control surface for it — kill switch and phase changes go
through scripts/status.py so there is one auditable path to trading state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_json(path: Path, default):
    import json

    try:
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


@dataclass
class Leg:
    """One rendered row: a core holding, or an option nested beneath it."""
    kind: str                      # "core" | "short_call" | "short_put" | "long_put"
    label: str
    strike: float | None = None
    secondary: float | None = None  # the smaller number under the strike
    contracts: int | None = None
    premium: float | None = None
    expiry: str | None = None
    effective: float | None = None  # effective buy/sell price
    children: list["Leg"] = field(default_factory=list)
    note: str = ""


@dataclass
class DashboardData:
    phase: str
    kill_switch: bool
    orders_allowed: bool
    gate_reason: str
    breaker_tripped: bool
    breaker_reason: str
    regime: str
    equity: float | None
    session_open_equity: float | None
    peak_equity: float | None
    price: float | None
    reference_price: float | None
    ladder: list[float]
    filled_zones: list[float]
    core_units: float
    excess_units: float
    target_units: float | None
    decline_pct: float
    groups: list[tuple[str, list[Leg]]]
    history: list[dict]
    events: list[dict]
    position_count: int
    avg_effective: float | None
    state_updated: str | None
    price_is_live: bool = False
    demo: bool = False


def _events(limit: int = 400) -> list[dict]:
    import json

    path = ROOT / "logs" / "events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().strip().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load(demo: bool = False) -> DashboardData:
    if demo:
        return _demo_data()

    import sys

    sys.path.insert(0, str(ROOT))
    from core.kill_switch import check_order_gate, kill_switch_enabled

    state = _read_json(ROOT / "state" / "qqq_state.json", {}) or {}
    breaker = _read_json(ROOT / "state" / "breaker.json", {}) or {}
    alloc = _read_json(ROOT / "allocator.json", {}) or {}
    arm = alloc.get("allocations", {}).get("qqq", {})
    gate = check_order_gate("qqq")

    core_units = float(state.get("core_units") or 0)
    excess_units = float(state.get("excess_units") or 0)
    reference = state.get("reference_price")

    # Prefer a live quote so the header is not silently showing the stored
    # reference and calling it the price. Falls back to state whenever the
    # broker is unreachable or credentials are absent, and the caller is told
    # which it got via `price_is_live`.
    price, price_is_live = reference, False
    core_value = core_pl = None
    try:
        from qqq.alpaca_adapter import AlpacaAdapter
        from qqq.config import StrategyConfig as _SC

        _cfg = _SC()
        if _cfg.alpaca_api_key and _cfg.alpaca_secret_key:
            _ad = AlpacaAdapter(_cfg)
            price, price_is_live = _ad.get_underlying_price(), True
            for _p in _ad.get_current_positions().positions:
                if _p.symbol == _cfg.symbol and _p.asset_class == "equity":
                    core_value, core_pl = _p.market_value, _p.unrealized_pl
    except Exception:
        pass

    core_children: list[Leg] = []
    for c in state.get("open_calls", []):
        if c.get("status") != "OPEN":
            continue
        prem = float(c.get("premium_received") or 0)
        core_children.append(
            Leg(
                kind="short_call",
                label="Short Call",
                strike=float(c["short_strike"]),
                contracts=int(c.get("contracts") or 0),
                premium=prem,
                expiry=c.get("expiry"),
                effective=float(c["short_strike"]) + prem,
            )
        )

    groups: list[tuple[str, list[Leg]]] = []
    if core_units or excess_units or core_children:
        groups.append(
            (
                "Core QQQ",
                [
                    Leg(
                        kind="core",
                        label="Long QQQ",
                        strike=reference,
                        secondary=price,
                        children=core_children,
                        note=(
                            f"{core_units:.2f} core + {excess_units:.2f} excess units"
                            + (f" · ${core_value:,.0f} · P&L ${core_pl:+,.2f}" if core_value is not None else "")
                        ),
                    )
                ],
            )
        )

    spreads: list[Leg] = []
    for s in state.get("open_put_spreads", []):
        if s.get("status") != "OPEN":
            continue
        credit = float(s.get("net_credit") or 0)
        short_k = float(s["short_strike"])
        spreads.append(
            Leg(
                kind="short_put",
                label="Put Credit Spread",
                strike=short_k,
                secondary=float(s["long_strike"]),
                contracts=int(s.get("contracts") or 0),
                premium=credit,
                expiry=s.get("expiry"),
                effective=short_k - credit,
                children=[
                    Leg(
                        kind="long_put",
                        label="Protective Put",
                        strike=float(s["long_strike"]),
                        contracts=int(s.get("contracts") or 0),
                        expiry=s.get("expiry"),
                    )
                ],
            )
        )
    if spreads:
        groups.append(("Put Credit Spreads", spreads))

    evs = _events()
    history = [
        e for e in evs
        if e.get("kind") in ("order_submitted", "order_blocked", "close_position")
    ]

    effectives = [l.effective for _, legs in groups for l in legs if l.effective]
    decline = 0.0
    if reference and price and reference > 0:
        decline = max(0.0, (reference - price) / reference)

    target = None
    try:
        from qqq.config import StrategyConfig
        from qqq.exposure_curve import target_units_for_decline

        target = target_units_for_decline(StrategyConfig(), decline)
    except Exception:
        pass

    return DashboardData(
        phase=arm.get("phase", "unknown"),
        kill_switch=kill_switch_enabled(),
        orders_allowed=gate.allowed,
        gate_reason=gate.reason,
        breaker_tripped=bool(breaker.get("tripped")),
        breaker_reason=breaker.get("trip_reason", ""),
        regime=state.get("current_regime", "—"),
        equity=breaker.get("last_equity"),
        session_open_equity=breaker.get("session_open_equity"),
        peak_equity=breaker.get("peak_equity"),
        price=price,
        reference_price=reference,
        ladder=state.get("acquisition_ladder") or [],
        filled_zones=state.get("filled_zones") or [],
        core_units=core_units,
        excess_units=excess_units,
        target_units=target,
        decline_pct=decline,
        groups=groups,
        history=history,
        events=evs[-60:],
        position_count=sum(len(legs) + sum(len(l.children) for l in legs) for _, legs in groups),
        avg_effective=(sum(effectives) / len(effectives)) if effectives else None,
        state_updated=state.get("last_updated"),
        price_is_live=price_is_live,
    )


def _demo_data() -> DashboardData:
    """Representative layout with sample numbers, for checking the view when
    the bot has not opened anything yet. Always badged as sample in the UI so
    it can never be mistaken for the live book.
    """
    today = date.today()

    def d(days: int) -> str:
        from datetime import timedelta

        return (today + timedelta(days=days)).isoformat()

    core = Leg(
        kind="core", label="Long QQQ", strike=612.40, secondary=598.15,
        note="1.00 core + 1.75 excess units",
        children=[
            Leg("short_call", "Short Call", 625.0, contracts=1, premium=4.85, expiry=d(28), effective=629.85),
            Leg("short_call", "Short Call", 632.0, contracts=1, premium=3.10, expiry=d(35), effective=635.10),
        ],
    )
    spreads = [
        Leg("short_put", "Put Credit Spread", 578.0, secondary=568.0, contracts=1, premium=1.62,
            expiry=d(24), effective=576.38,
            children=[Leg("long_put", "Protective Put", 568.0, contracts=1, expiry=d(24))]),
        Leg("short_put", "Put Credit Spread", 566.0, secondary=556.0, contracts=1, premium=1.44,
            expiry=d(31), effective=564.56,
            children=[Leg("long_put", "Protective Put", 556.0, contracts=1, expiry=d(31))]),
    ]
    evs = [
        {"ts": f"{today}T19:45:02", "kind": "cycle_complete", "mode": "daily", "regime": "BULL", "equity": 103480.22},
        {"ts": f"{today}T19:45:01", "kind": "order_submitted", "action": "submit_vertical_spread",
         "underlying": "QQQ", "short_strike": 566.0, "long_strike": 556.0, "contracts": 1, "limit_net_credit": 1.44},
        {"ts": f"{today}T16:30:00", "kind": "cycle_skipped", "mode": "intraday", "reason": "market_closed"},
    ]
    return DashboardData(
        phase="paper", kill_switch=True, orders_allowed=True, gate_reason="phase=paper, kill switch on, arm active",
        breaker_tripped=False, breaker_reason="", regime="BULL",
        equity=103480.22, session_open_equity=102910.05, peak_equity=104220.00,
        price=598.15, reference_price=612.40,
        ladder=[612.40, 598.15, 583.90, 555.40], filled_zones=[598.15],
        core_units=1.0, excess_units=1.75, target_units=1.70, decline_pct=0.0233,
        groups=[("Core QQQ", [core]), ("Put Credit Spreads", spreads)],
        history=[e for e in evs if e["kind"].startswith("order")],
        events=evs,
        position_count=1 + 2 + 2 + 2,
        avg_effective=(629.85 + 635.10 + 576.38 + 564.56) / 4,
        state_updated=f"{today}T19:45:02+00:00",
        price_is_live=False,
        demo=True,
    )
