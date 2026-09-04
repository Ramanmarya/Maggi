"""
PortfolioState — the strategy's working memory (core/excess units, open
spreads, open calls, ladder/reference bookkeeping) and its JSON persistence.

Persisted after every decision-cycle step via atomic write-then-rename so a
crash mid-write can't leave a corrupt state file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core.atomic_io import atomic_write_json, read_json

Regime = Literal["BULL", "DEFENSIVE", "NEUTRAL"]


@dataclass
class PutSpreadPosition:
    id: str
    short_strike: float
    long_strike: float
    expiry: str  # ISO date
    contracts: int
    net_credit: float
    opened_at: str
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    close_price: float | None = None
    closed_at: str | None = None
    # OCC symbols of the two legs. Without them an open spread cannot be
    # re-priced, so the only available exit is a time-based one. Defaulted so
    # state files written before profit-capture existed still load.
    short_symbol: str = ""
    long_symbol: str = ""


@dataclass
class CallPosition:
    id: str
    short_strike: float
    expiry: str  # ISO date
    contracts: int
    premium_received: float
    opened_at: str
    covers_units: float = 1.0
    status: Literal["OPEN", "CLOSED", "ASSIGNED"] = "OPEN"
    close_price: float | None = None
    closed_at: str | None = None


@dataclass
class LongCallPosition:
    """A PURCHASED call. Deliberately not a CallPosition.

    RiskManager treats every entry in open_calls as SHORT -- it prices
    unlimited loss above the strike and demands share coverage. A long call
    is the opposite in both respects: its loss is bounded at the premium
    paid, it needs no coverage, and its delta is +. Filing one in open_calls
    would invert the sign of both the shock test and the coverage check, so
    longs live in their own list.
    """
    id: str
    symbol: str  # OCC symbol; needed to mark and to close the position
    strike: float
    expiry: str  # ISO date
    contracts: int
    premium_paid: float
    opened_at: str
    status: Literal["OPEN", "CLOSED", "EXPIRED"] = "OPEN"
    close_price: float | None = None
    closed_at: str | None = None


@dataclass
class PortfolioState:
    core_units: float = 1.0  # target 1.0 == core_unit_shares (default 100)
    excess_units: float = 0.0
    open_put_spreads: list[PutSpreadPosition] = field(default_factory=list)
    open_calls: list[CallPosition] = field(default_factory=list)
    open_long_calls: list[LongCallPosition] = field(default_factory=list)
    reference_price: float | None = None
    acquisition_ladder: list[float] = field(default_factory=list)
    filled_zones: list[float] = field(default_factory=list)  # set() isn't JSON-native
    # zone -> ISO date it was filled, so a zone can re-arm after a cooldown
    # instead of being spent for the life of the ladder.
    zone_filled_on: dict = field(default_factory=dict)
    last_recenter_price: float | None = None
    # ISO date of the last put entry, for the scheduled writer's cadence.
    last_put_entry: str | None = None
    current_regime: Regime = "NEUTRAL"
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioState":
        put_spreads = [PutSpreadPosition(**p) for p in d.get("open_put_spreads", [])]
        calls = [CallPosition(**c) for c in d.get("open_calls", [])]
        # Absent in states written before long calls existed; default to empty
        # so an older state file loads rather than raising.
        longs = [LongCallPosition(**c) for c in d.get("open_long_calls", [])]
        return cls(
            core_units=d.get("core_units", 1.0),
            excess_units=d.get("excess_units", 0.0),
            open_put_spreads=put_spreads,
            open_calls=calls,
            open_long_calls=longs,
            reference_price=d.get("reference_price"),
            acquisition_ladder=d.get("acquisition_ladder", []),
            filled_zones=d.get("filled_zones", []),
            zone_filled_on=d.get("zone_filled_on", {}),
            last_recenter_price=d.get("last_recenter_price"),
            last_put_entry=d.get("last_put_entry"),
            current_regime=d.get("current_regime", "NEUTRAL"),
            last_updated=d.get("last_updated", datetime.now(timezone.utc).isoformat()),
        )


def load_state(path: Path) -> PortfolioState:
    """Load state from disk, or return a fresh PortfolioState if none exists.

    A corrupt file raises (via core.atomic_io.read_json) rather than quietly
    returning a fresh state — silently forgetting open positions is worse
    than failing the cycle loudly.
    """
    raw = read_json(path, default=None)
    if raw is None:
        return PortfolioState()
    return PortfolioState.from_dict(raw)


def save_state(state: PortfolioState, path: Path) -> None:
    """Persist via the shared atomic write-then-rename helper."""
    state.last_updated = datetime.now(timezone.utc).isoformat()
    atomic_write_json(path, state.to_dict())
