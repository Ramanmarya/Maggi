"""
Dashboard renderer — produces the single HTML page.

Layout follows the positions-app pattern the operator asked for: a grouped
tree of holdings where each core position owns the options written against
it, one dense row per leg, and a sticky summary bar. Numbers are the point,
so the type scale puts strike and effective price first and pushes
everything else to a supporting size.
"""

from __future__ import annotations

import html
from datetime import date, datetime

from .model import DashboardData, Leg

PILL = {
    "core": ("pill-solid", "Long QQQ"),
    "short_call": ("pill-call", "Short Call"),
    "short_put": ("pill-put", "Put Spread"),
    "long_put": ("pill-protect", "Protective"),
}


def _money(v, dp: int = 2, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v:,.{dp}f}"


def _short_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        d = date.fromisoformat(iso[:10])
        return f"{d.month}/{d.day}"
    except ValueError:
        return iso[:10]


def _row(leg: Leg, depth: int = 0) -> str:
    cls, _ = PILL.get(leg.kind, ("pill-solid", leg.label))
    indent = ' <span class="tree">└</span>' if depth else ""
    strike_cls = "strike-core" if leg.kind == "core" else "strike"
    eff_cls = {"core": "", "short_call": "eff-call", "short_put": "eff-put", "long_put": "eff-protect"}.get(leg.kind, "")

    secondary = f'<div class="sub">{_money(leg.secondary)}</div>' if leg.secondary is not None else ""
    prem = (
        f'<div class="qty">{leg.contracts}</div><div class="prem">{_money(leg.premium)}</div>'
        if leg.premium is not None
        else '<div class="dash">—</div>'
    )
    eff = f'<div class="eff {eff_cls}">{_money(leg.effective)}</div>' if leg.effective is not None else '<div class="dash">—</div>'
    note = f'<div class="note">{html.escape(leg.note)}</div>' if leg.note else ""

    out = [
        f'<div class="row{" nested" if depth else ""}">',
        f'  <div class="cell-label">{indent}<span class="pill {cls}">{html.escape(leg.label)}</span>{note}</div>',
        f'  <div class="cell-strike"><div class="{strike_cls}">{_money(leg.strike)}</div>{secondary}</div>',
        f'  <div class="cell-prem">{prem}</div>',
        f'  <div class="cell-date">{_short_date(leg.expiry)}</div>',
        f'  <div class="cell-eff">{eff}</div>',
        "</div>",
    ]
    for child in leg.children:
        out.append(_row(child, depth + 1))
    return "\n".join(out)


def _positions_tab(d: DashboardData) -> str:
    if not d.groups:
        return (
            '<div class="empty">'
            "<div class=\"empty-title\">No open positions</div>"
            f"<div class=\"empty-body\">The arm is in <b>{html.escape(d.phase)}</b> phase, so the order gate is "
            "closed and nothing has been opened yet. The decision cycle still runs end to end — "
            "check the <b>Bot</b> tab to confirm it is firing.<br><br>"
            "Run <code>python3 -m dashboard.server --demo</code> to preview this view with sample positions.</div>"
            "</div>"
        )

    blocks = []
    for title, legs in d.groups:
        count = len(legs) + sum(len(l.children) for l in legs)
        blocks.append(
            f'<div class="group"><div class="group-head">'
            f'<span class="group-title">{html.escape(title)} <span class="group-count">({count})</span></span>'
            f'<span class="col">STRIKE</span><span class="col">PREM</span>'
            f'<span class="col">DATE</span><span class="col col-eff">EFF PRICE</span>'
            "</div>"
            + "\n".join(_row(l) for l in legs)
            + "</div>"
        )
    return "\n".join(blocks)


