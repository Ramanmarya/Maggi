"""Shared construction of a refusal OrderResult, so the gate doesn't need to
import the strategy package (which would make core depend on qqq)."""

from __future__ import annotations


def blocked_result(reason: str):
    from qqq.broker_adapter import OrderResult

    return OrderResult(
        success=False,
        order_id=None,
        filled_avg_price=None,
        status="blocked",
        error=f"ORDER GATE: {reason}",
    )
