"""
simulator/strategies/config_loader.py

Translates the PMCC YAML config (pmcc_strategy_config.yaml) into a
PMCCConfig instance. Every "active: false" flag encountered is treated as
"leave this rule at PMCCConfig's off-default", never as an error -- so a
config with everything turned off just produces the vanilla PMCCConfig().

NOTE on liquidity fields: the YAML has separate liquidity blocks under
long_leg and short_leg_entry, but PMCCConfig currently has a single shared
set of liquidity fields (min_open_interest, max_bid_ask_spread_pct). This
loader applies short_leg_entry's liquidity block if active, falling back to
long_leg's if short's isn't set. Split PMCCConfig into long_/short_-specific
fields if you need them to differ in practice.

NOTE on custom_blackout_ranges: see the KNOWN GAP comment in pmcc.py --
these end up as raw date objects on PMCCConfig, which is not yet safely
round-trippable through TradingSession/PostgresSessionStore.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import yaml

from simulator.strategies.pmcc import PMCCConfig


def load_yaml(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _get(d: dict, *keys, default: Any = None) -> Any:
    """Safe nested dict getter -- returns `default` if any key in the path is missing."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _is_active(block: Any, default: bool = True) -> bool:
    """A missing block is treated as `default` (usually 'active'); a present
    block with no explicit 'active' key is also treated as `default`."""
    if not isinstance(block, dict):
        return default
    return block.get("active", default)


