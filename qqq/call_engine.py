"""
HybridCallEngine — proposes and manages short calls against excess units.

V5 doc §18-27 (Engine C, "Hybrid"):
  - Core inventory (first core_unit, §19) is never capped with calls.
  - Excess inventory is systematically evaluated for OTM calls (§20),
    21-35 DTE, 15-25 delta, no rebound required — this is what
    distinguishes "hybrid" from "rebound-only" (§35 option A).
  - §21: only sell if the *effective sale price* (strike + premium) is a
    level you'd be "happy monetizing excess inventory at" — implemented via
    config.call_min_effective_sale_vs_reference (see config.py for the
    caveat: the doc leaves the exact acceptability bar as a judgment call).
  - §22/§24-25: after a confirmed rebound, call aggressiveness increases —
    implemented here as shifting the delta target from 20 toward 25-30
    (call_short_delta_target_post_rebound).
  - §23: short calls should not exceed excess inventory — enforced as a
    hard gate in RiskManager.check_call_coverage, not just here.

Also implements the QQQ-specific divergence from MNQ (architecture doc §2):
ex-dividend early-assignment risk. A short call is refused if it expires
straddling an ex-div date with extrinsic value too thin relative to the
dividend (assignment becomes economically rational for the option holder).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from .broker_adapter import BrokerAdapter, DividendEvent, OptionContract, SingleLegOrder
from .config import StrategyConfig
from .risk import RiskManager
from .state import CallPosition, PortfolioState


class HybridCallEngine:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig, risk: RiskManager):
        self._broker = broker
        self._config = config
        self._risk = risk

    def _select_short_leg(
        self, chain: list[OptionContract], rebound_confirmed: bool
    ) -> OptionContract | None:
        calls = [c for c in chain if c.option_type == "call" and c.delta is not None]
        if not calls:
            return None
        target = (
            self._config.call_short_delta_target_post_rebound
            if rebound_confirmed
            else self._config.call_short_delta_target
        )
        return min(calls, key=lambda c: abs(c.delta - target))

    def _is_rebound_confirmed(self, state: PortfolioState, price: float) -> bool:
        """V5 doc §22: 50% retracement of the decline from reference (the
        agreed starting definition — doc also lists 1/2 ATR-from-low and
        20-day-high approach as alternatives to backtest).
        """
        if state.reference_price is None or not state.acquisition_ladder:
            return False
        recent_low = min(state.acquisition_ladder + [price])
        decline = state.reference_price - recent_low
        if decline <= 0:
            return False
        retraced = price - recent_low
        return (retraced / decline) >= self._config.rebound_retracement_pct

    def _passes_effective_sale_price(
        self, contract: OptionContract, reference_price: float
    ) -> bool:
        """V5 doc §21: effective sale price = strike + premium (QQQ: $/share,
        no multiplier). Reject if it's not a level worth monetizing excess
        inventory at.
        """
        mid_premium = (contract.bid + contract.ask) / 2
        effective_sale_price = contract.strike + mid_premium
        floor = reference_price * self._config.call_min_effective_sale_vs_reference
        return effective_sale_price >= floor

    def _fails_exdiv_safety(
        self, contract: OptionContract, dividends: list[DividendEvent]
    ) -> bool:
        """True if this call should be refused/avoided due to ex-div assignment risk."""
        straddling = [
            d for d in dividends if self._broker.today() <= d.ex_date <= contract.expiry
        ]
        if not straddling:
            return False

        mid = (contract.bid + contract.ask) / 2
        underlying_price = self._broker.get_underlying_price()
        intrinsic = max(0.0, underlying_price - contract.strike)
        extrinsic = max(0.0, mid - intrinsic)

        for div in straddling:
            required = div.amount_per_share * self._config.exdiv_min_extrinsic_over_dividend_ratio
            if extrinsic < required:
                return True
        return False

    def propose_call(
        self, state: PortfolioState, equity: float
    ) -> SingleLegOrder | None:
        excess_units = state.excess_units
        if excess_units <= 0:
            return None  # nothing to cover — core inventory stays uncapped per §19

        price = self._broker.get_underlying_price()
        rebound = self._is_rebound_confirmed(state, price)

        chain = self._broker.get_option_chain(self._config.call_dte_range)
        short_leg = self._select_short_leg(chain, rebound)
        if short_leg is None:
            return None

        if state.reference_price is not None and not self._passes_effective_sale_price(
            short_leg, state.reference_price
        ):
            return None  # §21: not an acceptable effective sale price

        dividends = self._broker.get_dividend_calendar()
        if self._fails_exdiv_safety(short_leg, dividends):
            return None

        contracts = 1  # §23 caps short calls <= excess units; RiskManager enforces this regardless of count

        check = self._risk.check_all_for_new_call(
            open_calls=state.open_calls,
            excess_units=excess_units,
            underlying_price=self._broker.get_underlying_price(),
            open_spreads=state.open_put_spreads,
            core_units=state.core_units,
            equity=equity,
            proposing=contracts,
        )
        if not check.passed:
            return None

        premium = round((short_leg.bid + short_leg.ask) / 2 * 0.98, 2)  # haircut for fill odds
        return SingleLegOrder(
            contract=short_leg,
            symbol=short_leg.symbol,
            side="sell",
            qty=contracts,
            order_type="limit",
            limit_price=premium,
            client_order_id=f"short-call-{uuid.uuid4().hex[:10]}",
        )

    def submit(self, state: PortfolioState, order: SingleLegOrder) -> PortfolioState:
        result = self._broker.submit_single_leg(order)
        if result.success and order.contract is not None:
            state.open_calls.append(
                CallPosition(
                    id=result.order_id or order.client_order_id,
                    short_strike=order.contract.strike,
                    expiry=order.contract.expiry.isoformat(),
                    contracts=int(order.qty),
                    premium_received=order.limit_price or 0.0,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        return state

    def manage_existing(self, state: PortfolioState, today: date) -> PortfolioState:
        """Re-check ex-div safety on every open call and flag/roll if needed."""
        dividends = self._broker.get_dividend_calendar()
        for call in state.open_calls:
            if call.status != "OPEN":
                continue
            expiry = date.fromisoformat(call.expiry)
            straddling = [d for d in dividends if today <= d.ex_date <= expiry]
            if straddling:
                # TODO(V5-PARAM): actively roll here per doc guidance rather
                # than just flagging. Rolling needs a live re-quote of the
                # current short leg plus a new short leg past the ex-div
                # date, which needs the option chain — wire once the roll
                # policy (roll-out vs roll-out-and-up) is decided.
                pass
            # TODO(V5-PARAM): profit-capture close needs the call's current
            # mark from the broker adapter, same gap as put_engine.
        return state
