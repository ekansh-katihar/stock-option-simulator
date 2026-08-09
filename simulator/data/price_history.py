"""
simulator/data/price_history.py

Pulls real historical stock closes via yfinance and normalizes them into
dict[date, float] -- the same shape BlackScholesSource.price_history expects,
so this drops straight into what we've already built.
"""

from __future__ import annotations

from datetime import date, timedelta

import yfinance as yf


def fetch_price_history(ticker: str, start: date, end: date) -> dict:
    """
    Fetch daily closing prices for `ticker` between start and end (inclusive).
    yfinance's `end` is exclusive, so we pad by a day to make our own range inclusive.
    """
    df = yf.download(
        ticker,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        progress=False,
    )
    if df.empty:
        raise ValueError(f"No price data returned for {ticker} between {start} and {end}.")

    # yfinance sometimes returns MultiIndex columns (e.g. for multi-ticker calls);
    # normalize to a plain 'Close' column either way.
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close[ticker]

    prices = {}
    for idx, value in close.items():
        d = idx.date() if hasattr(idx, "date") else idx
        if value == value:  # filters out NaN
            prices[d] = float(value)

    return prices