def build_pmcc_config(cfg: dict) -> PMCCConfig:
    kwargs: dict = {}

    # ------------------------------------------------------------------
    # Long leg
    # ------------------------------------------------------------------
    long_leg = cfg.get("long_leg", {})
    if _is_active(long_leg):
        delta_range = long_leg.get("delta_range")
        if delta_range:
            kwargs["long_delta_range"] = tuple(delta_range)
            kwargs["long_target_delta"] = sum(delta_range) / 2  # kept in sync as a fallback value

        expiry_range = long_leg.get("expiry_range_days")
        if expiry_range:
            kwargs["long_min_expiry_days"], kwargs["long_max_expiry_days"] = expiry_range

        long_liquidity = long_leg.get("liquidity", {})
        if _is_active(long_liquidity):
            if "min_open_interest" in long_liquidity:
                kwargs["min_open_interest"] = long_liquidity["min_open_interest"]
            if "max_bid_ask_spread_pct" in long_liquidity:
                kwargs["max_bid_ask_spread_pct"] = long_liquidity["max_bid_ask_spread_pct"]

    # ------------------------------------------------------------------
    # Short leg -- entry
    # ------------------------------------------------------------------
    short_entry = cfg.get("short_leg_entry", {})
    if _is_active(short_entry):
        delta_range = short_entry.get("delta_range")
        if delta_range:
            kwargs["short_delta_range"] = tuple(delta_range)

        expiry_range = short_entry.get("expiry_range_days")
        if expiry_range:
            kwargs["short_min_expiry_days"], kwargs["short_max_expiry_days"] = expiry_range

        min_iv_rank_block = short_entry.get("min_iv_rank", {})
        if _is_active(min_iv_rank_block, default=False):
            kwargs["min_iv_rank"] = min_iv_rank_block.get("threshold")

        short_liquidity = short_entry.get("liquidity", {})
        if _is_active(short_liquidity):
            # Overrides long_leg's liquidity settings if both are active --
            # see module note on the shared-field limitation.
            if "min_open_interest" in short_liquidity:
                kwargs["min_open_interest"] = short_liquidity["min_open_interest"]
            if "max_bid_ask_spread_pct" in short_liquidity:
                kwargs["max_bid_ask_spread_pct"] = short_liquidity["max_bid_ask_spread_pct"]

    # ------------------------------------------------------------------
    # Short leg -- exit
    # ------------------------------------------------------------------
    exit_cfg = cfg.get("short_leg_exit", {})
    if _is_active(exit_cfg):
        delta_threshold = exit_cfg.get("delta_threshold", {})
        if _is_active(delta_threshold):
            kwargs["exit_delta_threshold"] = delta_threshold.get("value")

        dte = exit_cfg.get("days_before_expiry", {})
        if _is_active(dte):
            kwargs["exit_days_before_expiry"] = dte.get("value")

        profit_target = exit_cfg.get("profit_target_pct", {})
        if _is_active(profit_target):
            kwargs["exit_profit_target_pct"] = profit_target.get("value")

        stop_loss = exit_cfg.get("stop_loss", {})
        if _is_active(stop_loss):
            kwargs["exit_stop_loss_base_multiplier"] = stop_loss.get("base_multiplier")

        price_buffer = exit_cfg.get("price_buffer", {})
        if _is_active(price_buffer):
            kwargs["exit_price_buffer_active"] = True
            kwargs["exit_use_expected_move"] = price_buffer.get("exit_use_expected_move", False)
            kwargs["exit_flat_buffer_pct"] = price_buffer.get("flat_buffer_pct", 0.02)

        dividend_risk = exit_cfg.get("dividend_risk", {})
        if _is_active(dividend_risk):
            kwargs["avoid_assignment_near_dividend"] = dividend_risk.get(
                "avoid_assignment_near_dividend", False
            )
            kwargs["dividend_buffer_days"] = dividend_risk.get("dividend_buffer_days", 3)

    # ------------------------------------------------------------------
    # Blackout periods
    # ------------------------------------------------------------------
    blackout = cfg.get("blackout_periods", {})
    if _is_active(blackout):
        earnings = blackout.get("earnings", {})
        if _is_active(earnings):
            kwargs["earnings_blackout_days_before"] = earnings.get("buffer_before_days", 0)
            kwargs["earnings_blackout_days_after"] = earnings.get("buffer_after_days", 0)

        custom = blackout.get("custom_dates", {})
        if _is_active(custom):
            ranges = []
            for r in custom.get("ranges", []):
                ranges.append((date.fromisoformat(r["start"]), date.fromisoformat(r["end"])))
            kwargs["custom_blackout_ranges"] = ranges

    # ------------------------------------------------------------------
    # Strike safety
    # ------------------------------------------------------------------
    strike_safety = cfg.get("strike_safety", {})
    kwargs["enforce_strike_safety"] = _is_active(strike_safety, default=False)

    # ------------------------------------------------------------------
    # Execution costs / sizing
    # ------------------------------------------------------------------
    execution = cfg.get("execution", {})
    if _is_active(execution):
        kwargs["commission_per_contract"] = execution.get("commission_per_contract", 0.0)
        kwargs["slippage_pct_of_mid"] = execution.get("slippage_pct_of_mid", 0.0)
        kwargs["contracts_per_position"] = execution.get("contracts_per_position", 1)

    # ------------------------------------------------------------------
    # roll_settings.mode is intentionally NOT mapped to a PMCCConfig field.
    # roll_short_call's current behavior already matches
    # "roll_if_possible_else_wait": it always tries to reopen after closing,
    # and _select_short_contract returning None naturally produces a flat
    # (uncovered) period. "always_roll" vs "always_close_reopen" aren't yet
    # behaviorally distinct in a single-short-leg-at-a-time strategy -- treat
    # this YAML field as documentation for now until/unless that distinction
    # becomes meaningful (e.g. multi-contract partial rolls).
    # ------------------------------------------------------------------

    return PMCCConfig(**kwargs)


# ---------------------------------------------------------------------
# Example usage (not executed on import):
#
#   from simulator.strategies.config_loader import load_yaml, build_pmcc_config
#   from simulator.data.corporate_events import fetch_earnings_dates, fetch_dividend_dates
#
#   raw = load_yaml("pmcc_strategy_config.yaml")
#   config = build_pmcc_config(raw)
#
#   # Corporate events are still fetched separately, per ticker, since they
#   # need network access this loader doesn't perform:
#   earnings_dates = fetch_earnings_dates("AAPL")
#   dividend_dates = fetch_dividend_dates("AAPL")
#
#   strategy = PMCCStrategy("AAPL", data_source, portfolio, config,
#                            earnings_dates=earnings_dates, dividend_dates=dividend_dates)
# ---------------------------------------------------------------------