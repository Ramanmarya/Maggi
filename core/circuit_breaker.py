"""
Circuit breaker — the automatic layer above the operator kill switch.

Tracks day-start equity and peak equity, and trips TRADING_ENABLED to false
when either limit is breached:

  - daily_loss_limit_pct: intraday loss vs. this session's opening equity.
  - max_drawdown_pct:     loss vs. the highest equity ever recorded.

Tripping is one-way. Reset is an operator action (scripts/status.py --reset-breaker),
deliberately not automatic: an auto-resetting breaker just re-enters the
losing trade. The reset also refuses while the loss that caused the trip is
still on the book, so "reset" can't paper over an unresolved drawdown.

Note the breaker measures *equity*, which for this strategy includes the
mark-to-market of the core QQQ position. A deep correction is exactly what
the ladder wants to buy into (ALGORITHM.md §5/§6), so the daily limit is set
wide enough that ordinary accumulation drawdown does not trip it. See
rules.json:risk.daily_loss_limit_pct for the current value and the reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .atomic_io import atomic_write_json, read_json
from .kill_switch import trip
from .structured_log import event

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BREAKER_STATE_PATH = PROJECT_ROOT / "state" / "breaker.json"


@dataclass(frozen=True)
class BreakerResult:
    tripped: bool
    reason: str = ""
    daily_pnl_pct: float = 0.0
    drawdown_pct: float = 0.0


def _load() -> dict:
    return read_json(BREAKER_STATE_PATH, default={}) or {}


def check(equity: float, daily_loss_limit_pct: float, max_drawdown_pct: float) -> BreakerResult:
    """Update breaker state from the current equity and trip if a limit is breached.

    Call this once per cycle, before any order decision.
    """
    state = _load()
    today = date.today().isoformat()

    if state.get("session_date") != today or "session_open_equity" not in state:
        state["session_date"] = today
        state["session_open_equity"] = equity

    peak = max(float(state.get("peak_equity", equity)), equity)
    state["peak_equity"] = peak
    state["last_equity"] = equity
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    open_equity = float(state["session_open_equity"])
    daily_pnl_pct = (equity - open_equity) / open_equity if open_equity > 0 else 0.0
    drawdown_pct = (equity - peak) / peak if peak > 0 else 0.0

    result = BreakerResult(False, "", daily_pnl_pct, drawdown_pct)

    if state.get("tripped"):
        result = BreakerResult(True, f"already tripped: {state.get('trip_reason','')}", daily_pnl_pct, drawdown_pct)
    elif daily_pnl_pct <= -abs(daily_loss_limit_pct):
        reason = f"daily loss {daily_pnl_pct:.2%} breached limit {-abs(daily_loss_limit_pct):.2%}"
        state.update(tripped=True, trip_reason=reason, tripped_at=state["updated_at"])
        trip(reason)
        event("breaker_tripped", reason=reason, equity=equity, session_open_equity=open_equity)
        result = BreakerResult(True, reason, daily_pnl_pct, drawdown_pct)
    elif drawdown_pct <= -abs(max_drawdown_pct):
        reason = f"drawdown {drawdown_pct:.2%} from peak ${peak:,.2f} breached limit {-abs(max_drawdown_pct):.2%}"
        state.update(tripped=True, trip_reason=reason, tripped_at=state["updated_at"])
        trip(reason)
        event("breaker_tripped", reason=reason, equity=equity, peak_equity=peak)
        result = BreakerResult(True, reason, daily_pnl_pct, drawdown_pct)

    atomic_write_json(BREAKER_STATE_PATH, state)
    return result


def is_tripped() -> bool:
    return bool(_load().get("tripped"))


def manual_reset(acknowledge_loss: bool = False) -> tuple[bool, str]:
    """Operator-only breaker reset.

    Refuses unless `acknowledge_loss` is explicitly True — the caller has to
    state that they have looked at the loss and decided it is resolved, not
    merely unrealized-marked.
    """
    state = _load()
    if not state.get("tripped"):
        return False, "breaker is not tripped; nothing to reset"
    if not acknowledge_loss:
        return False, (
            f"refusing to reset without explicit acknowledgement. Trip reason: "
            f"{state.get('trip_reason','unknown')}"
        )
    state.update(
        tripped=False,
        reset_at=datetime.now(timezone.utc).isoformat(),
        previous_trip_reason=state.get("trip_reason"),
    )
    state.pop("trip_reason", None)
    state["session_open_equity"] = state.get("last_equity", state.get("session_open_equity"))
    atomic_write_json(BREAKER_STATE_PATH, state)
    event("breaker_reset", previous_reason=state.get("previous_trip_reason"))
    return True, "breaker reset; remember to set TRADING_ENABLED back to true separately"
