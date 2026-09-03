"""
Metrics/reporting. TODO(V5-PARAM): the doc references a full §39 metrics
spec this doesn't have access to — this is a minimal starting set
(equity, open risk, regime, position counts) that's easy to extend once
that spec is available.
"""

from __future__ import annotations

from dataclasses import dataclass

from .broker_adapter import PortfolioSnapshot
from .state import PortfolioState


@dataclass(frozen=True)
class CycleMetrics:
    equity: float
    core_units: float
    excess_units: float
    open_put_spread_count: int
    open_call_count: int
    regime: str
    reference_price: float | None


def snapshot_metrics(state: PortfolioState, portfolio: PortfolioSnapshot) -> CycleMetrics:
    return CycleMetrics(
        equity=portfolio.equity,
        core_units=state.core_units,
        excess_units=state.excess_units,
        open_put_spread_count=sum(1 for s in state.open_put_spreads if s.status == "OPEN"),
        open_call_count=sum(1 for c in state.open_calls if c.status == "OPEN"),
        regime=state.current_regime,
        reference_price=state.reference_price,
    )


def format_metrics_line(m: CycleMetrics) -> str:
    return (
        f"equity=${m.equity:,.2f} regime={m.regime} "
        f"core_units={m.core_units:.2f} excess_units={m.excess_units:.2f} "
        f"open_puts={m.open_put_spread_count} open_calls={m.open_call_count} "
        f"ref_price={m.reference_price}"
    )
