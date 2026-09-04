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

from . import cash_sweep
from .broker_adapter import BrokerAdapter, SingleLegOrder
from .call_engine import HybridCallEngine
from .long_call_engine import LongCallEngine
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
        self._long_calls = LongCallEngine(broker, config)
        self._alloc_cache: float | None = None

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
            return actual_equity * self._allocation()
        logger.warning(
            "RISK BASIS OVERRIDE: gates sizing against $%s, not the actual $%s. "
            "Caps are %.0f%% of what this account can absorb.",
            f"{override:,.2f}", f"{actual_equity:,.2f}",
            actual_equity / override * 100,
        )
        return float(override) * self._allocation()

    def _allocation(self) -> float:
        """This arm's share of the account, per allocator.json.

        Every arm sizing against the full balance is how a two-arm book
        commits 200% of the capital it has. Cached per cycle so a mid-cycle
        edit to allocator.json cannot change sizing between two gates in the
        same run.
        """
        if self._alloc_cache is None:
            override = self._config.allocation_override
            if override is not None:
                self._alloc_cache = float(override)
                return self._alloc_cache
            from core.kill_switch import allocation_pct
            self._alloc_cache = allocation_pct(self._config.arm)
            if self._alloc_cache <= 0:
                logger.warning(
                    "Arm '%s' has no usable allocation in allocator.json — sizing against $0, "
                    "so nothing will open. Add an allocations.%s.pct entry.",
                    self._config.arm, self._config.arm,
                )
        return self._alloc_cache

    def run_daily_cycle(self) -> PortfolioState:
        state = self._load()
        today = self._broker.today()

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

        # 3b. Size the core. Sized off ACTUAL equity, never the risk-basis
        # override: the core is a real purchase, and an inflated basis would
        # have it buying shares the account cannot pay for.
        state.core_units = self._core_units_target(snapshot.equity, price)

        # 3c. Establish the core position (ALGORITHM.md 3, Engine A).
        # Nothing else in the strategy buys shares — put spreads only express
        # willingness to buy lower, and assignment is what converts them into
        # inventory. Without this the "permanent core" the whole design rests
        # on never actually exists.
        state = self._ensure_core_position(state, snapshot, price)

        # 4. Ladder check / recenter
        state = self._ladder.maybe_recenter(state, price, atr, state.current_regime)
        zone = self._ladder.unused_zone_at_or_below(state, price, today)

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
        wants_more = should_add_exposure(
            self._config, state.reference_price, price, total_delta
        )
        if self._should_open_put(state, zone, wants_more, today):
            # Accumulate inventory by BUYING SHARES, not by waiting for
            # assignment. A defined-risk spread assigns the short leg and
            # exercises the long leg for a net of zero shares, so §7's
            # instrument can never deliver the inventory §3 calls for.
            # Buying outright keeps the spreads defined-risk and still gets
            # the accumulation the exposure curve is built around.
            state = self._accumulate_shares(state, snapshot, price, target_exposure, total_delta)
            order = self._puts.propose_spread(state, equity, state.current_regime)
            if order is not None:
                state = self._puts.submit(state, order)
                state.last_put_entry = today.isoformat()
                if zone is not None:
                    state = self._ladder.mark_zone_filled(state, zone, today)
        state = self._puts.manage_existing(state, today)

        # 6. Call engine pass
        # Excess inventory is SHARES beyond the core, never total delta.
        # Engine C writes calls against it, and a call is only covered by
        # stock — put-spread delta cannot deliver shares if the call is
        # exercised. Deriving this from total_delta let +0.15 of spread delta
        # read as coverage and permitted a genuinely naked call, which §8
        # forbids outright.
        held_shares = sum(
            p.qty for p in snapshot.positions
            if p.asset_class == "equity" and p.symbol == self._config.symbol
        )
        # §2/§19 hold the core uncapped, so callable inventory is normally
        # only what was accumulated beyond it. With cover_core the whole
        # holding is callable: more premium, at the cost of the core's upside
        # in a rally — which §28 says to measure as premium MINUS upside
        # sacrificed, not premium alone.
        held_units = held_shares / self._config.core_unit_shares
        state.excess_units = (
            held_units if self._config.call_cover_core
            else max(0.0, held_units - state.core_units)
        )
        call_order = self._calls.propose_call(state, equity, state.current_regime)
        if call_order is not None:
            state = self._calls.submit(state, call_order)
        state = self._calls.manage_existing(state, today)

        # Long-call sleeve. Bought convexity, deliberately separate from every
        # selling engine above: it PAYS the variance risk premium the rest of
        # the strategy earns, so it stays off unless explicitly enabled and is
        # bounded by its own annual budget rather than by the put caps.
        state = self._long_calls.manage_existing(state, today)
        lc_order = self._long_calls.propose_call(state, equity, today)
        if lc_order is not None:
            state = self._long_calls.submit(state, lc_order, today)

        # 7. Portfolio crash-stress test (defensive re-check before persisting)
        stress = self._risk.check_crash_stress(
            price, state.open_put_spreads, state.open_calls, state.core_units, equity
        )
        if not stress.passed:
            logger.warning("Crash-stress cap breached at end of cycle: %s", stress.reason)

        # 8. Sweep whatever cash is genuinely idle. Runs LAST, so it only
        # ever moves what this cycle's trading decisions did not claim.
        try:
            cash_sweep.execute(self._broker, self._config, self._broker.get_current_positions())
        except Exception as e:  # never let a yield optimisation break the cycle
            logger.warning("Cash sweep skipped: %s: %s", type(e).__name__, e)

        # 9. Persist state
        self._save(state)
        return state

    def _core_units_target(self, equity: float, price: float) -> float:
        """How many units the core should hold.

        A fixed unit count means the exposure it represents drifts with price:
        100 QQQ was 16% of a $250k account in 2022 and is 29% at today's $717.
        A percentage target holds the RISK constant instead, which also buys
        more shares when QQQ is cheap and fewer when it is expensive — the
        behaviour the exposure curve was reaching for.
        """
        pct = self._config.core_target_pct
        if pct <= 0 or price <= 0:
            return self._config.core_units_target
        return (equity * pct) / (price * self._config.core_unit_shares)

    def _should_open_put(
        self, state: PortfolioState, zone: float | None, wants_more: bool, today: date
    ) -> bool:
        """Decide whether to attempt a put entry this cycle.

        "ladder" is V5 §8: price must touch an unused acquisition zone AND
        total delta must sit below the target curve. That gating is why trade
        count held at 79-84 across every DTE window tested — the ladder, not
        expiry, was always the limiter on premium collected.

        "scheduled" writes on a cadence instead, while the book is below its
        target position count, which is how a premium program actually
        harvests the variance risk premium: continuously, rather than only
        when price happens to touch a rung. The risk gates still bound every
        individual trade, so this changes frequency, not size.
        """
        mode = self._config.put_entry_mode
        ladder_ok = zone is not None and wants_more
        if mode == "ladder":
            return ladder_ok

        # The position count and cadence gate the SCHEDULED trigger only.
        # Applying them to the whole decision would let a recent scheduled
        # entry suppress a legitimate ladder touch in "both" mode — the two
        # triggers are meant to be independent.
        scheduled_ok = (
            sum(1 for s in state.open_put_spreads if s.status == "OPEN")
            < self._config.put_target_open_positions
        )
        if scheduled_ok and state.last_put_entry:
            try:
                gap = (today - date.fromisoformat(state.last_put_entry)).days
            except ValueError:
                gap = self._config.put_min_days_between_entries
            scheduled_ok = gap >= self._config.put_min_days_between_entries

        return scheduled_ok if mode == "scheduled" else (ladder_ok or scheduled_ok)

    def _accumulate_shares(
        self, state: PortfolioState, snapshot, price: float,
        target_units: float, held_units: float,
    ) -> PortfolioState:
        """Buy toward the target exposure curve, bounded three ways.

        By the shortfall (never overshoot the curve), by a per-fire cap (§5's
        gradual intent — a zone touch should add a slice, not the whole gap),
        and by settled cash. The crash-stress gate is then re-run against the
        larger share position and the purchase abandoned if it would breach:
        shares carry uncapped downside, so this is the one gate that sees the
        true risk of accumulating.
        """
        from .regime_policy import RegimePolicy

        # §4/§14: the regime scales accumulation aggressiveness. This is the
        # single most consequential place the filter applies — buying hardest
        # into a confirmed downtrend is exactly the failure mode §14 warns
        # about, and the one this strategy has no backtest evidence for.
        policy = RegimePolicy.for_regime(self._config, state.current_regime)
        per_fire = int(self._config.ladder_accumulate_shares_per_zone * policy.accumulate)
        if per_fire <= 0:
            return state

        unit = self._config.core_unit_shares
        shortfall = int((target_units - held_units) * unit)
        if shortfall < 1:
            return state

        held_shares = sum(
            p.qty for p in snapshot.positions
            if p.asset_class == "equity" and p.symbol == self._config.symbol
        )
        affordable = int(snapshot.cash // price) if price > 0 else 0
        qty = min(shortfall, per_fire, affordable)
        if qty < 1:
            logger.info(
                "Accumulation skipped: want %d shares, cash affords %d.", min(shortfall, per_fire), affordable
            )
            return state

        stress = self._risk.check_crash_stress(
            price, state.open_put_spreads, state.open_calls,
            (held_shares + qty) / unit, self._risk_equity(snapshot.equity),
        )
        if not stress.passed:
            logger.info("Accumulation refused by crash-stress: %s", stress.reason)
            return state

        order = SingleLegOrder(
            contract=None, symbol=self._config.symbol, side="buy", qty=qty,
            order_type="market", limit_price=None,
            client_order_id=f"accum-{uuid.uuid4().hex[:10]}",
        )
        result = self._broker.submit_single_leg(order)
        if result.success:
            logger.info(
                "Accumulated %d shares at ~%.2f (held %.0f -> %.0f, target %.2f units)",
                qty, price, held_shares, held_shares + qty, target_units,
            )
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
        today = self._broker.today()
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
