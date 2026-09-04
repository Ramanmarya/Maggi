"""Long calls: bought convexity, 2-3 weeks out.

This engine BUYS. Everything else in the strategy sells premium, which is
why it earns the variance risk premium; buying calls PAYS that same premium.
The position is therefore negative expected value in isolation, and is only
worth carrying as a deliberate directional bet on continued upside. Two
consequences shape the design:

  1. Spend is capped hard. A debit position bleeds on a schedule -- every
     expiry that finishes out of the money is a total loss on that contract.
     The annual budget gate is the difference between a convexity sleeve and
     a slow drain.

  2. Positions are held in their own list. RiskManager reads open_calls as
     SHORT calls: unlimited loss above the strike, share coverage required.
     A long call is bounded at the premium and needs no coverage, so filing
     one there would invert both tests.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date

from .broker_adapter import BrokerAdapter, OptionContract, SingleLegOrder
from .config import StrategyConfig
from .state import LongCallPosition, PortfolioState

logger = logging.getLogger(__name__)


class LongCallEngine:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig):
        self._broker = broker
        self._config = config

    # ---------------------------------------------------------------- budget

    def premium_spent_this_year(self, state: PortfolioState, today: date) -> float:
        """Premium committed to long calls in the trailing 365 days.

        Counts opens, not outcomes: the budget limits what can be RISKED,
        so a winner does not refund headroom to buy more.
        """
        total = 0.0
        for c in state.open_long_calls:
            try:
                opened = date.fromisoformat(c.opened_at)
            except (ValueError, TypeError):
                continue
            if (today - opened).days <= 365:
                total += c.premium_paid * self._config.core_unit_shares * c.contracts
        return total

    def _within_budget(self, state: PortfolioState, today: date,
                       cost: float, equity: float) -> bool:
        cap = equity * self._config.long_call_annual_budget_pct
        if cap <= 0:
            return False
        spent = self.premium_spent_this_year(state, today)
        if spent + cost > cap:
            logger.info(
                "Long call refused: $%.0f would take trailing-year spend to $%.0f, over the $%.0f cap",
                cost, spent + cost, cap,
            )
            return False
        return True

    # ---------------------------------------------------------------- select

    def _select_leg(self, chain):
        """Nearest to the target delta among calls in the DTE window."""
        target = self._config.long_call_delta_target
        best, best_gap = None, None
        for leg in chain:
            if getattr(leg, "option_type", "call") != "call":
                continue
            d = getattr(leg, "delta", None)
            if d is None:
                continue
            gap = abs(abs(d) - target)
            if best_gap is None or gap < best_gap:
                best, best_gap = leg, gap
        return best

    # ---------------------------------------------------------------- propose

    def propose_call(self, state: PortfolioState, equity: float,
                     today: date) -> SingleLegOrder | None:
        if not self._config.long_call_enabled:
            return None
        open_now = sum(1 for c in state.open_long_calls if c.status == "OPEN")
        if open_now >= self._config.long_call_max_open:
            return None

        # The chain carries puts and calls; _select_leg filters.
        chain = self._broker.get_option_chain(self._config.long_call_dte_range)
        leg = self._select_leg(chain)
        if leg is None:
            return None

        ask = getattr(leg, "ask", None)
        bid = getattr(leg, "bid", None)
        if not ask or not bid or ask <= 0:
            return None
        mid = (bid + ask) / 2
        contracts = max(1, self._config.long_call_contracts)
        cost = mid * self._config.core_unit_shares * contracts
        if not self._within_budget(state, today, cost, equity):
            return None

        return SingleLegOrder(
            contract=leg, symbol=leg.symbol, side="buy", qty=contracts,
            order_type="limit",
            # Pay up to the ask; a long call that never fills is not a
            # cheaper position, it is a missing one.
            limit_price=round(min(ask, mid * 1.05), 2),
            client_order_id=f"lc-{uuid.uuid4().hex[:10]}",
        )

    # ----------------------------------------------------------------- submit

    def submit(self, state: PortfolioState, order: SingleLegOrder,
               today: date) -> PortfolioState:
        result = self._broker.submit_single_leg(order)
        if not result.success:
            logger.warning("Long call rejected: %s", result.error)
            return state
        fill = result.filled_price if result.filled_price else order.limit_price
        state.open_long_calls.append(LongCallPosition(
            id=order.client_order_id, symbol=order.symbol,
            strike=float(order.contract.strike),
            expiry=order.contract.expiry.isoformat(), contracts=int(order.qty),
            premium_paid=float(fill), opened_at=today.isoformat(),
        ))
        logger.info("Long call opened: %s strike %.0f @ $%.2f",
                    order.symbol, order.contract.strike, fill)
        return state

    # ----------------------------------------------------------------- manage

    def manage_existing(self, state: PortfolioState, today: date) -> PortfolioState:
        """Take profit at the multiple, and cut before expiry.

        An expiring long call is worth its intrinsic value and nothing else,
        so holding one to the last day converts any remaining time value into
        zero. Closing at close_dte keeps whatever the market still pays.
        """
        for c in state.open_long_calls:
            if c.status != "OPEN":
                continue
            mark = self._broker.option_mark(c.symbol)
            try:
                dte = (date.fromisoformat(c.expiry) - today).days
            except (ValueError, TypeError):
                dte = 999
            take = mark is not None and c.premium_paid > 0 and (
                mark >= c.premium_paid * self._config.long_call_profit_multiple
            )
            expiring = dte <= self._config.long_call_close_dte
            if not (take or expiring):
                continue
            if mark is None or mark <= 0.01:
                c.status, c.close_price, c.closed_at = "EXPIRED", 0.0, today.isoformat()
                logger.info("Long call %s expired worthless (-$%.0f)", c.id,
                            c.premium_paid * self._config.core_unit_shares * c.contracts)
                continue
            # A close MUST carry a contract. With contract=None the adapter
            # takes its equity branch and prices an OCC option symbol as a
            # stock -- silently, and at a completely unrelated price. The
            # mark supplies both sides; the cost model applies the spread.
            leg = OptionContract(
                symbol=c.symbol, underlying=self._config.symbol,
                expiry=date.fromisoformat(c.expiry), strike=c.strike,
                option_type="call", bid=mark, ask=mark,
                delta=None, implied_vol=None, open_interest=None,
            )
            order = SingleLegOrder(
                contract=leg, symbol=c.symbol, side="sell",
                qty=c.contracts, order_type="limit", limit_price=round(mark * 0.95, 2),
                client_order_id=f"lcx-{uuid.uuid4().hex[:8]}",
            )
            res = self._broker.submit_single_leg(order)
            if res.success:
                c.status = "CLOSED"
                c.close_price = float(res.filled_price or mark)
                c.closed_at = today.isoformat()
                pl = (c.close_price - c.premium_paid) * self._config.core_unit_shares * c.contracts
                logger.info("Long call %s closed at $%.2f (%s$%.0f)", c.id,
                            c.close_price, "+" if pl >= 0 else "-", abs(pl))
        return state
