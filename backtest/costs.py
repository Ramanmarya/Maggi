"""
Transaction costs.

Neither data plan carries historical NBBO, so the bid/ask spread cannot be
measured and has to be modelled. That is a real limitation and it cuts one
way: a premium-selling strategy sells nearer the bid than the mid, so a
backtest run at mid systematically overstates the credit collected. On a
$1.71 credit a two-cent-per-side spread is over 2% of the premium, and it is
paid on every open and every close.

So the default is deliberately pessimistic. If results only work at zero
slippage, they do not work.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    option_commission_per_contract: float = 0.65
    equity_commission_per_share: float = 0.0     # Alpaca equities are commission-free
    option_spread_pct: float = 0.02              # full spread as a fraction of mid
    option_spread_min: float = 0.02              # ...but never tighter than 2 cents
    equity_slippage_bps: float = 1.0

    def option_half_spread(self, mid: float) -> float:
        return max(self.option_spread_min, mid * self.option_spread_pct) / 2.0

    def option_fill_price(self, mid: float, side: str,
                          half_spread: float | None = None) -> float:
        """Marketable fill: buyers pay up, sellers receive less. Never negative.

        `half_spread` overrides the model with a measured one. That is the
        whole point of paying for historical NBBO: the modelled figure below
        is a guess calibrated to be pessimistic, and a guess applied to every
        open and every close compounds into a large share of a premium
        seller's P&L. When a real quote is available, use it.
        """
        half = self.option_half_spread(mid) if half_spread is None else max(0.0, half_spread)
        return max(0.01, mid + half) if side == "buy" else max(0.0, mid - half)

    def equity_fill_price(self, price: float, side: str) -> float:
        slip = price * self.equity_slippage_bps / 10_000.0
        return price + slip if side == "buy" else price - slip

    def option_commission(self, contracts: float) -> float:
        return abs(contracts) * self.option_commission_per_contract

    def equity_commission(self, shares: float) -> float:
        return abs(shares) * self.equity_commission_per_share

    @classmethod
    def from_config(cls, config) -> "CostModel":
        rules = getattr(config, "backtest_costs", None) or {}
        return cls(**{k: v for k, v in rules.items() if k in cls.__dataclass_fields__})
