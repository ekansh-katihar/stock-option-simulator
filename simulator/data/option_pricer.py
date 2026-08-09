"""
simulator/data/option_pricer.py

Source-agnostic option data layer.

- OptionContract:   internal representation of a single option contract snapshot.
- OptionDataSource: abstract interface every data source implements.
- BlackScholesSource: Phase 1 implementation -- synthesizes a full option
  chain from stock price history alone. No options API needed.
- QCContractAdapter: Phase 2 stub -- will wrap QuantConnect's real historical
  option chain data. Same interface, so swapping is a one-line change.
- find_contract: source-agnostic contract selector (by delta or strike).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from statistics import NormalDist
import math


# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------

class OptionRight(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass
class OptionContract:
    strike: float
    expiry: date
    right: OptionRight
    bid: float
    ask: float
    last: float
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    underlying_price: float
    snapshot_date: date

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def days_to_expiry(self) -> int:
        return (self.expiry - self.snapshot_date).days


# ---------------------------------------------------------------------------
# Data source interface
# ---------------------------------------------------------------------------

class OptionDataSource(ABC):
    """Anything that can produce an option chain for a given underlying/date/spot."""

    @abstractmethod
    def get_chain(self, underlying: str, snapshot_date: date, spot_price: float) -> list[OptionContract]:
        """Return all available contracts for `underlying` as of `snapshot_date`."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Black-Scholes math (pure functions, no data dependency)
# ---------------------------------------------------------------------------

_N = NormalDist()


