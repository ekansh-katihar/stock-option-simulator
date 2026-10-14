"""
simulator/strategies/blackout.py

Combines earnings dates, dividend ex-dates, and user-supplied critical
dates into one blackout calendar, and checks whether a proposed holding
period (entry_date -> expiry_date) overlaps ANY blackout window -- not
just whether the expiry date itself falls near an event. A position opened
well before earnings but still open when earnings hits is just as exposed
as one expiring right on the date; checking only the expiry date misses
this (the gap identified during the Rule Set B design discussion).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from simulator.data.corporate_events import fetch_earnings_dates, fetch_dividend_dates
from simulator.strategies.rule_config import RuleConfig


@dataclass
class BlackoutWindow:
    event_date: date
    event_type: str       # "earnings" | "dividend" | "critical"
    window_start: date
    window_end: date


class BlackoutCalendar:
    """
    Built once per ticker/config (fetches earnings/dividends once), then
    queried cheaply per candidate holding period.
    """

    def __init__(self, ticker: str, config: RuleConfig,
                 earnings_dates: list | None = None, dividend_dates: list | None = None):
        self._config = config
        # Optional injection lets tests (and any caller who already has the
        # dates) skip the network call -- same pattern used throughout.
        self._earnings_dates = earnings_dates if earnings_dates is not None else fetch_earnings_dates(ticker)
        self._dividend_dates = dividend_dates if dividend_dates is not None else fetch_dividend_dates(ticker)
        self._windows = self._build_windows()

    def _build_windows(self) -> list:
        windows = []
        for d in self._earnings_dates:
            windows.append(BlackoutWindow(
                event_date=d, event_type="earnings",
                window_start=d - timedelta(days=self._config.earnings_blackout_days),
                window_end=d + timedelta(days=self._config.earnings_blackout_days),
            ))
        for d in self._dividend_dates:
            windows.append(BlackoutWindow(
                event_date=d, event_type="dividend",
                window_start=d - timedelta(days=self._config.dividend_blackout_days),
                window_end=d + timedelta(days=self._config.dividend_blackout_days),
            ))
        for d in self._config.critical_dates:
            windows.append(BlackoutWindow(
                event_date=d, event_type="critical",
                window_start=d - timedelta(days=self._config.critical_date_blackout_days),
                window_end=d + timedelta(days=self._config.critical_date_blackout_days),
            ))
        return windows

    def conflicts(self, entry_date: date, expiry_date: date) -> list:
        """
        Every BlackoutWindow overlapping the proposed holding period
        [entry_date, expiry_date]. Empty = clear. Two ranges overlap when
        start1 <= end2 AND start2 <= end1 (inclusive on both ends).
        """
        return [
            w for w in self._windows
            if entry_date <= w.window_end and w.window_start <= expiry_date
        ]

    def is_blocked(self, entry_date: date, expiry_date: date) -> bool:
        return len(self.conflicts(entry_date, expiry_date)) > 0

    def describe_conflicts(self, entry_date: date, expiry_date: date) -> str:
        """Human-readable reason, for the rule_log.csv."""
        found = self.conflicts(entry_date, expiry_date)
        if not found:
            return ""
        parts = [f"{w.event_type} on {w.event_date}" for w in found]
        return "blocked by: " + "; ".join(parts)