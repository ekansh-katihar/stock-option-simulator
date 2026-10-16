"""
simulator/engine/portfolio.py

Owns the money math: opening/closing/settling legs and tracking cash.
Strategies (e.g. PMCCStrategy) decide *what* to trade; Portfolio just
records the consequences and answers P&L questions.

- Leg:       our own position (what we did). Outlives the OptionContract
             snapshot that was used to price it at entry.
- Portfolio: cash + list of legs, open and closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import uuid
import math

from simulator.data.option_pricer import OptionRight, OptionDataSource

CONTRACT_MULTIPLIER = 100  # 1 option contract = 100 shares


class LegAction(str, Enum):
    BUY = "BUY"    # we are long this leg (paid premium)
    SELL = "SELL"  # we are short this leg (received premium)


class LegStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class Leg:
    right: OptionRight
    action: LegAction
    strike: float
    expiry: date
    entry_date: date
    entry_price: float          # per-share price paid/received at entry
    quantity: int = 1
    status: LegStatus = LegStatus.OPEN
    close_date: date | None = None
    close_price: float | None = None
    realized_pnl: float | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    entry_fee: float = 0.0
    exit_fee: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.status == LegStatus.OPEN

    def to_dict(self) -> dict:
        return {
            "entry_fee": self.entry_fee,
            "exit_fee": self.exit_fee,
            "id": self.id,
            "right": self.right.value,
            "action": self.action.value,
            "strike": self.strike,
            "expiry": self.expiry.isoformat(),
            "entry_date": self.entry_date.isoformat(),
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "status": self.status.value,
            "close_date": self.close_date.isoformat() if self.close_date else None,
            "close_price": self.close_price,
            "realized_pnl": self.realized_pnl,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Leg":
        return cls(
            entry_fee=data.get("entry_fee", 0.0),
            exit_fee=data.get("exit_fee", 0.0),
            id=data["id"],
            right=OptionRight(data["right"]),
            action=LegAction(data["action"]),
            strike=data["strike"],
            expiry=date.fromisoformat(data["expiry"]),
            entry_date=date.fromisoformat(data["entry_date"]),
            entry_price=data["entry_price"],
            quantity=data["quantity"],
            status=LegStatus(data["status"]),
            close_date=date.fromisoformat(data["close_date"]) if data.get("close_date") else None,
            close_price=data.get("close_price"),
            realized_pnl=data.get("realized_pnl"),
        )


class Portfolio:

    def __init__(self, starting_cash: float):
        if not math.isfinite(starting_cash) or starting_cash < 0:
            raise ValueError("Starting cash must be finite and nonnegative.")
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self._legs: list[Leg] = []

    # ------------------------------------------------------------------
    # Opening / closing / settling
    # ------------------------------------------------------------------

    def open_leg(
        self,
        right: OptionRight,
        action: LegAction,
        strike: float,
        expiry: date,
        entry_date: date,
        price: float,
        quantity: int = 1,
        fee: float = 0.0,
    ) -> Leg:
        """Record a new leg. Buying debits cash, selling credits cash."""
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Quantity must be a positive integer.")
        if (not all(math.isfinite(v) for v in (price, fee, strike))
                or price < 0 or fee < 0 or strike <= 0 or expiry <= entry_date):
            raise ValueError("Invalid price, strike or expiry for opening a leg.")
        notional = price * CONTRACT_MULTIPLIER * quantity
        self.cash += (-notional if action == LegAction.BUY else notional) - fee

        leg = Leg(
            right=right, action=action, strike=strike, expiry=expiry,
            entry_date=entry_date, entry_price=price, quantity=quantity, entry_fee=fee,
        )
        self._legs.append(leg)
        return leg

    def settle_leg(self, leg: Leg, settlement_date: date, underlying_price: float) -> float:
        """Cash-settle a leg at expiry based on intrinsic value. Returns realized P&L."""
        if not leg.is_open:
            raise ValueError("Cannot settle a leg that is already closed.")

        intrinsic = (
            max(0.0, underlying_price - leg.strike) if leg.right == OptionRight.CALL
            else max(0.0, leg.strike - underlying_price)
        )
        notional_settlement = intrinsic * CONTRACT_MULTIPLIER * leg.quantity
        notional_entry = leg.entry_price * CONTRACT_MULTIPLIER * leg.quantity

        if leg.action == LegAction.BUY:
            # We paid entry premium; at settlement we receive intrinsic value.
            self.cash += notional_settlement
            realized_pnl = notional_settlement - notional_entry
        else:
            # We received entry premium; at settlement we owe intrinsic value.
            self.cash -= notional_settlement
            realized_pnl = notional_entry - notional_settlement

        leg.status = LegStatus.CLOSED
        leg.close_date = settlement_date
        leg.close_price = intrinsic
        leg.realized_pnl = realized_pnl - leg.entry_fee
        return leg.realized_pnl

    def close_leg(self, leg: Leg, close_date: date, close_price: float, fee: float = 0.0) -> float:
        """Close a leg early at a given market price (not intrinsic). Returns realized P&L."""
        if not leg.is_open:
            raise ValueError("Cannot close a leg that is already closed.")

        if not all(math.isfinite(v) for v in (close_price, fee)) or close_price < 0 or fee < 0:
            raise ValueError("Close price and fee must be nonnegative.")
        notional_close = close_price * CONTRACT_MULTIPLIER * leg.quantity
        notional_entry = leg.entry_price * CONTRACT_MULTIPLIER * leg.quantity

        if leg.action == LegAction.BUY:
            # Selling to close: receive close_price now.
            self.cash += notional_close
            realized_pnl = notional_close - notional_entry
        else:
            # Buying to close: pay close_price now.
            self.cash -= notional_close
            realized_pnl = notional_entry - notional_close

        self.cash -= fee
        leg.exit_fee = fee
        leg.status = LegStatus.CLOSED
        leg.close_date = close_date
        leg.close_price = close_price
        leg.realized_pnl = realized_pnl - leg.entry_fee - fee
        return leg.realized_pnl

    # ------------------------------------------------------------------
    # Valuation
    # ------------------------------------------------------------------

    def _find_matching_contract(self, leg: Leg, chain: list):
        return next((c for c in chain if c.right == leg.right
                     and c.strike == leg.strike and c.expiry == leg.expiry), None)

    def find_matching_contract(self, leg: Leg, as_of_date: date,
                               data_source: OptionDataSource, underlying: str, spot_price: float):
        quote = data_source.get_quote(underlying, as_of_date, spot_price,
                                      leg.right, leg.strike, leg.expiry)
        if quote is not None:
            if (quote.right, quote.strike, quote.expiry, quote.snapshot_date) != (
                    leg.right, leg.strike, leg.expiry, as_of_date):
                raise ValueError("Pricing source returned a different contract or quote date.")
            if (not all(math.isfinite(v) for v in (quote.bid, quote.ask))
                    or not 0 <= quote.bid <= quote.ask):
                raise ValueError("Pricing source returned invalid bid/ask prices.")
        return quote

    def close_market_price(self, leg: Leg, as_of_date: date,
                           data_source: OptionDataSource, underlying: str, spot_price: float) -> float:
        quote = self.find_matching_contract(leg, as_of_date, data_source, underlying, spot_price)
        if quote is None:
            raise ValueError(f"Missing quote for {leg.right.value} {leg.strike} {leg.expiry}.")
        return quote.bid if leg.action == LegAction.BUY else quote.ask

    def current_market_price(
        self,
        leg: Leg,
        as_of_date: date,
        data_source: OptionDataSource,
        underlying: str,
        spot_price: float,
    ) -> float | None:
        """Executable closing price: bid for longs, ask for shorts; None if missing."""
        contract = self.find_matching_contract(leg, as_of_date, data_source, underlying, spot_price)
        if contract is None:
            return None
        return contract.bid if leg.action == LegAction.BUY else contract.ask

    def mark_to_market(
        self,
        as_of_date: date,
        data_source: OptionDataSource,
        underlying: str,
        spot_price: float,
    ) -> float:
        """Unrealized P&L at closing bid/ask, including booked entry fees only."""
        open_legs = self.open_legs()
        if not open_legs:
            return 0.0

        total = 0.0

        for leg in open_legs:
            contract = self.find_matching_contract(leg, as_of_date, data_source, underlying, spot_price)
            if contract is None:
                raise ValueError(f"Missing quote for {leg.right.value} {leg.strike} {leg.expiry}.")

            current_price = contract.bid if leg.action == LegAction.BUY else contract.ask
            notional_current = current_price * CONTRACT_MULTIPLIER * leg.quantity
            notional_entry = leg.entry_price * CONTRACT_MULTIPLIER * leg.quantity

            if leg.action == LegAction.BUY:
                total += notional_current - notional_entry
            else:
                total += notional_entry - notional_current
            total -= leg.entry_fee

        return total

    # ------------------------------------------------------------------
    # Accessors / reporting
    # ------------------------------------------------------------------

    def open_legs(self) -> list[Leg]:
        return [l for l in self._legs if l.is_open]

    def closed_legs(self) -> list[Leg]:
        return [l for l in self._legs if not l.is_open]

    def total_realized_pnl(self) -> float:
        return sum(l.realized_pnl for l in self.closed_legs() if l.realized_pnl is not None)

    def total_unrealized_pnl(self, as_of_date: date, data_source: OptionDataSource,
                              underlying: str, spot_price: float) -> float:
        return self.mark_to_market(as_of_date, data_source, underlying, spot_price)

    def summary(self, as_of_date: date, data_source: OptionDataSource,
                underlying: str, spot_price: float) -> dict:
        unrealized = self.total_unrealized_pnl(as_of_date, data_source, underlying, spot_price)
        realized = self.total_realized_pnl()
        entry_value = sum((1 if leg.action == LegAction.BUY else -1)
                          * leg.entry_price * CONTRACT_MULTIPLIER * leg.quantity
                          for leg in self.open_legs())
        market_value = entry_value + unrealized + sum(l.entry_fee for l in self.open_legs())
        return {
            "cash": self.cash,
            "market_value": market_value,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_equity": self.cash + market_value,
            "open_leg_count": len(self.open_legs()),
            "closed_leg_count": len(self.closed_legs()),
        }

    # ------------------------------------------------------------------
    # Serialization -- for persistence (e.g. PostgresSessionStore)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "legs": [leg.to_dict() for leg in self._legs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Portfolio":
        # Current cash may be negative even with a positive starting balance.
        portfolio = cls(starting_cash=data.get("starting_cash", max(data["cash"], 0)))
        portfolio.cash = data["cash"]
        portfolio._legs = [Leg.from_dict(leg_data) for leg_data in data["legs"]]
        entry_value = sum((1 if l.action == LegAction.BUY else -1)
                          * l.entry_price * CONTRACT_MULTIPLIER * l.quantity
                          for l in portfolio.open_legs())
        portfolio.starting_cash = data.get(
            "starting_cash", portfolio.cash - portfolio.total_realized_pnl() + entry_value
            + sum(l.entry_fee for l in portfolio.open_legs()))
        return portfolio
