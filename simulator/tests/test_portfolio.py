"""
Tests for simulator/engine/portfolio.py

Run: python -m simulator.tests.test_portfolio
"""

import random
import unittest
from datetime import date, timedelta

from simulator.data.option_pricer import OptionRight, BlackScholesSource
from simulator.engine.portfolio import Portfolio, LegAction, LegStatus


class TestOpenLeg(unittest.TestCase):

    def setUp(self):
        self.portfolio = Portfolio(starting_cash=100_000)

    def test_buy_debits_cash(self):
        leg = self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.BUY, strike=150,
            expiry=date(2024, 12, 20), entry_date=date(2024, 1, 2), price=25.0,
        )
        self.assertEqual(self.portfolio.cash, 100_000 - 25.0 * 100)
        self.assertEqual(leg.status, LegStatus.OPEN)

    def test_sell_credits_cash(self):
        self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.SELL, strike=190,
            expiry=date(2024, 2, 16), entry_date=date(2024, 2, 1), price=3.0,
        )
        self.assertEqual(self.portfolio.cash, 100_000 + 3.0 * 100)

    def test_quantity_scales_cash(self):
        self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.BUY, strike=150,
            expiry=date(2024, 12, 20), entry_date=date(2024, 1, 2), price=25.0,
            quantity=2,
        )
        self.assertEqual(self.portfolio.cash, 100_000 - 25.0 * 100 * 2)


class TestSettleLeg(unittest.TestCase):

    def setUp(self):
        self.portfolio = Portfolio(starting_cash=100_000)

    def test_settle_long_call_itm(self):
        leg = self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.BUY, strike=150,
            expiry=date(2024, 12, 20), entry_date=date(2024, 1, 2), price=25.0,
        )
        cash_after_open = self.portfolio.cash
        pnl = self.portfolio.settle_leg(leg, date(2024, 12, 20), underlying_price=180.0)

        # intrinsic = 30, entry cost = 25 -> pnl = (30-25)*100 = 500
        self.assertAlmostEqual(pnl, 500.0)
        self.assertEqual(leg.status, LegStatus.CLOSED)
        self.assertAlmostEqual(self.portfolio.cash, cash_after_open + 30.0 * 100)

    def test_settle_short_call_otm_keeps_full_premium(self):
        leg = self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.SELL, strike=190,
            expiry=date(2024, 2, 16), entry_date=date(2024, 2, 1), price=3.0,
        )
        pnl = self.portfolio.settle_leg(leg, date(2024, 2, 16), underlying_price=185.0)
        # OTM -> intrinsic 0 -> keep entire premium: 3 * 100 = 300
        self.assertAlmostEqual(pnl, 300.0)

    def test_settle_short_call_itm_costs_money(self):
        leg = self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.SELL, strike=190,
            expiry=date(2024, 2, 16), entry_date=date(2024, 2, 1), price=3.0,
        )
        pnl = self.portfolio.settle_leg(leg, date(2024, 2, 16), underlying_price=200.0)
        # intrinsic = 10, premium collected = 3 -> pnl = (3-10)*100 = -700
        self.assertAlmostEqual(pnl, -700.0)

    def test_cannot_settle_twice(self):
        leg = self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.SELL, strike=190,
            expiry=date(2024, 2, 16), entry_date=date(2024, 2, 1), price=3.0,
        )
        self.portfolio.settle_leg(leg, date(2024, 2, 16), underlying_price=185.0)
        with self.assertRaises(ValueError):
            self.portfolio.settle_leg(leg, date(2024, 2, 16), underlying_price=185.0)


class TestCloseLeg(unittest.TestCase):

    def setUp(self):
        self.portfolio = Portfolio(starting_cash=100_000)

    def test_close_short_call_early_at_a_loss(self):
        leg = self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.SELL, strike=190,
            expiry=date(2024, 2, 16), entry_date=date(2024, 2, 1), price=3.0,
        )
        # Buying back at a higher price than we sold it for -> a loss
        pnl = self.portfolio.close_leg(leg, date(2024, 2, 10), close_price=6.0)
        self.assertAlmostEqual(pnl, (3.0 - 6.0) * 100)

    def test_close_long_call_early_at_a_gain(self):
        leg = self.portfolio.open_leg(
            right=OptionRight.CALL, action=LegAction.BUY, strike=150,
            expiry=date(2024, 12, 20), entry_date=date(2024, 1, 2), price=25.0,
        )
        pnl = self.portfolio.close_leg(leg, date(2024, 6, 1), close_price=40.0)
        self.assertAlmostEqual(pnl, (40.0 - 25.0) * 100)


class TestMarkToMarketAndSummary(unittest.TestCase):

    def setUp(self):
        rng = random.Random(1)
        price = 180.0
        self.prices = {}
        for i in range(120):
            price *= 1 + rng.uniform(-0.015, 0.015)
            self.prices[date(2024, 1, 1) + timedelta(days=i)] = round(price, 2)
        self.source = BlackScholesSource(price_history=self.prices)
        self.portfolio = Portfolio(starting_cash=100_000)

    def test_mark_to_market_zero_with_no_open_legs(self):
        as_of = date(2024, 3, 1)
        unrealized = self.portfolio.mark_to_market(as_of, self.source, "AAPL", self.prices[as_of])
        self.assertEqual(unrealized, 0.0)

    def test_summary_matches_cash_and_pnl_components(self):
        entry_date = date(2024, 1, 10)
        spot = self.prices[entry_date]
        chain = self.source.get_chain("AAPL", entry_date, spot)
        contract = min(
            [c for c in chain if c.right.value == "CALL"],
            key=lambda c: abs(c.strike - spot * 0.95),
        )

        self.portfolio.open_leg(
            right=contract.right, action=LegAction.BUY, strike=contract.strike,
            expiry=contract.expiry, entry_date=entry_date, price=contract.ask,
        )

        later = date(2024, 3, 1)
        summary = self.portfolio.summary(later, self.source, "AAPL", self.prices[later])

        self.assertEqual(summary["open_leg_count"], 1)
        self.assertEqual(summary["closed_leg_count"], 0)
        self.assertAlmostEqual(summary["total_equity"], summary["cash"] + summary["unrealized_pnl"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
