"""
Tests for simulator/strategies/pmcc.py

Run: python -m simulator.tests.test_pmcc
"""

import random
import unittest
from datetime import date, timedelta

from simulator.data.option_pricer import BlackScholesSource
from simulator.engine.portfolio import Portfolio, LegAction
from simulator.strategies.pmcc import PMCCStrategy, PMCCConfig


def _make_price_series(start, days, start_price=180.0, seed=1, drift=0.0):
    rng = random.Random(seed)
    prices = {}
    price = start_price
    for i in range(days):
        price *= 1 + drift + rng.uniform(-0.015, 0.015)
        prices[start + timedelta(days=i)] = round(price, 2)
    return prices


class TestOpenLongCall(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _make_price_series(self.start, 400)
        self.source = BlackScholesSource(price_history=self.prices)
        self.portfolio = Portfolio(starting_cash=100_000)
        self.strategy = PMCCStrategy("AAPL", self.source, self.portfolio)

    def test_opens_deep_itm_call(self):
        entry_date = self.start + timedelta(days=10)
        spot = self.prices[entry_date]
        leg = self.strategy.open_long_call(entry_date, spot)

        self.assertEqual(leg.action, LegAction.BUY)
        self.assertLess(leg.strike, spot)  # ITM call: strike below spot
        self.assertTrue(self.strategy.is_active())

    def test_cannot_open_two_long_calls(self):
        entry_date = self.start + timedelta(days=10)
        spot = self.prices[entry_date]
        self.strategy.open_long_call(entry_date, spot)
        with self.assertRaises(ValueError):
            self.strategy.open_long_call(entry_date, spot)

    def test_cash_debited_on_open(self):
        entry_date = self.start + timedelta(days=10)
        spot = self.prices[entry_date]
        cash_before = self.portfolio.cash
        self.strategy.open_long_call(entry_date, spot)
        self.assertLess(self.portfolio.cash, cash_before)


class TestRollShortCall(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _make_price_series(self.start, 400)
        self.source = BlackScholesSource(price_history=self.prices)
        self.portfolio = Portfolio(starting_cash=100_000)
        self.config = PMCCConfig(short_otm_pct=0.05, short_min_expiry_days=25, short_max_expiry_days=35)
        self.strategy = PMCCStrategy("AAPL", self.source, self.portfolio, self.config)

        entry_date = self.start + timedelta(days=10)
        self.strategy.open_long_call(entry_date, self.prices[entry_date])

    def test_first_roll_opens_a_short_call(self):
        d = self.start + timedelta(days=11)
        result = self.strategy.roll_short_call(d, self.prices[d])

        self.assertIsNone(result.settled_leg)
        self.assertIsNotNone(result.opened_leg)
        self.assertEqual(result.opened_leg.action, LegAction.SELL)
        # strike should be roughly 5% above spot
        self.assertGreater(result.opened_leg.strike, self.prices[d])

    def test_roll_is_noop_while_short_still_open(self):
        d1 = self.start + timedelta(days=11)
        self.strategy.roll_short_call(d1, self.prices[d1])

        d2 = d1 + timedelta(days=5)  # before expiry
        result = self.strategy.roll_short_call(d2, self.prices[d2])

        self.assertIsNone(result.settled_leg)
        self.assertIsNone(result.opened_leg)  # nothing to do yet

    def test_second_roll_settles_and_reopens(self):
        d1 = self.start + timedelta(days=11)
        first = self.strategy.roll_short_call(d1, self.prices[d1])
        first_expiry = first.opened_leg.expiry

        # jump to (at/after) the first short leg's expiry
        d2 = first_expiry
        while d2 not in self.prices:
            d2 += timedelta(days=1)
        result = self.strategy.roll_short_call(d2, self.prices[d2])

        self.assertIsNotNone(result.settled_leg)
        self.assertIsNotNone(result.settled_pnl)
        self.assertIsNotNone(result.opened_leg)  # rolled into a new one same call

    def test_short_leg_settlement_reflected_in_portfolio(self):
        d1 = self.start + timedelta(days=11)
        first = self.strategy.roll_short_call(d1, self.prices[d1])
        first_expiry = first.opened_leg.expiry

        d2 = first_expiry
        while d2 not in self.prices:
            d2 += timedelta(days=1)
        self.strategy.roll_short_call(d2, self.prices[d2])

        self.assertEqual(len(self.portfolio.closed_legs()), 1)
        self.assertEqual(self.portfolio.closed_legs()[0].action, LegAction.SELL)


class TestStopLoss(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = _make_price_series(self.start, 60, start_price=180.0)
        self.source = BlackScholesSource(price_history=self.prices)
        self.portfolio = Portfolio(starting_cash=100_000)
        self.config = PMCCConfig(stop_loss_pct=0.10)
        self.strategy = PMCCStrategy("AAPL", self.source, self.portfolio, self.config)

        entry_date = self.start + timedelta(days=10)
        self.leg = self.strategy.open_long_call(entry_date, self.prices[entry_date])

    def test_no_trigger_above_threshold(self):
        # spot only slightly below strike -> should not trigger a 10% stop
        self.assertFalse(self.strategy.check_stop_loss(self.leg.strike * 0.98))

    def test_triggers_below_threshold(self):
        self.assertTrue(self.strategy.check_stop_loss(self.leg.strike * 0.85))

    def test_false_when_no_long_leg(self):
        empty_strategy = PMCCStrategy("AAPL", self.source, Portfolio(100_000), self.config)
        self.assertFalse(empty_strategy.check_stop_loss(100.0))

    def test_close_long_call_closes_position(self):
        self.strategy.close_long_call(self.start + timedelta(days=20),
                                       self.prices[self.start + timedelta(days=20)])
        self.assertFalse(self.strategy.is_active())
        self.assertEqual(len(self.portfolio.closed_legs()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
