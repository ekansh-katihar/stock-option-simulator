"""
simulator/run_backtest.py

THE thing you actually execute. Wires together price data, option pricing,
portfolio, strategy, and the engine; runs a full backtest; prints a report.

Fully automated -- no prompts. Uses the 5%-OTM-monthly-roll PMCC rule and a
10% stop-loss by default (see PMCCConfig for all the knobs).

Usage:
    python -m simulator.run_backtest
    python -m simulator.run_backtest --ticker AAPL --start 2024-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
from datetime import date

from simulator.data.price_history import fetch_price_history
from simulator.data.option_pricer import BlackScholesSource
from simulator.engine.portfolio import Portfolio
from simulator.strategies.pmcc import PMCCStrategy, PMCCConfig
from simulator.engine.simulator import BacktestEngine, BacktestResult


def run(
    ticker: str,
    start: date,
    end: date,
    starting_cash: float = 100_000,
    config: PMCCConfig | None = None,
) -> BacktestResult:
    prices = fetch_price_history(ticker, start, end)
    data_source = BlackScholesSource(price_history=prices)
    portfolio = Portfolio(starting_cash=starting_cash)
    strategy = PMCCStrategy(ticker, data_source, portfolio, config or PMCCConfig())
    engine = BacktestEngine(strategy, portfolio, data_source, prices)

    result = engine.run(start, end)
    _print_report(ticker, result)
    return result


def _print_report(ticker: str, result: BacktestResult) -> None:
    print(f"=== PMCC backtest report: {ticker} ===")
    print(f"Trading days simulated: {len(result.equity_curve)}")
    print(f"Short-call rolls: {len(result.roll_log)}")
    if result.stopped_out:
        print(f"Stop-loss triggered on {result.stopped_out_date}")

    print("\n--- Roll log ---")
    for r in result.roll_log:
        if r.settled_leg is not None:
            print(f"  [{r.date}] settled short strike {r.settled_leg.strike} "
                  f"-> pnl {r.settled_pnl:.2f}")
        if r.opened_leg is not None:
            print(f"  [{r.date}] opened short strike {r.opened_leg.strike}, "
                  f"expiry {r.opened_leg.expiry}, premium {r.opened_leg.entry_price}")

    print("\n--- Final summary ---")
    for k, v in result.final_summary.items():
        print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a PMCC backtest")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--cash", type=float, default=100_000)
    args = parser.parse_args()

    run(args.ticker, date.fromisoformat(args.start), date.fromisoformat(args.end), args.cash)
