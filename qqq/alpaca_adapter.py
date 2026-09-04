"""
AlpacaAdapter — implements BrokerAdapter against alpaca-py, for the paper
account. Uses multi-leg option orders for vertical spreads (per the
architecture doc §8) to avoid legging risk.

Requires: alpaca-py>=0.33.0, ALPACA_API_KEY / ALPACA_SECRET_KEY in .env.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from tenacity import retry, stop_after_attempt, wait_exponential

from .broker_adapter import (
    DividendEvent,
    OptionContract,
    OrderResult,
    PortfolioSnapshot,
    PositionSnapshot,
    SingleLegOrder,
    VerticalSpreadOrder,
)
from .config import StrategyConfig

logger = logging.getLogger("qqq_bot.alpaca_adapter")


class AlpacaAdapter:
    def __init__(self, config: StrategyConfig):
        if "paper" not in config.alpaca_base_url:
            raise RuntimeError(
                "Refusing to construct AlpacaAdapter against a non-paper base URL. "
                "This is a deliberate safety rail — see config.py."
            )
        self._config = config

        # Imports deferred so the rest of the package doesn't hard-require
        # alpaca-py just to run pure-logic unit tests against stub adapters.
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._trading = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=True,
            url_override=config.alpaca_base_url,
        )
        self._stock_data = StockHistoricalDataClient(
            api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key
        )
        self._option_data = OptionHistoricalDataClient(
            api_key=config.alpaca_api_key, secret_key=config.alpaca_secret_key
        )

    def today(self) -> date:
        return date.today()

    def is_market_open(self) -> bool:
        """Alpaca's clock is the source of truth — it already knows holidays
        and early closes, which a local calendar would have to re-derive and
        get wrong. Fails closed: an unreachable clock means 'assume closed'.
        """
        try:
            return bool(self._trading.get_clock().is_open)
        except Exception:
            logger.exception("Failed to fetch market clock; assuming CLOSED.")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def get_underlying_price(self) -> float:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        # IEX, not SIP: the free data plan rejects recent SIP queries with
        # 'subscription does not permit querying recent SIP data' rather than
        # falling back, so the feed has to be named explicitly.
        req = StockLatestTradeRequest(symbol_or_symbols=self._config.symbol, feed=DataFeed.IEX)
        trade = self._stock_data.get_stock_latest_trade(req)[self._config.symbol]
        return float(trade.price)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def get_atr(self, period: int = 20) -> float:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = datetime.utcnow()
        start = end - timedelta(days=period * 3)  # buffer for weekends/holidays
        req = StockBarsRequest(
            symbol_or_symbols=self._config.symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        bars = self._stock_data.get_stock_bars(req)[self._config.symbol]
        bars = bars[-period:]
        if len(bars) < 2:
            raise RuntimeError("Not enough bars to compute ATR.")

        true_ranges = []
        for i in range(1, len(bars)):
            high, low, prev_close = bars[i].high, bars[i].low, bars[i - 1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        return sum(true_ranges) / len(true_ranges)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def get_200dma(self) -> tuple[float, float]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = datetime.utcnow()
        start = end - timedelta(days=230)
        req = StockBarsRequest(
            symbol_or_symbols=self._config.symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        )
        bars = self._stock_data.get_stock_bars(req)[self._config.symbol][-200:]
        closes = [b.close for b in bars]
        if len(closes) < 40:
            raise RuntimeError("Not enough bars to compute 200DMA.")
        dma = sum(closes) / len(closes)

        lookback = self._config.regime_slope_lookback_days
        recent = closes[-lookback:]
        # Simple slope: average day-over-day % change over the lookback window
        pct_changes = [
            (recent[i] - recent[i - 1]) / recent[i - 1] for i in range(1, len(recent))
        ]
        slope = sum(pct_changes) / len(pct_changes) if pct_changes else 0.0
        return dma, slope

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def get_option_chain(self, dte_range: tuple[int, int]) -> list[OptionContract]:
        from alpaca.data.requests import OptionChainRequest

        req = OptionChainRequest(underlying_symbol=self._config.symbol)
        chain = self._option_data.get_option_chain(req)

        today = date.today()
        out: list[OptionContract] = []
        for symbol, snap in chain.items():
            try:
                expiry = _parse_occ_expiry(symbol)
            except ValueError:
                continue
            dte = (expiry - today).days
            if not (dte_range[0] <= dte <= dte_range[1]):
                continue

            quote = getattr(snap, "latest_quote", None)
            greeks = getattr(snap, "greeks", None)
            bid = float(quote.bid_price) if quote else 0.0
            ask = float(quote.ask_price) if quote else 0.0
            delta = float(greeks.delta) if greeks and greeks.delta is not None else None
            iv = float(getattr(snap, "implied_volatility", None) or 0) or None

            out.append(
                OptionContract(
                    symbol=symbol,
                    underlying=self._config.symbol,
                    expiry=expiry,
                    strike=_parse_occ_strike(symbol),
                    option_type="call" if "C" in symbol[len(self._config.symbol) + 6 :] else "put",
                    bid=bid,
                    ask=ask,
                    delta=delta,
                    implied_vol=iv,
                )
            )
        return out

    def equity_price(self, symbol: str) -> float | None:
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockLatestTradeRequest

            req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
            return float(self._stock_data.get_stock_latest_trade(req)[symbol].price)
        except Exception:
            logger.warning("equity_price unavailable for %s", symbol)
            return None

    def option_delta(self, symbol: str) -> float | None:
        """Alpaca's option snapshot carries greeks; None on any failure so the
        aggregator degrades rather than crashing the cycle."""
        try:
            from alpaca.data.requests import OptionSnapshotRequest

            snap = self._option_data.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=symbol)
            )[symbol]
            greeks = getattr(snap, "greeks", None)
            return float(greeks.delta) if greeks and greeks.delta is not None else None
        except Exception:
            logger.warning("option_delta unavailable for %s", symbol)
            return None

    def option_mark(self, symbol: str) -> float | None:
        """Latest mid for one contract. None on any failure, so a data gap
        degrades to the time-based exit rather than crashing the cycle."""
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest

            q = self._option_data.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=symbol)
            )[symbol]
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            return (bid + ask) / 2 if bid > 0 and ask > 0 else None
        except Exception:
            logger.warning("option_mark unavailable for %s", symbol)
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def get_current_positions(self) -> PortfolioSnapshot:
        account = self._trading.get_account()
        raw_positions = self._trading.get_all_positions()

        positions = []
        for p in raw_positions:
            # Only this arm's instrument. The Alpaca account may be shared with
            # other systems, and counting their holdings as QQQ exposure would
            # corrupt the unit-delta calculation that drives every sizing
            # decision. Matches QQQ shares and OCC option symbols rooted on QQQ.
            sym = str(p.symbol)
            # The arm's own instrument, plus the cash-sweep instrument — the
            # sweep has to be able to see its own holding to rebalance it.
            keep = sym == self._config.symbol or sym.startswith(self._config.symbol)
            keep = keep or sym == self._config.cash_sweep_symbol
            if not keep:
                continue
            asset_class = "option" if getattr(p, "asset_class", None) == "us_option" else "equity"
            positions.append(
                PositionSnapshot(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_entry_price=float(p.avg_entry_price),
                    current_price=float(p.current_price or 0),
                    market_value=float(p.market_value or 0),
                    unrealized_pl=float(p.unrealized_pl or 0),
                    asset_class=asset_class,
                )
            )

        return PortfolioSnapshot(
            equity=float(account.equity),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
            positions=positions,
        )

    def submit_vertical_spread(self, spread: VerticalSpreadOrder) -> OrderResult:
        """
        Submits both legs as a single multi-leg order per the doc's guidance
        to avoid legging risk. alpaca-py's multi-leg option order support
        should be verified against the currently installed version before
        relying on this in production — the request shape below matches the
        documented MLeg order request as of writing but option-order APIs
        have moved fast; smoke-test against paper before trusting it.
        """
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

        if spread.long_leg is None:
            # §8 forbids naked puts in the base strategy. The engine can only
            # produce one when protective_leg is explicitly disabled for
            # testing, so refuse rather than route it to a live account.
            return OrderResult(
                False, None, None, "rejected",
                error="naked short put refused: §8 excludes them from the base strategy",
            )
        legs = [
            OptionLegRequest(symbol=spread.short_leg.symbol, side=OrderSide.SELL, ratio_qty=1),
            OptionLegRequest(symbol=spread.long_leg.symbol, side=OrderSide.BUY, ratio_qty=1),
        ]
        try:
            order_req = LimitOrderRequest(
                qty=spread.contracts,
                limit_price=spread.limit_net_credit,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                legs=legs,
                client_order_id=spread.client_order_id,
            )
            order = self._trading.submit_order(order_req)
            return OrderResult(
                success=True,
                order_id=str(order.id),
                filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
                status=str(order.status),
                raw=order.__dict__ if hasattr(order, "__dict__") else None,
            )
        except Exception as e:  # noqa: BLE001 — surface broker errors to the caller, don't crash the cycle
            logger.exception("submit_vertical_spread failed")
            return OrderResult(success=False, order_id=None, filled_avg_price=None, status="error", error=str(e))

    def submit_single_leg(self, order: SingleLegOrder) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        side = OrderSide.BUY if order.side == "buy" else OrderSide.SELL
        try:
            if order.order_type == "limit":
                req = LimitOrderRequest(
                    symbol=order.symbol,
                    qty=order.qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=order.limit_price,
                    client_order_id=order.client_order_id,
                )
            else:
                req = MarketOrderRequest(
                    symbol=order.symbol,
                    qty=order.qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=order.client_order_id,
                )
            result = self._trading.submit_order(req)
            return OrderResult(
                success=True,
                order_id=str(result.id),
                filled_avg_price=float(result.filled_avg_price) if result.filled_avg_price else None,
                status=str(result.status),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("submit_single_leg failed")
            return OrderResult(success=False, order_id=None, filled_avg_price=None, status="error", error=str(e))

    def close_position(self, position_id: str, limit_pct: float | None) -> OrderResult:
        try:
            result = self._trading.close_position(position_id)
            return OrderResult(
                success=True,
                order_id=str(result.id) if hasattr(result, "id") else position_id,
                filled_avg_price=None,
                status="closed",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("close_position failed")
            return OrderResult(success=False, order_id=None, filled_avg_price=None, status="error", error=str(e))

    def get_dividend_calendar(self) -> list[DividendEvent]:
        # alpaca-py's corporate actions endpoint covers this; left as a TODO
        # since it needs the corporate-actions API surface confirmed against
        # your account's data plan. Polygon's dividends endpoint (already
        # used in backtest_adapter.py) is a reliable fallback if Alpaca's
        # corporate-actions data isn't available on your plan.
        logger.warning(
            "get_dividend_calendar: not yet wired to a live source — "
            "returning empty list. Ex-div safety checks will pass trivially "
            "until this is implemented. Do not sell calls live until fixed."
        )
        return []

    def unit_multiplier(self) -> float:
        return float(self._config.core_unit_shares)


def _parse_occ_expiry(occ_symbol: str) -> date:
    # OCC symbol format: {ROOT}{YYMMDD}{C/P}{STRIKE*1000, 8 digits}
    # Root can contain digits/varying length, so parse from the back.
    digits_and_type = occ_symbol[-15:]  # YYMMDD + C/P + 8-digit strike
    yy_mm_dd = digits_and_type[:6]
    return datetime.strptime(yy_mm_dd, "%y%m%d").date()


def _parse_occ_strike(occ_symbol: str) -> float:
    strike_digits = occ_symbol[-8:]
    return int(strike_digits) / 1000.0
