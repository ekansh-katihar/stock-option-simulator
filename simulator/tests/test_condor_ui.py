"""Exercise actual Streamlit controls; stock downloads are deterministic fixtures."""

from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from simulator.storage.session_store import InMemorySessionStore


def prices(ticker, start, end):
    return {start + timedelta(days=i): 100 + (i % 7 - 3) * 1.5
            for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5}


class TestCondorUI(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'ui' / 'app.py'
        self.app = AppTest.from_file(str(path), default_timeout=15)
        # Keep UI tests isolated from the user's configured database.
        self.app.session_state['store'] = InMemorySessionStore()
        self.download = patch('simulator.ui.condor.fetch_price_history', side_effect=prices)
        self.download.start()
        self.addCleanup(self.download.stop)
        self.app.run()

    def button(self, label):
        return next(b for b in self.app.button if b.label == label)

    def assert_healthy(self):
        self.assertEqual(list(self.app.exception), [])
        self.assertEqual(list(self.app.error), [])

    def start_and_open(self):
        self.button('Start iron-condor session').click().run()
        self.assert_healthy()
        self.assertFalse(self.button('Open iron condor').disabled)
        self.button('Open iron condor').click().run()
        self.assert_healthy()

    def test_choose_contracts_open_advance_and_close(self):
        self.start_and_open()
        session = self.app.session_state['condor_session']
        original = {(l['strike'], l['expiry']) for l in session.snapshot()['legs']}
        day = session.current_date
        self.button('Next trading day').click().run()
        self.assert_healthy()
        self.assertGreater(session.current_date, day)
        self.assertEqual(original, {(l['strike'], l['expiry']) for l in session.snapshot()['legs']})
        self.button('Close whole position').click().run()
        self.assert_healthy()
        self.assertEqual(session.snapshot()['legs'], [])
        self.assertNotEqual(session.snapshot()['realized_pnl'], 0)

    def test_dashboard_uses_bid_ask_and_zero_fee_totals_agree(self):
        fee = next(n for n in self.app.number_input if n.label == 'Fee per contract per trade ($)')
        fee.set_value(0.0).run()
        self.start_and_open()
        snap = self.app.session_state['condor_session'].snapshot()
        self.assertAlmostEqual(snap['total_pnl'], snap['pnl_if_closed'])
        self.assertAlmostEqual(snap['total_equity'], snap['starting_cash'] + snap['pnl_if_closed'])
        labels = [m.label for m in self.app.metric]
        self.assertIn('Equity (bid/ask)', labels)
        self.assertIn('Unrealized P&L (bid/ask)', labels)
        self.assertFalse(any('(mid)' in label for label in labels))
        self.assertTrue(all('mid' not in row and 'close_price' in row for row in snap['legs']))

    def test_roll_preserves_history_and_realized_pnl(self):
        self.start_and_open()
        session = self.app.session_state['condor_session']
        ids = {l['id'] for l in session.snapshot()['legs']}
        expiry_box = next(s for s in self.app.selectbox if s.label == 'Common expiry')
        expiry_box.select_index(expiry_box.index + 1).run()
        self.button('Roll position to selected contracts').click().run()
        self.assert_healthy()
        self.assertTrue(ids.isdisjoint({l['id'] for l in session.snapshot()['legs']}))
        self.assertLess(session.snapshot()['realized_pnl'], 0)
        self.assertTrue(any(e['event'] == 'rolled_position' for e in session.history()))

    def test_partial_close_and_reopen_one_leg(self):
        self.start_and_open()
        session = self.app.session_state['condor_session']
        leg = next(l for l in session.snapshot()['legs'] if l['role'] == 'lower_put')
        close_selector = next(m for m in self.app.multiselect if m.label.startswith('Legs to close'))
        close_selector.set_value([leg['id']]).run()
        self.button('Close selected legs').click().run()
        self.assert_healthy()
        self.assertEqual(len(session.snapshot()['legs']), 3)
        self.assertTrue(any('no longer form' in w.value for w in self.app.warning))
        replacement_expiry = next(s for s in self.app.selectbox if s.label == 'Replacement expiry')
        replacement_expiry.set_value(leg['expiry']).run()
        replacement_contract = next(s for s in self.app.selectbox if s.label == 'Replacement contract')
        # Select the original strike from this expiry's actual contract list.
        candidates = [c for c in session.view_chain() if c.right.value == 'PUT' and c.expiry == leg['expiry']]
        original = next(c for c in candidates if c.strike == leg['strike'])
        replacement_contract.set_value(original).run()
        self.button('Replace selected leg').click().run()
        self.assert_healthy()
        self.assertEqual(len(session.snapshot()['legs']), 4)

    def test_strategy_switch_keeps_condor_session(self):
        self.start_and_open()
        session = self.app.session_state['condor_session']
        self.app.selectbox(key='strategy_mode').set_value('PMCC').run()
        self.assert_healthy()
        self.app.selectbox(key='strategy_mode').set_value('Iron Condor').run()
        self.assert_healthy()
        self.assertIs(self.app.session_state['condor_session'], session)
        self.assertEqual(len(session.snapshot()['legs']), 4)


if __name__ == '__main__':
    unittest.main()
