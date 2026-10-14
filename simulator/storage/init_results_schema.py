"""
simulator/storage/init_results_schema.py

Creates the PMCC backtest-results tables (backtest_runs, daily_snapshots,
roll_events, corporate_events, blackout_windows) in Postgres/Supabase.

Meant to be run locally, once, whenever the schema changes:
    python -m simulator.storage.init_results_schema

Reads the connection string from .streamlit/secrets.toml (the same file
your Streamlit app already uses), under the key `postgres_connection_string`:

    # .streamlit/secrets.toml
    postgres_connection_string = "postgresql://user:pass@host:5432/dbname"

This module does NOT import streamlit -- it parses the TOML file directly,
so it can be run as a plain script outside the Streamlit runtime. If the
secrets file or key isn't found, it falls back to the
PMCC_POSTGRES_CONNECTION_STRING environment variable.

Same caveat as postgres_session_store.py: this has been written against the
documented psycopg2 API but not run against a live Postgres instance from
this environment (no network path to external DB hosts here) -- run it
yourself and report back any errors.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCHEMA_SQL = """
-- ============================================================
-- PMCC Backtest Results Schema (for Grafana analysis)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    underlying      TEXT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    config_snapshot JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id                      BIGSERIAL PRIMARY KEY,
    run_id                  UUID NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    underlying              TEXT NOT NULL,
    snapshot_date           DATE NOT NULL,

    spot_price              NUMERIC(12, 4) NOT NULL,

    cash                    NUMERIC(14, 4) NOT NULL,
    realized_pnl            NUMERIC(14, 4) NOT NULL,
    unrealized_pnl          NUMERIC(14, 4) NOT NULL,
    total_equity            NUMERIC(14, 4) NOT NULL,

    long_leg_strike         NUMERIC(12, 4),
    long_leg_expiry         DATE,
    long_leg_mid_price      NUMERIC(12, 4),
    long_leg_delta          NUMERIC(6, 4),

    short_leg_strike        NUMERIC(12, 4),
    short_leg_expiry        DATE,
    short_leg_mid_price     NUMERIC(12, 4),
    short_leg_delta         NUMERIC(6, 4),
    short_leg_days_to_expiry INTEGER,

    iv                      NUMERIC(6, 4),
    iv_rank                 NUMERIC(5, 4),

    is_blackout_day         BOOLEAN NOT NULL DEFAULT FALSE,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (run_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_snapshots_run_date
    ON daily_snapshots (run_id, snapshot_date);

CREATE TABLE IF NOT EXISTS roll_events (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    underlying      TEXT NOT NULL,
    event_date      DATE NOT NULL,

    event_type      TEXT NOT NULL,
    reason          TEXT,

    strike          NUMERIC(12, 4),
    expiry          DATE,
    entry_price     NUMERIC(12, 4),
    close_price     NUMERIC(12, 4),
    pnl             NUMERIC(14, 4),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_roll_events_run_date
    ON roll_events (run_id, event_date);

CREATE TABLE IF NOT EXISTS corporate_events (
    id              BIGSERIAL PRIMARY KEY,
    underlying      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    event_date      DATE NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (underlying, event_type, event_date)
);

CREATE INDEX IF NOT EXISTS idx_corporate_events_ticker_date
    ON corporate_events (underlying, event_date);

CREATE TABLE IF NOT EXISTS blackout_windows (
    id              BIGSERIAL PRIMARY KEY,
    underlying      TEXT NOT NULL,
    source          TEXT NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    label           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (end_date >= start_date)
);

CREATE INDEX IF NOT EXISTS idx_blackout_windows_ticker_dates
    ON blackout_windows (underlying, start_date, end_date);
"""


def _load_toml(path: Path) -> dict:
    """Parse a TOML file without requiring streamlit to be installed/running."""
    try:
        import tomllib  # Python 3.11+
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ModuleNotFoundError:
        import toml  # pip install toml  -- fallback for Python < 3.11
        with open(path, "r") as f:
            return toml.load(f)


def get_connection_string(secrets_path: str | None = None) -> str:
    """
    Reads `postgres_connection_string` from the given secrets.toml path
    (the project's .streamlit/secrets.toml by default, independent of the
    current working directory). Explicit paths are used as supplied. Falls back to the
    PMCC_POSTGRES_CONNECTION_STRING environment variable if the file or key
    isn't found.
    """
    path = (
        Path(secrets_path)
        if secrets_path is not None
        else Path(__file__).resolve().parents[2] / ".streamlit" / "secrets.toml"
    )
    if path.exists():
        secrets = _load_toml(path)
        conn_str = secrets.get("postgres_connection_string")
        if conn_str:
            return conn_str

    env_conn_str = os.environ.get("PMCC_POSTGRES_CONNECTION_STRING")
    if env_conn_str:
        return env_conn_str

    raise RuntimeError(
        f"No Postgres connection string found. Checked '{path}' for "
        f"'postgres_connection_string' and the PMCC_POSTGRES_CONNECTION_STRING "
        f"environment variable -- neither was set."
    )


def init_schema(connection_string: str | None = None) -> None:
    import psycopg2  # imported lazily, same reasoning as postgres_session_store.py

    connection_string = connection_string or get_connection_string()

    print("Connecting to Postgres...")
    with psycopg2.connect(connection_string, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            print("Running schema (CREATE TABLE / CREATE INDEX, all IF NOT EXISTS)...")
            cur.execute(SCHEMA_SQL)
        conn.commit()
    print("Schema created/verified successfully:")
    print("  - backtest_runs")
    print("  - daily_snapshots")
    print("  - roll_events")
    print("  - corporate_events")
    print("  - blackout_windows")


if __name__ == "__main__":
    try:
        init_schema()
    except Exception as e:
        print(f"Failed to initialize schema: {e}", file=sys.stderr)
        sys.exit(1)
