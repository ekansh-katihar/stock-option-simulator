"""
simulator/strategies/pmcc.py

PMCCStrategy makes PMCC-specific decisions (which contract to pick, when to
roll, whether the stop-loss has triggered). It does not touch cash directly --
that's Portfolio's job. It does not know about QuantConnect or Black-Scholes
specifics -- that's OptionDataSource's job. This separation is what makes
Phase 2 (LLM-driven strike selection) a matter of swapping config/behavior
here, not rewriting the engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from simulator.data.option_pricer import OptionRight, OptionDataSource, find_contract
from simulator.engine.portfolio import Portfolio, Leg, LegAction


@dataclass
class PMCCConfig:
    # --- long leg (the "stock substitute") ---
    long_target_delta: float = 0.85
    long_min_expiry_days: int = 300

    # --- short leg (the monthly income call) ---
    # Default rule: strike = spot * (1 + short_otm_pct), e.g. 0.05 = 5% OTM.
    # If short_target_delta is set, delta-based selection is used instead.
    short_otm_pct: float = 0.05
    short_target_delta: float | None = None
    short_min_expiry_days: int = 20
    short_max_expiry_days: int = 35

    # --- risk control ---
    # Long leg is considered "stopped out" if spot falls this far below its strike.
    stop_loss_pct: float = 0.10


@dataclass
class RollResult:
    date: date
    settled_leg: Leg | None = None
    settled_pnl: float | None = None
    opened_leg: Leg | None = None


class PMCCStrategy:

    def __init__(
        self,
        underlying: str,
        data_source: OptionDataSource,
        portfolio: Portfolio,
        config: PMCCConfig | None = None,
    ):
        self._underlying = underlying
        self._data_source = data_source
        self._portfolio = portfolio
        self._config = config or PMCCConfig()

        self._long_leg: Leg | None = None
        self._short_leg: Leg | None = None

    # ------------------------------------------------------------------
    # Long leg
    # ------------------------------------------------------------------

    def open_long_call(self, entry_date: date, spot_price: float) -> Leg:
        if self._long_leg is not None:
            raise ValueError("Long call already open; close it before opening another.")

        chain = self._data_source.get_chain(self._underlying, entry_date, spot_price)
        contract = find_contract(
            chain, right=OptionRight.CALL,
            target_delta=self._config.long_target_delta,
            min_expiry_days=self._config.long_min_expiry_days,
        )
        if contract is None:
            raise ValueError(
                f"No suitable long call found on {entry_date} "
                f"(target_delta={self._config.long_target_delta}, "
                f"min_expiry_days={self._config.long_min_expiry_days})."
            )

        leg = self._portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.BUY,
            strike=contract.strike, expiry=contract.expiry,
            entry_date=entry_date, price=contract.ask,
        )
        self._long_leg = leg
        return leg

    def close_long_call(self, current_date: date, spot_price: float) -> float:
        if self._long_leg is None:
            raise ValueError("No long call is currently open.")

        price = self._portfolio.current_market_price(
            self._long_leg, current_date, self._data_source, self._underlying, spot_price
        )
        if price is None:
            # Fall back to intrinsic value if we can't find a market quote
            # (e.g. right at/after expiry, thin chain).
            price = max(0.0, spot_price - self._long_leg.strike)

        pnl = self._portfolio.close_leg(self._long_leg, current_date, price)
        self._long_leg = None
        return pnl

    def check_stop_loss(self, spot_price: float) -> bool:
        """True if spot has fallen more than stop_loss_pct below the long leg's strike."""
        if self._long_leg is None:
            return False
        threshold = self._long_leg.strike * (1 - self._config.stop_loss_pct)
        return spot_price < threshold

    # ------------------------------------------------------------------
    # Short leg (monthly roll)
    # ------------------------------------------------------------------

    def roll_short_call(self, current_date: date, spot_price: float) -> RollResult:
        """
        Settle the current short leg if it has reached expiry, then open a new
        one if none is currently open. Safe to call repeatedly (e.g. daily) --
        it only acts when there's actually something to do.
        """
        result = RollResult(date=current_date)

        if self._short_leg is not None and current_date >= self._short_leg.expiry:
            pnl = self._portfolio.settle_leg(self._short_leg, current_date, spot_price)
            result.settled_leg = self._short_leg
            result.settled_pnl = pnl
            self._short_leg = None

        if self._short_leg is None:
            contract = self._select_short_contract(current_date, spot_price)
            if contract is not None:
                leg = self._portfolio.open_leg(
                    right=OptionRight.CALL, action=LegAction.SELL,
                    strike=contract.strike, expiry=contract.expiry,
                    entry_date=current_date, price=contract.bid,
                )
                self._short_leg = leg
                result.opened_leg = leg

        return result

    def _select_short_contract(self, current_date: date, spot_price: float):
        chain = self._data_source.get_chain(self._underlying, current_date, spot_price)

        if self._config.short_target_delta is not None:
            return find_contract(
                chain, right=OptionRight.CALL,
                target_delta=self._config.short_target_delta,
                min_expiry_days=self._config.short_min_expiry_days,
                max_expiry_days=self._config.short_max_expiry_days,
            )

        target_strike = spot_price * (1 + self._config.short_otm_pct)
        return find_contract(
            chain, right=OptionRight.CALL,
            target_strike=target_strike,
            min_expiry_days=self._config.short_min_expiry_days,
            max_expiry_days=self._config.short_max_expiry_days,
        )

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
        }
