
# Build
`pip install -r requirements.txt`

# Interactive iron condor

Run `PYTHONPATH=. streamlit run simulator/ui/app.py`, then choose **Iron Condor**
in the strategy selector. PMCC remains available in the same app.

1. Choose a stock, replay start/end dates, starting cash and a per-contract fee.
2. Choose a common expiry and four contracts: buy lower put, sell upper put,
   sell lower call, buy upper call. Preview credit, max loss and breakevens.
3. Open the condor and use **Next trading day** or advance to a later date.
4. Close selected legs (including a spread), close the whole position, roll the
   position into four new contracts, or replace an individual leg.

The replay shows each leg's bid/ask and Greeks, signed position Greeks, marked
equity/P&L, P&L if closed now, daily equity history and trade/settlement events.
Rolls retain prior realized P&L. Invalid selections are rejected before trading.

**Model assumptions:** stock prices are historical unadjusted closes. Options
use a fixed dollar strike grid and synthetic Friday weekly/monthly expiries;
these are not historical exchange listings. The held contract's original
strike/expiry is repriced directly with Black–Scholes, remaining time and
trailing realized volatility. An extra 400 calendar days of stock history is
loaded before the replay starts. The rate remains a model parameter (default
4.5%), and volatility is a proxy for IV, not observed market IV.

Opening and closing buys fill at ask, sells at bid; spreads are synthetic and
fees are booked separately. All open-position valuations use closing bid/ask:
longs at bid, shorts at ask. Unrealized P&L includes entry fees; close-now P&L
also subtracts future closing fees, so the totals agree when closing fees are zero.
Equity is cash plus signed closing value of open positions. Same-close fills are an
explicit manual-replay assumption; this is not a signal backtest with a
next-session execution model. Expiry cash-settles intrinsic value without
automatically reopening; holiday expiries use the last available close on or
before expiry. Dividends, early assignment, physical stock delivery and adjusted
option contracts after corporate actions are not modeled.

Full condor entry/roll checks equity against max expiry loss. Individual leg
adjustments use a conservative total-wing-width reserve and require short-leg
protection through expiry. Partial closes can leave uncovered shorts and the
UI warns that the original risk limits no longer apply. Condor risk metrics
describe only the current four same-expiry legs and exclude earlier realized
P&L and future closing fees. Mixed-expiry positions show portfolio P&L/Greeks
but suppress condor-specific expiry metrics.

The shared portfolio, exact-quote interface and multi-leg replay can be reused
for a reverse iron condor; its reversed action template and debit payoff rules
are a future addition. Real historical option data can implement
`OptionDataSource.get_chain` / `get_quote`; unavailable quotes are explicit
errors, never replaced with neighboring strikes or expiries.

Run all checks with `python -m unittest discover -s simulator/tests -v`.
UI interaction checks use Streamlit AppTest and mock only stock downloads and
the persistent store, so they do not contact your database.

# TESTS
```
 python3 -m simulator.tests.test_option_pricer -v
 python3 -m simulator.tests.test_portfolio -v
 python3 -m simulator.tests.test_pmcc -v
 python -m simulator.tests.test_engine
 python -m simulator.tests.test_session_store
 ```

 # Run PMCC

Running a fully automated backtest
```
python -m simulator.run_backtest

python -m simulator.run_backtest --ticker AAPL --start 2024-01-01 --end 2024-12-31
```

Running an interactive session : Note you need to adapt this script to add your own entry / exit / adjust logic , here is the template:

```
python3 -m simulator.try_session
```

UI using Streamlit 

```
PYTHONPATH=. streamlit run simulator/ui/app.py
PYTHONPATH=. streamlit run simulator/ui/app.py --server.port 8080
#if you want it reachable from another machine on your network, not just localhost
PYTHONPATH=. streamlit run simulator/ui/app.py --server.port 8080 --server.address 0.0.0.0
#stops it from trying to auto-open a browser tab (handy if you're running it somewhere without a display, e.g. a remote server)
PYTHONPATH=. streamlit run simulator/ui/app.py --server.port 8080 --server.address 0.0.0.0 --server.headless true
```

