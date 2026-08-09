"""
Tests for simulator/data/option_pricer.py

Run directly:  python -m simulator.tests.test_option_pricer
Or with unittest discovery: python -m unittest discover
"""

import unittest
from datetime import date, timedelta

from simulator.data.option_pricer import (
    OptionRight,
    black_scholes_price,
    black_scholes_greeks,
    BlackScholesSource,
    find_contract,
)


class TestBlackScholesMath(unittest.TestCase):

    def test_deep_itm_call_delta_near_one(self):
        greeks = black_scholes_greeks(S=200, K=100, T=0.5, r=0.045, sigma=0.25, right=OptionRight.CALL)
        self.assertGreater(greeks["delta"], 0.95)

    def test_deep_otm_call_delta_near_zero(self):
        greeks = black_scholes_greeks(S=100, K=200, T=0.5, r=0.045, sigma=0.25, right=OptionRight.CALL)
        self.assertLess(greeks["delta"], 0.05)

    def test_atm_call_delta_near_half(self):
        greeks = black_scholes_greeks(S=100, K=100, T=0.5, r=0.045, sigma=0.25, right=OptionRight.CALL)
        self.assertAlmostEqual(greeks["delta"], 0.55, delta=0.1)

    def test_put_call_parity(self):
        S, K, T, r, sigma = 150, 145, 0.5, 0.045, 0.30
        call = black_scholes_price(S, K, T, r, sigma, OptionRight.CALL)
        put = black_scholes_price(S, K, T, r, sigma, OptionRight.PUT)
        # C - P = S - K*e^(-rT)
        lhs = call - put
        rhs = S - K * (2.71828 ** (-r * T))
        self.assertAlmostEqual(lhs, rhs, delta=0.05)

    def test_zero_time_returns_intrinsic(self):
        price = black_scholes_price(S=110, K=100, T=0, r=0.045, sigma=0.25, right=OptionRight.CALL)
        self.assertEqual(price, 10.0)

    def test_call_theta_is_negative(self):
        greeks = black_scholes_greeks(S=100, K=100, T=0.5, r=0.045, sigma=0.25, right=OptionRight.CALL)
        self.assertLess(greeks["theta"], 0)


class TestBlackScholesSource(unittest.TestCase):

    def setUp(self):
        # Fake 60 days of mildly upward-drifting, noisy price history
        start = date(2024, 1, 1)
        prices = {}
        price = 180.0
        for i in range(60):
            d = start + timedelta(days=i)
            price *= 1 + (0.001 if i % 3 else -0.002)  # small synthetic wiggle
            prices[d] = round(price, 2)
        self.price_history = prices
        self.snapshot_date = start + timedelta(days=59)
        self.spot = prices[self.snapshot_date]
        self.source = BlackScholesSource(price_history=self.price_history)

    def test_chain_not_empty(self):
        chain = self.source.get_chain("AAPL", self.snapshot_date, self.spot)
        self.assertGreater(len(chain), 0)

    def test_chain_has_both_rights(self):
        chain = self.source.get_chain("AAPL", self.snapshot_date, self.spot)
        rights = {c.right for c in chain}
        self.assertEqual(rights, {OptionRight.CALL, OptionRight.PUT})

    def test_atm_call_priced_above_zero(self):
        chain = self.source.get_chain("AAPL", self.snapshot_date, self.spot)
        atm_calls = [c for c in chain if c.right == OptionRight.CALL and abs(c.strike - self.spot) < 5]
        self.assertTrue(any(c.mid > 0 for c in atm_calls))

    def test_bid_below_ask(self):
        chain = self.source.get_chain("AAPL", self.snapshot_date, self.spot)
        for c in chain[:20]:
            self.assertLessEqual(c.bid, c.ask)


class TestFindContract(unittest.TestCase):

    def setUp(self):
        import random
        rng = random.Random(42)
        price = 180.0
        prices = {}
        for i in range(40):
            price *= 1 + rng.uniform(-0.02, 0.02)  # realistic daily noise
            prices[date(2024, 1, 1) + timedelta(days=i)] = round(price, 2)
        self.snapshot_date = max(prices)
        self.spot = prices[self.snapshot_date]
        source = BlackScholesSource(price_history=prices)
        self.chain = source.get_chain("AAPL", self.snapshot_date, self.spot)

    def test_find_by_target_delta(self):
        result = find_contract(self.chain, right=OptionRight.CALL, target_delta=0.30, max_expiry_days=35)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.delta, 0.30, delta=0.15)

    def test_find_by_target_strike(self):
        target = round(self.spot * 1.05, 0)
        result = find_contract(self.chain, right=OptionRight.CALL, target_strike=target, max_expiry_days=35)
        self.assertIsNotNone(result)
        self.assertLess(abs(result.strike - target), 10)

    def test_find_returns_none_when_no_match(self):
        result = find_contract(self.chain, right=OptionRight.CALL, max_expiry_days=1, min_expiry_days=2)
        self.assertIsNone(result)


def _manual_sanity_run():
    """Quick eyeball check, not part of the automated test suite."""
    import random
    rng = random.Random(7)
    price = 180.0
    prices = {}
    for i in range(40):
        price *= 1 + rng.uniform(-0.02, 0.02)
        prices[date(2024, 1, 1) + timedelta(days=i)] = round(price, 2)
    snapshot_date = max(prices)
    spot = prices[snapshot_date]

    source = BlackScholesSource(price_history=prices)
    chain = source.get_chain("AAPL", snapshot_date, spot)

    print(f"Spot: {spot:.2f} on {snapshot_date} | chain size: {len(chain)}")

    deep_itm = find_contract(chain, right=OptionRight.CALL, target_delta=0.85, min_expiry_days=300)
    otm_30d = find_contract(chain, right=OptionRight.CALL, target_delta=0.30, max_expiry_days=35)

    for label, c in [("Deep ITM long call", deep_itm), ("~30-delta short call", otm_30d)]:
        if c is None:
            print(f"{label}: no match found")
            continue
        print(f"{label}: strike {c.strike}, expiry {c.expiry}, delta {c.delta:.3f}, "
              f"mid {c.mid:.2f}, iv {c.iv:.3f}")


if __name__ == "__main__":
    _manual_sanity_run()
    print("\nRunning unit tests...\n")
    unittest.main()
