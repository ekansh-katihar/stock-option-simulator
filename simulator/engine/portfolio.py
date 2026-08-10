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

    @property
    def is_open(self) -> bool:
        return self.status == LegStatus.OPEN


class Portfolio:

    def __init__(self, starting_cash: float):
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
    ) -> Leg:
        """Record a new leg. Buying debits cash, selling credits cash."""
        notional = price * CONTRACT_MULTIPLIER * quantity
        self.cash += -notional if action == LegAction.BUY else notional

        leg = Leg(
            right=right, action=action, strike=strike, expiry=expiry,
            entry_date=entry_date, entry_price=price, quantity=quantity,
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
        leg.realized_pnl = realized_pnl
        return realized_pnl

    def close_leg(self, leg: Leg, close_date: date, close_price: float) -> float:
        """Close a leg early at a given market price (not intrinsic). Returns realized P&L."""
        if not leg.is_open:
            raise ValueError("Cannot close a leg that is already closed.")

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

        leg.status = LegStatus.CLOSED
        leg.close_date = close_date
        leg.close_price = close_price
        leg.realized_pnl = realized_pnl
        return realized_pnl

    # ------------------------------------------------------------------
    # Valuation
    # ------------------------------------------------------------------

    def _find_matching_contract(self, leg: Leg, chain: list):
        """Best-effort match: same right, closest expiry, then closest strike."""
        candidates = [c for c in chain if c.right == leg.right]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (abs((c.expiry - leg.expiry).days), abs(c.strike - leg.strike)))
        return candidates[0]

    def find_matching_contract(self, leg: Leg, as_of_date: date,
                                data_source: OptionDataSource, underlying: str, spot_price: float):
        """Public lookup: best-effort matching live contract for a leg (full OptionContract, not just price)."""
        chain = data_source.get_chain(underlying, as_of_date, spot_price)
        return self._find_matching_contract(leg, chain)

    def current_market_price(
        self,
        leg: Leg,
        as_of_date: date,
        data_source: OptionDataSource,
        underlying: str,
        spot_price: float,
    ) -> float | None:
        """Current mid-price for a single leg, or None if no matching contract found."""
        contract = self.find_matching_contract(leg, as_of_date, data_source, underlying, spot_price)
        return contract.mid if contract is not None else None

    def mark_to_market(
        self,
        as_of_date: date,
        data_source: OptionDataSource,
        underlying: str,
        spot_price: float,
    ) -> float:
        """Unrealized P&L across all open legs, valued at current market price."""
        open_legs = self.open_legs()
        if not open_legs:
            return 0.0

        chain = data_source.get_chain(underlying, as_of_date, spot_price)
        total = 0.0

        for leg in open_legs:
            contract = self._find_matching_contract(leg, chain)
            if contract is None:
                continue  # no market data available for this leg right now

            current_price = contract.mid
            notional_current = current_price * CONTRACT_MULTIPLIER * leg.quantity
            notional_entry = leg.entry_price * CONTRACT_MULTIPLIER * leg.quantity

            if leg.action == LegAction.BUY:
                total += notional_current - notional_entry
            else:
                total += notional_entry - notional_current

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
        return {
            "cash": self.cash,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_equity": self.cash + unrealized,
            "open_leg_count": len(self.open_legs()),
            "closed_leg_count": len(self.closed_legs()),
        }