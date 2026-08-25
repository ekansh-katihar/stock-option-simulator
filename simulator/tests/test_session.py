"""
Tests for simulator/engine/session.py

Run: python -m simulator.tests.test_session
"""

import random
import unittest
from datetime import date, timedelta

from simulator.data.option_pricer import BlackScholesSource
from simulator.engine.session import TradingSession


def _make_price_series(start, days, start_price=180.0, seed=1, drift=0.0):
    rng = random.Random(seed)
    prices = {}
    price = start_price
    for i in range(days):
        price *= 1 + drift + rng.uniform(-0.015, 0.015)
        prices[start + timedelta(days=i)] = round(price, 2)
    return prices


class TestOpenAndSnapshot(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _make_price_series(self.start, 400)
        self.source = BlackScholesSource(price_history=self.prices)
        self.session = TradingSession("AAPL", self.source, self.prices, start_date=self.start)

    def test_open_long_populates_greeks(self):
        snap = self.session.open_long(target_delta=0.85, min_expiry_days=300)
        self.assertIsNotNone(snap["long_leg"])
        self.assertIsNotNone(snap["long_leg"]["delta"])
        self.assertGreater(snap["long_leg"]["delta"], 0.7)

    def test_open_short_by_strike(self):
        self.session.open_long(target_delta=0.85, min_expiry_days=300)
        snap = self.session.open_short(strike=self.session.spot_price * 1.05)
        self.assertIsNotNone(snap["short_leg"])
        self.assertEqual(snap["short_leg"]["action"], "SELL")

    def test_cannot_open_two_longs(self):
        self.session.open_long(target_delta=0.85, min_expiry_days=300)
        with self.assertRaises(ValueError):
            self.session.open_long(target_delta=0.85, min_expiry_days=300)

    def test_view_chain_returns_candidates(self):
        candidates = self.session.view_chain(max_expiry_days=35, limit=5)
        self.assertGreater(len(candidates), 0)
        self.assertLessEqual(len(candidates), 5)

    def test_view_chain_reaches_far_dated_contracts_without_limit(self):
        # Regression: previously, sorting by (expiry, strike) then truncating
        # to `limit` silently dropped everything past the first couple of
        # expiries. With limit=None, a 300-400 day window must return results.
        candidates = self.session.view_chain(min_expiry_days=300, max_expiry_days=400)
        self.assertGreater(len(candidates), 0)

    def test_view_chain_filters_by_delta_and_strike(self):
        candidates = self.session.view_chain(min_delta=0.4, max_delta=0.6,
                                              min_strike=150, max_strike=220)
        self.assertGreater(len(candidates), 0)
        for c in candidates:
            self.assertTrue(0.4 <= c["delta"] <= 0.6)
            self.assertTrue(150 <= c["strike"] <= 220)


class TestAdvanceAndSettlement(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _make_price_series(self.start, 400)
        self.source = BlackScholesSource(price_history=self.prices)
        self.session = TradingSession("AAPL", self.source, self.prices, start_date=self.start)
        self.session.open_long(target_delta=0.85, min_expiry_days=300)
        self.session.open_short(strike=self.session.spot_price * 1.05, max_expiry_days=35)
        self._short_expiry = self.session.snapshot()["short_leg"]["expiry"]

    def test_advance_settles_expired_short(self):
        days_to_expiry = (self._short_expiry - self.session.current_date).days + 1
        self.session.advance(days=days_to_expiry)
        snap = self.session.snapshot()
        self.assertIsNone(snap["short_leg"])  # settled and cleared

        settle_events = [e for e in self.session.history() if e["event"] == "settled"]
        self.assertEqual(len(settle_events), 1)

    def test_advance_does_not_auto_reopen(self):
        days_to_expiry = (self._short_expiry - self.session.current_date).days + 1
        self.session.advance(days=days_to_expiry)
        # No new short leg should exist until we explicitly open one
        self.assertIsNone(self.session.snapshot()["short_leg"])

    def test_advance_to_specific_date(self):
        target = self._short_expiry + timedelta(days=5)
        # snap to nearest available date
        while target not in self.prices:
            target += timedelta(days=1)
        self.session.advance(to_date=target)
        self.assertEqual(self.session.current_date, target)


class TestCloseAndRoll(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _make_price_series(self.start, 400)
        self.source = BlackScholesSource(price_history=self.prices)
        self.session = TradingSession("AAPL", self.source, self.prices, start_date=self.start)
        self.session.open_long(target_delta=0.85, min_expiry_days=300)
        self.session.open_short(strike=self.session.spot_price * 1.05, max_expiry_days=35)

    def test_close_short_early(self):
        snap = self.session.close_short()
        self.assertIsNone(snap["short_leg"])
        closed_events = [e for e in self.session.history() if e["event"] == "closed_short"]
        self.assertEqual(len(closed_events), 1)

    def test_close_then_open_short_replaces_leg(self):
        self.session.close_short()
        self.session.advance(days=5)
        new_spot = self.session.spot_price
        snap = self.session.open_short(strike=new_spot * 1.08)
        self.assertIsNotNone(snap["short_leg"])
        self.assertIsNotNone(snap["short_leg"]["expiry"])

    def test_close_then_open_long_at_new_strike(self):
        self.session.close_long()
        self.session.advance(days=30)
        snap = self.session.open_long(target_delta=0.85, min_expiry_days=250)
        self.assertIsNotNone(snap["long_leg"])

    def test_close_both_legs(self):
        self.session.close_short()
        snap = self.session.close_long()
        self.assertIsNone(snap["long_leg"])
        self.assertIsNone(snap["short_leg"])


class TestStopLoss(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _make_price_series(self.start, 60)
        self.source = BlackScholesSource(price_history=self.prices)
        self.session = TradingSession("AAPL", self.source, self.prices, start_date=self.start)
        self.session.open_long(target_delta=0.85, min_expiry_days=300)

    def test_no_trigger_when_close_to_strike(self):
        strike = self.session.snapshot()["long_leg"]["strike"]
        result = self.session.check_stop_loss(stop_loss_pct=0.10)
        # spot at session start is well above a deep-ITM strike, shouldn't trigger
        self.assertFalse(result["triggered"])

    def test_no_long_leg_returns_false(self):
        empty_session = TradingSession("AAPL", self.source, self.prices, start_date=self.start)
        result = empty_session.check_stop_loss()
        self.assertFalse(result["triggered"])


if __name__ == "__main__":
    unittest.main(verbosity=2)