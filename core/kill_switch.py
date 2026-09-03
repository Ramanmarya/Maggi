"""
The order gate. Two independent conditions must BOTH hold before any order
reaches the broker:

  1. TRADING_ENABLED contains "true"  — the operator kill switch, and what
     the circuit breaker flips when it trips.
  2. allocator.json phase is an order-permitting phase — design and
     scanner_only produce signals and digests but never orders.

Fail-closed everywhere: a missing file, unparseable JSON, an unknown phase
or an unreadable kill switch all mean NO ORDERS. The only way to get a
`True` out of this module is for everything to be explicitly, readably
correct.

Closing an existing position is deliberately NOT gated here (see
gated_broker.GatedBroker) — a kill switch must stop the bot opening new
risk, not trap it in positions it can no longer exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .atomic_io import read_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRADING_ENABLED_PATH = PROJECT_ROOT / "TRADING_ENABLED"
ALLOCATOR_PATH = PROJECT_ROOT / "allocator.json"

ORDER_PERMITTING_PHASES = frozenset({"paper", "tiny_live", "live"})
KNOWN_PHASES = frozenset({"design", "scanner_only", "paper", "tiny_live", "live", "killed"})


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    phase: str | None = None


def kill_switch_enabled(path: Path | None = None) -> bool:
    """True only if the file exists and reads exactly 'true' (case-insensitive).

    The path is resolved at call time, not bound as a default argument — a
    default would freeze the module-level constant at import and make the
    location impossible to redirect afterwards, including from tests.
    """
    try:
        return (path or TRADING_ENABLED_PATH).read_text().strip().lower() == "true"
    except Exception:
        return False  # fail closed


def current_phase(arm: str = "qqq", allocator_path: Path | None = None) -> str | None:
    try:
        data = read_json(allocator_path or ALLOCATOR_PATH, default=None)
        if not isinstance(data, dict):
            return None
        alloc = data.get("allocations", {}).get(arm)
        if not isinstance(alloc, dict):
            return None
        phase = alloc.get("phase")
        return phase if isinstance(phase, str) else None
    except Exception:
        return None  # fail closed


def check_order_gate(arm: str = "qqq") -> GateResult:
    """The single authority on whether a new order may be submitted."""
    if not kill_switch_enabled():
        return GateResult(False, f"kill switch is OFF ({TRADING_ENABLED_PATH})")

    phase = current_phase(arm)
    if phase is None:
        return GateResult(False, f"could not read phase for arm '{arm}' from {ALLOCATOR_PATH}")
    if phase not in KNOWN_PHASES:
        return GateResult(False, f"unrecognized phase {phase!r} — refusing to guess", phase)
    if phase not in ORDER_PERMITTING_PHASES:
        return GateResult(False, f"phase is {phase!r}; orders permitted only in {sorted(ORDER_PERMITTING_PHASES)}", phase)

    alloc = (read_json(ALLOCATOR_PATH, default={}) or {}).get("allocations", {}).get(arm, {})
    if not alloc.get("active", False):
        return GateResult(False, f"arm '{arm}' is not active in allocator.json", phase)

    return GateResult(True, f"phase={phase}, kill switch on, arm active", phase)


def trip(reason: str, path: Path | None = None) -> None:
    """Flip the kill switch off. Called by the circuit breaker, never by strategy code."""
    from .atomic_io import atomic_write_text
    from .structured_log import event

    atomic_write_text(path or TRADING_ENABLED_PATH, "false\n")
    event("kill_switch_tripped", reason=reason)
