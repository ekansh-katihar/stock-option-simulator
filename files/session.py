"""
simulator/engine/session.py

Stateful, interactive Python API for manually driving a PMCC position --
open legs, advance time, roll or close early, all on your own schedule
instead of the fully-automated BacktestEngine. Designed so the exact same
command set could later be handed to an LLM as tool definitions (Phase 2)
without changing this file.

Typical use (from a script or notebook):

    from datetime import date
    from simulator.data.price_history import fetch_price_history
    from simulator.data.option_pricer import BlackScholesSource
    from simulator.engine.session import TradingSession

    prices = fetch_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
    source = BlackScholesSource(price_history=prices)
    session = TradingSession("AAPL", source, prices)

    session.open_long(target_delta=0.85, min_expiry_days=300)
    session.view_chain(max_expiry_days=35)          # browse before choosing
    session.open_short(strike=195)
    session.advance(days=25)                         # settles at expiry if crossed
    session.snapshot()                                # current state
    session.history()                                 # full event log

    # "Rolling" is just close + open, composed by you:
    #   session.close_short()
    #   session.open_short(strike=210)
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from simulator.data.option_pricer import OptionRight, OptionDataSource, find_contract, filter_contracts
from simulator.engine.portfolio import Portfolio, Leg, LegAction
from simulator.strategies.pmcc import PMCCConfig


class TradingSession:

    def __init__(
        self,
        underlying: str,
        data_source: OptionDataSource,
        price_history: dict,
        portfolio: Portfolio | None = None,
        start_date: date | None = None,
        config: PMCCConfig | None = None,
    ):
        self._underlying = underlying
        self._data_source = data_source
        self._price_history = price_history
        self._portfolio = portfolio or Portfolio(starting_cash=100_000)
        self._config = config or PMCCConfig()
        self._current_date = start_date or min(price_history)

        self._long_leg: Leg | None = None
        self._short_leg: Leg | None = None
        self._log: list[dict] = []

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    @property
    def current_date(self) -> date:
        return self._current_date

    @property
    def price_history(self) -> dict:
        return self._price_history

    @property
    def underlying(self) -> str:
        return self._underlying

    @property
    def price_history_bounds(self) -> tuple:
        """(earliest, latest) dates available in this session's price history."""
        return min(self._price_history), max(self._price_history)

    @property
    def spot_price(self) -> float:
        return self._price_history[self._current_date]

    def _quote_for(self, leg: Leg | None) -> dict | None:
        if leg is None:
            return None
        contract = self._portfolio.find_matching_contract(
            leg, self._current_date, self._data_source, self._underlying, self.spot_price
        )
        return {
            "right": leg.right.value,
            "action": leg.action.value,
            "strike": leg.strike,
            "expiry": leg.expiry,
            "quantity": leg.quantity,
            "entry_date": leg.entry_date,
            "entry_price": leg.entry_price,
            "status": leg.status.value,
            "current_price": contract.mid if contract else None,
            "delta": contract.delta if contract else None,
            "gamma": contract.gamma if contract else None,
            "theta": contract.theta if contract else None,
            "vega": contract.vega if contract else None,
            "iv": contract.iv if contract else None,
        }

    def snapshot(self) -> dict:
        """Current stock price, both legs' live pricing/greeks, and portfolio totals."""
        summary = self._portfolio.summary(
            self._current_date, self._data_source, self._underlying, self.spot_price
        )
        return {
            "date": self._current_date,
            "spot_price": self.spot_price,
            "long_leg": self._quote_for(self._long_leg),
            "short_leg": self._quote_for(self._short_leg),
            "cash": summary["cash"],
            "realized_pnl": summary["realized_pnl"],
            "unrealized_pnl": summary["unrealized_pnl"],
            "total_equity": summary["total_equity"],
        }

    def history(self) -> list:
        """Every event recorded so far (opens, closes, settles, rolls) -- chart-ready."""
        return list(self._log)

    def view_chain(self, right: OptionRight = OptionRight.CALL,
                    min_strike: float | None = None, max_strike: float | None = None,
                    min_delta: float | None = None, max_delta: float | None = None,
                    min_expiry_days: int | None = None, max_expiry_days: int | None = None,
                    limit: int | None = None) -> list:
        """
        Browse available contracts on the current date, filtered by any
        combination of strike, delta, and expiry-days bounds. Returns ALL
        matches (sorted by expiry then strike) unless `limit` is given --
        `limit` is a display cap only, applied AFTER filtering, never a
        reason for a contract to silently disappear. Callers doing their
        own pagination (e.g. a UI) should leave `limit` as None and slice
        the full returned list themselves.
        """
        chain = self._data_source.get_chain(self._underlying, self._current_date, self.spot_price)
        candidates = filter_contracts(
            chain, right=right,
            min_strike=min_strike, max_strike=max_strike,
            min_delta=min_delta, max_delta=max_delta,
            min_expiry_days=min_expiry_days, max_expiry_days=max_expiry_days,
        )
        candidates.sort(key=lambda c: (c.expiry, c.strike))
        if limit is not None:
            candidates = candidates[:limit]
        return [
            {"strike": c.strike, "expiry": c.expiry, "bid": c.bid, "ask": c.ask,
             "delta": c.delta, "iv": c.iv}
            for c in candidates
        ]

    # ------------------------------------------------------------------
    # Time
    # ------------------------------------------------------------------

    def advance(self, days: int | None = None, to_date: date | None = None) -> dict:
        """
        Move the session forward. Any leg whose expiry is crossed along the
        way is automatically settled at its own expiry-day price ("let it
        settle/expire ITM") -- it does NOT auto-reopen a replacement; that's
        your call via open_long/open_short.
        """
        if to_date is None:
            if days is None:
                raise ValueError("Provide either days or to_date.")
            to_date = self._current_date + timedelta(days=days)

        crossed = sorted(d for d in self._price_history if self._current_date < d <= to_date)
        for d in crossed:
            spot = self._price_history[d]
            for attr in ("_long_leg", "_short_leg"):
                leg = getattr(self, attr)
                if leg is not None and leg.is_open and d >= leg.expiry:
                    pnl = self._portfolio.settle_leg(leg, d, spot)
                    self._log.append({"date": d, "event": "settled", "which": attr.strip("_"),
                                       "strike": leg.strike, "pnl": pnl})
                    setattr(self, attr, None)
            self._current_date = d

        return self.snapshot()

    # ------------------------------------------------------------------
    # Long leg
    # ------------------------------------------------------------------

    def open_long(self, strike: float | None = None, target_delta: float | None = None,
                   min_expiry_days: int | None = None, max_expiry_days: int | None = None,
                   quantity: int = 1) -> dict:
        if self._long_leg is not None and self._long_leg.is_open:
            raise ValueError("Long leg already open; close it first.")

        chain = self._data_source.get_chain(self._underlying, self._current_date, self.spot_price)
        contract = find_contract(
            chain, right=OptionRight.CALL,
            target_strike=strike,
            target_delta=target_delta if strike is None else None,
            min_expiry_days=min_expiry_days or self._config.long_min_expiry_days,
            max_expiry_days=max_expiry_days,
        ) if (strike is not None or target_delta is not None) else find_contract(
            chain, right=OptionRight.CALL,
            target_delta=self._config.long_target_delta,
            min_expiry_days=min_expiry_days or self._config.long_min_expiry_days,
            max_expiry_days=max_expiry_days,
        )
        if contract is None:
            raise ValueError("No matching long call contract found for the given criteria.")

        leg = self._portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.BUY, strike=contract.strike,
            expiry=contract.expiry, entry_date=self._current_date, price=contract.ask,
            quantity=quantity,
        )
        self._long_leg = leg
        self._log.append({"date": self._current_date, "event": "opened_long",
                           "strike": leg.strike, "expiry": leg.expiry, "price": leg.entry_price})
        return self.snapshot()

    def close_long(self) -> dict:
        if self._long_leg is None:
            raise ValueError("No long leg is currently open.")

        price = self._portfolio.current_market_price(
            self._long_leg, self._current_date, self._data_source, self._underlying, self.spot_price
        )
        if price is None:
            price = max(0.0, self.spot_price - self._long_leg.strike)

        pnl = self._portfolio.close_leg(self._long_leg, self._current_date, price)
        self._log.append({"date": self._current_date, "event": "closed_long",
                           "strike": self._long_leg.strike, "pnl": pnl})
        self._long_leg = None
        return self.snapshot()

    # ------------------------------------------------------------------
    # Short leg
    # ------------------------------------------------------------------

    def open_short(self, strike: float | None = None, target_delta: float | None = None,
                    min_expiry_days: int | None = None, max_expiry_days: int | None = None,
                    quantity: int = 1) -> dict:
        if self._short_leg is not None and self._short_leg.is_open:
            raise ValueError("Short leg already open; close it first.")

        if strike is None and target_delta is None:
            strike = self.spot_price * (1 + self._config.short_otm_pct)

        chain = self._data_source.get_chain(self._underlying, self._current_date, self.spot_price)
        contract = find_contract(
            chain, right=OptionRight.CALL,
            target_strike=strike, target_delta=target_delta,
            min_expiry_days=min_expiry_days or self._config.short_min_expiry_days,
            max_expiry_days=max_expiry_days or self._config.short_max_expiry_days,
        )
        if contract is None:
            raise ValueError("No matching short call contract found for the given criteria.")

        leg = self._portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.SELL, strike=contract.strike,
            expiry=contract.expiry, entry_date=self._current_date, price=contract.bid,
            quantity=quantity,
        )
        self._short_leg = leg
        self._log.append({"date": self._current_date, "event": "opened_short",
                           "strike": leg.strike, "expiry": leg.expiry, "price": leg.entry_price})
        return self.snapshot()

    def close_short(self) -> dict:
        if self._short_leg is None:
            raise ValueError("No short leg is currently open.")

        price = self._portfolio.current_market_price(
            self._short_leg, self._current_date, self._data_source, self._underlying, self.spot_price
        )
        if price is None:
            price = max(0.0, self.spot_price - self._short_leg.strike)

        pnl = self._portfolio.close_leg(self._short_leg, self._current_date, price)
        self._log.append({"date": self._current_date, "event": "closed_short",
                           "strike": self._short_leg.strike, "pnl": pnl})
        self._short_leg = None
        return self.snapshot()

    # ------------------------------------------------------------------
    # Read-only checks
    # ------------------------------------------------------------------

    def check_stop_loss(self, stop_loss_pct: float | None = None) -> dict:
        """Read-only check -- does not act. You decide what to do with the result."""
        pct = stop_loss_pct if stop_loss_pct is not None else self._config.stop_loss_pct
        if self._long_leg is None:
            return {"triggered": False, "reason": "no long leg open"}
        threshold = self._long_leg.strike * (1 - pct)
        triggered = self.spot_price < threshold
        return {"triggered": triggered, "threshold": threshold, "spot_price": self.spot_price,
                "long_strike": self._long_leg.strike}

    # ------------------------------------------------------------------
    # Serialization -- for persistence (e.g. PostgresSessionStore).
    # price_history is intentionally NOT included here -- it's the same
    # shape/size as the OptionDataSource's own input, so the store persists
    # it separately (see PostgresSessionStore) rather than duplicating it
    # inside this dict.
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "underlying": self._underlying,
            "current_date": self._current_date.isoformat(),
            "config": asdict(self._config),
            "log": _encode_dates(self._log),
            "portfolio": self._portfolio.to_dict(),
            "long_leg_id": self._long_leg.id if self._long_leg else None,
            "short_leg_id": self._short_leg.id if self._short_leg else None,
        }

    @classmethod
    def from_dict(cls, data: dict, data_source: OptionDataSource, price_history: dict) -> "TradingSession":
        portfolio = Portfolio.from_dict(data["portfolio"])
        session = cls(
            underlying=data["underlying"],
            data_source=data_source,
            price_history=price_history,
            portfolio=portfolio,
            start_date=date.fromisoformat(data["current_date"]),
            config=PMCCConfig(**data["config"]),
        )
        session._log = _decode_dates(data["log"])

        legs_by_id = {leg.id: leg for leg in portfolio.open_legs() + portfolio.closed_legs()}
        session._long_leg = legs_by_id.get(data["long_leg_id"])
        session._short_leg = legs_by_id.get(data["short_leg_id"])
        return session


def _encode_dates(obj):
    """Recursively convert date objects to a JSON-safe marker form."""
    if isinstance(obj, date):
        return {"__date__": obj.isoformat()}
    if isinstance(obj, dict):
        return {k: _encode_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_encode_dates(v) for v in obj]
    return obj


def _decode_dates(obj):
    """Reverse of _encode_dates."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"__date__"}:
            return date.fromisoformat(obj["__date__"])
        return {k: _decode_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_dates(v) for v in obj]
    return obj
