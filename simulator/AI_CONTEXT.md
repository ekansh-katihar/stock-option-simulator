# PMCC Options Strategy Simulator — Project Summary

**Purpose:** Backtest and interactively simulate options strategies (starting with Poor Man's Covered Call), initially fully-automated, now with a manual/interactive mode and a Streamlit UI on top. Long-term goal: swap manual decision points for an LLM agent (Phase 2, not started).

**Status as of this handoff:** Core simulation engine (data → portfolio → strategy → engine → interactive session → UI) is built and unit-tested (61 tests passing). UI is functional but only partially validated interactively — see Known Issues.

---

## Architecture

```
simulator/
  data/
    option_pricer.py     # OptionContract model, OptionDataSource interface,
                          # BlackScholesSource (synthetic pricing), QCContractAdapter (stub)
    price_history.py      # Real stock price fetch via yfinance
  engine/
    portfolio.py           # Leg dataclass, Portfolio (cash/P&L bookkeeping)
    strategies/pmcc.py      # PMCCStrategy (decision logic) -- note: actually at simulator/strategies/pmcc.py
    simulator.py             # BacktestEngine (fully automated day-by-day loop)
    session.py                # TradingSession (interactive/manual control, primitives only)
  strategies/
    pmcc.py                    # PMCCStrategy + PMCCConfig + RollResult
  storage/
    session_store.py            # SessionStore interface + InMemorySessionStore
  ui/
    app.py                       # Streamlit dashboard, wraps TradingSession directly
  run_backtest.py                 # CLI entry point for the fully-automated backtest
  tests/                           # unittest suite, one file per module, 61 tests total
requirements.txt                   # yfinance, pandas, streamlit, plotly
```

### Layered design (each layer only depends on the one below it)
1. **`OptionDataSource`** (ABC) — anything that can produce an option chain for (underlying, date, spot price). `BlackScholesSource` is the only working implementation (synthesizes a chain from stock price history + realized volatility, no options API needed). `QCContractAdapter` is a stub for real QuantConnect data, not implemented.
2. **`Portfolio`** — owns cash and `Leg` records (our positions). Knows nothing about strategy or data source. Handles opening, closing, settling legs, and mark-to-market valuation.
3. **`PMCCStrategy`** — decision logic (which contract to pick, when to roll). Talks to `Portfolio` and `OptionDataSource` only through their interfaces.
4. **Two ways to drive a strategy:**
   - **`BacktestEngine`** — fully automated, walks a whole date range in one pass, no human input. Used by `run_backtest.py`.
   - **`TradingSession`** — interactive/manual. Exposes primitives only (`open_long`, `close_long`, `open_short`, `close_short`, `advance`, `check_stop_loss`, `view_chain`, `snapshot`, `history`). **Deliberately has no compound "roll" or "close everything" commands** — the user composes those from close+open by design (explicit user decision, see conversation history).
5. **`SessionStore`** — holds `TradingSession` instances. Only `InMemorySessionStore` exists; `SQLiteSessionStore` was discussed but deferred until UI/data shape stabilizes.
6. **`ui/app.py`** — Streamlit dashboard, calls `TradingSession` methods directly (same Python process, no backend API). This was a deliberate choice over React specifically to avoid needing a FastAPI backend right now; migration path to React+FastAPI was discussed and is low-cost since the entire `simulator/` package has zero UI-specific code in it.

---

## Key design decisions worth knowing

- **No real orders placed.** Everything is our own ledger (`Portfolio`), not LEAN/broker order execution — avoids margin-system complexity we don't need for backtesting.
- **`BlackScholesSource` has a volatility floor (`max(realized_vol, 0.05)`)** — without it, smooth/linear synthetic price data collapses realized vol to ~0%, degenerating all greeks to binary 0/1. Real market data won't hit this edge case, but any synthetic test data should stay noisy.
- **`TradingSession.advance()` auto-settles any leg whose expiry is crossed, but never auto-reopens a replacement** — that's an explicit user action, matching "let it settle/expire ITM" as the passive default while keeping rolls manual.
- **Config-driven strategy behavior** (`PMCCConfig`: long delta target, short OTM%/delta, expiry windows, stop-loss %) — intentional, so Phase 2's LLM can override config/behavior without rewriting the strategy class.
- **The Adapter pattern is used twice** (`OptionDataSource` for pricing, `SessionStore` for persistence) — both have a working simple implementation now and a documented swap-in-later path (`QCContractAdapter`, `SQLiteSessionStore`) without touching calling code.

---

## What's been tested vs. not

**Unit tested (61 tests, all passing as of last run):** Black-Scholes math sanity checks, chain generation, contract selection, all Portfolio cash/settlement math (including edge cases: double-settle guard, ITM/OTM short settlement, early close in both directions), PMCCStrategy long/short open/roll/stop-loss logic, BacktestEngine full-year run + stop-loss path, TradingSession primitives, InMemorySessionStore.

**Not tested / caveats:**
- **`price_history.py`'s real yfinance call was never tested end-to-end by Claude** — my sandbox can't reach Yahoo Finance's network endpoints, so only the parsing logic was validated against a mocked response. The user has since run it successfully for real.
- **`ui/app.py`'s interactive behavior (button clicks, form submission, session-state reruns) could not be tested by Claude** — Streamlit needs a live server + browser. Only syntax/import correctness was verified in-sandbox. **Two real bugs were found by the user running it locally and fixed:**
  1. `TradingSession(start_date=price_start)` crashed with `KeyError` when `price_start` fell on a non-trading day (e.g. New Year's Day) — fixed by snapping to `min(d for d in prices if d >= price_start)`.
  2. The long/short leg "select by" radio buttons were placed *inside* `st.form(...)`, so switching modes didn't trigger a rerun and the wrong input silently stayed active — fixed by moving mode radios outside their forms.
  - **Given this pattern, further interactive bugs in `app.py` are plausible and should be expected** — the user is the only one who can actually exercise the live UI.

---

## Real backtest result obtained (for context, not necessarily current)

One full run via `run_backtest.py` on real AAPL 2024 data (Black-Scholes pricing, 5%-OTM monthly short calls, no active management) produced: realized P&L −$976.57, unrealized +$2,036.00, total equity $96,238.43 (−3.76% for the year), vs. AAPL's actual ~30%+ rise — illustrating the structural cost of an uncapped-upside stock outperforming a capped-upside strategy with no defensive rolling. This was **synthetic Black-Scholes pricing**, not real option chain data, so exact figures would shift against real historical quotes (especially around AAPL's 4 annual earnings dates, which realized-vol-based pricing won't capture).

---

## Explicitly deferred / not built

- **`QCContractAdapter`** — real QuantConnect option chain data source. Stubbed (`raise NotImplementedError`). Was the original data plan before pivoting to free Black-Scholes synthesis for faster iteration; swap back in once product viability is validated.
- **`SQLiteSessionStore`** — persistent session storage. Deferred until UI/interaction patterns stabilize, to avoid designing a schema prematurely.
- **Active-management strategy variant** — early short-call rolling when it goes ITM, long-call roll-down on stop-loss trigger. Discussed at length (scenarios walked through in detail) but not implemented; current `PMCCStrategy`/`BacktestEngine` only support the fully passive version.
- **Compound session commands** (`roll_short`, `roll_long_down`, `close_all`) — deliberately removed after being built once; user wants only primitives (`close` + `open` composed manually).
- **Phase 2 (LLM-driven decisions)** — not started. The interactive command set (`TradingSession` primitives) was explicitly designed so the same functions could later be handed to an LLM as tool definitions instead of a human calling them, without changing the engine.
- **Charting inside the chat/artifact layer** (lightweight-charts / KLineChart) — discussed as a future possibility once a real UI exists; not built. Streamlit's Plotly equity curve is the only charting so far.
- **React frontend + FastAPI backend** — discussed as an alternative to Streamlit; not built. Migration path assessed as low-cost since `simulator/` has no UI coupling.
- **Save/load session state, undo** — explicitly deferred by user request ("lowest amount of changes" instruction).
- **Iron Condor / Reverse Iron Condor strategies** — original 3-strategy goal from early in the project; only PMCC has been built so far.

---

## How to run things today

```bash
pip install -r requirements.txt

# Fully automated backtest (no UI, no interactivity):
python -m simulator.run_backtest --ticker AAPL --start 2024-01-01 --end 2024-12-31

# Interactive Streamlit dashboard:
streamlit run simulator/ui/app.py --server.port 8502

# Run all tests:
python -m unittest discover -s simulator/tests -v
```

---

## Conversational/process context worth knowing

- User is comparing an interactive/manual PMCC workflow against building automated rules, and evaluating whether "active management" (rolling before assignment) actually beats the passive baseline — that comparison hasn't been run yet.
- User previously evaluated and rejected several off-the-shelf backtesting tools (Pineify, ORATS, ThinkBack) as insufficient for the conditional/path-dependent decision logic this project needs — that research is why a custom engine was built instead of using an existing product.
- User has a QuantConnect account and originally started prototyping there (hello-world scripts for pulling greeks) before pivoting to the current pure-Python, Black-Scholes-first architecture for faster, dependency-free iteration. QuantConnect remains the intended real-data source later via `QCContractAdapter`.
- User is Java-background, prefers explicit interfaces/ABCs over Python's more implicit conventions (e.g. requested an ABC-based Adapter pattern rather than a bare function) — worth keeping that in mind for future extensions to remain consistent with their taste.