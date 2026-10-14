"""
simulator/strategies/rule_config.py

All thresholds for the automated PMCC rules engine (Rule Sets A/B/C from
the design discussion), collected in one dataclass so a backtest run is
fully described by one config object -- same pattern as PMCCConfig.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuleConfig:
    # --- Long leg (Rule C.1) ---
    long_target_delta: float = 0.85
    long_min_expiry_days: int = 365
    long_stop_loss_pct: float = 0.10  # existing PMCCConfig-style stop-loss

    # --- Short leg entry window (separate from management -- see the
    # 21-DTE discussion: these are two different knobs) ---
    short_entry_min_dte: int = 30
    short_entry_max_dte: int = 45

    # --- Short leg selection (Rule C.2 / C.3) ---
    short_target_delta: float = 0.35
    # Escape hatch for the C.2/C.3 conflict: how far below the strict
    # breakeven floor a strike is allowed to sit, rather than either never
    # relaxing the floor (-> dry periods) or ignoring it entirely (-> risk
    # of locking in a loss). In absolute stock-price dollars.
    breakeven_floor_tolerance: float = 2.00
    # Exclude contracts this close to expiry from delta-based selection --
    # avoids the near-zero-DTE degenerate-greeks artifact seen earlier
    # (delta snaps to a hard 0/1, IV/theta/vega zero out).
    min_dte_for_delta_selection: int = 3

    # --- Short leg management / exit (Rule A) ---
    management_dte: int = 21          # close/roll by this many days to expiry, regardless
    close_delta_threshold: float = 0.65  # close if delta rises above this (replaces "day-before" check)
    profit_take_pct: float = 0.50     # the 50% rule: close once premium has decayed to this fraction

    # --- Blackout periods (Rule B) ---
    earnings_blackout_days: int = 7     # no *part* of the holding period may fall within +/- this many days
    dividend_blackout_days: int = 7
    critical_dates: list = field(default_factory=list)   # user-supplied list[date]
    critical_date_blackout_days: int = 7

    # --- Consecutive-conflict -> long leg review trigger ---
    consecutive_conflict_limit: int = 3   # cycles in a row hitting the C.2/C.3 conflict before auto roll-down