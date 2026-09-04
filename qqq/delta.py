"""
DeltaAggregator — total portfolio delta in unit-equivalents:
    long share units + short-put delta - protective-put delta - short-call delta

Unit-equivalents let core shares and options be compared on the same scale
(1 unit = core_unit_shares, default 100 shares = 1 option contract).
"""

from __future__ import annotations

from .broker_adapter import BrokerAdapter, PortfolioSnapshot
from .config import StrategyConfig


class DeltaAggregator:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig):
        self._broker = broker
        self._config = config

    def total_unit_delta(self, snapshot: PortfolioSnapshot) -> float:
        """Portfolio delta in unit-equivalents (1 unit = core_unit_shares).

        Options are weighted by their actual delta, not their contract count.
        The previous version added `pos.qty` directly, which scored a short
        20-delta put — a bullish position worth about +0.20 units — as -1.00
        units, the same as being short 100 shares. Sign and magnitude both
        wrong. Put spreads happened to cancel the error (short -1 plus long +1
        summing to zero), which is why it never showed up in results while the
        strategy traded nothing else.

        A contract's delta is per share, and a contract covers unit_size
        shares, so its contribution in units is simply qty * delta.
        """
        unit_size = self._config.core_unit_shares
        total = 0.0
        for pos in snapshot.positions:
            if pos.asset_class == "equity":
                # The cash-sweep instrument is a cash equivalent, not Nasdaq
                # exposure. Counting it would read a Treasury ETF as a long
                # QQQ position and shut the put engine down completely.
                if pos.symbol == getattr(self._config, "cash_sweep_symbol", None):
                    continue
                total += pos.qty / unit_size
            elif pos.asset_class == "option":
                delta = self._broker.option_delta(pos.symbol)
                if delta is None:
                    # Unknown greek: contribute nothing rather than invent a
                    # number. Understating exposure is the safer failure here —
                    # it can only make the engine more willing to add, which
                    # the risk gates still bound.
                    continue
                total += pos.qty * delta
        return total
