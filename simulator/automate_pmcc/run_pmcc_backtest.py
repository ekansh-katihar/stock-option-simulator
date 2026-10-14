"""
run_pmcc_backtest.py

End-to-end example wiring together everything built in this session:
YAML config -> PMCCConfig -> earnings/dividend dates -> PMCCStrategy ->
BacktestEngine -> a printed summary.

Run from your project root (the directory containing simulator/):
    python -m simulator.automate_pmcc.run_pmcc_backtest
"""

from datetime import date
from pathlib import Path

from simulator.data.price_history import fetch_price_history
from simulator.data.option_pricer import BlackScholesSource
from simulator.data.corporate_events import fetch_earnings_dates, fetch_dividend_dates
from simulator.engine.portfolio import Portfolio
from simulator.engine.simulator import BacktestEngine, EquityPoint
from simulator.strategies.pmcc import PMCCStrategy
from simulator.strategies.config_loader import load_yaml, build_pmcc_config
from simulator.storage.results_writer import write_full_backtest_results


def main():
    raw_config = load_yaml(Path(__file__).resolve().with_name("pmcc_strategy_config.yaml"))
    underlying_cfg = raw_config["underlying"]

    ticker = underlying_cfg["tickers"][0]   # first ticker only for now -- see note below
    start_date = date.fromisoformat(underlying_cfg["simulation_start_date"])
    end_date = date.fromisoformat(underlying_cfg["simulation_end_date"])

    config = build_pmcc_config(raw_config)
    print(f"Loaded config: exit_delta_threshold={config.exit_delta_threshold}, "
          f"exit_days_before_expiry={config.exit_days_before_expiry}, "
          f"enforce_strike_safety={config.enforce_strike_safety}")

    # 2. Fetch real stock price history (yfinance).
    prices = fetch_price_history(ticker, start_date, end_date)
    print(f"Fetched {len(prices)} daily closes for {ticker}")

    # 3. Fetch corporate events -- needed for blackout + dividend-risk rules.
    #    NOTE: fetch_earnings_dates/fetch_dividend_dates hit the network; if
    #    this errors out (rate limit, no network in this environment, etc.)
    #    fall back to empty lists so the backtest still runs, just without
    #    those two rules active.
    try:
        earnings_dates = fetch_earnings_dates(ticker)
    except Exception as e:
        print(f"Could not fetch earnings dates ({e}); continuing with none.")
        earnings_dates = []

    try:
        dividend_dates = fetch_dividend_dates(ticker)
    except Exception as e:
        print(f"Could not fetch dividend dates ({e}); continuing with none.")
        dividend_dates = []

    # 4. Build the synthetic option chain source (Phase 1 -- no options API needed).
    data_source = BlackScholesSource(price_history=prices)

    # 5. Wire up the strategy and engine.
    portfolio = Portfolio(starting_cash=100_000)
    strategy = PMCCStrategy(
        ticker, data_source, portfolio, config,
        earnings_dates=earnings_dates,
        dividend_dates=dividend_dates,
    )
    engine = BacktestEngine(strategy, portfolio, data_source, prices)

    # 6. Run it.
    result = engine.run(start_date, end_date)

    # 7. Print what happened.
    print(f"\n--- Roll log ({len(result.roll_log)} events) ---")
    for r in result.roll_log:
        if r.opened_leg is not None:
            print(f"  {r.date}: opened short strike={r.opened_leg.strike} expiry={r.opened_leg.expiry}")
        if r.settled_leg is not None:
            tag = "CLOSED EARLY" if r.closed_early else "SETTLED"
            reason = f" ({r.closed_early_reason})" if r.closed_early_reason else ""
            print(f"  {r.date}: {tag}{reason} strike={r.settled_leg.strike} pnl={r.settled_pnl:.2f}")
        if r.stayed_flat_reason is not None:
            print(f"  {r.date}: stayed flat ({r.stayed_flat_reason})")

    long_leg = strategy.status()["long_leg"]
    if long_leg is not None:
        print(f"\nLong leg: strike={long_leg.strike} expiry={long_leg.expiry} entry_price={long_leg.entry_price}")

    print(f"\n--- Final summary (mark-to-market, nothing force-closed) ---")
    for k, v in result.final_summary.items():
        print(f"  {k}: {v}")

    # Force-close any remaining open legs at the last available price, so we
    # can compare "still holding" (mark-to-market above) vs "fully realized".
    last_date = max(d for d in prices if start_date <= d <= end_date)
    last_spot = prices[last_date]

    if strategy.status()["short_leg"] is not None:
        strategy.close_short_call(last_date, last_spot)
    if strategy.status()["long_leg"] is not None:
        strategy.close_long_call(last_date, last_spot)

    closed_out_summary = portfolio.summary(last_date, data_source, ticker, last_spot)
    print(f"\n--- Final summary (all legs force-closed) ---")
    for k, v in closed_out_summary.items():
        print(f"  {k}: {v}")

    # result.equity_curve's last entry was built inside engine.run(), BEFORE
    # the force-close above happened -- overwrite it so daily_snapshots'
    # final row reflects the fully-closed state, not the stale mark-to-market
    # snapshot (which we already printed separately above).
    if result.equity_curve and result.equity_curve[-1].date == last_date:
        result.equity_curve[-1] = EquityPoint(
            date=last_date,
            cash=closed_out_summary["cash"],
            realized_pnl=closed_out_summary["realized_pnl"],
            unrealized_pnl=closed_out_summary["unrealized_pnl"],
            total_equity=closed_out_summary["total_equity"],
        )

    if result.stopped_out:
        print(f"\nStop-loss triggered on {result.stopped_out_date}")

    # 8. Write everything to Postgres for Grafana analysis.
    print("\nWriting results to Postgres...")
    write_full_backtest_results(
        underlying=ticker,
        start_date=start_date,
        end_date=end_date,
        config=config,
        strategy=strategy,
        portfolio=portfolio,
        data_source=data_source,
        price_history=prices,
        result=result,
        earnings_dates=earnings_dates,
        dividend_dates=dividend_dates,
    )


if __name__ == "__main__":
    main()
