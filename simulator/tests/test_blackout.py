"""
Tests for simulator/strategies/blackout.py

Run: python -m simulator.tests.test_blackout
"""

import unittest
from datetime import date

from simulator.strategies.blackout import BlackoutCalendar
from simulator.strategies.rule_config import RuleConfig


class TestEarningsBlackout(unittest.TestCase):

    def setUp(self):
        self.config = RuleConfig(earnings_blackout_days=7)
        self.cal = BlackoutCalendar(
            "AAPL", self.config,
            earnings_dates=[date(2024, 4, 25)],
            dividend_dates=[],
        )

    def test_expiry_within_window_is_blocked(self):
        self.assertTrue(self.cal.is_blocked(date(2024, 4, 1), date(2024, 4, 28)))

    def test_period_fully_outside_window_is_clear(self):
        self.assertFalse(self.cal.is_blocked(date(2024, 5, 10), date(2024, 5, 20)))

    def test_boundary_dates_are_inclusive(self):
        self.assertTrue(self.cal.is_blocked(date(2024, 4, 18), date(2024, 4, 18)))
        self.assertTrue(self.cal.is_blocked(date(2024, 5, 2), date(2024, 5, 2)))
        self.assertFalse(self.cal.is_blocked(date(2024, 4, 17), date(2024, 4, 17)))
        self.assertFalse(self.cal.is_blocked(date(2024, 5, 3), date(2024, 5, 3)))

    def test_event_falling_mid_period_is_caught_even_when_expiry_is_far_from_it(self):
        self.assertTrue(self.cal.is_blocked(date(2024, 3, 1), date(2024, 6, 1)))


class TestDividendBlackout(unittest.TestCase):

    def test_dividend_window_blocks_overlapping_period(self):
        config = RuleConfig(dividend_blackout_days=5)
        cal = BlackoutCalendar("AAPL", config, earnings_dates=[], dividend_dates=[date(2024, 2, 9)])
        self.assertTrue(cal.is_blocked(date(2024, 2, 5), date(2024, 2, 12)))
        self.assertFalse(cal.is_blocked(date(2024, 3, 1), date(2024, 3, 10)))


class TestCriticalDatesBlackout(unittest.TestCase):

    def test_user_supplied_critical_date_blocks(self):
        config = RuleConfig(critical_dates=[date(2024, 6, 15)], critical_date_blackout_days=3)
        cal = BlackoutCalendar("AAPL", config, earnings_dates=[], dividend_dates=[])
        self.assertTrue(cal.is_blocked(date(2024, 6, 14), date(2024, 6, 16)))
        self.assertFalse(cal.is_blocked(date(2024, 6, 1), date(2024, 6, 10)))


class TestMultipleSourcesCombined(unittest.TestCase):

    def setUp(self):
        self.config = RuleConfig(
            earnings_blackout_days=7, dividend_blackout_days=5,
            critical_dates=[date(2024, 6, 15)], critical_date_blackout_days=3,
        )
        self.cal = BlackoutCalendar(
            "AAPL", self.config,
            earnings_dates=[date(2024, 4, 25)],
            dividend_dates=[date(2024, 2, 9)],
        )

    def test_all_three_sources_active(self):
        self.assertTrue(self.cal.is_blocked(date(2024, 4, 20), date(2024, 4, 30)))
        self.assertTrue(self.cal.is_blocked(date(2024, 2, 8), date(2024, 2, 10)))
        self.assertTrue(self.cal.is_blocked(date(2024, 6, 14), date(2024, 6, 16)))
        self.assertFalse(self.cal.is_blocked(date(2024, 3, 1), date(2024, 3, 10)))

    def test_describe_conflicts_lists_all_overlapping_events(self):
        desc = self.cal.describe_conflicts(date(2024, 4, 20), date(2024, 4, 30))
        self.assertIn("earnings", desc)
        self.assertIn("2024-04-25", desc)

    def test_describe_conflicts_empty_when_clear(self):
        self.assertEqual(self.cal.describe_conflicts(date(2024, 3, 1), date(2024, 3, 10)), "")

    def test_period_overlapping_two_events_lists_both(self):
        desc = self.cal.describe_conflicts(date(2024, 4, 20), date(2024, 6, 16))
        self.assertIn("earnings", desc)
        self.assertIn("critical", desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)