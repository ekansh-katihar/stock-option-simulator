import copy
from dataclasses import replace
from datetime import date, timedelta
import json
import unittest
from unittest.mock import patch, PropertyMock

from simulator.data.option_pricer import (
    OptionContract, OptionDataSource, OptionRight, BlackScholesSource,
    black_scholes_price, black_scholes_greeks,
)
from simulator.engine.multi_leg_session import MultiLegSession
from simulator.engine.portfolio import Portfolio, LegAction
from simulator.engine.session import TradingSession
from simulator.strategies.pmcc import PMCCStrategy, PMCCConfig


class FixedSource(OptionDataSource):
    """Known prices make ledger checks independent of the pricing implementation."""
    def __init__(self):
        self.missing = None

    def get_chain(self, underlying, snapshot_date, spot_price):
        contracts = []
        for expiry in (date(2024, 1, 12), date(2024, 1, 19)):
            for right, strike, mid in ((OptionRight.PUT, 90, 1), (OptionRight.PUT, 95, 3),
                                      (OptionRight.CALL, 105, 3), (OptionRight.CALL, 110, 1)):
                if (right, strike, expiry) == self.missing:
                    continue
                contracts.append(OptionContract(
                    strike, expiry, right, mid - .1, mid + .1, mid,
                    .3, -.2 if right == OptionRight.PUT else .2, .01, -.02, .05,
                    spot_price, snapshot_date))
        return contracts