def _history_tab(d: DashboardData) -> str:
    if not d.history:
        return '<div class="empty"><div class="empty-title">No trade history</div><div class="empty-body">Submitted, blocked and closed orders appear here as the bot runs.</div></div>'
    rows = []
    for e in reversed(d.history):
        kind = e.get("kind")
        badge = {"order_submitted": "ok", "order_blocked": "warn", "close_position": "muted"}.get(kind, "muted")
        if kind == "order_submitted" and e.get("action") == "submit_vertical_spread":
            desc = f"Put spread {_money(e.get('short_strike'))}/{_money(e.get('long_strike'))} × {e.get('contracts')} @ {_money(e.get('limit_net_credit'))}"
        elif kind == "order_submitted":
            desc = f"{e.get('side','')} {e.get('qty','')} {e.get('symbol','')} @ {_money(e.get('limit_price'))}"
        elif kind == "order_blocked":
            desc = f"{e.get('detail','')} — {e.get('reason','')}"
        else:
            desc = e.get("position_id", "")
        rows.append(
            f'<div class="hrow"><div class="htime">{html.escape(str(e.get("ts",""))[:19].replace("T"," "))}</div>'
            f'<div class="hkind {badge}">{html.escape(str(kind).replace("_"," "))}</div>'
            f'<div class="hdesc">{html.escape(desc)}</div></div>'
        )
    return f'<div class="history">{"".join(rows)}</div>'


def _bot_tab(d: DashboardData) -> str:
    gate_cls = "ok" if d.orders_allowed else "warn"
    ks_cls = "ok" if d.kill_switch else "bad"
    br_cls = "bad" if d.breaker_tripped else "ok"
    cards = f"""
    <div class="cards">
      <div class="card"><div class="k">Phase</div><div class="v">{html.escape(d.phase)}</div>
        <div class="hint">design and scanner_only never place orders</div></div>
      <div class="card"><div class="k">Kill switch</div><div class="v {ks_cls}">{"ON" if d.kill_switch else "OFF"}</div>
        <div class="hint">TRADING_ENABLED</div></div>
      <div class="card"><div class="k">Order gate</div><div class="v {gate_cls}">{"OPEN" if d.orders_allowed else "CLOSED"}</div>
        <div class="hint">{html.escape(d.gate_reason)}</div></div>
      <div class="card"><div class="k">Circuit breaker</div><div class="v {br_cls}">{"TRIPPED" if d.breaker_tripped else "ok"}</div>
        <div class="hint">{html.escape(d.breaker_reason or "no limit breached")}</div></div>
      <div class="card"><div class="k">Regime</div><div class="v">{html.escape(d.regime)}</div>
        <div class="hint">200DMA + slope</div></div>
      <div class="card"><div class="k">State updated</div><div class="v small">{html.escape(str(d.state_updated or "never")[:19].replace("T"," "))}</div>
        <div class="hint">last persisted cycle</div></div>
    </div>"""

    ev = []
    for e in reversed(d.events[-25:]):
        # Clamp every value: one oversized field (a traceback, a long reason)
        # would otherwise push the whole stream off the page.
        def _clip(v: object, limit: int = 90) -> str:
            text = " ".join(str(v).split())
            return text if len(text) <= limit else text[: limit - 1] + "\u2026"

        extra = " ".join(
            f'<span class="ek">{html.escape(str(k))}</span>=<span class="ev">{html.escape(_clip(v))}</span>'
            for k, v in e.items()
            if k not in ("ts", "kind") and not isinstance(v, (list, dict))
        )
        ev.append(
            f'<div class="erow"><span class="etime">{html.escape(str(e.get("ts",""))[11:19])}</span>'
            f'<span class="ekind">{html.escape(str(e.get("kind","")))}</span><span class="edet">{extra}</span></div>'
        )
    log = f'<div class="section-title">Event stream</div><div class="events">{"".join(ev) or "<div class=\'empty-body\'>No events yet.</div>"}</div>'
    return cards + log


