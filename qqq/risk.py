"""
RiskManager — the four hard gates. Per the architecture doc's "NO OVERRIDE"
language: no code path in put_engine.py or call_engine.py may call
broker_adapter.submit_* without passing through these checks first.

These functions are pure (no I/O) so they're directly unit-testable against
worked examples.
"""

from __future__ import annotations

from dataclasses import dataclass

from .broker_adapter import OptionContract
from .config import StrategyConfig
from .state import CallPosition, PutSpreadPosition


@dataclass(frozen=True)
class RiskCheckResult:
    passed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, config: StrategyConfig):
        self._config = config

    def check_spread_max_loss(
        self,
        short_strike: float,
        long_strike: float,
        net_credit: float,
        contracts: int,
        equity: float,
    ) -> RiskCheckResult:
        width = abs(short_strike - long_strike)
        max_loss_per_contract = width * self._config.core_unit_shares - (
            net_credit * self._config.core_unit_shares
        )
        total_max_loss = max_loss_per_contract * contracts
        cap = equity * self._config.max_loss_per_spread_pct
        if total_max_loss > cap:
            return RiskCheckResult(
                False,
                f"Spread max loss ${total_max_loss:,.2f} exceeds cap "
                f"${cap:,.2f} ({self._config.max_loss_per_spread_pct:.1%} of equity).",
            )
        return RiskCheckResult(True)

    def check_aggregate_put_risk(
        self, open_spreads: list[PutSpreadPosition], equity: float
    ) -> RiskCheckResult:
        total = 0.0
        for s in open_spreads:
            if s.status != "OPEN":
                continue
            width = abs(s.short_strike - s.long_strike)
            max_loss = (
                width * self._config.core_unit_shares
                - s.net_credit * self._config.core_unit_shares
            ) * s.contracts
            total += max_loss

        cap = equity * self._config.max_aggregate_put_risk_pct
        if total > cap:
            return RiskCheckResult(
                False,
                f"Aggregate open put-spread max loss ${total:,.2f} exceeds cap "
                f"${cap:,.2f} ({self._config.max_aggregate_put_risk_pct:.1%} of equity).",
            )
        return RiskCheckResult(True)

    def check_crash_stress(
        self,
        underlying_price: float,
        open_spreads: list[PutSpreadPosition],
        open_calls: list[CallPosition],
        core_units: float,
        equity: float,
    ) -> RiskCheckResult:
        """
        Repricing under each configured shock is a simplification here:
        for short verticals we take the worst case as the full spread width
        (options go to intrinsic at expiry-equivalent under a shock), and
        for short calls we take unlimited-to-the-shock loss on the
        underlying move above the strike. A production version should price
        options properly (e.g. Black-Scholes re-price at the shocked spot
        with time decay held constant) rather than assume expiry intrinsic —
        this placeholder is deliberately conservative in the put-spread case
        and approximate in the call case.
        """
        unit_size = self._config.core_unit_shares
        worst_case_loss = 0.0
        binding_loss = 0.0
        binding = self._config.crash_stress_binding_shock

        for shock in self._config.crash_stress_shocks:
            shocked_price = underlying_price * (1 + shock)
            scenario_loss = 0.0

            # Core shares: mark-to-market loss (or gain, if shock is up)
            scenario_loss += -(shocked_price - underlying_price) * core_units * unit_size

            for s in open_spreads:
                if s.status != "OPEN":
                    continue
                width = abs(s.short_strike - s.long_strike)
                # Conservative: assume worst case is full width minus credit received
                max_loss = (width * unit_size - s.net_credit * unit_size) * s.contracts
                if shocked_price < s.short_strike:
                    scenario_loss += max_loss

            for c in open_calls:
                if c.status != "OPEN":
                    continue
                if shocked_price > c.short_strike:
                    intrinsic_loss = (shocked_price - c.short_strike) * unit_size * c.contracts
                    scenario_loss += max(0.0, intrinsic_loss - c.premium_received * unit_size * c.contracts)

            worst_case_loss = max(worst_case_loss, scenario_loss)
            if abs(shock - binding) < 1e-9:
                binding_loss = scenario_loss

        # §31 caps the loss under the -20% shock. The deeper shocks in §30 are
        # simulated so the tail is visible, not so the cap binds on them —
        # capping the worst of them is a materially stricter rule than the
        # specification, and strict enough here to stop the arm trading at all.
        cap = equity * self._config.max_crash_stress_pct
        if binding_loss > cap:
            return RiskCheckResult(
                False,
                f"Crash-stress loss ${binding_loss:,.2f} under a {binding:.0%} shock exceeds "
                f"cap ${cap:,.2f} ({self._config.max_crash_stress_pct:.1%} of equity). "
                f"Worst modelled shock would lose ${worst_case_loss:,.2f}.",
            )
        return RiskCheckResult(True)

    def check_call_coverage(
        self, open_calls: list[CallPosition], excess_units: float, proposing: int = 0
    ) -> RiskCheckResult:
        """No naked calls, ever: short call contracts must not exceed excess units.

        `proposing` is the contract count about to be written. It has to be
        included: counting only positions already open always permits one more
        contract than coverage allows, which is a naked call whenever excess
        inventory is below one unit.
        """
        open_call_contracts = sum(c.contracts for c in open_calls if c.status == "OPEN")
        open_call_contracts += max(0, proposing)
        if open_call_contracts > excess_units:
            return RiskCheckResult(
                False,
                f"Open short call contracts ({open_call_contracts}) would exceed "
                f"excess units ({excess_units}) — this would create a naked call. Refused.",
            )
        return RiskCheckResult(True)

    def check_all_for_new_put_spread(
        self,
        short_strike: float,
        long_strike: float,
        net_credit: float,
        contracts: int,
        equity: float,
        existing_open_spreads: list[PutSpreadPosition],
        underlying_price: float,
        core_units: float,
        open_calls: list[CallPosition],
    ) -> RiskCheckResult:
        """Convenience wrapper: run every gate relevant to opening a new put spread.

        The aggregate and crash-stress gates are evaluated against the book as
        it would stand AFTER this spread, not before it. Checking the prior
        book approves any trade whose predecessors were within limits and so
        permits exactly one spread more than the cap allows — which is what
        produced the "crash-stress cap breached at end of cycle" warning on
        essentially every run: the trade was approved and the resulting book
        was over the limit. §9 caps the portfolio, not the portfolio it used
        to be.
        """
        from dataclasses import replace as _replace

        prospective = list(existing_open_spreads) + [
            PutSpreadPosition(
                id="__prospective__", short_strike=short_strike, long_strike=long_strike,
                expiry="2099-01-01", contracts=contracts, net_credit=net_credit,
                opened_at="", status="OPEN",
            )
        ]
        checks = [
            self.check_spread_max_loss(short_strike, long_strike, net_credit, contracts, equity),
            self.check_aggregate_put_risk(prospective, equity),
            self.check_crash_stress(
                underlying_price, prospective, open_calls, core_units, equity
            ),
        ]
        for result in checks:
            if not result.passed:
                return result
        return RiskCheckResult(True)

    def check_all_for_new_call(
        self,
        open_calls: list[CallPosition],
        excess_units: float,
        underlying_price: float,
        open_spreads: list[PutSpreadPosition],
        core_units: float,
        equity: float,
        proposing: int = 1,
    ) -> RiskCheckResult:
        """Convenience wrapper: run every gate relevant to opening a new short call."""
        prospective_calls = list(open_calls)
        if proposing > 0:
            prospective_calls.append(
                CallPosition(
                    id="__prospective__", short_strike=underlying_price, expiry="2099-01-01",
                    contracts=proposing, premium_received=0.0, opened_at="", status="OPEN",
                )
            )
        checks = [
            self.check_call_coverage(open_calls, excess_units, proposing),
            self.check_crash_stress(
                underlying_price, open_spreads, prospective_calls, core_units, equity
            ),
        ]
        for result in checks:
            if not result.passed:
                return result
        return RiskCheckResult(True)
