"""
StrategyCycle — wires every engine together into the decision cycle from
the architecture doc §6. Two entrypoints:

  run_daily_cycle()    — full 8-step cycle (regime, ladder, put/call
                          proposals, risk gating, submission, persistence)
  run_intraday_check() — lightweight: crash-stress re-check + ex-div/
                          assignment monitoring only. Never opens new
                          positions. This is what makes "daily + intraday"
                          cadence safe: intraday passes can only defend,
                          never add exposure.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from .broker_adapter import BrokerAdapter, SingleLegOrder
from .call_engine import HybridCallEngine
from .config import StrategyConfig
from .delta import DeltaAggregator
from .exposure_curve import decline_from_reference, should_add_exposure, target_units_for_decline
from .ladder import AcquisitionLadder
from .put_engine import PutSpreadEngine
from .regime import RegimeEngine
from .risk import RiskManager
from .state import PortfolioState, load_state, save_state

logger = logging.getLogger("qqq_bot.cycle")


class StrategyCycle:
    def __init__(self, broker: BrokerAdapter, config: StrategyConfig):
        self._broker = broker
        self._config = config
        self._risk = RiskManager(config)
        self._regime = RegimeEngine(broker, config)
        self._ladder = AcquisitionLadder(broker, config)
        self._delta = DeltaAggregator(broker, config)
        self._puts = PutSpreadEngine(broker, config, self._risk)
        self._calls = HybridCallEngine(broker, config, self._risk)

    def _load(self) -> PortfolioState:
        return load_state(self._config.state_file_path)

    def _save(self, state: PortfolioState) -> None:
        save_state(state, self._config.state_file_path)

    def _risk_equity(self, actual_equity: float) -> float:
        """The figure the risk gates size against.

        Normally the live account equity. When rules.json sets an
        equity_basis_override the gates use that instead — which lets them
        authorise a loss larger than the account actually holds, so it is
        announced on every cycle rather than applied quietly.
        """
        override = self._config.equity_basis_override
        if not override:
            return actual_equity
        logger.warning(
            "RISK BASIS OVERRIDE: gates sizing against $%s, not the actual $%s. "
            "Caps are %.0f%% of what this account can absorb.",
            f"{override:,.2f}", f"{actual_equity:,.2f}",
            actual_equity / override * 100,
        )
        return float(override)

    def run_daily_cycle(self) -> PortfolioState:
        state = self._load()
        today = date.today()

        # 1. Pull market data
        price = self._broker.get_underlying_price()
        atr = self._broker.get_atr()
        snapshot = self._broker.get_current_positions()
        equity = self._risk_equity(snapshot.equity)

        # 2. Update regime
        state.current_regime = self._regime.current_regime()
        logger.info("Regime: %s | price=%.2f | equity=%.2f", state.current_regime, price, equity)

        # 3. Recompute total portfolio delta
        total_delta = self._delta.total_unit_delta(snapshot)
        logger.info("Total unit-equivalent delta: %.3f", total_delta)

        # 3b. Establish the core position (ALGORITHM.md 3, Engine A).
        # Nothing else in the strategy buys shares — put spreads only express
        # willingness to buy lower, and assignment is what converts them into
        # inventory. Without this the "permanent core" the whole design rests
        # on never actually exists.
        state = self._ensure_core_position(state, snapshot, price)

        # 4. Ladder check / recenter
        state = self._ladder.maybe_recenter(state, price, atr)
        zone = self._ladder.unused_zone_at_or_below(state, price)

        # 5. Put engine pass
        # V5 doc §17: reaching an unused zone does NOT automatically mean
        # adding exposure — only propose a new spread if current total delta
        # is below the §15 target-exposure curve for the current decline.
        decline = decline_from_reference(state.reference_price, price)
        target_exposure = target_units_for_decline(self._config, decline)
        logger.info(
            "Decline from reference: %.1f%% | target exposure: %.2f units | current: %.2f units",
            decline * 100, target_exposure, total_delta,
        )
        if zone is not None and should_add_exposure(
            self._config, state.reference_price, price, total_delta
        ):
            order = self._puts.propose_spread(state, equity)
            if order is not None:
                state = self._puts.submit(state, order)
                state = self._ladder.mark_zone_filled(state, zone)
        state = self._puts.manage_existing(state, today)

        # 6. Call engine pass
        # excess_units should reflect (current unit-equivalent holdings - core target);
        # this is a placeholder wiring until DeltaAggregator distinguishes
        # share-unit holdings from option-delta contributions cleanly.
        state.excess_units = max(0.0, total_delta - state.core_units)
        call_order = self._calls.propose_call(state, equity)
        if call_order is not None:
            state = self._calls.submit(state, call_order)
        state = self._calls.manage_existing(state, today)

        # 7. Portfolio crash-stress test (defensive re-check before persisting)
        stress = self._risk.check_crash_stress(
            price, state.open_put_spreads, state.open_calls, state.core_units, equity
        )
        if not stress.passed:
            logger.warning("Crash-stress cap breached at end of cycle: %s", stress.reason)

        # 8. Persist state
        self._save(state)
        return state

    def _ensure_core_position(self, state: PortfolioState, snapshot, price: float) -> PortfolioState:
        """Buy up to the core target if the account holds fewer shares than it.

        Deliberately buys only the shortfall against shares actually held at
        the broker, not against a number carried in state — otherwise a
        restart, a manual sale or a partial fill would silently double the
        core. Never sells: trimming the core is not this strategy's job.
        """
        unit = self._config.core_unit_shares
        target_shares = state.core_units * unit
        held = sum(
            p.qty
            for p in snapshot.positions
            if p.asset_class == "equity" and p.symbol == self._config.symbol
        )
        missing = target_shares - held
        if missing < 1:
            return state

        order = SingleLegOrder(
            contract=None,
            symbol=self._config.symbol,
            side="buy",
            qty=int(missing),
            order_type="market",
            limit_price=None,
            client_order_id=f"core-{uuid.uuid4().hex[:10]}",
        )
        logger.info(
            "Core position short by %d shares (held %.0f, target %.0f) — buying.",
            int(missing), held, target_shares,
        )
        result = self._broker.submit_single_leg(order)
        if result.success:
            logger.info("Core buy submitted: %s", result.order_id)
        else:
            logger.warning("Core buy not placed: %s", result.error or result.status)
        return state

    def run_intraday_check(self) -> PortfolioState:
        """Defensive-only pass: no new positions, ever."""
        state = self._load()
        today = date.today()
        price = self._broker.get_underlying_price()
        snapshot = self._broker.get_current_positions()
        equity = self._risk_equity(snapshot.equity)

        stress = self._risk.check_crash_stress(
            price, state.open_put_spreads, state.open_calls, state.core_units, equity
        )
        if not stress.passed:
            logger.warning("INTRADAY crash-stress cap breached: %s", stress.reason)
            # TODO(V5-PARAM): define the defensive action here (e.g. close
            # the riskiest open spread) — currently just logs.

        # Re-check ex-div safety on open calls (assignment risk monitoring)
        state = self._calls.manage_existing(state, today)

        self._save(state)
        return state
