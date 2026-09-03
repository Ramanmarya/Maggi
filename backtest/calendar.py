"""
NYSE trading calendar, from Alpaca's own calendar endpoint.

A weekday filter is not a market calendar: it trades on Thanksgiving and
Good Friday and treats half-days as full ones. Alpaca already publishes the
real thing, so the calendar is fetched once per backtest and cached.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache


@lru_cache(maxsize=8)
def _fetch(api_key: str, secret_key: str, start: date, end: date) -> tuple[date, ...]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
    days = client.get_calendar(GetCalendarRequest(start=start, end=end))
    return tuple(d.date for d in days)


def trading_days(config, start: date, end: date) -> list[date]:
    """Actual NYSE sessions between start and end, inclusive."""
    return list(_fetch(config.alpaca_api_key, config.alpaca_secret_key, start, end))
