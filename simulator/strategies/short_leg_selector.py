"""
simulator/strategies/short_leg_selector.py

Combines Rule C.2 (target delta ~0.35) and C.3 (breakeven floor) into one
selection function, matching the design discussion's resolution of their
conflict:

  1. Filter candidates to the entry DTE window, drop near-zero-DTE
     contracts (degenerate greeks), and drop any whose holding period the
     BlackoutCalendar flags.
  2. Among survivors, prefer strikes meeting the STRICT breakeven floor --
     pick whichever is closest to target delta among those.
  3. If none meet the strict floor, retry allowing it to relax by
     breakeven_floor_tolerance. If any now qualify, pick closest to target
     delta among those -- and mark this a "conflict" (what the
     consecutive-conflict counter in the engine watches for).
  4. If still nothing, no selection at all this cycle (a dry cycle) --
     also a conflict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from simulator.data.option_pricer import OptionRight, OptionContract, filter_contracts
from simulator.strategies.blackout import BlackoutCalendar
from simulator.strategies.rule_config import RuleConfig


@dataclass
class ShortLegSelection:
    contract: OptionContract | None
    floor: float                    # strict breakeven floor used
    relaxed: bool                   # True if the tolerance escape hatch was needed
    conflict: bool                  # True if relaxed OR nothing found (feeds consecutive-conflict counter)
    reason: str                     # human-readable, for rule_log.csv
    candidates_in_window: int       # after DTE + blackout filtering, before floor
    candidates_meeting_floor: int   # after whichever floor (strict or relaxed) applied


def select_short_leg_contract(
    chain: list,
    entry_date: date,
    long_strike: float,
    long_premium: float,
    profit_so_far: float,
    config: RuleConfig,
    blackout: BlackoutCalendar,
) -> ShortLegSelection:
    strict_floor = long_strike + long_premium - profit_so_far

    candidates = filter_contracts(
        chain, right=OptionRight.CALL,
        min_expiry_days=max(config.short_entry_min_dte, config.min_dte_for_delta_selection),
        max_expiry_days=config.short_entry_max_dte,
    )
    candidates = [c for c in candidates if not blackout.is_blocked(entry_date, c.expiry)]
    candidates_in_window = len(candidates)

    if candidates_in_window == 0:
        return ShortLegSelection(
            contract=None, floor=strict_floor, relaxed=False, conflict=True,
            reason="no contracts available in entry DTE window after blackout filtering",
            candidates_in_window=0, candidates_meeting_floor=0,
        )

    strict_ok = [c for c in candidates if c.strike >= strict_floor]
    if strict_ok:
        best = min(strict_ok, key=lambda c: abs(c.delta - config.short_target_delta))
        return ShortLegSelection(
            contract=best, floor=strict_floor, relaxed=False, conflict=False,
            reason=(f"selected strike {best.strike} (delta {best.delta:.3f}), "
                    f"meets strict floor {strict_floor:.2f}"),
            candidates_in_window=candidates_in_window, candidates_meeting_floor=len(strict_ok),
        )

    relaxed_floor = strict_floor - config.breakeven_floor_tolerance
    relaxed_ok = [c for c in candidates if c.strike >= relaxed_floor]
    if relaxed_ok:
        best = min(relaxed_ok, key=lambda c: abs(c.delta - config.short_target_delta))
        return ShortLegSelection(
            contract=best, floor=strict_floor, relaxed=True, conflict=True,
            reason=(f"strict floor {strict_floor:.2f} unmet by any candidate; relaxed to "
                    f"{relaxed_floor:.2f} (tolerance {config.breakeven_floor_tolerance}) -- "
                    f"selected strike {best.strike} (delta {best.delta:.3f})"),
            candidates_in_window=candidates_in_window, candidates_meeting_floor=len(relaxed_ok),
        )

    return ShortLegSelection(
        contract=None, floor=strict_floor, relaxed=False, conflict=True,
        reason=(f"no candidate meets even the relaxed floor {relaxed_floor:.2f} "
                f"(strict {strict_floor:.2f}, tolerance {config.breakeven_floor_tolerance}) "
                f"-- skipping this cycle"),
        candidates_in_window=candidates_in_window, candidates_meeting_floor=0,
    )