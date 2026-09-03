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
        unit_size = self._config.core_unit_shares
        total = 0.0
        for pos in snapshot.positions:
            if pos.asset_class == "equity":
                total += pos.qty / unit_size
            elif pos.asset_class == "option":
                # NOTE: this assumes `qty` already reflects contract count
                # (negative = short). Actual option delta weighting requires
                # per-contract greeks from the option chain, not just qty —
                # a full implementation should fetch live delta per open
                # contract and multiply by qty here rather than treating
                # every option leg as delta = 1 per contract.
                total += pos.qty
        return total
