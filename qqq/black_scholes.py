"""
Minimal Black-Scholes helpers, used only because Polygon's historical
options endpoints don't return greeks the way the live snapshot endpoint
does (greeks are a real-time-snapshot-only field). For backtesting we
back out implied vol from the quoted mid price, then compute delta from
that — same approach any options backtest has to take without a paid
historical-greeks feed (e.g. ORATS).

This is intentionally minimal (European-style pricing, continuous
dividend yield) — QQQ options are American-style, so early-exercise value
is not modeled. For OTM short premium (the profile this strategy sells),
the American/European price difference is small; it matters more for
deep ITM contracts, which this strategy generally avoids selecting.
"""

from __future__ import annotations

import math
from typing import Literal


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(
    spot: float,
    strike: float,
    dte_years: float,
    vol: float,
    rate: float,
    dividend_yield: float,
    option_type: Literal["call", "put"],
) -> float:
    if dte_years <= 0 or vol <= 0:
        intrinsic = max(0.0, spot - strike) if option_type == "call" else max(0.0, strike - spot)
        return intrinsic

    d1 = (
        math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol * vol) * dte_years
    ) / (vol * math.sqrt(dte_years))
    d2 = d1 - vol * math.sqrt(dte_years)

    disc_r = math.exp(-rate * dte_years)
    disc_q = math.exp(-dividend_yield * dte_years)

    if option_type == "call":
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)


def bs_delta(
    spot: float,
    strike: float,
    dte_years: float,
    vol: float,
    rate: float,
    dividend_yield: float,
    option_type: Literal["call", "put"],
) -> float:
    if dte_years <= 0 or vol <= 0:
        if option_type == "call":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0

    d1 = (
        math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol * vol) * dte_years
    ) / (vol * math.sqrt(dte_years))
    disc_q = math.exp(-dividend_yield * dte_years)

    if option_type == "call":
        return disc_q * _norm_cdf(d1)
    return disc_q * (_norm_cdf(d1) - 1.0)


def implied_vol(
    market_price: float,
    spot: float,
    strike: float,
    dte_years: float,
    rate: float,
    dividend_yield: float,
    option_type: Literal["call", "put"],
    tol: float = 1e-4,
    max_iterations: int = 100,
) -> float | None:
    """Bisection solve for implied vol. Returns None if it can't converge
    (e.g. price outside any achievable Black-Scholes value — happens with
    stale/bad quotes)."""
    if market_price <= 0 or dte_years <= 0:
        return None

    intrinsic = max(0.0, spot - strike) if option_type == "call" else max(0.0, strike - spot)
    if market_price < intrinsic:
        return None  # bad/stale quote, below intrinsic value

    lo, hi = 1e-4, 5.0  # 0.01% to 500% vol, generous bounds
    price_lo = bs_price(spot, strike, dte_years, lo, rate, dividend_yield, option_type)
    price_hi = bs_price(spot, strike, dte_years, hi, rate, dividend_yield, option_type)
    if not (price_lo <= market_price <= price_hi):
        return None

    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        price_mid = bs_price(spot, strike, dte_years, mid, rate, dividend_yield, option_type)
        if abs(price_mid - market_price) < tol:
            return mid
        if price_mid < market_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
