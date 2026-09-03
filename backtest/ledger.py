"""
The fill ledger: cash, positions, realised P&L, and expiry settlement.

This is what the backtest was missing. The old adapter returned a successful
OrderResult and changed nothing, so equity sat at the starting balance no
matter what the strategy did — it produced decisions, not returns.

Conventions, stated once because sign errors here are silent and fatal:
  - `qty` is signed. Negative is short.
  - `price` is per share, or per contract-unit for options (so a $1.71 credit
    is 1.71, not 171).
  - Cash moves by  -qty * price * multiplier.  Buying (qty > 0) reduces cash;
    selling (qty < 0) increases it.
  - Options carry a 100x multiplier; shares 1x.
  - Realised P&L is booked only when a position moves toward zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

OPTION_MULTIPLIER = 100.0


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    kind: str  # "equity" | "option"

    @property
    def multiplier(self) -> float:
        return OPTION_MULTIPLIER if self.kind == "option" else 1.0


@dataclass
class Fill:
    day: date
    symbol: str
    qty: float
    price: float
    kind: str
    reason: str
    cash_delta: float
    realized: float


@dataclass
class Ledger:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl: float = 0.0
    fills: list[Fill] = field(default_factory=list)

    # ---- core ------------------------------------------------------------
    def fill(
        self, day: date, symbol: str, qty: float, price: float, kind: str, reason: str = ""
    ) -> Fill:
        """Apply a signed fill. Returns the Fill for the audit trail."""
        if qty == 0:
            raise ValueError("fill with qty=0")
        mult = OPTION_MULTIPLIER if kind == "option" else 1.0
        cash_delta = -qty * price * mult
        realized = 0.0

        pos = self.positions.get(symbol)
        if pos is None:
            self.positions[symbol] = Position(symbol, qty, price, kind)
        else:
            if (pos.qty > 0) == (qty > 0):
                # Adding to the position: weighted-average the entry price.
                total = pos.qty + qty
                pos.avg_price = (pos.avg_price * pos.qty + price * qty) / total
                pos.qty = total
            else:
                # Reducing or flipping: book P&L on the portion that closes.
                closing = min(abs(qty), abs(pos.qty))
                direction = 1.0 if pos.qty > 0 else -1.0
                realized = closing * (price - pos.avg_price) * mult * direction
                self.realized_pnl += realized
                remaining = pos.qty + qty
                if abs(remaining) < 1e-9:
                    del self.positions[symbol]
                elif (remaining > 0) == (pos.qty > 0):
                    pos.qty = remaining  # partial close, entry price unchanged
                else:
                    pos.qty = remaining  # flipped: the remainder opens at this price
                    pos.avg_price = price

        self.cash += cash_delta
        rec = Fill(day, symbol, qty, price, kind, reason, cash_delta, realized)
        self.fills.append(rec)
        return rec

    # ---- valuation -------------------------------------------------------
    def market_value(self, prices: dict[str, float]) -> float:
        """Mark every open position. A position with no price is held at its
        entry price rather than dropped — silently valuing it at zero would
        flatter or wreck the curve depending on its sign."""
        total = 0.0
        for pos in self.positions.values():
            px = prices.get(pos.symbol, pos.avg_price)
            total += pos.qty * px * pos.multiplier
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def shares_held(self, symbol: str) -> float:
        pos = self.positions.get(symbol)
        return pos.qty if pos and pos.kind == "equity" else 0.0

    def open_options(self) -> list[Position]:
        return [p for p in self.positions.values() if p.kind == "option"]

    # ---- expiry ----------------------------------------------------------
    def settle_expiries(self, day: date, spot: float, root: str) -> list[str]:
        """Settle every option expiring on `day`. Returns human-readable events.

        Physical settlement, not cash: an in-the-money short put delivers
        shares, which is precisely how this strategy accumulates inventory
        (ALGORITHM.md §3, Engine A). A backtest that cash-settled here would
        never test the accumulation thesis at all.
        """
        from .data import parse_occ

        events: list[str] = []
        for pos in list(self.open_options()):
            try:
                expiry, kind, strike = parse_occ(pos.symbol)
            except (ValueError, IndexError):
                continue
            if expiry != day:
                continue

            intrinsic = max(0.0, strike - spot) if kind == "put" else max(0.0, spot - strike)
            contracts = abs(pos.qty)
            short = pos.qty < 0

            # Close the option at its settlement value, booking the P&L.
            self.fill(day, pos.symbol, -pos.qty, intrinsic, "option",
                      reason="expiry_itm" if intrinsic > 0 else "expiry_worthless")

            if intrinsic <= 0:
                events.append(f"{pos.symbol} expired worthless ({'short' if short else 'long'})")
                continue

            # In the money: shares change hands at the strike. Modelled as a
            # share trade at spot, which combined with the intrinsic close
            # above is arithmetically identical to transacting at the strike.
            share_qty = contracts * 100.0
            if kind == "put":
                delta_shares = share_qty if short else -share_qty   # short assigned / long exercised
            else:
                delta_shares = -share_qty if short else share_qty   # short called away / long exercised
            self.fill(day, root, delta_shares, spot, "equity",
                      reason="assignment" if short else "exercise")
            events.append(
                f"{pos.symbol} {'ASSIGNED' if short else 'exercised'} at {strike:.2f} "
                f"(spot {spot:.2f}) -> {delta_shares:+.0f} shares"
            )
        return events
