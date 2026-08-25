"""
simulator/storage/postgres_session_store.py

Persists TradingSession state to Postgres (built for Supabase, but works
against any standard Postgres -- Neon, RDS, local, etc.). One row per
session, state stored as a single JSONB blob rather than normalized into
separate tables -- nothing currently needs to run SQL queries against
individual legs, so full normalization isn't justified yet (revisit if a
real query need shows up later).

IMPORTANT CAVEAT: this file's SQL has NOT been run against a live Postgres
instance -- this sandbox has no network path to external database hosts.
The serialization logic it depends on (Leg/Portfolio/TradingSession
to_dict/from_dict) IS tested, without a database, in test_serialization.py.
Test this file's actual DB calls yourself and report back any errors.
"""

from __future__ import annotations

import json
from datetime import date

from simulator.data.option_pricer import BlackScholesSource
from simulator.engine.session import TradingSession
from simulator.storage.session_store import SessionStore

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trading_sessions (
    session_id TEXT PRIMARY KEY,
    state JSONB NOT NULL,
    price_history JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PostgresSessionStore(SessionStore):
    """
    create() is an upsert (create-or-replace) -- call it again after any
    mutating action (open/close/advance) to persist the updated state, not
    just once at session start.
    """

    def __init__(self, connection_string: str):
        import psycopg2  # imported lazily so this module doesn't hard-require
                          # psycopg2 for people only using InMemorySessionStore
        self._psycopg2 = psycopg2
        self._conn_string = connection_string
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
            conn.commit()

    def _connect(self):
        return self._psycopg2.connect(self._conn_string)

    def create(self, session_id: str, session: TradingSession) -> None:
        state = session.to_dict()
        price_history = {d.isoformat(): p for d, p in session.price_history.items()}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trading_sessions (session_id, state, price_history, updated_at)
                    VALUES (%s, %s::jsonb, %s::jsonb, now())
                    ON CONFLICT (session_id)
                    DO UPDATE SET state = EXCLUDED.state,
                                  price_history = EXCLUDED.price_history,
                                  updated_at = now()
                    """,
                    (session_id, json.dumps(state), json.dumps(price_history)),
                )
            conn.commit()

    def get(self, session_id: str) -> TradingSession | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, price_history FROM trading_sessions WHERE session_id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None

        state, price_history_raw = row
        price_history = {date.fromisoformat(d): p for d, p in price_history_raw.items()}
        data_source = BlackScholesSource(price_history=price_history)
        return TradingSession.from_dict(state, data_source=data_source, price_history=price_history)

    def delete(self, session_id: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM trading_sessions WHERE session_id = %s", (session_id,))
            conn.commit()

    def list_ids(self) -> list:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT session_id FROM trading_sessions")
                return [row[0] for row in cur.fetchall()]