def _risk_tab(d: DashboardData) -> str:
    daily = None
    if d.equity and d.session_open_equity:
        daily = (d.equity - d.session_open_equity) / d.session_open_equity
    dd = None
    if d.equity and d.peak_equity:
        dd = (d.equity - d.peak_equity) / d.peak_equity

    def pct(v):
        return "—" if v is None else f"{v*100:+.2f}%"

    zones = "".join(
        f'<div class="zone{" filled" if z in d.filled_zones else ""}">'
        f'<div class="zl">{_money(z)}</div>'
        f'<div class="zt">{"used" if z in d.filled_zones else "open"}</div></div>'
        for z in d.ladder
    ) or '<div class="empty-body">Ladder not built yet — it is created on the first daily cycle.</div>'

    exposure = ""
    if d.target_units is not None:
        held = d.core_units + d.excess_units
        ratio = held / max(d.target_units, 0.01)
        width = min(100, max(2, ratio * 100))
        under = held < d.target_units
        hint = (
            "Below target — reaching the next unfilled zone will propose a spread."
            if under
            else "At or above target — reaching a zone adds nothing until price falls further "
                 "or the curve's target rises."
        )
        exposure = f"""
        <div class="section-title">Exposure vs target curve</div>
        <div class="expo">
          <div class="expo-nums"><span>held <b>{held:.2f}</b> units</span><span>target <b>{d.target_units:.2f}</b> at −{d.decline_pct*100:.1f}%</span></div>
          <div class="bar"><div class="bar-fill{'' if under else ' over'}" style="width:{width:.0f}%"></div></div>
          <div class="hint">{hint}</div>
        </div>"""

    return f"""
    <div class="cards">
      <div class="card"><div class="k">Equity</div><div class="v">${_money(d.equity)}</div><div class="hint">broker account value</div></div>
      <div class="card"><div class="k">Session P&amp;L</div><div class="v {'bad' if daily and daily<0 else 'ok'}">{pct(daily)}</div><div class="hint">vs session open</div></div>
      <div class="card"><div class="k">Drawdown</div><div class="v {'bad' if dd and dd<-0.05 else ''}">{pct(dd)}</div><div class="hint">vs peak ${_money(d.peak_equity)}</div></div>
      <div class="card"><div class="k">Reference</div><div class="v">{_money(d.reference_price)}</div><div class="hint">ladder high-water mark</div></div>
      <div class="card"><div class="k">Decline</div><div class="v">−{d.decline_pct*100:.2f}%</div><div class="hint">from reference</div></div>
      <div class="card"><div class="k">Units</div><div class="v">{d.core_units:.2f} <span class="small">core</span> / {d.excess_units:.2f} <span class="small">excess</span></div><div class="hint">core is never call-capped</div></div>
    </div>
    {exposure}
    <div class="section-title">Acquisition ladder</div>
    <div class="zones">{zones}</div>"""


