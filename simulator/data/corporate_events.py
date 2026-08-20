"""
simulator/data/corporate_events.py

Earnings and dividend ex-dates via yfinance -- same network caveat as
price_history.py: untested against the real Yahoo endpoint in this sandbox,
only against a mocked yf.Ticker response.
"""

from __future__ import annotations

from datetime import date

import yfinance as yf


def fetch_dividend_dates(ticker: str) -> list:
    """All historical dividend ex-dates for `ticker`, oldest to newest."""
    t = yf.Ticker(ticker)
    series = t.dividends
    if series is None or series.empty:
        return []
    return sorted(idx.date() if hasattr(idx, "date") else idx for idx in series.index)


def fetch_earnings_dates(ticker: str, limit: int = 12) -> list:
    """Known earnings dates for `ticker` -- yfinance mixes past reported dates with a
    few scheduled future ones, up to `limit` rows."""
    t = yf.Ticker(ticker)
    df = t.get_earnings_dates(limit=limit)
    if df is None or df.empty:
        return []
    return sorted(idx.date() if hasattr(idx, "date") else idx for idx in df.index)


def next_event_on_or_after(event_dates: list, as_of: date):
    """First event date >= as_of, or None if none found."""
    upcoming = [d for d in event_dates if d >= as_of]
    return min(upcoming) if upcoming else None