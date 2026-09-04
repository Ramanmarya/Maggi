"""
PutSpreadEngine — proposes and manages put credit spreads.

V5 doc §8-13:
  - DTE 21-35, short put 15-25 delta (default 20), protective put ~5 delta
    "OR a strike necessary to satisfy the maximum-loss rule" — implemented
    below as: try the 5-delta protective leg first; RiskManager (the single
    source of truth for the max-loss gate) rejects the trade if it still
    violates the cap rather than this engine silently widening the spread,
    per the doc's "NO OVERRIDE" language in §10.
  - §12: reject trades with a poor risk/reward ratio (max_loss/max_profit)
    even if premium looks attractive on its own.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from .broker_adapter import BrokerAdapter, OptionContract, VerticalSpreadOrder
from .config import StrategyConfig
from .risk import RiskManager
from .state import PortfolioState, PutSpreadPosition


class PutSpreadEngine:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig, risk: RiskManager):
        self._broker = broker
        self._config = config
        self._risk = risk

    def _select_short_leg(
        self, chain: list[OptionContract], regime: str = "BULL"
    ) -> OptionContract | None:
        """§4: the regime changes put selection. A lower delta target in a
        downtrend puts the short strike further out of the money."""
        from .regime_policy import RegimePolicy

        puts = [c for c in chain if c.option_type == "put" and c.delta is not None]
        if not puts:
            return None
        policy = RegimePolicy.for_regime(self._config, regime)
        target = -self._config.put_spread_short_delta_target * policy.put_delta
        return min(puts, key=lambda c: abs(c.delta - target))

    def _spread_economics(
        self, short_leg: OptionContract, long_leg: OptionContract
    ) -> tuple[float, float, float] | None:
        """Return (net_credit, max_loss, risk_reward) or None if there is no credit."""
        credit = (short_leg.bid + short_leg.ask) / 2 - (long_leg.bid + long_leg.ask) / 2
        if credit <= 0:
            return None
        unit = self._config.core_unit_shares
        width = short_leg.strike - long_leg.strike
        max_loss = width * unit - credit * unit
        max_profit = credit * unit
        if max_profit <= 0:
            return None
        return credit, max_loss, max_loss / max_profit

    def _within_caps(
        self, short_leg: OptionContract, long_leg: OptionContract, equity: float,
        state: PortfolioState | None = None, underlying_price: float | None = None,
        contracts: int = 1,
    ) -> bool:
        """Would this exact spread clear every gate?

        When `state` is supplied this runs the full §9 battery, including the
        portfolio crash-stress test — not just the per-spread cap. Leg
        selection has to know about the portfolio budget, or it picks the
        widest leg that fits the per-spread cap, the portfolio gate rejects
        it, and the engine gives up having never tried a narrower one. §10 is
        explicit about the order: "narrow spread, change strikes, obtain
        better credit, OR reject trade" — rejecting is the last resort.
        """
        econ = self._spread_economics(short_leg, long_leg)
        if econ is None:
            return False
        credit, max_loss, risk_reward = econ
        if max_loss > equity * self._config.max_loss_per_spread_pct:
            return False
        rr_cap = self._config.put_spread_max_risk_reward_ratio
        if rr_cap is not None and risk_reward > rr_cap:
            return False
        if state is None or underlying_price is None:
            return True
        return self._risk.check_all_for_new_put_spread(
            short_strike=short_leg.strike, long_strike=long_leg.strike,
            net_credit=credit, contracts=contracts, equity=equity,
            existing_open_spreads=state.open_put_spreads,
            underlying_price=underlying_price, core_units=state.core_units,
            open_calls=state.open_calls,
        ).passed

    def _select_long_leg(
        self, chain: list[OptionContract], short_leg: OptionContract, equity: float | None = None,
        state: PortfolioState | None = None, underlying_price: float | None = None,
        contracts: int = 1,
    ) -> OptionContract | None:
        """§7: protective put at ~5 delta, "or a strike necessary to satisfy the
        maximum-loss rule".

        Only the first half used to be implemented, which made the engine
        unusable on QQQ: at ~$717 the 5-delta strike sits ~56 points below the
        20-delta short, so every proposal was a $5,600-wide spread whose max
        loss was several times the per-spread cap, and the risk manager
        refused all of them. Selecting purely on delta ignores that spread
        width — and therefore max loss — scales with the underlying's price.

        So: take the 5-delta leg when it fits the caps, otherwise take the
        WIDEST leg that does. Widest, not narrowest, because within a fixed
        max-loss budget a wider spread collects more premium; narrowing past
        that just gives up credit for risk the cap already bounds.
        """
        candidates = [
            c
            for c in chain
            if c.option_type == "put"
            and c.expiry == short_leg.expiry
            and c.delta is not None
            and c.strike < short_leg.strike
        ]
        if not candidates:
            return None

        target = -self._config.put_spread_protective_delta_target
        by_delta = min(candidates, key=lambda c: abs(c.delta - target))
        if equity is None or self._within_caps(
            short_leg, by_delta, equity, state, underlying_price, contracts
        ):
            return by_delta

        fitting = [
            c for c in candidates
            if self._within_caps(short_leg, c, equity, state, underlying_price, contracts)
        ]
        if not fitting:
            return None

        if self._config.put_spread_leg_selection == "best_risk_reward":
            # Risk/reward is U-shaped in width, not monotonic: very narrow
            # spreads collect too little against their width, very wide ones
            # add width faster than they add credit. The optimum sits in the
            # middle, so "widest that fits" is not the same as "best odds".
            def _rr(c):
                econ = self._spread_economics(short_leg, c)
                return econ[2] if econ else float("inf")

            return min(fitting, key=_rr)

        return min(fitting, key=lambda c: c.strike)  # lowest strike = widest spread

    def propose_spread(
        self, state: PortfolioState, equity: float, regime: str = "BULL"
    ) -> VerticalSpreadOrder | None:
        chain = self._broker.get_option_chain(self._config.put_spread_dte_range)
        short_leg = self._select_short_leg(chain, regime)
        if short_leg is None:
            return None
        price = self._broker.get_underlying_price()
        contracts = max(1, self._config.put_spread_contracts)
        cash_secured = self._config.put_structure == "cash_secured"
        if self._config.put_protective_leg and not cash_secured:
            long_leg = self._select_long_leg(chain, short_leg, equity, state, price, contracts)
            if long_leg is None:
                return None
        else:
            # cash_secured: no long leg, full strike held in cash.
            # naked (protective_leg False): no long leg and no collateral —
            # §39 Q1 testing only, refused by the live adapter.
            long_leg = None

        long_mid = (long_leg.bid + long_leg.ask) / 2 if long_leg else 0.0
        net_credit = (short_leg.bid + short_leg.ask) / 2 - long_mid
        if net_credit <= 0:
            return None  # never pay to open a "credit" spread

        # §12: risk/reward quality filter (max_loss / max_profit).
        unit = self._config.core_unit_shares
        if long_leg is not None:
            width = short_leg.strike - long_leg.strike
            max_loss = width * unit - net_credit * unit
        else:
            max_loss = short_leg.strike * unit - net_credit * unit  # strike to zero
        max_profit = net_credit * unit
        if max_profit <= 0:
            return None
        risk_reward = max_loss / max_profit
        # §12's risk/reward filter is a SPREAD quality metric: max loss comes
        # from §10's (short - long) x multiplier, so the ratio is meaningful
        # only when there is a long leg. An unspread put scores 140:1 on
        # strike-to-zero and still 25:1 against the -20% shock, so applying it
        # refuses every cash-secured put ever proposed. Cash-secured positions
        # are governed by collateral and §31's portfolio shock test instead —
        # which is exactly how the CBOE PutWrite index is controlled.
        if (
            not cash_secured
            and self._config.put_spread_max_risk_reward_ratio is not None
            and risk_reward > self._config.put_spread_max_risk_reward_ratio
        ):
            return None

        # §11 left sizing open. The count is configurable; RiskManager enforces
        # the per-spread and aggregate caps regardless of it, so a size that is
        # too large is refused rather than silently taken.

        if cash_secured:
            cash = getattr(self._broker.get_current_positions(), "cash", 0.0)
            collateral = self._risk.check_cash_secured(short_leg.strike, contracts, cash)
            if not collateral.passed:
                return None
            # Only the portfolio shock test applies; §10's width cap does not.
            stress = self._risk.check_crash_stress(
                price,
                state.open_put_spreads + [
                    PutSpreadPosition(
                        id="__prospective__", short_strike=short_leg.strike, long_strike=0.0,
                        expiry=short_leg.expiry.isoformat(), contracts=contracts,
                        net_credit=net_credit, opened_at="",
                    )
                ],
                state.open_calls, state.core_units, equity,
            )
            if not stress.passed:
                return None
            return VerticalSpreadOrder(
                underlying=self._config.symbol, short_leg=short_leg, long_leg=None,
                contracts=contracts, limit_net_credit=round(net_credit * 0.95, 2),
                client_order_id=f"csp-{uuid.uuid4().hex[:10]}",
            )

        check = self._risk.check_all_for_new_put_spread(
            short_strike=short_leg.strike,
            long_strike=long_leg.strike if long_leg else 0.0,
            net_credit=net_credit,
            contracts=contracts,
            equity=equity,
            existing_open_spreads=state.open_put_spreads,
            underlying_price=price,
            core_units=state.core_units,
            open_calls=state.open_calls,
        )
        if not check.passed:
            return None

        return VerticalSpreadOrder(
            underlying=self._config.symbol,
            short_leg=short_leg,
            long_leg=long_leg,
            contracts=contracts,
            limit_net_credit=round(net_credit * 0.95, 2),  # small haircut off mid for fill odds
            client_order_id=f"put-spread-{uuid.uuid4().hex[:10]}",
        )

    def submit(self, state: PortfolioState, order: VerticalSpreadOrder) -> PortfolioState:
        result = self._broker.submit_vertical_spread(order)
        if result.success:
            state.open_put_spreads.append(
                PutSpreadPosition(
                    id=result.order_id or order.client_order_id,
                    short_strike=order.short_leg.strike,
                    long_strike=order.long_leg.strike,
                    short_symbol=order.short_leg.symbol,
                    long_symbol=order.long_leg.symbol,
                    expiry=order.short_leg.expiry.isoformat(),
                    contracts=order.contracts,
                    net_credit=order.limit_net_credit or 0.0,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        return state

    def manage_existing(self, state: PortfolioState, today: date) -> PortfolioState:
        """Close at profit-capture target or inside the force-close DTE window."""
        for spread in state.open_put_spreads:
            if spread.status != "OPEN":
                continue
            expiry = date.fromisoformat(spread.expiry)
            dte = (expiry - today).days
            reason = None
            if dte <= self._config.put_spread_close_dte:
                reason = "dte"
            elif self._captured(spread) >= self._config.put_spread_profit_capture_pct:
                # §7: take the win rather than hold through the highest-gamma
                # stretch of the contract's life for the last few percent.
                reason = "profit_capture"

            if reason is not None:
                result = self._broker.close_position(spread.id, limit_pct=None)
                if result.success:
                    spread.status = "CLOSED"
                    spread.close_price = result.filled_avg_price
                    spread.closed_at = datetime.now(timezone.utc).isoformat()
        return state

    def _captured(self, spread: PutSpreadPosition) -> float:
        """Fraction of maximum profit currently realised, 0.0 if unknown.

        Max profit on a credit spread is the credit received, so capture is
        (credit - cost to close) / credit. Returns 0.0 when either leg has no
        mark, which degrades to the time-based exit rather than guessing.
        """
        credit = spread.net_credit
        if credit <= 0:
            return 0.0
        short_mark = self._broker.option_mark(spread.short_symbol) if spread.short_symbol else None
        long_mark = self._broker.option_mark(spread.long_symbol) if spread.long_symbol else None
        if short_mark is None or long_mark is None:
            return 0.0
        cost_to_close = short_mark - long_mark
        return (credit - cost_to_close) / credit