def render(d: DashboardData) -> str:
    banner = ""
    if d.demo:
        banner = '<div class="banner">SAMPLE DATA — this is a layout preview, not the live book.</div>'
    elif not d.orders_allowed:
        banner = f'<div class="banner banner-info">ORDER GATE CLOSED — {html.escape(d.gate_reason)}</div>'

    footer = (
        f'<span class="fcount">{d.position_count} POSITION{"" if d.position_count == 1 else "S"}</span>'
        f'<span class="favg">{"Avg " + _money(d.avg_effective) if d.avg_effective else ""}</span>'
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta http-equiv="refresh" content="60">
<title>Maggi QQQ</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">
  <header>
    <h1>Maggi QQQ <span class="accent">Positions</span></h1>
    <div class="status"><span class="dot {'live' if d.kill_switch and not d.breaker_tripped else 'off'}"></span>
      {"Connected" if d.kill_switch and not d.breaker_tripped else "Halted"}
      <span class="phase-chip">{html.escape(d.phase)}</span></div>
  </header>
  {banner}
  <div class="ticker">
    <span class="dot live"></span><span class="sym">QQQ</span>
    <span class="px">{_money(d.price)}</span>
    <span class="px-sub">{("live" if d.price_is_live else "last cycle")} &middot; {"ref " + _money(d.reference_price) if d.reference_price else ""}</span>
  </div>

  <nav class="tabs">
    <button class="tab active" data-tab="positions">Positions</button>
    <button class="tab" data-tab="history">History</button>
    <button class="tab" data-tab="bot">Bot</button>
    <button class="tab" data-tab="risk">Risk</button>
  </nav>

  <main>
    <section id="positions" class="panel active">{_positions_tab(d)}</section>
    <section id="history" class="panel">{_history_tab(d)}</section>
    <section id="bot" class="panel">{_bot_tab(d)}</section>
    <section id="risk" class="panel">{_risk_tab(d)}</section>
  </main>

  <div class="footer">{footer}</div>
</div>
<script>
document.querySelectorAll('.tab').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    document.querySelectorAll('.tab').forEach(function (b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('.panel').forEach(function (p) {{ p.classList.remove('active'); }});
    btn.classList.add('active');
    document.getElementById(btn.dataset.tab).classList.add('active');
    try {{ localStorage.setItem('maggi-tab', btn.dataset.tab); }} catch (e) {{}}
  }});
}});
try {{
  var saved = localStorage.getItem('maggi-tab');
  if (saved && document.getElementById(saved)) {{
    document.querySelector('.tab[data-tab="' + saved + '"]').click();
  }}
}} catch (e) {{}}
</script>
</body></html>"""


CSS = """
:root{
  --bg:#f2f4f7; --card:#ffffff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
  --green:#16a34a; --blue:#2563eb; --purple:#7c3aed; --amber:#d97706; --red:#dc2626;
  --head:#eef1f5;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0b0f16; --card:#141a23; --ink:#e6edf6; --muted:#8b9bb0; --line:#232c39;
         --green:#4ade80; --blue:#60a5fa; --purple:#a78bfa; --amber:#fbbf24; --red:#f87171;
         --head:#1a212c; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;}
.wrap{max-width:760px;margin:0 auto;padding:0 0 84px;}
header{padding:20px 16px 8px;}
h1{margin:0;font-size:30px;letter-spacing:-.02em;font-weight:800;}
h1 .accent{color:var(--blue);}
.status{margin-top:6px;color:var(--muted);font-size:14px;display:flex;align-items:center;gap:7px;}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block;flex:none;}
.dot.live{background:var(--green);} .dot.off{background:var(--red);}
.phase-chip{margin-left:auto;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  padding:3px 9px;border-radius:999px;background:var(--head);color:var(--muted);border:1px solid var(--line);}
.banner{margin:10px 16px;padding:9px 12px;border-radius:10px;font-size:12.5px;font-weight:600;
  background:#fef3c7;color:#92400e;border:1px solid #fde68a;}
.banner-info{background:var(--head);color:var(--muted);border-color:var(--line);}
@media (prefers-color-scheme: dark){ .banner{background:#3a2c07;color:#fcd34d;border-color:#5b4708;} }

.ticker{margin:8px 12px 4px;background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:12px 16px;display:flex;align-items:center;gap:10px;}
.sym{background:var(--blue);color:#fff;font-weight:700;font-size:13px;padding:4px 12px;border-radius:999px;}
.px{font-size:26px;font-weight:800;color:var(--blue);letter-spacing:-.02em;}
.px-sub{margin-left:auto;color:var(--muted);font-size:12.5px;}

.tabs{display:flex;gap:4px;padding:10px 12px 6px;position:sticky;top:0;background:var(--bg);z-index:5;}
.tab{flex:1;border:1px solid var(--line);background:var(--card);color:var(--muted);
  padding:8px 4px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;}
.tab.active{background:var(--blue);border-color:var(--blue);color:#fff;}
.panel{display:none;padding:0 12px;} .panel.active{display:block;}

.group{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:12px;}
.group-head{display:grid;grid-template-columns:1.55fr .85fr .7fr .5fr .8fr;gap:6px;align-items:center;
  background:var(--head);padding:9px 12px;border-bottom:1px solid var(--line);}
.group-title{font-size:14px;font-weight:700;} .group-count{color:var(--muted);font-weight:600;}
.col{font-size:10px;font-weight:700;letter-spacing:.09em;color:var(--muted);text-align:right;}
.col-eff{text-decoration:underline;text-underline-offset:3px;}

.row{display:grid;grid-template-columns:1.55fr .85fr .7fr .5fr .8fr;gap:6px;align-items:center;
  padding:11px 12px;border-bottom:1px solid var(--line);}
.row:last-child{border-bottom:0;}
.row.nested{background:color-mix(in srgb, var(--head) 45%, transparent);}
.tree{color:var(--muted);margin-right:2px;}
.pill{display:inline-block;font-size:12px;font-weight:700;padding:5px 11px;border-radius:999px;white-space:nowrap;}
.pill-solid{background:var(--green);color:#fff;}
.pill-call{border:1.5px solid var(--blue);color:var(--blue);}
.pill-put{border:1.5px solid var(--purple);color:var(--purple);}
.pill-protect{border:1.5px solid var(--muted);color:var(--muted);}
.note{font-size:11px;color:var(--muted);margin-top:4px;}
.cell-strike,.cell-prem,.cell-date,.cell-eff{text-align:right;}
.strike-core{font-size:19px;font-weight:800;color:var(--green);letter-spacing:-.01em;}
.strike{font-size:18px;font-weight:700;}
.sub{font-size:12px;color:var(--muted);}
.qty{font-size:12px;color:var(--muted);} .prem{font-size:13.5px;font-weight:700;color:var(--blue);}
.cell-date{font-size:12.5px;color:var(--muted);}
.eff{font-size:16px;font-weight:800;} .eff-call{color:var(--blue);} .eff-put{color:var(--amber);} .eff-protect{color:var(--muted);}
.dash{color:var(--muted);}

.footer{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);
  padding:13px 20px calc(13px + env(safe-area-inset-bottom));display:flex;justify-content:space-between;align-items:center;}
.fcount{font-size:11.5px;font-weight:700;letter-spacing:.09em;color:var(--muted);}
.favg{font-size:16px;font-weight:800;}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 13px;}
.card .k{font-size:10.5px;font-weight:700;letter-spacing:.08em;color:var(--muted);text-transform:uppercase;}
.card .v{font-size:20px;font-weight:800;margin-top:3px;letter-spacing:-.01em;}
.card .v.small{font-size:13.5px;font-weight:700;}
.card .hint{font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.35;}
.ok{color:var(--green);} .warn{color:var(--amber);} .bad{color:var(--red);} .muted{color:var(--muted);}
.small{font-size:11.5px;font-weight:600;color:var(--muted);}

.section-title{font-size:11px;font-weight:700;letter-spacing:.09em;color:var(--muted);
  text-transform:uppercase;margin:16px 2px 8px;}
.history,.events{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;}
.hrow{display:grid;grid-template-columns:auto auto 1fr;gap:10px;align-items:center;padding:10px 13px;border-bottom:1px solid var(--line);}
.hrow:last-child,.erow:last-child{border-bottom:0;}
.htime{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;}
.hkind{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;}
.hdesc{font-size:13px;}
.erow{display:flex;gap:9px;align-items:baseline;padding:8px 13px;border-bottom:1px solid var(--line);font-size:12px;}
.etime{color:var(--muted);font-variant-numeric:tabular-nums;flex:none;}
.ekind{font-weight:700;flex:none;} .edet{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.ek{color:var(--muted);} .ev{color:var(--ink);font-weight:600;}

.zones{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px;}
.zone{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center;}
.zone.filled{border-color:var(--amber);}
.zl{font-size:16px;font-weight:800;} .zt{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-top:2px;}
.zone.filled .zt{color:var(--amber);}
.expo{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px;}
.expo-nums{display:flex;justify-content:space-between;font-size:13px;margin-bottom:8px;}
.bar{height:9px;background:var(--head);border-radius:999px;overflow:hidden;}
.bar-fill{height:100%;background:var(--blue);border-radius:999px;}
.bar-fill.over{background:var(--green);}

.empty{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:26px 20px;text-align:center;}
.empty-title{font-size:16px;font-weight:700;margin-bottom:7px;}
.empty-body{font-size:13.5px;color:var(--muted);line-height:1.55;}
code{background:var(--head);padding:2px 6px;border-radius:5px;font-size:12.5px;}
"""
