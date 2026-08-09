"""
Tests for simulator/engine/simulator.py and simulator/data/price_history.py

Run: python -m simulator.tests.test_engine
"""

import random
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from simulator.data.option_pricer import BlackScholesSource
from simulator.engine.portfolio import Portfolio
from simulator.strategies.pmcc import PMCCStrategy, PMCCConfig
from simulator.engine.simulator import BacktestEngine


def _make_price_series(start, days, start_price=180.0, seed=1, drift=0.0):
    rng = random.Random(seed)
    prices = {}
    price = start_price
    for i in range(days):
        price *= 1 + drift + rng.uniform(-0.015, 0.015)
        prices[start + timedelta(days=i)] = round(price, 2)
    return prices


class TestBacktestEngineFullRun(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        self.end = date(2024, 12, 31)
        self.prices = _make_price_series(self.start, 366)
        self.source = BlackScholesSource(price_history=self.prices)
        self.portfolio = Portfolio(starting_cash=100_000)
        self.config = PMCCConfig(short_min_expiry_days=25, short_max_expiry_days=35)
        self.strategy = PMCCStrategy("AAPL", self.source, self.portfolio, self.config)
        self.engine = BacktestEngine(self.strategy, self.portfolio, self.source, self.prices)

    def test_run_produces_full_equity_curve(self):
        result = self.engine.run(self.start, self.end)
        self.assertEqual(len(result.equity_curve), len(self.prices))

    def test_long_call_opened_on_first_day(self):
        self.engine.run(self.start, self.end)
        self.assertTrue(self.strategy.is_active() or self.strategy.status()["long_leg"] is not None)

    def test_multiple_short_rolls_occur_over_a_year(self):
        result = self.engine.run(self.start, self.end)
        # ~12 months of ~30-day rolls -> expect several settle+reopen cycles
        self.assertGreater(len(result.roll_log), 5)

    def test_final_summary_has_expected_keys(self):
        result = self.engine.run(self.start, self.end)
        for key in ("cash", "realized_pnl", "unrealized_pnl", "total_equity"):
            self.assertIn(key, result.final_summary)

    def test_equity_curve_is_chronological(self):
        result = self.engine.run(self.start, self.end)
        dates = [p.date for p in result.equity_curve]
        self.assertEqual(dates, sorted(dates))


class TestBacktestEngineStopLoss(unittest.TestCase):

    def setUp(self):
        self.start = date(2024, 1, 1)
        # Sharp, sustained decline: definitely breaches a 10% stop-loss
        self.prices = {}
        price = 180.0
        for i in range(200):
            price *= 0.995  # steady grind down
            self.prices[self.start + timedelta(days=i)] = round(price, 2)

        self.source = BlackScholesSource(price_history=self.prices)
        self.portfolio = Portfolio(starting_cash=100_000)
        self.config = PMCCConfig(stop_loss_pct=0.10)
        self.strategy = PMCCStrategy("AAPL", self.source, self.portfolio, self.config)
        self.engine = BacktestEngine(self.strategy, self.portfolio, self.source, self.prices)

    def test_stop_loss_triggers_on_sustained_decline(self):
        result = self.engine.run(self.start, self.start + timedelta(days=199))
        self.assertTrue(result.stopped_out)
        self.assertIsNotNone(result.stopped_out_date)

    def test_strategy_inactive_after_stop_out(self):
        self.engine.run(self.start, self.start + timedelta(days=199))
        self.assertFalse(self.strategy.is_active())


class TestFetchPriceHistory(unittest.TestCase):
    """
    yfinance hits an external network endpoint not reachable from this
    sandbox, so we verify the parsing/normalization logic against a mocked
    response instead of a live call.
    """

    def test_parses_mocked_yfinance_response(self):
        import pandas as pd

        idx = pd.date_range("2024-01-02", periods=3, freq="D")
        fake_df = pd.DataFrame({"Close": [180.1, 181.5, 179.8]}, index=idx)

        with patch("simulator.data.price_history.yf.download", return_value=fake_df):
            from simulator.data.price_history import fetch_price_history
            prices = fetch_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 4))

        self.assertEqual(len(prices), 3)
        self.assertAlmostEqual(prices[date(2024, 1, 2)], 180.1)

    def test_raises_on_empty_response(self):
        import pandas as pd

        with patch("simulator.data.price_history.yf.download", return_value=pd.DataFrame()):
            from simulator.data.price_history import fetch_price_history
            with self.assertRaises(ValueError):
                fetch_price_history("AAPL", date(2024, 1, 2), date(2024, 1, 4))


if __name__ == "__main__":
    unittest.main(verbosity=2)
