"""
Target exposure curve — V5 doc §15/§17: how much MNQ/QQQ-equivalent delta
the strategy *wants* at a given decline from the reference price, and the
check that reaching an acquisition zone does not automatically mean adding
more exposure if options already provide it.

Explicitly a hypothesis to test per the doc, not a tuned constant — see
StrategyConfig.exposure_curve for the table itself.
"""

from __future__ import annotations

from .config import StrategyConfig


def target_units_for_decline(config: StrategyConfig, decline_pct: float) -> float:
    """Linear-interpolate the target unit-equivalent exposure for a given
    decline fraction from reference (0.0 = at highs, 0.20 = -20%, etc.).
    Clamped to the min/max keys in the curve.
    """
    points = sorted(config.exposure_curve.items())
    if decline_pct <= points[0][0]:
        return points[0][1]
    if decline_pct >= points[-1][0]:
        return points[-1][1]

    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= decline_pct <= x1:
            if x1 == x0:
                return y0
            frac = (decline_pct - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return points[-1][1]  # unreachable in practice


def decline_from_reference(reference_price: float | None, current_price: float) -> float:
    if not reference_price or reference_price <= 0:
        return 0.0
    return max(0.0, (reference_price - current_price) / reference_price)


def should_add_exposure(
    config: StrategyConfig,
    reference_price: float | None,
    current_price: float,
    current_total_unit_delta: float,
) -> bool:
    """V5 doc §17: reaching a zone does NOT automatically mean adding
    exposure — only add if current total delta is below the target curve
    for the current decline.
    """
    decline = decline_from_reference(reference_price, current_price)
    target = target_units_for_decline(config, decline)
    return current_total_unit_delta < target