def black_scholes_price(S: float, K: float, T: float, r: float, sigma: float, right: OptionRight) -> float:
    """European option theoretical price. T in years, sigma annualized."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if right == OptionRight.CALL else max(0.0, K - S)

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if right == OptionRight.CALL:
        return S * _N.cdf(d1) - K * math.exp(-r * T) * _N.cdf(d2)
    return K * math.exp(-r * T) * _N.cdf(-d2) - S * _N.cdf(-d1)


def black_scholes_greeks(S: float, K: float, T: float, r: float, sigma: float, right: OptionRight) -> dict:
    """Returns delta, gamma, theta (per day), vega (per 1% vol move)."""
    if T <= 0 or sigma <= 0:
        if right == OptionRight.CALL:
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf_d1 = _N.pdf(d1)

    gamma = pdf_d1 / (S * sigma * sqrtT)
    vega = S * pdf_d1 * sqrtT / 100  # per 1 percentage-point change in vol

    if right == OptionRight.CALL:
        delta = _N.cdf(d1)
        theta = (-S * pdf_d1 * sigma / (2 * sqrtT) - r * K * math.exp(-r * T) * _N.cdf(d2)) / 365
    else:
        delta = _N.cdf(d1) - 1
        theta = (-S * pdf_d1 * sigma / (2 * sqrtT) + r * K * math.exp(-r * T) * _N.cdf(-d2)) / 365

    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


# ---------------------------------------------------------------------------
# Black-Scholes-based data source (Phase 1: no options API needed)
# ---------------------------------------------------------------------------

class BlackScholesSource(OptionDataSource):
    """
    Synthesizes a full option chain from stock price history alone.

    price_history:    dict[date, float] of historical closes, used to
                       estimate realized volatility.
    risk_free_rate:    annualized, e.g. 0.045 for 4.5%.
    strike_pct_range:  how far above/below spot to generate strikes (0.30 = +/-30%).
    strike_step_pct:   spacing between strikes as % of spot (0.025 = 2.5%).
    expiries_days:     day-offsets from snapshot_date to generate expiries for.
    vol_lookback_days: window for realized volatility calculation.
    """

    def __init__(
        self,
        price_history: dict,
        risk_free_rate: float = 0.045,
        strike_pct_range: float = 0.30,
        strike_step_pct: float = 0.025,
        expiries_days: list | None = None,
        vol_lookback_days: int = 30,
    ):
        self.price_history = price_history
        self.risk_free_rate = risk_free_rate
        self.strike_pct_range = strike_pct_range
        self.strike_step_pct = strike_step_pct
        self.expiries_days = expiries_days or [7, 14, 21, 30, 45, 60, 90, 180, 365]
        self.vol_lookback_days = vol_lookback_days

    def _realized_vol(self, as_of: date) -> float:
        """Annualized realized volatility from trailing daily log returns."""
        dates = sorted(d for d in self.price_history if d <= as_of)
        window = dates[-(self.vol_lookback_days + 1):]
        if len(window) < 2:
            return 0.30  # fallback if not enough history yet

        returns = []
        for i in range(1, len(window)):
            p0 = self.price_history[window[i - 1]]
            p1 = self.price_history[window[i]]
            if p0 > 0 and p1 > 0:
                returns.append(math.log(p1 / p0))

        if len(returns) < 2:
            return 0.30

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        annualized = math.sqrt(variance) * math.sqrt(252)
        return max(annualized, 0.05)  # floor: real stocks are never ~0% vol

    def get_chain(self, underlying: str, snapshot_date: date, spot_price: float) -> list[OptionContract]:
        sigma = self._realized_vol(snapshot_date)
        contracts = []
        num_steps = int(self.strike_pct_range / self.strike_step_pct)

        for i in range(-num_steps, num_steps + 1):
            strike = round(spot_price * (1 + i * self.strike_step_pct), 2)
            if strike <= 0:
                continue

            for days_out in self.expiries_days:
                expiry = snapshot_date + timedelta(days=days_out)
                T = days_out / 365.0

                for right in (OptionRight.CALL, OptionRight.PUT):
                    price = black_scholes_price(spot_price, strike, T, self.risk_free_rate, sigma, right)
                    greeks = black_scholes_greeks(spot_price, strike, T, self.risk_free_rate, sigma, right)

                    spread = max(0.01, price * 0.02)  # synthetic bid/ask spread
                    contracts.append(OptionContract(
                        strike=strike,
                        expiry=expiry,
                        right=right,
                        bid=round(max(0.0, price - spread / 2), 2),
                        ask=round(price + spread / 2, 2),
                        last=round(price, 2),
                        iv=sigma,
                        delta=greeks["delta"],
                        gamma=greeks["gamma"],
                        theta=greeks["theta"],
                        vega=greeks["vega"],
                        underlying_price=spot_price,
                        snapshot_date=snapshot_date,
                    ))

        return contracts


# ---------------------------------------------------------------------------
# QuantConnect adapter (Phase 2 -- stubbed for now)
# ---------------------------------------------------------------------------

class QCContractAdapter(OptionDataSource):
    """
    Will wrap QuantConnect's real historical option chain data, converting
    c.bid_price / c.greeks.delta / etc. into OptionContract instances.
    Same interface as BlackScholesSource, so swapping in is a one-line change
    wherever the data source is constructed.
    """

    def __init__(self, qc_algorithm_or_qb):
        self._qc = qc_algorithm_or_qb

    def get_chain(self, underlying: str, snapshot_date: date, spot_price: float) -> list[OptionContract]:
        raise NotImplementedError(
            "QCContractAdapter not implemented yet -- swap in once the "
            "Black-Scholes version has validated the product."
        )


# ---------------------------------------------------------------------------
# Source-agnostic contract selector
# ---------------------------------------------------------------------------

def find_contract(
    contracts: list,
    *,
    right: OptionRight | None = None,
    target_delta: float | None = None,
    target_strike: float | None = None,
    max_expiry_days: int | None = None,
    min_expiry_days: int | None = None,
):
    """
    Filters by right/expiry window, then returns the closest match to
    target_delta or target_strike (whichever is given). None if nothing matches.
    """
    candidates = list(contracts)

    if right is not None:
        candidates = [c for c in candidates if c.right == right]
    if max_expiry_days is not None:
        candidates = [c for c in candidates if c.days_to_expiry <= max_expiry_days]
    if min_expiry_days is not None:
        candidates = [c for c in candidates if c.days_to_expiry >= min_expiry_days]

    if not candidates:
        return None

    if target_delta is not None:
        return min(candidates, key=lambda c: abs(c.delta - target_delta))
    if target_strike is not None:
        return min(candidates, key=lambda c: abs(c.strike - target_strike))

    return candidates[0]