class TestCondorSession(unittest.TestCase):
    def setUp(self):
        self.start = date(2024, 1, 8)
        self.prices = {self.start + timedelta(days=i): 100.0
                       for i in range(12) if (self.start + timedelta(days=i)).weekday() < 5}
        self.source = FixedSource()
        self.session = MultiLegSession('TEST', self.source, self.prices,
                                       Portfolio(10_000), self.start, .65)

    def selections(self, expiry=date(2024, 1, 12)):
        contracts = [c for c in self.session.view_chain() if c.expiry == expiry]
        return dict(zip(('lower_put', 'upper_put', 'lower_call', 'upper_call'), contracts))

    def test_entry_equity_and_credit_are_not_double_counted(self):
        snap = self.session.open_condor(self.selections())
        self.assertAlmostEqual(snap['cash'], 10_357.4)
        self.assertAlmostEqual(snap['total_equity'], 9_917.4)
        self.assertAlmostEqual(snap['unrealized_pnl'], -82.6)
        self.assertAlmostEqual(snap['pnl_if_closed'], -85.2)
        self.assertAlmostEqual(snap['condor_risk']['entry_credit'], 357.4)
        self.assertAlmostEqual(snap['condor_risk']['max_loss'], 142.6)
        self.assertAlmostEqual(snap['total_equity'], 10_000 + snap['total_pnl'])

    def test_screenshot_quotes_give_five_dollar_liquidation_loss_without_fees(self):
        original = self.source.get_chain
        terms = {90: (168, .04, .05), 95: (175, .37, .38),
                 105: (196, .67, .69), 110: (203, .13, .14)}
        def screenshot_chain(underlying, snapshot_date, spot_price):
            return [replace(c, strike=terms[c.strike][0], bid=terms[c.strike][1],
                            ask=terms[c.strike][2])
                    for c in original(underlying, snapshot_date, spot_price)]
        self.session._portfolio = Portfolio(100_000)
        self.session.commission_per_contract = 0
        with patch.object(self.source, 'get_chain', side_effect=screenshot_chain):
            snap = self.session.open_condor(self.selections())
            self.assertAlmostEqual(snap['cash'], 100_085)
            self.assertAlmostEqual(snap['market_value'], -90)
            self.assertAlmostEqual(snap['unrealized_pnl'], -5)
            self.assertAlmostEqual(snap['total_equity'], 99_995)
            self.assertAlmostEqual(snap['total_pnl'], -5)
            self.assertAlmostEqual(snap['pnl_if_closed'], -5)
            self.assertEqual([l['close_price'] for l in snap['legs']], [.04, .38, .69, .13])
            closed = self.session.close_legs()
            self.assertAlmostEqual(closed['realized_pnl'], -5)
            self.assertAlmostEqual(closed['total_equity'], snap['total_equity'])

    def test_replay_and_trading_do_not_read_midpoint(self):
        with patch.object(OptionContract, 'mid', new_callable=PropertyMock,
                          side_effect=AssertionError('Midpoint must not value or execute positions')):
            self.session.open_condor(self.selections())
            self.session.advance()
            self.session.roll_condor(self.selections(date(2024, 1, 19)))
            self.session.replace_leg('lower_put', self.selections(date(2024, 1, 19))['lower_put'])
            snap = self.session.snapshot()
            closing_fees = 4 * .65
            self.assertAlmostEqual(snap['pnl_if_closed'], snap['total_pnl'] - closing_fees)
            self.assertAlmostEqual(sum(l['unrealized_pnl'] for l in snap['legs']), snap['unrealized_pnl'])
            closed = self.session.close_legs()
            self.assertAlmostEqual(closed['realized_pnl'], snap['pnl_if_closed'])

    def test_close_uses_bid_for_longs_ask_for_shorts_and_fees(self):
        self.session.open_condor(self.selections(), quantity=2)
        expected = self.session.snapshot()['pnl_if_closed']
        snap = self.session.close_legs()
        self.assertEqual(snap['legs'], [])
        self.assertAlmostEqual(snap['realized_pnl'], -170.4)
        self.assertAlmostEqual(snap['realized_pnl'], expected)
        self.assertAlmostEqual(snap['total_equity'], 10_000 + expected)
        prices = {l.action: l.close_price for l in self.session._portfolio.closed_legs()}
        self.assertEqual(prices[LegAction.BUY], .9)
        self.assertEqual(prices[LegAction.SELL], 3.1)

    def test_invalid_structure_does_not_open_partial_position(self):
        contracts = self.selections()
        contracts['upper_call'] = contracts['lower_call']
        before = copy.deepcopy(self.session.to_dict())
        with self.assertRaisesRegex(ValueError, 'Strikes'):
            self.session.open_condor(contracts)
        self.assertEqual(before, self.session.to_dict())

    def test_mixed_expiry_entry_is_rejected(self):
        contracts = self.selections()
        contracts['upper_call'] = self.selections(date(2024, 1, 19))['upper_call']
        with self.assertRaisesRegex(ValueError, 'same expiry'):
            self.session.open_condor(contracts)
        self.assertEqual(self.session.snapshot()['legs'], [])

    def test_unlisted_contract_is_rejected(self):
        contracts = self.selections()
        contracts['lower_put'] = replace(contracts['lower_put'], strike=89)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.session.open_condor(contracts)

    def test_invalid_quantity(self):
        for quantity in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                self.session.open_condor(self.selections(), quantity)

    def test_insufficient_capital_leaves_position_unchanged(self):
        self.session._portfolio = Portfolio(100)
        before = copy.deepcopy(self.session.to_dict())
        with self.assertRaisesRegex(ValueError, 'Insufficient equity'):
            self.session.open_condor(self.selections())
        self.assertEqual(before, self.session.to_dict())

    def test_roll_keeps_realized_pnl_and_replaces_all_ids(self):
        first = self.session.open_condor(self.selections())
        old_ids = {l['id'] for l in first['legs']}
        rolled = self.session.roll_condor(self.selections(date(2024, 1, 19)))
        self.assertAlmostEqual(rolled['realized_pnl'], -85.2)
        self.assertTrue(old_ids.isdisjoint({l['id'] for l in rolled['legs']}))
        self.assertTrue(all(l['expiry'] == date(2024, 1, 19) for l in rolled['legs']))
        self.assertAlmostEqual(rolled['total_equity'], 10_000 + rolled['total_pnl'])

    def test_failed_roll_preserves_existing_position(self):
        self.session.open_condor(self.selections())
        bad = self.selections(date(2024, 1, 19))
        bad['upper_call'] = bad['lower_call']
        before = copy.deepcopy(self.session.to_dict())
        with self.assertRaises(ValueError):
            self.session.roll_condor(bad)
        self.assertEqual(before, self.session.to_dict())

    def test_missing_quote_is_explicit_and_close_is_atomic(self):
        self.session.open_condor(self.selections())
        self.source.missing = (OptionRight.CALL, 110, date(2024, 1, 12))
        before = copy.deepcopy(self.session.to_dict())
        with self.assertRaisesRegex(ValueError, 'Missing quote'):
            self.session.close_legs()
        self.assertEqual(before, self.session.to_dict())

    def test_missing_future_quote_does_not_partially_advance(self):
        self.session.open_condor(self.selections())
        original_chain = self.source.get_chain
        def missing_day(underlying, snapshot_date, spot_price):
            chain = original_chain(underlying, snapshot_date, spot_price)
            return ([c for c in chain if c.strike != 110]
                    if snapshot_date == date(2024, 1, 10) else chain)
        before = copy.deepcopy(self.session.to_dict())
        with patch.object(self.source, 'get_chain', side_effect=missing_day):
            with self.assertRaisesRegex(ValueError, 'Missing quote on'):
                self.session.advance(date(2024, 1, 11))
        self.assertEqual(before, self.session.to_dict())

    def test_fees_cannot_turn_entry_into_an_accepted_debit(self):
        self.session.commission_per_contract = 100
        with self.assertRaisesRegex(ValueError, 'positive credit'):
            self.session.open_condor(self.selections())
        self.assertEqual(self.session.snapshot()['legs'], [])

    def test_legacy_portfolio_reconstructs_starting_cash(self):
        self.session.open_condor(self.selections())
        data = self.session._portfolio.to_dict()
        data.pop('starting_cash')
        restored = Portfolio.from_dict(data)
        self.assertAlmostEqual(restored.starting_cash, 10_000)
        self.assertAlmostEqual(restored.cash, self.session._portfolio.cash)

    def test_restore_negative_current_cash_preserves_starting_cash(self):
        portfolio = Portfolio(100)
        portfolio.open_leg(OptionRight.CALL, LegAction.BUY, 100, date(2024, 1, 12),
                           self.start, 2, fee=.65)
        restored = Portfolio.from_dict(portfolio.to_dict())
        self.assertAlmostEqual(restored.cash, -100.65)
        self.assertEqual(restored.starting_cash, 100)

    def test_close_put_spread_retains_calls_and_suppresses_condor_risk(self):
        snap = self.session.open_condor(self.selections())
        ids = [l['id'] for l in snap['legs'] if l['right'] == 'PUT']
        snap = self.session.close_legs(ids)
        self.assertEqual(len(snap['legs']), 2)
        self.assertIsNone(snap['condor_risk'])
        self.assertAlmostEqual(snap['total_equity'], 10_000 + snap['total_pnl'])

    def test_replace_single_leg_and_reopen_closed_leg(self):
        snap = self.session.open_condor(self.selections())
        old = next(l for l in snap['legs'] if l['role'] == 'lower_put')
        snap = self.session.replace_leg('lower_put', self.selections(date(2024, 1, 19))['lower_put'])
        self.assertIsNone(snap['condor_risk'])
        self.assertNotIn(old['id'], [l['id'] for l in snap['legs']])
        short_id = next(l['id'] for l in snap['legs'] if l['role'] == 'upper_put')
        self.session.close_legs([short_id])
        snap = self.session.replace_leg('upper_put', self.selections(date(2024, 1, 19))['upper_put'])
        self.assertEqual(len(snap['legs']), 4)

    def test_short_roll_cannot_outlive_protection(self):
        self.session.open_condor(self.selections())
        before = copy.deepcopy(self.session.to_dict())
        with self.assertRaisesRegex(ValueError, 'protection'):
            self.session.replace_leg('upper_put', self.selections(date(2024, 1, 19))['upper_put'])
        self.assertEqual(before, self.session.to_dict())

    def test_next_day_skips_weekend_and_bounds_are_enforced(self):
        self.session.advance(date(2024, 1, 12))
        self.session.advance()
        self.assertEqual(self.session.current_date, date(2024, 1, 15))
        for target in (date(2024, 1, 1), date(2024, 2, 1)):
            with self.assertRaises(ValueError):
                self.session.advance(target)

    def test_expiry_cash_settlement_and_no_automatic_reopening(self):
        self.session.open_condor(self.selections())
        snap = self.session.advance(date(2024, 1, 12))
        self.assertEqual(snap['legs'], [])
        self.assertAlmostEqual(snap['realized_pnl'], 357.4)
        self.assertAlmostEqual(snap['total_equity'], 10_357.4)
        self.assertEqual(len([e for e in self.session.history() if e['event'] == 'settled']), 4)
        self.assertEqual(len(self.session.equity_history()), 6)

    def test_expiry_holiday_uses_last_close_not_following_day(self):
        self.session.open_condor(self.selections())
        del self.prices[date(2024, 1, 12)]
        self.prices[date(2024, 1, 15)] = 150
        snap = self.session.advance(date(2024, 1, 15))
        self.assertAlmostEqual(snap['realized_pnl'], 357.4)
        events = [e for e in self.session.history() if e['event'] == 'settled']
        self.assertTrue(all(e['spot_date'] == date(2024, 1, 11) for e in events))

    def test_expiry_outside_wings_matches_max_loss(self):
        risk = self.session.open_condor(self.selections())['condor_risk']
        self.prices[date(2024, 1, 12)] = 150
        snap = self.session.advance(date(2024, 1, 12))
        self.assertAlmostEqual(snap['realized_pnl'], -risk['max_loss'])

    def test_expiry_below_put_wing_matches_max_loss(self):
        risk = self.session.open_condor(self.selections())['condor_risk']
        self.prices[date(2024, 1, 12)] = 50
        snap = self.session.advance(date(2024, 1, 12))
        self.assertAlmostEqual(snap['realized_pnl'], -risk['max_loss'])

    def test_position_greeks_include_short_sign_and_contract_multiplier(self):
        original = self.source.get_chain
        def differentiated(underlying, snapshot_date, spot_price):
            return [replace(c, delta={90: -.05, 95: -.2, 105: .3, 110: .1}[c.strike],
                            gamma=.01 if c.strike in (90, 110) else .03,
                            theta=-.02 if c.strike in (90, 110) else -.06,
                            vega=.1 if c.strike in (90, 110) else .3)
                    for c in original(underlying, snapshot_date, spot_price)]
        with patch.object(self.source, 'get_chain', side_effect=differentiated):
            snap = self.session.open_condor(self.selections(), quantity=2)
            for name, expected in {'delta': -10, 'gamma': -8, 'theta': 16, 'vega': -80}.items():
                self.assertAlmostEqual(snap['net_greeks'][name], expected)

    def test_session_roundtrip_preserves_positions_fees_and_history(self):
        self.session.open_condor(self.selections())
        self.session.advance()
        data = json.loads(json.dumps(self.session.to_dict()))
        restored = TradingSession.from_dict(data, self.source, self.prices)
        self.assertIsInstance(restored, MultiLegSession)
        self.assertEqual(restored.snapshot(), self.session.snapshot())
        self.assertEqual(restored.equity_history(), self.session.equity_history())
        self.assertEqual(restored.history(), self.session.history())


