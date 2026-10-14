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
- filter_contracts: source-agnostic multi-bound filter (delta/strike/expiry/
  liquidity/iv_rank), used for range-based selection.

MOCK DATA NOTE: BlackScholesSource has no real market to pull open_interest,
volume, or true implied-vol-rank history from. Fields below marked "MOCK"
are heuristic placeholders so that liquidity/IV-rank config filters have
something meaningful to act on today. Replace them once a real data source
(e.g. QCContractAdapter, or a paid chain-data provider) is wired in -- no
other code should need to change, since they live behind the same
OptionContract/OptionDataSource interface.
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

    # --- MOCK fields (see module docstring) ---
    open_interest: int = 0     # MOCK -- synthetic liquidity proxy
    volume: int = 0            # MOCK -- synthetic liquidity proxy
    iv_rank: float = 0.5       # MOCK -- realized-vol percentile, proxy for true IV rank

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def days_to_expiry(self) -> int:
        return (self.expiry - self.snapshot_date).days

    @property
    def spread_pct(self) -> float:
        """(ask - bid) / mid, as a fraction. 0.0 if mid is 0 to avoid a division error."""
        return (self.ask - self.bid) / self.mid if self.mid > 0 else 0.0


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
        self.expiries_days = expiries_days or [7, 14, 21, 30, 45, 60, 90, 180, 365, 545, 730]
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

    def _iv_rank(self, as_of: date, current_vol: float, lookback_days: int = 252, sample_every: int = 5) -> float:
        """
        MOCK IV rank. There is no real implied-vol history behind a synthetic
        chain, so this approximates "IV rank" as the percentile of today's
        realized volatility against a trailing distribution of realized
        volatility -- sampled every `sample_every` days to keep this cheap
        over a year-long lookback. Replace with true IV rank (computed from
        real chain history) once a real data source is wired in.
        """
        dates = sorted(d for d in self.price_history if d <= as_of)
        if len(dates) < 30:
            return 0.5  # not enough history to rank meaningfully -- assume mid-range

        window_dates = dates[-lookback_days:]
        sample_dates = window_dates[::sample_every] or window_dates

        historical_vols = [self._realized_vol(d) for d in sample_dates]
        below_or_equal = sum(1 for v in historical_vols if v <= current_vol)
        return below_or_equal / len(historical_vols)

    def _mock_liquidity(self, strike: float, spot_price: float, days_to_expiry: int) -> tuple[int, int]:
        """
        MOCK liquidity heuristic. No real open interest/volume exists for a
        synthetic chain -- this approximates the real-world pattern (higher
        OI/volume near-the-money and near-term, tapering off further out and
        further out in time) so liquidity filters in config have something
        meaningful to act on. Replace once a real data source is wired in.
        """
        moneyness_distance = abs(strike - spot_price) / spot_price if spot_price > 0 else 1.0
        moneyness_factor = max(0.0, 1 - moneyness_distance / self.strike_pct_range)

        # Near-term contracts (weeklies/monthlies) trade far more than LEAPS.
        tenor_factor = max(0.1, 1 - days_to_expiry / 730)

        base_open_interest = 5000
        base_volume = 500

        open_interest = int(base_open_interest * moneyness_factor * tenor_factor)
        volume = int(base_volume * moneyness_factor * tenor_factor)
        return open_interest, volume

    def get_chain(self, underlying: str, snapshot_date: date, spot_price: float) -> list[OptionContract]:
        sigma = self._realized_vol(snapshot_date)
        iv_rank = self._iv_rank(snapshot_date, sigma)  # MOCK -- same value across the whole chain, see docstring

        contracts = []
        num_steps = int(self.strike_pct_range / self.strike_step_pct)

        for i in range(-num_steps, num_steps + 1):
            strike = round(spot_price * (1 + i * self.strike_step_pct), 2)
            if strike <= 0:
                continue

            for days_out in self.expiries_days:
                expiry = snapshot_date + timedelta(days=days_out)
                T = days_out / 365.0
                open_interest, volume = self._mock_liquidity(strike, spot_price, days_out)

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
                        open_interest=open_interest,
                        volume=volume,
                        iv_rank=iv_rank,
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
    wherever the data source is constructed. Once real data is available,
    open_interest/volume/iv_rank should come from the real chain instead of
    the MOCK heuristics in BlackScholesSource above.
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

def filter_contracts(
    contracts: list,
    *,
    right: OptionRight | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_delta: float | None = None,
    max_delta: float | None = None,
    min_expiry_days: int | None = None,
    max_expiry_days: int | None = None,
    min_open_interest: int | None = None,
    max_spread_pct: float | None = None,
    min_iv_rank: float | None = None,
) -> list:
    """
    Returns ALL contracts matching every given bound (inclusive) -- never
    truncates. Unlike find_contract (which picks the single closest match
    to a target), this is for browsing/paginating a full range of
    candidates, or for range-based selection (e.g. delta_range from config).
    Callers are responsible for any display-level pagination.
    """
    result = list(contracts)
    if right is not None:
        result = [c for c in result if c.right == right]
    if min_strike is not None:
        result = [c for c in result if c.strike >= min_strike]
    if max_strike is not None:
        result = [c for c in result if c.strike <= max_strike]
    if min_delta is not None:
        result = [c for c in result if c.delta >= min_delta]
    if max_delta is not None:
        result = [c for c in result if c.delta <= max_delta]
    if min_expiry_days is not None:
        result = [c for c in result if c.days_to_expiry >= min_expiry_days]
    if max_expiry_days is not None:
        result = [c for c in result if c.days_to_expiry <= max_expiry_days]
    if min_open_interest is not None:
        result = [c for c in result if c.open_interest >= min_open_interest]
    if max_spread_pct is not None:
        result = [c for c in result if c.spread_pct <= max_spread_pct]
    if min_iv_rank is not None:
        result = [c for c in result if c.iv_rank >= min_iv_rank]
    return result


def find_contract(
    contracts: list,
    *,
    right: OptionRight | None = None,
    target_delta: float | None = None,
    target_strike: float | None = None,
    max_expiry_days: int | None = None,
    min_expiry_days: int | None = None,
    min_open_interest: int | None = None,
    max_spread_pct: float | None = None,
):
    """
    Filters by right/expiry window/liquidity, then returns the closest match
    to target_delta or target_strike (whichever is given). None if nothing matches.
    """
    candidates = list(contracts)

    if right is not None:
        candidates = [c for c in candidates if c.right == right]
    if max_expiry_days is not None:
        candidates = [c for c in candidates if c.days_to_expiry <= max_expiry_days]
    if min_expiry_days is not None:
        candidates = [c for c in candidates if c.days_to_expiry >= min_expiry_days]
    if min_open_interest is not None:
        candidates = [c for c in candidates if c.open_interest >= min_open_interest]
    if max_spread_pct is not None:
        candidates = [c for c in candidates if c.spread_pct <= max_spread_pct]

    if not candidates:
        return None

    if target_delta is not None:
        return min(candidates, key=lambda c: abs(c.delta - target_delta))
    if target_strike is not None:
        return min(candidates, key=lambda c: abs(c.strike - target_strike))

    return candidates[0]