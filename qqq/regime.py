"""
RegimeEngine — classifies BULL / DEFENSIVE / NEUTRAL from 200DMA + slope.

V5 doc §4, exact rule:
  BULL:       price > 200DMA AND 200DMA rising
  DEFENSIVE:  price < 200DMA AND 200DMA falling
  otherwise:  NEUTRAL

Note this leaves real gaps as NEUTRAL by design (e.g. price > DMA but DMA
flat/falling, or price < DMA but DMA still rising) — that's the doc's rule,
not a bug. The strategy stays structurally bullish in every regime; regime
only affects accumulation aggressiveness, ATR spacing, put selection, and
call aggressiveness (per §4), not direction.
"""

from __future__ import annotations

from .broker_adapter import BrokerAdapter
from .config import StrategyConfig
from .state import Regime


class RegimeEngine:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig):
        self._broker = broker
        self._config = config

    def current_regime(self) -> Regime:
        price = self._broker.get_underlying_price()
        dma, slope = self._broker.get_200dma()
        band = self._config.regime_slope_flat_band

        if price > dma and slope > band:
            return "BULL"
        if price < dma and slope < -band:
            return "DEFENSIVE"
        return "NEUTRAL"
