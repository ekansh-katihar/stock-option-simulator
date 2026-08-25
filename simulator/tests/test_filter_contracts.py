"""
Tests for filter_contracts() in simulator/data/option_pricer.py

Run: python -m simulator.tests.test_filter_contracts
"""

import random
import unittest
from datetime import date, timedelta

from simulator.data.option_pricer import OptionRight, BlackScholesSource, filter_contracts


def _price_history(days=400, seed=1):
    rng = random.Random(seed)
    price = 180.0
    prices = {}
    for i in range(days):
        price *= 1 + rng.uniform(-0.015, 0.015)
        prices[date(2024, 1, 1) + timedelta(days=i)] = round(price, 2)
    return prices


class TestFilterContracts(unittest.TestCase):

    def setUp(self):
        self.prices = _price_history()
        self.source = BlackScholesSource(price_history=self.prices)
        self.snapshot_date = date(2024, 1, 2)
        self.spot = self.prices[self.snapshot_date]
        self.chain = self.source.get_chain("AAPL", self.snapshot_date, self.spot)

    def test_no_filters_returns_everything(self):
        result = filter_contracts(self.chain)
        self.assertEqual(len(result), len(self.chain))

    def test_filters_by_right(self):
        result = filter_contracts(self.chain, right=OptionRight.CALL)
        self.assertTrue(all(c.right == OptionRight.CALL for c in result))
        self.assertGreater(len(result), 0)

    def test_filters_by_strike_range(self):
        result = filter_contracts(self.chain, min_strike=170, max_strike=190)
        self.assertTrue(all(170 <= c.strike <= 190 for c in result))
        self.assertGreater(len(result), 0)

    def test_filters_by_delta_range(self):
        result = filter_contracts(self.chain, right=OptionRight.CALL,
                                   min_delta=0.4, max_delta=0.6)
        self.assertTrue(all(0.4 <= c.delta <= 0.6 for c in result))

    def test_filters_by_expiry_days_reaches_far_dated_contracts(self):
        # This is the exact regression: a wide expiry window must return
        # contracts near its upper bound, not just the earliest ones.
        result = filter_contracts(self.chain, right=OptionRight.CALL,
                                   min_expiry_days=300, max_expiry_days=400)
        self.assertGreater(len(result), 0)
        self.assertTrue(all(300 <= c.days_to_expiry <= 400 for c in result))

    def test_never_truncates_even_with_many_matches(self):
        result = filter_contracts(self.chain, right=OptionRight.CALL)
        # Sanity: with no strike/delta/expiry bounds, every CALL contract
        # in the raw chain should come back -- no hidden limit.
        raw_calls = [c for c in self.chain if c.right == OptionRight.CALL]
        self.assertEqual(len(result), len(raw_calls))

    def test_combined_filters(self):
        result = filter_contracts(
            self.chain, right=OptionRight.CALL,
            min_strike=150, max_strike=200,
            min_delta=0.2, max_delta=0.9,
            min_expiry_days=1, max_expiry_days=60,
        )
        for c in result:
            self.assertEqual(c.right, OptionRight.CALL)
            self.assertTrue(150 <= c.strike <= 200)
            self.assertTrue(0.2 <= c.delta <= 0.9)
            self.assertTrue(1 <= c.days_to_expiry <= 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)