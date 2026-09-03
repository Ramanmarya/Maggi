"""
Preflight fire test — run the entire execution path offline, against a stub
broker, before letting the arm near the network or a real phase promotion.

This exists because a phase gate that short-circuits early leaves the
order-placing code untested until the day it first places an order. Preflight
drives the same StrategyCycle with a deterministic stub adapter, so
NameError / ImportError / signature-drift bugs surface here rather than on
the first paper cycle.

    python3 -m qqq.orchestrator --mode preflight
"""

from __future__ import annotations

import math

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from qqq.broker_adapter import (
    DividendEvent,
    OptionContract,
    OrderResult,
    PortfolioSnapshot,
    PositionSnapshot,
)


class StubBroker:
    """Deterministic in-memory broker. Produces a realistic QQQ option chain
    so strike/delta selection, risk gating and state persistence all execute.
    """

    def __init__(self, price: float = 500.0, equity: float = 100_000.0, shares: float = 100.0):
        self._price = price
        self._equity = equity
        self._shares = shares
        self.submitted: list[str] = []

    def today(self) -> date:
        return date.today()

    def is_market_open(self) -> bool:
        return True

    def get_underlying_price(self) -> float:
        return self._price

    def get_atr(self, period: int = 20) -> float:
        return self._price * 0.015

    def get_200dma(self) -> tuple[float, float]:
        return self._price * 0.95, 0.0008  # below price, rising => BULL

    def get_option_chain(self, dte_range: tuple[int, int]) -> list[OptionContract]:
        expiry = date.today() + timedelta(days=(dte_range[0] + dte_range[1]) // 2)
        out: list[OptionContract] = []
        for offset in range(-60, 65, 5):
            strike = round(self._price + offset, 2)
            moneyness = (strike - self._price) / self._price
            # Sign convention matters: a put gets MORE negative as its strike
            # rises above spot (deeper ITM) and approaches 0 far below spot.
            # Getting this backwards makes the engines select nonsense strikes
            # and silently propose nothing, which would make preflight pass
            # while testing nothing.
            put_delta = -max(0.01, min(0.99, 0.5 + moneyness * 8))
            call_delta = max(0.01, min(0.99, 0.5 - moneyness * 8))
            # Premium decays convexly away from ATM. A linear decay produces
            # spreads with an unrealistically thin credit relative to width,
            # which the §7 risk/reward filter then correctly rejects — making
            # preflight look like it passed while never reaching submission.
            atm_premium = self._price * 0.02
            mid = max(0.02, atm_premium * math.exp(-abs(moneyness) * 35))
            for opt_type, dlt in (("put", put_delta), ("call", call_delta)):
                out.append(
                    OptionContract(
                        symbol=f"QQQ{expiry:%y%m%d}{'P' if opt_type=='put' else 'C'}{int(strike*1000):08d}",
                        underlying="QQQ",
                        expiry=expiry,
                        strike=strike,
                        option_type=opt_type,
                        bid=round(mid * 0.97, 2),
                        ask=round(mid * 1.03, 2),
                        delta=dlt,
                        implied_vol=0.20,
                    )
                )
        return out

    def get_current_positions(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            equity=self._equity,
            cash=self._equity,
            buying_power=self._equity * 2,
            positions=[] if not self._shares else [
                PositionSnapshot(
                    symbol="QQQ",
                    qty=self._shares,
                    avg_entry_price=self._price,
                    current_price=self._price,
                    market_value=self._price * self._shares,
                    unrealized_pl=0.0,
                    asset_class="equity",
                )
            ],
        )

    def get_dividend_calendar(self) -> list[DividendEvent]:
        ex = date.today() + timedelta(days=20)
        return [DividendEvent(ex_date=ex, pay_date=ex + timedelta(days=5), amount_per_share=0.75)]

    def unit_multiplier(self) -> float:
        return 100.0

    def submit_vertical_spread(self, spread) -> OrderResult:
        self.submitted.append("vertical_spread")
        return OrderResult(True, "stub-spread-1", None, "accepted")

    def submit_single_leg(self, order) -> OrderResult:
        self.submitted.append("single_leg")
        return OrderResult(True, "stub-leg-1", None, "accepted")

    def close_position(self, position_id: str, limit_pct: float | None) -> OrderResult:
        self.submitted.append("close")
        return OrderResult(True, position_id, None, "closed")


def run_preflight(config, gate) -> tuple[bool, list[str]]:
    """Returns (passed, findings)."""
    from qqq.cycle import StrategyCycle

    findings: list[str] = []
    passed = True

    with TemporaryDirectory() as tmp:
        scratch_config = replace(config, state_file_path=Path(tmp) / "preflight_state.json")

        # Drive the RAW stub, not the gated wrapper. If preflight ran through
        # the gate while the phase forbids orders, submission would be blocked
        # and the order-placing code — the part that has never run in anger —
        # would go untested. The gate is asserted separately below.
        scenarios = (
            ("empty account", 500.0, 0.0),
            ("at highs", 500.0, 100.0),
            ("-12% correction", 440.0, 100.0),
            ("-28% crash", 360.0, 100.0),
        )
        for label, price, shares in scenarios:
            stub = StubBroker(price=price, shares=shares)
            cycle = StrategyCycle(stub, scratch_config)
            try:
                state = cycle.run_daily_cycle()
                findings.append(
                    f"daily cycle @ {label}: OK — regime={state.current_regime} "
                    f"ref={state.reference_price} zones={len(state.acquisition_ladder)} "
                    f"submitted={stub.submitted or 'none'}"
                )
                if shares == 0 and "single_leg" not in stub.submitted:
                    passed = False
                    findings.append(
                        "core bootstrap: FAILED — account holds no shares but no core "
                        "buy was submitted; Engine A's core would never exist"
                    )
            except Exception as e:
                passed = False
                findings.append(f"daily cycle @ {label}: FAILED — {type(e).__name__}: {e}")

            try:
                cycle.run_intraday_check()
                findings.append(f"intraday check @ {label}: OK")
            except Exception as e:
                passed = False
                findings.append(f"intraday check @ {label}: FAILED — {type(e).__name__}: {e}")

    # The gate must actually block when the phase says so — assert it rather
    # than trusting that it was wired correctly.
    from core.gated_broker import GatedBroker

    stub = StubBroker()
    gated = GatedBroker(stub, arm="qqq")
    from qqq.broker_adapter import SingleLegOrder

    probe = SingleLegOrder(
        contract=None, symbol="QQQ", side="buy", qty=1,
        order_type="market", limit_price=None, client_order_id="preflight-probe",
    )
    result = gated.submit_single_leg(probe)
    if gate.allowed and not result.success:
        passed = False
        findings.append(f"gate: expected OPEN but submission was refused — {result.error}")
    elif not gate.allowed and result.success:
        passed = False
        findings.append("gate: phase forbids orders but the probe order was SUBMITTED — gate is not wired")
    else:
        findings.append(
            f"gate: behaves correctly (allowed={gate.allowed}, probe {'submitted' if result.success else 'blocked'})"
        )

    problems = config.validate()
    if problems:
        findings.extend(f"config: {p}" for p in problems)

    return passed, findings
