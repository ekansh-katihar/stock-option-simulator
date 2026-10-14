"""
Tests for simulator/strategies/short_leg_selector.py

Run: python -m simulator.tests.test_short_leg_selector
"""

import unittest
from datetime import date, timedelta

from simulator.data.option_pricer import OptionContract, OptionRight
from simulator.strategies.blackout import BlackoutCalendar
from simulator.strategies.rule_config import RuleConfig
from simulator.strategies.short_leg_selector import select_short_leg_contract

ENTRY = date(2024, 1, 2)


def _contract(strike, delta, dte=35, right=OptionRight.CALL):
    return OptionContract(
        strike=strike, expiry=ENTRY + timedelta(days=dte), right=right,
        bid=1.0, ask=1.1, last=1.05, iv=0.25, delta=delta, gamma=0.02,
        theta=-0.05, vega=0.1, underlying_price=180.0, snapshot_date=ENTRY,
    )


def _no_blackout(config=None):
    return BlackoutCalendar("AAPL", config or RuleConfig(), earnings_dates=[], dividend_dates=[])


class TestStrictFloorSatisfied(unittest.TestCase):

    def test_picks_closest_to_target_delta_among_strict_survivors(self):
        chain = [
            _contract(strike=190, delta=0.40),
            _contract(strike=195, delta=0.30),
            _contract(strike=200, delta=0.20),
            _contract(strike=175, delta=0.35),  # exact delta match but BELOW floor (180)
        ]
        config = RuleConfig(short_target_delta=0.35, short_entry_min_dte=30, short_entry_max_dte=45)
        result = select_short_leg_contract(
            chain, ENTRY, long_strike=150, long_premium=40, profit_so_far=10,
            config=config, blackout=_no_blackout(config),
        )
        # strict_floor = 150 + 40 - 10 = 180 -> strike 175 is correctly excluded
        # despite being the exact delta match; among 190/195/200, 190 and 195
        # tie at 0.05 off target -- min() picks the first encountered (190).
        self.assertFalse(result.conflict)
        self.assertFalse(result.relaxed)
        self.assertIn(result.contract.strike, (190, 195))

    def test_floor_value_computed_correctly(self):
        chain = [_contract(strike=200, delta=0.35)]
        config = RuleConfig(short_entry_min_dte=30, short_entry_max_dte=45)
        result = select_short_leg_contract(
            chain, ENTRY, long_strike=150, long_premium=30, profit_so_far=5,
            config=config, blackout=_no_blackout(config),
        )
        self.assertAlmostEqual(result.floor, 150 + 30 - 5)


class TestToleranceEscapeHatch(unittest.TestCase):

    def test_relaxes_when_nothing_meets_strict_floor(self):
        chain = [_contract(strike=168, delta=0.35)]
        config = RuleConfig(short_entry_min_dte=30, short_entry_max_dte=45,
                             breakeven_floor_tolerance=5.0)
        result = select_short_leg_contract(
            chain, ENTRY, long_strike=150, long_premium=30, profit_so_far=10,
            config=config, blackout=_no_blackout(config),
        )
        self.assertTrue(result.conflict)
        self.assertTrue(result.relaxed)
        self.assertIsNotNone(result.contract)
        self.assertEqual(result.contract.strike, 168)

    def test_no_selection_when_even_relaxed_floor_fails(self):
        chain = [_contract(strike=150, delta=0.35)]
        config = RuleConfig(short_entry_min_dte=30, short_entry_max_dte=45,
                             breakeven_floor_tolerance=5.0)
        result = select_short_leg_contract(
            chain, ENTRY, long_strike=150, long_premium=30, profit_so_far=10,
            config=config, blackout=_no_blackout(config),
        )
        self.assertTrue(result.conflict)
        self.assertIsNone(result.contract)
        self.assertEqual(result.candidates_meeting_floor, 0)


class TestDTEWindowAndDegenerateGuard(unittest.TestCase):

    def test_contracts_outside_dte_window_excluded(self):
        chain = [
            _contract(strike=200, delta=0.35, dte=5),
            _contract(strike=200, delta=0.35, dte=100),
        ]
        config = RuleConfig(short_entry_min_dte=30, short_entry_max_dte=45)
        result = select_short_leg_contract(
            chain, ENTRY, long_strike=150, long_premium=30, profit_so_far=10,
            config=config, blackout=_no_blackout(config),
        )
        self.assertEqual(result.candidates_in_window, 0)
        self.assertIsNone(result.contract)


class TestBlackoutInteraction(unittest.TestCase):

    def test_blacked_out_expiry_excluded(self):
        earnings = ENTRY + timedelta(days=40)
        chain = [_contract(strike=200, delta=0.35, dte=40)]
        config = RuleConfig(short_entry_min_dte=30, short_entry_max_dte=45, earnings_blackout_days=3)
        cal = BlackoutCalendar("AAPL", config, earnings_dates=[earnings], dividend_dates=[])

        result = select_short_leg_contract(
            chain, ENTRY, long_strike=150, long_premium=30, profit_so_far=10,
            config=config, blackout=cal,
        )
        self.assertEqual(result.candidates_in_window, 0)
        self.assertIn("blackout", result.reason)

    def test_non_blacked_out_expiry_survives(self):
        earnings = ENTRY + timedelta(days=100)
        chain = [_contract(strike=200, delta=0.35, dte=40)]
        config = RuleConfig(short_entry_min_dte=30, short_entry_max_dte=45, earnings_blackout_days=3)
        cal = BlackoutCalendar("AAPL", config, earnings_dates=[earnings], dividend_dates=[])

        result = select_short_leg_contract(
            chain, ENTRY, long_strike=150, long_premium=30, profit_so_far=10,
            config=config, blackout=cal,
        )
        self.assertIsNotNone(result.contract)


if __name__ == "__main__":
    unittest.main(verbosity=2)