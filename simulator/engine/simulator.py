"""
simulator/engine/simulator.py

Day-by-day loop that drives a strategy (currently PMCCStrategy) over a real
price history and records what happened. This is orchestration only -- all
the actual decisions live in the strategy, all the money math lives in
Portfolio. Not the end-user entry point itself; a thin driver script/notebook
cell is expected to sit on top of this (see usage example at the bottom).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from simulator.data.option_pricer import OptionDataSource
from simulator.engine.portfolio import Portfolio
from simulator.strategies.pmcc import PMCCStrategy, RollResult


@dataclass
class EquityPoint:
    date: date
    cash: float
    realized_pnl: float
    unrealized_pnl: float
    total_equity: float


@dataclass
class BacktestResult:
    equity_curve: list = field(default_factory=list)   # list[EquityPoint]
    roll_log: list = field(default_factory=list)        # list[RollResult]
    final_summary: dict = field(default_factory=dict)
    stopped_out: bool = False
    stopped_out_date: date | None = None


class BacktestEngine:

    def __init__(
        self,
        strategy: PMCCStrategy,
        portfolio: Portfolio,
        data_source: OptionDataSource,
        price_history: dict,
    ):
        self._strategy = strategy
        self._portfolio = portfolio
        self._data_source = data_source
        self._price_history = price_history

        self._long_call_opened = False

    # ------------------------------------------------------------------
    # Single-day step -- exposed separately so a notebook can step through
    # manually, and so Phase 2's LLM hook has a natural per-day insertion
    # point without touching the loop below.
    # ------------------------------------------------------------------

    def step(self, current_date: date, spot_price: float) -> dict:
        info = {"date": current_date, "opened_long": False,
                "stopped_out": False, "stop_loss_pnl": None, "roll_result": None}

        if not self._long_call_opened:
            self._strategy.open_long_call(current_date, spot_price)
            self._long_call_opened = True
            info["opened_long"] = True

        elif self._strategy.is_active():
            if self._strategy.check_stop_loss(spot_price):
                pnl = self._strategy.close_long_call(current_date, spot_price)
                info["stopped_out"] = True
                info["stop_loss_pnl"] = pnl
            else:
                info["roll_result"] = self._strategy.roll_short_call(current_date, spot_price)

        info["equity"] = self._record_equity(current_date, spot_price)
        return info

    def _record_equity(self, current_date: date, spot_price: float) -> EquityPoint:
        summary = self._portfolio.summary(
            current_date, self._data_source, self._strategy.underlying, spot_price
        )
        return EquityPoint(
            date=current_date,
            cash=summary["cash"],
            realized_pnl=summary["realized_pnl"],
            unrealized_pnl=summary["unrealized_pnl"],
            total_equity=summary["total_equity"],
        )

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------

    def run(self, start_date: date, end_date: date) -> BacktestResult:
        result = BacktestResult()
        dates = sorted(d for d in self._price_history if start_date <= d <= end_date)

        for d in dates:
            spot = self._price_history[d]
            info = self.step(d, spot)

            result.equity_curve.append(info["equity"])
            roll_result = info["roll_result"]
            if roll_result is not None and (roll_result.settled_leg is not None
                                             or roll_result.opened_leg is not None):
                result.roll_log.append(roll_result)
            if info["stopped_out"]:
                result.stopped_out = True
                result.stopped_out_date = d

        if dates:
            last_date = dates[-1]
            last_spot = self._price_history[last_date]
            result.final_summary = self._portfolio.summary(
                last_date, self._data_source, self._strategy.underlying, last_spot
            )

        return result


# ---------------------------------------------------------------------
# Example usage (not executed on import):
#
#   from datetime import date
#   from simulator.data.price_history import fetch_price_history
#   from simulator.data.option_pricer import BlackScholesSource
#   from simulator.engine.portfolio import Portfolio
#   from simulator.strategies.pmcc import PMCCStrategy, PMCCConfig
#   from simulator.engine.simulator import BacktestEngine
#
#   prices = fetch_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
#   data_source = BlackScholesSource(price_history=prices)
#   portfolio = Portfolio(starting_cash=100_000)
#   strategy = PMCCStrategy("AAPL", data_source, portfolio, PMCCConfig())
#   engine = BacktestEngine(strategy, portfolio, data_source, prices)
#   result = engine.run(date(2024, 1, 1), date(2024, 12, 31))
#   print(result.final_summary)
# ---------------------------------------------------------------------