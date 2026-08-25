"""
Tests for to_dict/from_dict round-trips on Leg, Portfolio, and TradingSession.
These don't need a real database -- they verify the serialization logic that
PostgresSessionStore will rely on.

Run: python -m simulator.tests.test_serialization
"""

import random
import unittest
from datetime import date, timedelta

from simulator.data.option_pricer import OptionRight, BlackScholesSource
from simulator.engine.portfolio import Portfolio, Leg, LegAction, LegStatus
from simulator.engine.session import TradingSession
from simulator.strategies.pmcc import PMCCConfig


def _price_history(days=400, seed=1):
    rng = random.Random(seed)
    price = 180.0
    prices = {}
    for i in range(days):
        price *= 1 + rng.uniform(-0.015, 0.015)
        prices[date(2024, 1, 1) + timedelta(days=i)] = round(price, 2)
    return prices


class TestLegRoundTrip(unittest.TestCase):

    def test_open_leg_round_trips(self):
        leg = Leg(right=OptionRight.CALL, action=LegAction.BUY, strike=150.0,
                  expiry=date(2024, 12, 20), entry_date=date(2024, 1, 2), entry_price=25.0)
        restored = Leg.from_dict(leg.to_dict())
        self.assertEqual(restored.id, leg.id)
        self.assertEqual(restored.strike, leg.strike)
        self.assertEqual(restored.expiry, leg.expiry)
        self.assertEqual(restored.status, LegStatus.OPEN)
        self.assertIsNone(restored.close_date)

    def test_closed_leg_round_trips(self):
        leg = Leg(right=OptionRight.CALL, action=LegAction.SELL, strike=190.0,
                  expiry=date(2024, 2, 16), entry_date=date(2024, 2, 1), entry_price=3.0)
        leg.status = LegStatus.CLOSED
        leg.close_date = date(2024, 2, 16)
        leg.close_price = 0.0
        leg.realized_pnl = 300.0

        restored = Leg.from_dict(leg.to_dict())
        self.assertEqual(restored.status, LegStatus.CLOSED)
        self.assertEqual(restored.close_date, date(2024, 2, 16))
        self.assertEqual(restored.realized_pnl, 300.0)


class TestPortfolioRoundTrip(unittest.TestCase):

    def test_empty_portfolio_round_trips(self):
        p = Portfolio(starting_cash=100_000)
        restored = Portfolio.from_dict(p.to_dict())
        self.assertEqual(restored.cash, 100_000)
        self.assertEqual(len(restored.open_legs()), 0)

    def test_portfolio_with_legs_round_trips(self):
        p = Portfolio(starting_cash=100_000)
        leg = p.open_leg(right=OptionRight.CALL, action=LegAction.BUY, strike=150.0,
                          expiry=date(2024, 12, 20), entry_date=date(2024, 1, 2), price=25.0)
        p.settle_leg(leg, date(2024, 12, 20), underlying_price=180.0)

        restored = Portfolio.from_dict(p.to_dict())
        self.assertAlmostEqual(restored.cash, p.cash)
        self.assertEqual(len(restored.closed_legs()), 1)
        self.assertEqual(restored.closed_legs()[0].id, leg.id)
        self.assertAlmostEqual(restored.closed_legs()[0].realized_pnl, leg.realized_pnl)


class TestTradingSessionRoundTrip(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _price_history()
        self.source = BlackScholesSource(price_history=self.prices)

    def test_fresh_session_round_trips(self):
        session = TradingSession("AAPL", self.source, self.prices, start_date=self.start)
        data = session.to_dict()

        restored_source = BlackScholesSource(price_history=self.prices)
        restored = TradingSession.from_dict(data, data_source=restored_source, price_history=self.prices)

        self.assertEqual(restored.underlying, "AAPL")
        self.assertEqual(restored.current_date, session.current_date)
        self.assertIsNone(restored.snapshot()["long_leg"])
        self.assertIsNone(restored.snapshot()["short_leg"])

    def test_session_with_open_legs_round_trips(self):
        session = TradingSession("AAPL", self.source, self.prices, start_date=self.start,
                                  config=PMCCConfig(short_min_expiry_days=20, short_max_expiry_days=35))
        entry_date = self.start + timedelta(days=10)
        # advance the session's internal clock by opening at a later date directly
        session._current_date = entry_date  # test-only shortcut; normal usage is via advance()
        session.open_long(target_delta=0.85, min_expiry_days=300)
        session.open_short(strike=session.spot_price * 1.05)

        data = session.to_dict()
        restored_source = BlackScholesSource(price_history=self.prices)
        restored = TradingSession.from_dict(data, data_source=restored_source, price_history=self.prices)

        orig_snap = session.snapshot()
        restored_snap = restored.snapshot()

        self.assertIsNotNone(restored_snap["long_leg"])
        self.assertIsNotNone(restored_snap["short_leg"])
        self.assertEqual(restored_snap["long_leg"]["strike"], orig_snap["long_leg"]["strike"])
        self.assertEqual(restored_snap["short_leg"]["strike"], orig_snap["short_leg"]["strike"])
        self.assertAlmostEqual(restored_snap["cash"], orig_snap["cash"])

    def test_session_history_log_round_trips_with_dates(self):
        session = TradingSession("AAPL", self.source, self.prices, start_date=self.start)
        session.open_long(target_delta=0.85, min_expiry_days=300)
        session.open_short(strike=session.spot_price * 1.05, max_expiry_days=35)

        data = session.to_dict()
        restored_source = BlackScholesSource(price_history=self.prices)
        restored = TradingSession.from_dict(data, data_source=restored_source, price_history=self.prices)

        self.assertEqual(len(restored.history()), len(session.history()))
        for orig_event, restored_event in zip(session.history(), restored.history()):
            self.assertEqual(orig_event["date"], restored_event["date"])
            self.assertIsInstance(restored_event["date"], date)
            if "expiry" in orig_event:
                self.assertEqual(orig_event["expiry"], restored_event["expiry"])
                self.assertIsInstance(restored_event["expiry"], date)

    def test_session_after_settlement_round_trips(self):
        session = TradingSession("AAPL", self.source, self.prices, start_date=self.start,
                                  config=PMCCConfig(short_min_expiry_days=20, short_max_expiry_days=35))
        session.open_long(target_delta=0.85, min_expiry_days=300)
        session.open_short(strike=session.spot_price * 1.05)
        short_expiry = session.snapshot()["short_leg"]["expiry"]

        target = short_expiry
        while target not in self.prices:
            target += timedelta(days=1)
        session.advance(to_date=target)

        data = session.to_dict()
        restored_source = BlackScholesSource(price_history=self.prices)
        restored = TradingSession.from_dict(data, data_source=restored_source, price_history=self.prices)

        self.assertIsNone(restored.snapshot()["short_leg"])  # settled and cleared
        self.assertIsNotNone(restored.snapshot()["long_leg"])
        self.assertAlmostEqual(restored.snapshot()["realized_pnl"], session.snapshot()["realized_pnl"])


if __name__ == "__main__":
    unittest.main(verbosity=2)