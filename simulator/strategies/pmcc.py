"""
simulator/strategies/pmcc.py

PMCCStrategy makes PMCC-specific decisions (which contract to pick, when to
roll, whether the stop-loss has triggered). It does not touch cash directly --
that's Portfolio's job. It does not know about QuantConnect or Black-Scholes
specifics -- that's OptionDataSource's job. This separation is what makes
Phase 2 (LLM-driven strike selection) a matter of swapping config/behavior
here, not rewriting the engine.

Earnings/dividend dates are passed in by the caller (see corporate_events.py)
rather than fetched here, so this class stays free of any network dependency.

KNOWN GAP (not fixed in this file): PMCCConfig.custom_blackout_ranges holds
date objects. TradingSession.to_dict() serializes config via
dataclasses.asdict(), which does NOT run those dates through the
_encode_dates/_decode_dates helpers used for the event log. If you populate
custom_blackout_ranges and then persist a session via PostgresSessionStore,
the round-trip will likely fail json.dumps() or mis-restore on load. This
needs a fix in session.py's to_dict/from_dict before custom blackout ranges
are used with persisted sessions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from simulator.data.option_pricer import OptionRight, OptionDataSource, filter_contracts
from simulator.data.corporate_events import next_event_on_or_after
from simulator.engine.portfolio import Portfolio, Leg, LegAction, CONTRACT_MULTIPLIER


@dataclass
class PMCCConfig:
    # --- long leg (the "stock substitute") ---
    long_target_delta: float = 0.85
    long_delta_range: tuple | None = None          # (min, max) -- overrides long_target_delta if set
    long_min_expiry_days: int = 300
    long_max_expiry_days: int | None = None

    # --- short leg (the monthly/weekly income call) ---
    # Selection priority: short_delta_range > short_target_delta > short_otm_pct.
    short_otm_pct: float = 0.05
    short_target_delta: float | None = None
    short_delta_range: tuple | None = None         # (min, max) -- overrides short_target_delta if set
    short_min_expiry_days: int = 20
    short_max_expiry_days: int = 35

    # --- liquidity filters (MOCK data today -- see option_pricer.py) ---
    # NOTE: shared between long and short leg selection for now. Split into
    # separate long_/short_ fields if you need independent thresholds.
    min_open_interest: int | None = None
    max_bid_ask_spread_pct: float | None = None
    min_iv_rank: float | None = None

    # --- position sizing ---
    contracts_per_position: int = 1

    # --- risk control: long leg ---
    stop_loss_pct: float = 0.10

    # --- risk control: short leg exit rules (Rule Set A) ---
    exit_delta_threshold: float | None = None        # e.g. 0.65 -- close if short delta rises above this
    exit_days_before_expiry: int | None = None        # e.g. 21 -- the "21 DTE" rule, independent of price
    exit_profit_target_pct: float | None = None       # e.g. 0.50 -- close once this fraction of max premium captured
    exit_stop_loss_base_multiplier: float | None = None  # e.g. 1.0 -- close if buy-back price exceeds this DTE-scaled multiple of entry credit
    exit_price_buffer_active: bool = False            # on/off switch for the price_buffer rule below
    exit_use_expected_move: bool = False              # True = dynamic buffer from IV; False = flat_buffer_pct
    exit_flat_buffer_pct: float = 0.02
    avoid_assignment_near_dividend: bool = False
    dividend_buffer_days: int = 3

    # --- blackout periods (Rule Set B) ---
    earnings_blackout_days_before: int = 0
    earnings_blackout_days_after: int = 0
    custom_blackout_ranges: list = field(default_factory=list)   # list[tuple[date, date]] -- see KNOWN GAP above

    # --- strike safety floor (Rule Set C) ---
    enforce_strike_safety: bool = False

    # --- transaction costs (folded into fill price -- see module note) ---
    commission_per_contract: float = 0.0
    slippage_pct_of_mid: float = 0.0


@dataclass
class RollResult:
    date: date
    settled_leg: Leg | None = None
    settled_pnl: float | None = None
    opened_leg: Leg | None = None
    closed_early: bool = False
    closed_early_reason: str | None = None
    stayed_flat_reason: str | None = None


class PMCCStrategy:

    def __init__(
        self,
        underlying: str,
        data_source: OptionDataSource,
        portfolio: Portfolio,
        config: PMCCConfig | None = None,
        earnings_dates: list | None = None,
        dividend_dates: list | None = None,
    ):
        self._underlying = underlying
        self._data_source = data_source
        self._portfolio = portfolio
        self._config = config or PMCCConfig()
        self._earnings_dates = earnings_dates or []
        self._dividend_dates = dividend_dates or []

        self._long_leg: Leg | None = None
        self._short_leg: Leg | None = None

        # Per-share cumulative short premium *retained* against the current
        # long leg's cost basis. Reset whenever a new long leg is opened.
        # Used by the strike-safety floor (Rule Set C).
        self._short_premium_collected: float = 0.0

    # ------------------------------------------------------------------
    # Transaction cost helper
    # ------------------------------------------------------------------

    def _apply_costs(self, price: float, is_buying: bool) -> float:
        """
        Folds slippage and commission into the effective fill price, since
        Portfolio doesn't track fees as a separate cash line item today.
        This is a simplification (a real broker integration would book
        commission separately) but keeps Portfolio's interface unchanged.
        """
        quantity = self._config.contracts_per_position
        slippage = price * self._config.slippage_pct_of_mid
        commission_per_share = (
            self._config.commission_per_contract / (CONTRACT_MULTIPLIER * quantity)
            if quantity else 0.0
        )
        cost = slippage + commission_per_share
        return price + cost if is_buying else max(0.0, price - cost)

    # ------------------------------------------------------------------
    # Long leg
    # ------------------------------------------------------------------

    def open_long_call(self, entry_date: date, spot_price: float) -> Leg:
        if self._long_leg is not None:
            raise ValueError("Long call already open; close it before opening another.")

        chain = self._data_source.get_chain(self._underlying, entry_date, spot_price)
        candidates = filter_contracts(
            chain, right=OptionRight.CALL,
            min_expiry_days=self._config.long_min_expiry_days,
            max_expiry_days=self._config.long_max_expiry_days,
            min_open_interest=self._config.min_open_interest,
            max_spread_pct=self._config.max_bid_ask_spread_pct,
        )
        if not candidates:
            raise ValueError(
                f"No long call candidates survived filtering on {entry_date} "
                f"(min_expiry_days={self._config.long_min_expiry_days}, "
                f"liquidity filters applied={self._config.min_open_interest is not None or self._config.max_bid_ask_spread_pct is not None})."
            )

        if self._config.long_delta_range is not None:
            lo, hi = self._config.long_delta_range
            in_range = [c for c in candidates if lo <= c.delta <= hi]
            pool = in_range if in_range else candidates
            target = (lo + hi) / 2
            contract = min(pool, key=lambda c: abs(c.delta - target))
        else:
            contract = min(candidates, key=lambda c: abs(c.delta - self._config.long_target_delta))

        price = self._apply_costs(contract.ask, is_buying=True)
        leg = self._portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.BUY,
            strike=contract.strike, expiry=contract.expiry,
            entry_date=entry_date, price=price,
            quantity=self._config.contracts_per_position,
        )
        self._long_leg = leg
        self._short_premium_collected = 0.0  # fresh cost-basis tracking for this long leg
        return leg

    def close_long_call(self, current_date: date, spot_price: float) -> float:
        if self._long_leg is None:
            raise ValueError("No long call is currently open.")

        price = self._portfolio.close_market_price(
            self._long_leg, current_date, self._data_source, self._underlying, spot_price
        )
        if price is None:
            # Fall back to intrinsic value if we can't find a market quote
            # (e.g. right at/after expiry, thin chain).
            price = max(0.0, spot_price - self._long_leg.strike)
        price = self._apply_costs(price, is_buying=False)

        pnl = self._portfolio.close_leg(self._long_leg, current_date, price)
        self._long_leg = None
        return pnl

    def close_short_call(self, current_date: date, spot_price: float) -> float:
        """
        Force-close the short leg right now, at current market price (buying
        to close) -- used e.g. to fully realize P&L at the end of a backtest,
        mirroring close_long_call but for the short leg.
        """
        if self._short_leg is None:
            raise ValueError("No short call is currently open.")

        price = self._portfolio.close_market_price(
            self._short_leg, current_date, self._data_source, self._underlying, spot_price
        )
        if price is None:
            price = max(0.0, spot_price - self._short_leg.strike)
        price = self._apply_costs(price, is_buying=True)  # buying to close

        pnl = self._portfolio.close_leg(self._short_leg, current_date, price)
        self._short_premium_collected += pnl / (CONTRACT_MULTIPLIER * self._short_leg.quantity)
        self._short_leg = None
        return pnl

    def check_stop_loss(self, spot_price: float) -> bool:
        """True if spot has fallen more than stop_loss_pct below the long leg's strike."""
        if self._long_leg is None:
            return False
        threshold = self._long_leg.strike * (1 - self._config.stop_loss_pct)
        return spot_price < threshold

    # ------------------------------------------------------------------
    # Short leg -- blackout / exit-rule helpers
    # ------------------------------------------------------------------

    def is_blackout_day(self, current_date: date) -> bool:
        """
        Public check: does this calendar date fall within an earnings or
        custom blackout window. Same underlying logic as _is_in_blackout
        (used internally to filter candidate expiries) -- exposed here so
        external code (e.g. the results writer) can flag a day as a
        blackout day without reaching into a private method.
        """
        return self._is_in_blackout(current_date)

    def _is_in_blackout(self, expiry_date: date) -> bool:
        for earnings_date in self._earnings_dates:
            start = earnings_date - timedelta(days=self._config.earnings_blackout_days_before)
            end = earnings_date + timedelta(days=self._config.earnings_blackout_days_after)
            if start <= expiry_date <= end:
                return True
        for start, end in self._config.custom_blackout_ranges:
            if start <= expiry_date <= end:
                return True
        return False

    def _price_buffer(self, strike: float, iv: float, days_remaining: int) -> float:
        """
        The "how close is too close to the strike" distance. If
        exit_use_expected_move is True, this is the 1-day-ish expected move
        derived from IV; otherwise a flat percentage of the strike.
        """
        if self._config.exit_use_expected_move:
            T = max(days_remaining, 0) / 365.0
            return strike * iv * math.sqrt(T)
        return strike * self._config.exit_flat_buffer_pct

    def _check_dividend_risk(self, current_date: date, spot_price: float) -> bool:
        if not self._config.avoid_assignment_near_dividend or self._short_leg is None:
            return False
        if spot_price < self._short_leg.strike:
            return False  # only relevant when the short call is ITM (or effectively so)
        upcoming = next_event_on_or_after(self._dividend_dates, current_date)
        if upcoming is None:
            return False
        return (upcoming - current_date).days <= self._config.dividend_buffer_days

    def check_short_leg_exit(self, current_date: date, spot_price: float) -> tuple:
        """
        Read-only: does NOT act. Returns (should_close: bool, reason: str | None).
        Called by roll_short_call before the expiry check, so an at-risk
        short leg can be closed proactively instead of always riding to its
        own expiry (see Rule Set A discussion).
        """
        if self._short_leg is None:
            return False, None

        contract = self._portfolio.find_matching_contract(
            self._short_leg, current_date, self._data_source, self._underlying, spot_price
        )
        days_remaining = (self._short_leg.expiry - current_date).days

        # 1. Dividend/assignment risk -- independent override, checked first.
        if self._check_dividend_risk(current_date, spot_price):
            return True, "dividend_assignment_risk"

        # 2. Delta threshold -- rising delta = rising assignment/pin risk.
        if (self._config.exit_delta_threshold is not None and contract is not None
                and contract.delta >= self._config.exit_delta_threshold):
            return True, "delta_threshold"

        # 3. Days-before-expiry -- the "21 DTE" rule, independent of price/delta.
        if (self._config.exit_days_before_expiry is not None
                and days_remaining <= self._config.exit_days_before_expiry):
            return True, "days_before_expiry"

        # 4. Profit target -- close once enough of max premium has been captured.
        if self._config.exit_profit_target_pct is not None and contract is not None:
            max_premium = self._short_leg.entry_price
            captured_pct = (max_premium - contract.ask) / max_premium if max_premium > 0 else 0.0
            if captured_pct >= self._config.exit_profit_target_pct:
                return True, "profit_target"

        # 4b. Stop-loss -- close if buy-back price exceeds a DTE-scaled
        # multiple of the entry credit (looser far from expiry, tighter
        # as expiry nears, mirroring the sqrt(time) shape of expected move).
        if self._config.exit_stop_loss_base_multiplier is not None and contract is not None:
            max_dte = self._config.short_max_expiry_days
            threshold = self._short_leg.entry_price * (
                1 + self._config.exit_stop_loss_base_multiplier * math.sqrt(max(days_remaining, 0) / max_dte)
            )
            if contract.ask >= threshold:
                return True, "stop_loss"

        # 5. Price buffer -- "a little bit less than the strike" rule.
        # Gated behind exit_price_buffer_active since this rule (as currently
        # written) checks the full-remaining-life expected move every day,
        # not just near expiry -- see conversation history for why that
        # caused excessive churn. Off by default until reworked.
        if self._config.exit_price_buffer_active and contract is not None:
            buffer = self._price_buffer(self._short_leg.strike, contract.iv, days_remaining)
            if spot_price >= (self._short_leg.strike - buffer):
                return True, "price_buffer"

        return False, None

    def _remaining_net_debit(self) -> float:
        """
        Per-share remaining cost basis of the current long leg: what was
        paid for it, minus cumulative short premium retained against it so
        far. Floored at 0 -- once short premium collected exceeds the long
        leg's cost, the strike-safety floor collapses to the long strike itself.
        """
        if self._long_leg is None:
            return 0.0
        return max(0.0, self._long_leg.entry_price - self._short_premium_collected)

    # ------------------------------------------------------------------
    # Short leg -- selection
    # ------------------------------------------------------------------

    def _select_short_contract(self, current_date: date, spot_price: float):
        chain = self._data_source.get_chain(self._underlying, current_date, spot_price)
        candidates = filter_contracts(
            chain, right=OptionRight.CALL,
            min_expiry_days=self._config.short_min_expiry_days,
            max_expiry_days=self._config.short_max_expiry_days,
            min_open_interest=self._config.min_open_interest,
            max_spread_pct=self._config.max_bid_ask_spread_pct,
            min_iv_rank=self._config.min_iv_rank,
        )

        # Blackout: drop any candidate whose expiry falls inside an earnings
        # or custom blackout window (Rule Set B).
        candidates = [c for c in candidates if not self._is_in_blackout(c.expiry)]

        # Strike-safety floor (Rule Set C): don't sell a strike that would
        # lock in a guaranteed loss if assigned and the long leg is exercised.
        if self._config.enforce_strike_safety and self._long_leg is not None:
            floor = self._long_leg.strike + self._remaining_net_debit()
            candidates = [c for c in candidates if c.strike >= floor]

        if not candidates:
            return None  # deliberately stay flat -- blackout, liquidity, or strike-safety ruled everything out

        if self._config.short_delta_range is not None:
            lo, hi = self._config.short_delta_range
            in_range = [c for c in candidates if lo <= c.delta <= hi]
            pool = in_range if in_range else candidates
            target = (lo + hi) / 2
            return min(pool, key=lambda c: abs(c.delta - target))

        if self._config.short_target_delta is not None:
            return min(candidates, key=lambda c: abs(c.delta - self._config.short_target_delta))

        target_strike = spot_price * (1 + self._config.short_otm_pct)
        return min(candidates, key=lambda c: abs(c.strike - target_strike))

    # ------------------------------------------------------------------
    # Short leg -- roll (settle/close, then reopen if possible)
    # ------------------------------------------------------------------

    def roll_short_call(self, current_date: date, spot_price: float) -> RollResult:
        """
        Close/settle the current short leg if any exit rule fires or it has
        reached expiry, then open a new one if none is currently open AND a
        valid candidate exists. Safe to call repeatedly (e.g. daily) -- it
        only acts when there's something to do, and will stay flat (no
        short leg) across multiple calls if blackout/liquidity/strike-safety
        rules keep ruling out every candidate.
        """
        result = RollResult(date=current_date)

        if self._short_leg is not None:
            should_close, reason = self.check_short_leg_exit(current_date, spot_price)

            if should_close:
                price = self._portfolio.close_market_price(
                    self._short_leg, current_date, self._data_source, self._underlying, spot_price
                )
                if price is None:
                    price = max(0.0, spot_price - self._short_leg.strike)
                price = self._apply_costs(price, is_buying=True)  # buying to close

                pnl = self._portfolio.close_leg(self._short_leg, current_date, price)
                self._short_premium_collected += pnl / (CONTRACT_MULTIPLIER * self._short_leg.quantity)
                result.settled_leg = self._short_leg
                result.settled_pnl = pnl
                result.closed_early = True
                result.closed_early_reason = reason
                self._short_leg = None

            elif current_date >= self._short_leg.expiry:
                pnl = self._portfolio.settle_leg(self._short_leg, current_date, spot_price)
                self._short_premium_collected += pnl / (CONTRACT_MULTIPLIER * self._short_leg.quantity)
                result.settled_leg = self._short_leg
                result.settled_pnl = pnl
                self._short_leg = None

        if self._short_leg is None:
            contract = self._select_short_contract(current_date, spot_price)
            if contract is not None:
                price = self._apply_costs(contract.bid, is_buying=False)  # selling to open
                leg = self._portfolio.open_leg(
                    right=OptionRight.CALL, action=LegAction.SELL,
                    strike=contract.strike, expiry=contract.expiry,
                    entry_date=current_date, price=price,
                    quantity=self._config.contracts_per_position,
                )
                self._short_leg = leg
                result.opened_leg = leg
            else:
                result.stayed_flat_reason = "no_eligible_contract_or_blackout"

        return result

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def underlying(self) -> str:
        return self._underlying

    def is_active(self) -> bool:
        return self._long_leg is not None and self._long_leg.is_open

    def status(self) -> dict:
        return {
            "long_leg": self._long_leg,
            "short_leg": self._short_leg,
            "is_active": self.is_active(),
            "short_premium_collected": self._short_premium_collected,
        }
