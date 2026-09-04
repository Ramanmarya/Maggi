"""
Idle-cash sweep into short-duration Treasuries.

The strategy keeps a large cash balance — about $28,500 of a $100,000 account
at current prices — because the core consumes most of the capital and the
options program risks only a few thousand at a time. In the backtest that cash
earned nothing, which quietly costs more than the entire options overlay
produces: $1,283/yr at 4.5% against roughly $1,620/yr from every spread the
engine writes.

Sweeping it into SGOV (iShares 0-3 Month Treasury) collects that yield at
near-zero duration risk. The important constraint is that the options program
must never be starved: the sweep only ever moves cash ABOVE a reserve sized to
cover the aggregate put-risk cap plus a buffer, so a spread can always be
opened, closed, or assigned without a forced sale.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from .broker_adapter import SingleLegOrder

logger = logging.getLogger("qqq.cash_sweep")


@dataclass(frozen=True)
class SweepResult:
    action: str          # "buy" | "sell" | "hold"
    shares: int
    reason: str


def required_reserve(config, equity: float, spot: float | None = None) -> float:
    """Cash the options program must always be able to reach.

    For SPREADS this is the aggregate put-risk cap — every open spread could
    lose its maximum at once — plus a buffer for an assignment and ordinary
    settlement timing.

    For CASH-SECURED PUTS it is a different and much larger number: the whole
    structure is defined by holding the strike in cash, so the reserve must
    cover the collateral for every position the engine may open. Sizing this
    the spread way swept $187,909 into Treasuries and left $17,522 against a
    $42,590 collateral requirement, so the engine wrote nothing at all — the
    sweep silently disabled the strategy it was meant to fund.

    Deliberately generous either way: holding too much cash costs a few
    dollars of yield, holding too little forces a liquidation at the worst
    possible moment or, as here, stops the strategy trading.
    """
    basis = config.equity_basis_override or equity
    if getattr(config, "put_structure", "spread") == "cash_secured" and spot:
        contracts = max(1, config.put_spread_contracts) * config.cash_secured_reserve_positions
        return spot * config.core_unit_shares * contracts + config.cash_sweep_buffer
    return basis * config.max_aggregate_put_risk_pct + config.cash_sweep_buffer


def plan(config, cash: float, equity: float, sweep_price: float | None,
         sweep_held: float, spot: float | None = None) -> SweepResult:
    """Decide the sweep trade. Pure — no I/O — so it is directly testable."""
    if not config.cash_sweep_enabled:
        return SweepResult("hold", 0, "sweep disabled")
    if not sweep_price or sweep_price <= 0:
        return SweepResult("hold", 0, "no price for the sweep instrument")

    reserve = required_reserve(config, equity, spot)
    excess = cash - reserve

    # Hysteresis. Without a dead band the sweep buys whenever cash is a dollar
    # over the reserve and sells whenever it is a dollar under, which in
    # backtest produced a buy and a sell of the same size on alternate days,
    # every day, paying the spread each time for no yield at all.
    band = config.cash_sweep_min_trade

    if excess >= sweep_price and excess >= band:
        shares = int(excess // sweep_price)
        # Ignore trivial rebalances; the commission-free spread still costs
        # something and churn has no upside at this yield.
        if shares * sweep_price < band:
            return SweepResult("hold", 0, f"excess ${excess:,.0f} below minimum trade")
        return SweepResult("buy", shares, f"${excess:,.0f} above the ${reserve:,.0f} reserve")

    # Only sell once cash is a full band BELOW the reserve, not the instant it
    # dips under, so the buy and sell thresholds cannot straddle a single day's
    # cash movement.
    if excess < -band and sweep_held > 0:
        # Cash has dipped under the reserve — sell just enough to restore it.
        needed = int(min(sweep_held, (-excess) // sweep_price + 1))
        if needed > 0:
            return SweepResult("sell", needed, f"cash ${cash:,.0f} below the ${reserve:,.0f} reserve")

    return SweepResult("hold", 0, f"cash ${cash:,.0f} within reserve ${reserve:,.0f}")


def execute(broker, config, snapshot) -> SweepResult:
    """Price the sweep instrument, decide, and place the order."""
    symbol = config.cash_sweep_symbol
    price = broker.equity_price(symbol)
    held = sum(
        p.qty for p in snapshot.positions
        if p.asset_class == "equity" and p.symbol == symbol
    )
    underlying = broker.equity_price(config.symbol)
    result = plan(config, snapshot.cash, snapshot.equity, price, held, underlying)
    if result.action == "hold" or result.shares < 1:
        return result

    order = SingleLegOrder(
        contract=None, symbol=symbol, side=result.action, qty=result.shares,
        order_type="market", limit_price=None,
        client_order_id=f"sweep-{uuid.uuid4().hex[:10]}",
    )
    fill = broker.submit_single_leg(order)
    if fill.success:
        logger.info("Cash sweep: %s %d %s — %s", result.action, result.shares, symbol, result.reason)
    else:
        logger.warning("Cash sweep %s failed: %s", result.action, fill.error or fill.status)
    return result
