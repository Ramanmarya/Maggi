"""
AcquisitionLadder — ATR-zone ladder below the reference price, recenter
logic, and anti-clustering (don't refill a zone that's already been used
since the last recenter).

V5 doc §5: zones at Reference, Reference-1.5ATR, Reference-3ATR,
Reference-5ATR (translated directly — same ATR multipliers, QQQ dollars
instead of NQ points, per the architecture doc's translation table).

Recenter trigger (doc §6) is left as "highest closing price since previous
ladder reset, recenter after a meaningful new high" — the doc doesn't pin
an exact "meaningful" threshold, so the agreed starting default of
>= 0.5 ATR above the prior reference (config.recenter_trigger_atr_mult) is
used, overridable in config.py.
"""

from __future__ import annotations

from datetime import date

from .broker_adapter import BrokerAdapter
from .config import StrategyConfig
from .state import PortfolioState


class AcquisitionLadder:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig):
        self._broker = broker
        self._config = config

    def build_zones(
        self, reference_price: float, atr: float, regime: str = "BULL"
    ) -> list[float]:
        """§4: the regime changes ATR spacing. Wider spacing in a downtrend
        means fewer, deeper entries rather than a rapid walk down the ladder."""
        from .regime_policy import RegimePolicy

        policy = RegimePolicy.for_regime(self._config, regime)
        mults = policy.scaled_multipliers(tuple(self._config.ladder_atr_multipliers))
        return [round(reference_price - m * atr, 2) for m in mults]

    def maybe_recenter(
        self, state: PortfolioState, price: float, atr: float, regime: str = "BULL"
    ) -> PortfolioState:
        """Recenter the reference price on a qualifying new high, per §6.

        Trigger: new closing high that exceeds prior reference by
        >= recenter_trigger_atr_mult * ATR (default 0.5 ATR).
        """
        threshold = self._config.recenter_trigger_atr_mult * atr

        if state.reference_price is None:
            state.reference_price = price
            state.last_recenter_price = price
            state.acquisition_ladder = self.build_zones(price, atr, regime)
            state.filled_zones = []
            state.zone_filled_on = {}
            return state

        if price >= state.reference_price + threshold:
            state.reference_price = price
            state.last_recenter_price = price
            state.acquisition_ladder = self.build_zones(price, atr, regime)
            state.filled_zones = []  # anti-clustering resets on recenter
            state.zone_filled_on = {}

        return state

    def unused_zone_at_or_below(
        self, state: PortfolioState, price: float, today: date | None = None
    ) -> float | None:
        """Return the shallowest unfilled acquisition zone that `price` has
        reached (price <= zone level), or None.

        The reference price itself (zones[0], multiplier 0.0 — "at the
        highs" per doc Phase A) is never treated as an acquisition zone.
        When price has fallen through several zones at once (a gap down),
        this deliberately returns the shallowest unfilled one rather than
        the deepest — matching the doc's "gradual" accumulation intent
        (§14): the next zone down is picked up on a later cycle rather than
        all at once.
        """
        # zones[0] is the reference itself. ALGORITHM.md §5 excludes it — "at
        # the highs" is Phase A, where the strategy waits rather than sells.
        # The operator can opt into trading it; see config for the trade-off.
        acquisition_zones = (
            state.acquisition_ladder
            if self._config.ladder_trade_at_reference
            else state.acquisition_ladder[1:]
        )
        candidates = [
            z for z in acquisition_zones
            if price <= z and self._zone_available(state, z, today)
        ]
        if not candidates:
            return None
        # max() is the SHALLOWEST zone: zones are prices, so the shallowest
        # (nearest the reference) is the highest number. This was min(), which
        # returned the deepest zone — the opposite of the docstring above and
        # of §5's gradual intent. On a gap-down the engine jumped straight to
        # its most aggressive level and burned zones fastest, which made the
        # exhaustion problem worse.
        return max(candidates)

    def _zone_available(self, state: PortfolioState, zone: float, today: date | None) -> bool:
        """A zone is available if unused, or used long enough ago to re-arm.

        With zone_rearm_days at 0 this is the source doc's rule: spent until
        the ladder recenters. That rule assumes the reference recenters
        reasonably often, which is true in a rising or choppy market and false
        in exactly the market the strategy is built for — a sustained decline
        consumes every zone in its first few percent and the engine then does
        nothing for the rest of it. A cooldown keeps accumulation gradual,
        which is what §5 actually asks for, without making it terminal.
        """
        if zone not in state.filled_zones:
            return True
        rearm = self._config.ladder_zone_rearm_days
        if rearm <= 0 or today is None:
            return False
        filled_on = state.zone_filled_on.get(str(zone))
        if not filled_on:
            return False
        try:
            return (today - date.fromisoformat(filled_on)).days >= rearm
        except ValueError:
            return False

    def mark_zone_filled(
        self, state: PortfolioState, zone: float, today: date | None = None
    ) -> PortfolioState:
        if zone not in state.filled_zones:
            state.filled_zones.append(zone)
        if today is not None:
            state.zone_filled_on[str(zone)] = today.isoformat()
        return state
