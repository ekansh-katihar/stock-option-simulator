"""
simulator/storage/results_writer.py

Takes a completed backtest run (BacktestEngine + BacktestResult) and writes
it into the Postgres tables created by init_results_schema.py:
  - backtest_runs        (one row, metadata + resolved config)
  - daily_snapshots       (one row per simulated day -- accurate mode: a
                           fresh chain lookup is done for each leg on each
                           day, not reused/cached from the roll log)
  - roll_events           (one row per open/close/settle/stay-flat event)
  - corporate_events       (upserted, ticker-scoped -- earnings/dividend dates)
  - blackout_windows       (refreshed, ticker-scoped -- earnings + custom ranges)

Each call to write_backtest_run() creates a NEW run_id (gen_random_uuid()),
so daily_snapshots/roll_events never collide across separate runs -- no
upsert logic needed there. corporate_events/blackout_windows ARE shared
across runs for the same ticker, so those two are refreshed (delete+insert
or upsert) rather than duplicated every run.

Same caveat as the other Postgres files in this project: written against
the documented psycopg2 API, not executed against a live instance from
this environment. Run it yourself and report back any errors.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date

from simulator.data.option_pricer import OptionRight
from simulator.engine.portfolio import Portfolio, Leg, LegAction, LegStatus
from simulator.engine.simulator import BacktestResult
from simulator.strategies.pmcc import PMCCStrategy, PMCCConfig
from simulator.storage.init_results_schema import get_connection_string


# ---------------------------------------------------------------------
# JSON-safety helper for the config snapshot (dates/tuples aren't
# natively JSON-serializable -- same issue this project hit before in
# session.py's to_dict/from_dict).
# ---------------------------------------------------------------------

def _json_safe(obj):
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, tuple):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


# ---------------------------------------------------------------------
# Leg lookup helper -- which long/short leg (if any) was open on a given
# calendar day, searched across the full history of legs ever opened
# (Portfolio only tracks legs individually, not "what was active on date X"
# directly, so this reconstructs it from entry_date/close_date).
# ---------------------------------------------------------------------

def _leg_active_on(all_legs: list, right: OptionRight, action: LegAction, as_of: date) -> Leg | None:
    for leg in all_legs:
        if leg.right != right or leg.action != action:
            continue
        if leg.entry_date > as_of:
            continue
        if leg.status == LegStatus.CLOSED and leg.close_date is not None and as_of > leg.close_date:
            continue
        return leg
    return None


# ---------------------------------------------------------------------
# 1. backtest_runs
# ---------------------------------------------------------------------

def write_backtest_run(conn, underlying: str, start_date: date, end_date: date, config: PMCCConfig) -> str:
    config_json = json.dumps(_json_safe(asdict(config)))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_runs (underlying, start_date, end_date, config_snapshot)
            VALUES (%s, %s, %s, %s::jsonb)
            RETURNING run_id
            """,
            (underlying, start_date, end_date, config_json),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


# ---------------------------------------------------------------------
# 2. daily_snapshots
# ---------------------------------------------------------------------