class TestPMCCExecutableChecks(unittest.TestCase):
    def strategy(self, entry_price, config):
        portfolio = Portfolio(10_000)
        strategy = PMCCStrategy('TEST', FixedSource(), portfolio, config)
        strategy._short_leg = portfolio.open_leg(
            OptionRight.CALL, LegAction.SELL, 105, date(2024, 1, 12),
            date(2024, 1, 8), entry_price)
        return strategy

    def test_profit_target_does_not_assume_midpoint_buyback(self):
        strategy = self.strategy(6, PMCCConfig(exit_profit_target_pct=.5))
        # Midpoint 3 implies 50% profit, but buying back at ask 3.1 does not.
        self.assertEqual(strategy.check_short_leg_exit(date(2024, 1, 8), 100), (False, None))

    def test_stop_loss_uses_ask_buyback(self):
        strategy = self.strategy(2.3, PMCCConfig(exit_stop_loss_base_multiplier=1))
        # Threshold ~3.08 is crossed by ask 3.1, even though midpoint is only 3.
        self.assertEqual(strategy.check_short_leg_exit(date(2024, 1, 8), 100), (True, 'stop_loss'))


class TestExactRepricing(unittest.TestCase):
    def setUp(self):
        self.start = date(2024, 1, 1)
        self.prices = {self.start + timedelta(days=i): 100 + (i % 3 - 1)
                       for i in range(70)}
        self.source = BlackScholesSource(self.prices)

    def test_held_contract_identity_and_greeks_match_original_terms(self):
        opened = next(c for c in self.source.get_chain('TEST', self.start, 99)
                      if c.right == OptionRight.PUT and c.days_to_expiry > 20)
        later = self.start + timedelta(days=10)
        q = self.source.get_quote('TEST', later, 120, opened.right, opened.strike, opened.expiry)
        T = (opened.expiry - later).days / 365
        self.assertEqual((q.strike, q.expiry, q.right), (opened.strike, opened.expiry, opened.right))
        expected = black_scholes_price(120, opened.strike, T, .045, q.iv, q.right)
        self.assertEqual(q.last, round(expected, 2))
        expected_greeks = black_scholes_greeks(120, opened.strike, T, .045, q.iv, q.right)
        for greek, value in expected_greeks.items():
            self.assertAlmostEqual(getattr(q, greek), value)

    def test_exact_quote_exists_outside_displayed_strike_range(self):
        q = self.source.get_quote('TEST', self.start, 200, OptionRight.CALL, 50, date(2024, 1, 19))
        self.assertEqual(q.strike, 50)
        self.assertGreater(q.mid, 149)

    def test_future_prices_do_not_change_current_chain_or_greeks(self):
        as_of = self.start + timedelta(days=30)
        alternate = {d: p if d <= as_of else 1000 for d, p in self.prices.items()}
        other = BlackScholesSource(alternate)
        self.assertEqual(self.source.get_chain('TEST', as_of, 99), other.get_chain('TEST', as_of, 99))

    def test_contract_grid_and_calendar_dates_are_stable(self):
        a = self.source.get_chain('TEST', self.start, 99)
        b = self.source.get_chain('TEST', self.start + timedelta(days=1), 101)
        keys = lambda chain: {(c.right, c.strike, c.expiry) for c in chain}
        self.assertGreater(len(keys(a) & keys(b)), len(a) * .8)
        self.assertTrue(all(c.expiry.weekday() == 4 for c in a))

    def test_missing_quote_cannot_be_substituted_with_a_nearby_contract(self):
        source = FixedSource()
        portfolio = Portfolio(10_000)
        leg = portfolio.open_leg(OptionRight.PUT, LegAction.BUY, 91, date(2024, 1, 12),
                                 date(2024, 1, 8), 1)
        self.assertIsNone(portfolio.find_matching_contract(leg, date(2024, 1, 8), source, 'TEST', 100))
        with self.assertRaisesRegex(ValueError, 'Missing quote'):
            portfolio.summary(date(2024, 1, 8), source, 'TEST', 100)


if __name__ == '__main__':
    unittest.main()
