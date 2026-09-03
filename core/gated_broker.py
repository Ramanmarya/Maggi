"""
GatedBroker — the enforcement wrapper that makes the order gate
unbypassable.

The strategy engines depend on the BrokerAdapter protocol, not on any
concrete adapter. By handing them a GatedBroker instead of an AlpacaAdapter,
every opening order is forced through core.kill_switch.check_order_gate()
before it can reach the network. There is deliberately no flag, argument or
config value that disables this: turning it off means not constructing it,
which is a visible code change in orchestrator.py, not a runtime toggle.

Asymmetry by design:
  - OPENING risk (submit_vertical_spread, submit_single_leg) is gated.
  - CLOSING risk (close_position) is always allowed. A kill switch that
    also blocked exits would trap the book in the exact scenario the switch
    exists for.
  - Read-only market/account calls pass straight through.
"""

from __future__ import annotations

from datetime import date

from .kill_switch import check_order_gate
from .structured_log import event


class OrderBlocked(Exception):
    """Raised only when a caller opts into strict mode; default is a soft refusal."""


class GatedBroker:
    def __init__(self, inner, arm: str = "qqq"):
        self._inner = inner
        self._arm = arm

    # ---- read-only passthrough -------------------------------------------
    def is_market_open(self) -> bool:
        return self._inner.is_market_open()

    def get_underlying_price(self) -> float:
        return self._inner.get_underlying_price()

    def get_atr(self, period: int = 20) -> float:
        return self._inner.get_atr(period)

    def get_200dma(self) -> tuple[float, float]:
        return self._inner.get_200dma()

    def get_option_chain(self, dte_range: tuple[int, int]):
        return self._inner.get_option_chain(dte_range)

    def get_current_positions(self):
        return self._inner.get_current_positions()

    def get_dividend_calendar(self):
        return self._inner.get_dividend_calendar()

    def unit_multiplier(self) -> float:
        return self._inner.unit_multiplier()

    # ---- gated: anything that OPENS risk ---------------------------------
    def _blocked(self, action: str, detail: str):
        from .broker_result import blocked_result

        gate = check_order_gate(self._arm)
        if gate.allowed:
            return None
        event("order_blocked", action=action, detail=detail, reason=gate.reason, phase=gate.phase)
        return blocked_result(gate.reason)

    def submit_vertical_spread(self, spread):
        refusal = self._blocked(
            "submit_vertical_spread",
            f"{spread.underlying} {spread.short_leg.strike}/{spread.long_leg.strike} x{spread.contracts}",
        )
        if refusal is not None:
            return refusal
        event(
            "order_submitted",
            action="submit_vertical_spread",
            underlying=spread.underlying,
            short_strike=spread.short_leg.strike,
            long_strike=spread.long_leg.strike,
            contracts=spread.contracts,
            limit_net_credit=spread.limit_net_credit,
        )
        return self._inner.submit_vertical_spread(spread)

    def submit_single_leg(self, order):
        refusal = self._blocked("submit_single_leg", f"{order.side} {order.qty} {order.symbol}")
        if refusal is not None:
            return refusal
        event(
            "order_submitted",
            action="submit_single_leg",
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            limit_price=order.limit_price,
        )
        return self._inner.submit_single_leg(order)

    # ---- never gated: closing risk ---------------------------------------
    def close_position(self, position_id: str, limit_pct: float | None):
        """Always permitted, including while the kill switch is off.

        Blocking exits would mean a tripped breaker leaves the book frozen
        with open short options it cannot buy back.
        """
        event("close_position", position_id=position_id)
        return self._inner.close_position(position_id, limit_pct)
