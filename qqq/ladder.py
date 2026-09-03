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

from .broker_adapter import BrokerAdapter
from .config import StrategyConfig
from .state import PortfolioState


class AcquisitionLadder:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig):
        self._broker = broker
        self._config = config

    def build_zones(self, reference_price: float, atr: float) -> list[float]:
        return [
            round(reference_price - mult * atr, 2)
            for mult in self._config.ladder_atr_multipliers
        ]

    def maybe_recenter(self, state: PortfolioState, price: float, atr: float) -> PortfolioState:
        """Recenter the reference price on a qualifying new high, per §6.

        Trigger: new closing high that exceeds prior reference by
        >= recenter_trigger_atr_mult * ATR (default 0.5 ATR).
        """
        threshold = self._config.recenter_trigger_atr_mult * atr

        if state.reference_price is None:
            state.reference_price = price
            state.last_recenter_price = price
            state.acquisition_ladder = self.build_zones(price, atr)
            state.filled_zones = []
            return state

        if price >= state.reference_price + threshold:
            state.reference_price = price
            state.last_recenter_price = price
            state.acquisition_ladder = self.build_zones(price, atr)
            state.filled_zones = []  # anti-clustering resets on recenter

        return state

    def unused_zone_at_or_below(self, state: PortfolioState, price: float) -> float | None:
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
        acquisition_zones = state.acquisition_ladder[1:]  # exclude reference itself
        candidates = [
            z for z in acquisition_zones if price <= z and z not in state.filled_zones
        ]
        if not candidates:
            return None
        return min(candidates)

    def mark_zone_filled(self, state: PortfolioState, zone: float) -> PortfolioState:
        if zone not in state.filled_zones:
            state.filled_zones.append(zone)
        return state
