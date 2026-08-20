"""
Tests for simulator/storage/session_store.py

Run: python -m simulator.tests.test_session_store
"""

import unittest
from datetime import date

from simulator.data.option_pricer import BlackScholesSource
from simulator.engine.session import TradingSession
from simulator.storage.session_store import InMemorySessionStore


class TestInMemorySessionStore(unittest.TestCase):

    def setUp(self):
        prices = {date(2024, 1, 1): 180.0, date(2024, 1, 2): 181.0}
        source = BlackScholesSource(price_history=prices)
        self.session = TradingSession("AAPL", source, prices)
        self.store = InMemorySessionStore()

    def test_create_and_get(self):
        self.store.create("default", self.session)
        self.assertIs(self.store.get("default"), self.session)

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_delete(self):
        self.store.create("default", self.session)
        self.store.delete("default")
        self.assertIsNone(self.store.get("default"))

    def test_list_ids(self):
        self.store.create("a", self.session)
        self.store.create("b", self.session)
        self.assertEqual(sorted(self.store.list_ids()), ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