def write_daily_snapshots(
    conn,
    run_id: str,
    underlying: str,
    strategy: PMCCStrategy,
    portfolio: Portfolio,
    data_source,
    price_history: dict,
    result: BacktestResult,
) -> None:
    from psycopg2.extras import execute_values

    all_legs = portfolio.open_legs() + portfolio.closed_legs()
    rows = []

    for point in result.equity_curve:
        d = point.date
        spot = price_history[d]

        long_leg = _leg_active_on(all_legs, OptionRight.CALL, LegAction.BUY, d)
        short_leg = _leg_active_on(all_legs, OptionRight.CALL, LegAction.SELL, d)

        long_contract = (
            portfolio.find_matching_contract(long_leg, d, data_source, underlying, spot)
            if long_leg is not None else None
        )
        short_contract = (
            portfolio.find_matching_contract(short_leg, d, data_source, underlying, spot)
            if short_leg is not None else None
        )

        # iv / iv_rank are chain-wide values in BlackScholesSource today
        # (same across every contract for a given day) -- pull from
        # whichever leg's contract we already fetched.
        ref_contract = long_contract or short_contract
        iv = ref_contract.iv if ref_contract is not None else None
        iv_rank = ref_contract.iv_rank if ref_contract is not None else None

        short_days_to_expiry = (short_leg.expiry - d).days if short_leg is not None else None
        is_blackout = strategy.is_blackout_day(d)

        rows.append((
            run_id, underlying, d,
            spot,
            point.cash, point.realized_pnl, point.unrealized_pnl, point.total_equity,
            long_leg.strike if long_leg else None,
            long_leg.expiry if long_leg else None,
            # Keep legacy *_mid_price column names for schema compatibility;
            # stored values now use the executable closing side.
            long_contract.bid if long_contract else None,
            long_contract.delta if long_contract else None,
            short_leg.strike if short_leg else None,
            short_leg.expiry if short_leg else None,
            short_contract.ask if short_contract else None,
            short_contract.delta if short_contract else None,
            short_days_to_expiry,
            iv, iv_rank,
            is_blackout,
        ))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO daily_snapshots (
                run_id, underlying, snapshot_date,
                spot_price,
                cash, realized_pnl, unrealized_pnl, total_equity,
                long_leg_strike, long_leg_expiry, long_leg_mid_price, long_leg_delta,
                short_leg_strike, short_leg_expiry, short_leg_mid_price, short_leg_delta,
                short_leg_days_to_expiry,
                iv, iv_rank,
                is_blackout_day
            ) VALUES %s
            """,
            rows,
        )
    conn.commit()
    print(f"Wrote {len(rows)} daily_snapshots rows for run {run_id}")


# ---------------------------------------------------------------------
# 3. roll_events
# ---------------------------------------------------------------------

def write_roll_events(conn, run_id: str, underlying: str, portfolio: Portfolio, result: BacktestResult) -> None:
    from psycopg2.extras import execute_values

    rows = []
    all_legs = portfolio.open_legs() + portfolio.closed_legs()
    long_leg = next((l for l in all_legs if l.right == OptionRight.CALL and l.action == LegAction.BUY), None)
    if long_leg is not None:
        rows.append((
            run_id, underlying, long_leg.entry_date, "opened_long", None,
            long_leg.strike, long_leg.expiry, long_leg.entry_price, None, None,
        ))
    for r in result.roll_log:
        if r.opened_leg is not None:
            rows.append((
                run_id, underlying, r.date, "opened_short", None,
                r.opened_leg.strike, r.opened_leg.expiry, r.opened_leg.entry_price, None, None,
            ))
        if r.settled_leg is not None:
            event_type = "closed_early" if r.closed_early else "settled"
            rows.append((
                run_id, underlying, r.date, event_type, r.closed_early_reason,
                r.settled_leg.strike, r.settled_leg.expiry,
                r.settled_leg.entry_price, r.settled_leg.close_price, r.settled_pnl,
            ))
        if r.stayed_flat_reason is not None:
            rows.append((
                run_id, underlying, r.date, "stayed_flat", r.stayed_flat_reason,
                None, None, None, None, None,
            ))

    if not rows:
        return

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO roll_events (
                run_id, underlying, event_date, event_type, reason,
                strike, expiry, entry_price, close_price, pnl
            ) VALUES %s
            """,
            rows,
        )
    conn.commit()
    print(f"Wrote {len(rows)} roll_events rows for run {run_id}")


# ---------------------------------------------------------------------
# 4a/4b. corporate_events + blackout_windows -- ticker-scoped, refreshed
# (not tied to a single run_id).
# ---------------------------------------------------------------------

def write_corporate_events(conn, underlying: str, earnings_dates: list, dividend_dates: list) -> None:
    from psycopg2.extras import execute_values

    rows = [(underlying, "earnings", d) for d in earnings_dates]
    rows += [(underlying, "dividend", d) for d in dividend_dates]
    if not rows:
        return

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO corporate_events (underlying, event_type, event_date)
            VALUES %s
            ON CONFLICT (underlying, event_type, event_date) DO NOTHING
            """,
            rows,
        )
    conn.commit()
    print(f"Upserted {len(rows)} corporate_events rows for {underlying}")


def write_blackout_windows(conn, underlying: str, earnings_dates: list, config: PMCCConfig) -> None:
    from datetime import timedelta
    from psycopg2.extras import execute_values

    rows = []
    for e in earnings_dates:
        start = e - timedelta(days=config.earnings_blackout_days_before)
        end = e + timedelta(days=config.earnings_blackout_days_after)
        rows.append((underlying, "earnings", start, end, f"Earnings blackout ({e.isoformat()})"))

    for start, end in config.custom_blackout_ranges:
        rows.append((underlying, "custom", start, end, "Custom blackout"))

    with conn.cursor() as cur:
        # Ticker-scoped table with no unique constraint -- refresh (delete +
        # reinsert) rather than upsert, so reruns don't accumulate duplicates.
        cur.execute("DELETE FROM blackout_windows WHERE underlying = %s", (underlying,))
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO blackout_windows (underlying, source, start_date, end_date, label)
                VALUES %s
                """,
                rows,
            )
    conn.commit()
    print(f"Refreshed {len(rows)} blackout_windows rows for {underlying}")


# ---------------------------------------------------------------------
# Convenience: do everything for one completed run in one call.
# ---------------------------------------------------------------------

def write_full_backtest_results(
    underlying: str,
    start_date: date,
    end_date: date,
    config: PMCCConfig,
    strategy: PMCCStrategy,
    portfolio: Portfolio,
    data_source,
    price_history: dict,
    result: BacktestResult,
    earnings_dates: list,
    dividend_dates: list,
    connection_string: str | None = None,
) -> str:
    import psycopg2

    connection_string = connection_string or get_connection_string()
    with psycopg2.connect(connection_string, connect_timeout=10) as conn:
        run_id = write_backtest_run(conn, underlying, start_date, end_date, config)
        write_daily_snapshots(conn, run_id, underlying, strategy, portfolio, data_source, price_history, result)
        write_roll_events(conn, run_id, underlying, portfolio, result)
        write_corporate_events(conn, underlying, earnings_dates, dividend_dates)
        write_blackout_windows(conn, underlying, earnings_dates, config)

    print(f"\nDone. run_id = {run_id}")
    return run_id
