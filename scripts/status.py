#!/usr/bin/env python3
"""
Operator status + control. Run this BEFORE flipping the kill switch back on.

    python3 scripts/status.py                    show status
    python3 scripts/status.py --enable           turn the kill switch on
    python3 scripts/status.py --disable "reason" turn it off
    python3 scripts/status.py --reset-breaker    reset a tripped breaker (asks for acknowledgement)

The point of the "safe to enable" verdict is to stop the specific mistake of
re-enabling a kill switch without first asking why it went off. A breaker
that tripped on a real loss will simply trip again on the next cycle.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import circuit_breaker  # noqa: E402
from core.atomic_io import atomic_write_text, read_json  # noqa: E402
from core.kill_switch import TRADING_ENABLED_PATH, check_order_gate, kill_switch_enabled  # noqa: E402
from core.structured_log import EVENTS_PATH, event  # noqa: E402


def _last_events(n: int = 5) -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    lines = EVENTS_PATH.read_text().strip().splitlines()[-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def show() -> int:
    alloc = read_json(ROOT / "allocator.json", default={}) or {}
    arm = alloc.get("allocations", {}).get("qqq", {})
    breaker = read_json(ROOT / "state" / "breaker.json", default={}) or {}
    state = read_json(ROOT / "state" / "qqq_state.json", default={}) or {}
    gate = check_order_gate("qqq")

    print("=" * 66)
    print("  MAGGI — QQQ drift-harvest (single arm)")
    print("=" * 66)
    print(f"  Kill switch      : {'ON' if kill_switch_enabled() else 'OFF'}")
    print(f"  Phase            : {arm.get('phase')}  (target_pct={arm.get('target_pct')}, pct={arm.get('pct')})")
    print(f"  Active           : {arm.get('active')}")
    print(f"  Order gate       : {'OPEN' if gate.allowed else 'CLOSED'} — {gate.reason}")
    print("-" * 66)
    tripped = breaker.get("tripped")
    print(f"  Breaker          : {'TRIPPED' if tripped else 'ok'}")
    if tripped:
        print(f"    reason         : {breaker.get('trip_reason')}")
        print(f"    tripped_at     : {breaker.get('tripped_at')}")
    if breaker.get("last_equity") is not None:
        print(f"    last equity    : ${float(breaker['last_equity']):,.2f}")
        print(f"    session open   : ${float(breaker.get('session_open_equity', 0)):,.2f}")
        print(f"    peak equity    : ${float(breaker.get('peak_equity', 0)):,.2f}")
    print("-" * 66)
    print(f"  Regime           : {state.get('current_regime', '(no state yet)')}")
    print(f"  Reference price  : {state.get('reference_price')}")
    print(f"  Ladder zones     : {state.get('acquisition_ladder')}")
    print(f"  Filled zones     : {state.get('filled_zones')}")
    print(f"  Core / excess    : {state.get('core_units')} / {state.get('excess_units')}")
    open_puts = [s for s in state.get("open_put_spreads", []) if s.get("status") == "OPEN"]
    open_calls = [c for c in state.get("open_calls", []) if c.get("status") == "OPEN"]
    print(f"  Open put spreads : {len(open_puts)}")
    print(f"  Open short calls : {len(open_calls)}")
    print(f"  State updated    : {state.get('last_updated', '(never)')}")
    print("-" * 66)
    print("  Recent events:")
    for e in _last_events():
        print(f"    {e.get('ts','?')[:19]}  {e.get('kind')}")
    if not _last_events():
        print("    (none yet)")
    print("-" * 66)

    safe = kill_switch_enabled() and not tripped
    if tripped:
        print("  VERDICT: NOT safe to enable — breaker is tripped.")
        print("           Resolve the loss, then --reset-breaker, then --enable.")
    elif not kill_switch_enabled():
        print("  VERDICT: kill switch is off. Check WHY before --enable.")
    else:
        print("  VERDICT: running normally.")
    print("=" * 66)
    return 0 if safe else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Maggi QQQ arm status and control")
    ap.add_argument("--enable", action="store_true", help="turn the kill switch ON")
    ap.add_argument("--disable", metavar="REASON", help="turn the kill switch OFF with a reason")
    ap.add_argument("--reset-breaker", action="store_true", help="reset a tripped circuit breaker")
    args = ap.parse_args()

    if args.disable:
        atomic_write_text(TRADING_ENABLED_PATH, "false\n")
        event("kill_switch_disabled", reason=args.disable, by="operator")
        print(f"Kill switch OFF — {args.disable}")
        return 0

    if args.reset_breaker:
        if circuit_breaker.is_tripped():
            reason = read_json(ROOT / "state" / "breaker.json", default={}).get("trip_reason")
            print(f"Breaker tripped: {reason}")
            answer = input("Has this loss been reviewed and resolved (not just unrealized-marked)? [yes/N] ")
            ok, msg = circuit_breaker.manual_reset(acknowledge_loss=answer.strip().lower() == "yes")
        else:
            ok, msg = circuit_breaker.manual_reset()
        print(msg)
        return 0 if ok else 1

    if args.enable:
        if circuit_breaker.is_tripped():
            print("REFUSING: the circuit breaker is tripped. Run --reset-breaker first.")
            return 1
        atomic_write_text(TRADING_ENABLED_PATH, "true\n")
        event("kill_switch_enabled", by="operator", at=datetime.now(timezone.utc).isoformat())
        print("Kill switch ON")
        return 0

    return show()


if __name__ == "__main__":
    sys.exit(main())
