"""
simulator/ui/app.py

Streamlit dashboard for TradingSession. Runs in the same Python process as
the simulator -- no backend API needed, buttons call session methods directly.

Run:  streamlit run simulator/ui/app.py

NOTE: row selection in the "Browse chain" tables requires Streamlit >= 1.35
      (st.dataframe's on_select/selection_mode args). Check with:
          python -c "import streamlit; print(streamlit.__version__)"
      If you're on an older version, `pip install -U streamlit` first --
      on an old version this will silently fall back to a non-interactive
      table (no crash, but clicking still won't do anything).

Contract selection: Browse chain is now the only way to pick a contract for
either leg -- filter by strike/expiry/delta, click a row, then "Open leg".
No more Target-delta/OTM quick-entry radio; that was removed in favor of
always picking an exact contract from the filtered table.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import streamlit as st
import plotly.graph_objects as go

import uuid

from simulator.data.price_history import fetch_price_history
from simulator.data.option_pricer import BlackScholesSource
from simulator.data.corporate_events import (
    fetch_earnings_dates, fetch_dividend_dates, next_event_on_or_after,
)
from simulator.engine.portfolio import Portfolio
from simulator.engine.session import TradingSession
from simulator.storage.session_store import InMemorySessionStore

st.set_page_config(page_title="PMCC Simulator", layout="wide")

# Unique per browser session -- without this, every visitor would share the
# exact same TradingSession, stepping on each other's trades.
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
SESSION_ID = st.session_state.session_id


def _build_store():
    """Postgres (Supabase/Neon/etc.) if configured via st.secrets, else
    in-memory only -- lets local dev work without a database set up."""
    conn_string = st.secrets.get("postgres_connection_string") if hasattr(st, "secrets") else None
    if conn_string:
        from simulator.storage.postgres_session_store import PostgresSessionStore
        return PostgresSessionStore(conn_string)
    return InMemorySessionStore()


if "store" not in st.session_state:
    st.session_state.store = _build_store()
if "equity_log" not in st.session_state:
    st.session_state.equity_log = []  # UI-level history for the chart, separate from session.history()
if "starting_cash" not in st.session_state:
    st.session_state.starting_cash = 100_000

store = st.session_state.store


def _record_equity():
    """Called after every mutating action. Also persists the session --
    with PostgresSessionStore this is a real save; with InMemorySessionStore
    it's a cheap no-op-ish dict reassignment, safe to call every time."""
    session = store.get(SESSION_ID)
    if session is not None:
        store.create(SESSION_ID, session)
        snap = session.snapshot()
        st.session_state.equity_log.append({"date": snap["date"], "total_equity": snap["total_equity"]})


def _chain_df(candidates) -> pd.DataFrame:
    """Normalize whatever view_chain() returns into a DataFrame for display+selection.
    'expiry' is guaranteed to be plain python date objects (not pandas Timestamps),
    regardless of how this pandas version infers dtypes from a list of dicts."""
    df = candidates if isinstance(candidates, pd.DataFrame) else pd.DataFrame(candidates)
    if "expiry" in df.columns and len(df):
        df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
    return df


@st.cache_data(ttl=3600)
def _cached_events(ticker: str):
    return fetch_earnings_dates(ticker), fetch_dividend_dates(ticker)


# Pagination is for display only -- filtering (strike/delta/expiry) happens
# server-side in session.view_chain, which never truncates.
CHAIN_PAGE_SIZE = 10


def _clamp_page(page_key: str, total_pages: int) -> int:
    """Read the current page for a pager, clamped to valid bounds, and store it back."""
    page = st.session_state.get(page_key, 0)
    page = max(0, min(page, total_pages - 1))
    st.session_state[page_key] = page
    return page


def _render_chain_pager(page_key: str, total_pages: int):
    if total_pages <= 1:
        return
    page = st.session_state.get(page_key, 0)
    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        if st.button("\u2190 Lower strikes", key=f"{page_key}_prev", disabled=(page <= 0)):
            st.session_state[page_key] = page - 1
            st.rerun()
    with c2:
        st.markdown(f"<div style='text-align:center'>Page {page + 1} of {total_pages}</div>",
                    unsafe_allow_html=True)
    with c3:
        if st.button("Higher strikes \u2192", key=f"{page_key}_next", disabled=(page >= total_pages - 1)):
            st.session_state[page_key] = page + 1
            st.rerun()


def _reset_chain_state(base_table_key: str, page_key: str, strike_state_key: str):
    """Clear pagination/selection state for a chain browser after a position is opened,
    so a stale click or page position doesn't bleed into the next contract search."""
    st.session_state.pop(strike_state_key, None)
    st.session_state.pop(page_key, None)
    for k in list(st.session_state.keys()):
        if k.startswith(f"_{base_table_key}") or k.startswith(f"{base_table_key}_p"):
            del st.session_state[k]


def _pending_selection_rows(table_key: str) -> list:
    """
    Read a dataframe widget's *last* return value out of session_state,
    without (re-)instantiating the widget. Streamlit keeps a widget's return
    value under its key across reruns, so this is safe to call before the
    widget itself is drawn this run.
    """
    event = st.session_state.get(table_key)
    if not event:
        return []
    if hasattr(event, "selection"):
        return list(event.selection.rows or [])
    return list(event.get("selection", {}).get("rows", []) or [])


def _render_chain_table(df: pd.DataFrame, table_key: str):
    """Draw the chain table and show a caption for whatever row is currently selected."""
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=table_key,
    )
    rows = _pending_selection_rows(table_key)
    if rows and rows[0] < len(df):
        picked = df.iloc[rows[0]].to_dict()
        delta_display = picked.get("delta")
        delta_display = f"{delta_display}" if delta_display is not None else "\u2014"
        st.caption(
            f"Selected ${picked['strike']} exp {picked['expiry']} (delta {delta_display})"
        )


def _render_leg_picker(session: TradingSession, snap: dict, side: str,
                        default_max_expiry_days: int, default_strike_at_spot: bool = False):
    """
    Filter -> select -> open flow for one leg ("long" or "short"). Filtering
    happens server-side via session.view_chain (single source of truth for
    filter logic -- see filter_contracts in option_pricer.py); this function
    only paginates the already-filtered result for display.

    default_strike_at_spot: if True, the strike slider's lower bound starts
    at the current spot price rather than the data's minimum -- useful for
    the short leg, which is almost always struck above spot (OTM), while
    still letting the user drag it lower if they want to.
    """
    # Phase 1: a wide fetch purely to discover real min/max bounds, so the
    # filter widgets below start at accurate ranges instead of a guessed
    # window that could silently hide contracts (see the bug this replaced).
    bounds_chain = _chain_df(session.view_chain(max_expiry_days=max(default_max_expiry_days, 400)))
    if bounds_chain.empty:
        st.warning("No contracts available to browse.")
        return

    strike_lo, strike_hi = float(bounds_chain["strike"].min()), float(bounds_chain["strike"].max())
    delta_lo, delta_hi = float(bounds_chain["delta"].min()), float(bounds_chain["delta"].max())
    expiry_lo, expiry_hi = bounds_chain["expiry"].min(), bounds_chain["expiry"].max()
    default_expiry_hi = min(expiry_hi, snap["date"] + timedelta(days=default_max_expiry_days))

    default_strike_lo = strike_lo
    if default_strike_at_spot:
        default_strike_lo = max(strike_lo, min(snap["spot_price"], strike_hi))

    page_key = f"{side}_chain_page"

    with st.expander("Browse chain", expanded=True):
        fc1, fc2 = st.columns(2)
        with fc1:
            strike_range = st.slider("Strike range", strike_lo, strike_hi,
                                      (default_strike_lo, strike_hi), key=f"{side}_strike_filter")
        with fc2:
            delta_range = st.slider("Delta range", delta_lo, delta_hi,
                                     (delta_lo, delta_hi), key=f"{side}_delta_filter")

        ec1, ec2 = st.columns(2)
        with ec1:
            expiry_from = st.date_input("Expiry from", value=expiry_lo,
                                         min_value=expiry_lo, max_value=expiry_hi,
                                         key=f"{side}_expiry_from")
        with ec2:
            expiry_to = st.date_input("Expiry to", value=default_expiry_hi,
                                       min_value=expiry_lo, max_value=expiry_hi,
                                       key=f"{side}_expiry_to")
        if expiry_from > expiry_to:
            expiry_from, expiry_to = expiry_to, expiry_from

        # Phase 2: authoritative, fully server-side filtered fetch -- no
        # client-side re-filtering, no silent truncation.
        min_expiry_days = max(0, (expiry_from - snap["date"]).days)
        max_expiry_days = (expiry_to - snap["date"]).days

        filtered = _chain_df(session.view_chain(
            min_strike=strike_range[0], max_strike=strike_range[1],
            min_delta=delta_range[0], max_delta=delta_range[1],
            min_expiry_days=min_expiry_days, max_expiry_days=max_expiry_days,
        ))

        total_pages = max(1, math.ceil(len(filtered) / CHAIN_PAGE_SIZE))
        page = _clamp_page(page_key, total_pages)
        page_df = filtered.iloc[page * CHAIN_PAGE_SIZE:(page + 1) * CHAIN_PAGE_SIZE]
        table_key = f"{side}_chain_table_p{page}"

        st.caption(f"{len(filtered)} contracts match filters")
        _render_chain_table(page_df, table_key=table_key)
        _render_chain_pager(page_key, total_pages)

    selected_rows = _pending_selection_rows(table_key)
    selected_contract = (page_df.iloc[selected_rows[0]].to_dict()
                          if selected_rows and selected_rows[0] < len(page_df) else None)

    if selected_contract:
        if st.button(f"Open {side} leg", key=f"open_{side}_btn", type="primary"):
            try:
                dte = (selected_contract["expiry"] - snap["date"]).days
                open_fn = session.open_long if side == "long" else session.open_short
                open_fn(strike=selected_contract["strike"],
                        min_expiry_days=max(0, dte), max_expiry_days=dte)
                _record_equity()
                _reset_chain_state(f"{side}_chain_table", page_key, f"{side}_strike_value")
                st.rerun()
            except ValueError as e:
                st.error(str(e))
    else:
        st.caption("Select a contract above to open it.")


# ---------------------------------------------------------------------
# Sidebar: create a session
# ---------------------------------------------------------------------

with st.sidebar:
    st.header("New session")
    ticker = st.text_input("Ticker", value="AAPL")
    price_start = st.date_input("Price history start", value=date(2024, 1, 1))
    price_end = st.date_input("Price history end", value=date(2024, 12, 31))
    starting_cash_input = st.number_input("Starting cash", value=100_000, step=1000)

    if st.button("Start session", type="primary"):
        try:
            with st.spinner("Fetching price history..."):
                prices = fetch_price_history(ticker, price_start, price_end)
                trading_start = min(d for d in prices if d >= price_start)
                source = BlackScholesSource(price_history=prices)
                session = TradingSession(
                    ticker, source, prices,
                    portfolio=Portfolio(starting_cash=starting_cash_input),
                    start_date=trading_start,
                )
                store.create(SESSION_ID, session)
                st.session_state.equity_log = []
                st.session_state.starting_cash = starting_cash_input
                _record_equity()
        except ValueError as e:
            st.error(f"Couldn't start session: {e}")
        except Exception as e:
            st.error(f"Unexpected error fetching data for '{ticker}': {e}")
        else:
            st.rerun()

session: TradingSession | None = store.get(SESSION_ID)

if session is None:
    st.info("Start a session from the sidebar to begin.")
    st.stop()

snap = session.snapshot()
starting_cash = st.session_state.starting_cash

# ---------------------------------------------------------------------
# Top bar
# ---------------------------------------------------------------------

top = st.columns([2, 2, 2, 2, 2])
top[0].markdown(f"### {session.underlying}")
top[1].metric("Date", str(snap["date"]))
top[2].metric("Spot price", f"${snap['spot_price']:,.2f}")

with top[3]:
    _, latest_date = session.price_history_bounds
    target_date = st.date_input(
        "Advance to date",
        value=snap["date"],
        min_value=snap["date"],
        max_value=latest_date,
        label_visibility="collapsed",
    )
with top[4]:
    if st.button("Advance"):
        if target_date <= session.current_date:
            st.warning("Pick a date after the current one.")
        else:
            session.advance(to_date=target_date)
            _record_equity()
            # Strikes/expiries meaningfully shift with date + spot price, so any
            # previously selected/paginated chain state is stale after Advance --
            # clear it for both legs rather than letting a picked strike or page
            # position silently carry over from before the time jump.
            _reset_chain_state("long_chain_table", "long_chain_page", "long_strike_value")
            _reset_chain_state("short_chain_table", "short_chain_page", "short_strike_value")
            st.rerun()

earnings_dates, dividend_dates = _cached_events(session.underlying)
next_earnings = next_event_on_or_after(earnings_dates, snap["date"])
next_dividend = next_event_on_or_after(dividend_dates, snap["date"])

ev1, ev2 = st.columns(2)
ev1.metric("Next earnings", str(next_earnings) if next_earnings else "\u2014")
ev2.metric("Next dividend (ex-date)", str(next_dividend) if next_dividend else "\u2014")

st.divider()

# ---------------------------------------------------------------------
# Capital summary
# ---------------------------------------------------------------------

deployed = 0.0
breakeven = None
if snap["long_leg"] is not None:
    deployed = snap["long_leg"]["entry_price"] * 100 * snap["long_leg"]["quantity"]
    # Combined PMCC breakeven: long strike + long premium paid, offset by
    # premium collected from short calls so far (approximated via realized P&L).
    premium_collected_so_far = snap["realized_pnl"] / 100 if snap["realized_pnl"] else 0
    breakeven = snap["long_leg"]["strike"] + snap["long_leg"]["entry_price"] - premium_collected_so_far

return_pct = (snap["total_equity"] - starting_cash) / starting_cash * 100 if starting_cash else 0

cols = st.columns(6)
cols[0].metric(
    "Cash", f"${snap['cash']:,.2f}",
    help="Starting cash, adjusted by every premium paid/collected and every "
         "settlement so far. Does not include unrealized value of open legs."
)
cols[1].metric(
    "Deployed", f"${deployed:,.2f}",
    help="Cost basis of the currently open long call: entry price \u00d7 100 \u00d7 quantity. "
         "$0 if no long leg is open."
)
cols[2].metric(
    "Realized P&L", f"${snap['realized_pnl']:,.2f}",
    help="Sum of P&L from every leg that has actually settled or been closed. "
         "Locked in -- won't change until another leg closes/settles."
)
cols[3].metric(
    "Unrealized P&L", f"${snap['unrealized_pnl']:,.2f}",
    help="Mark-to-market gain/loss on currently open legs only: "
         "(current price \u2212 entry price) \u00d7 100 for the long leg, "
         "(entry price \u2212 current price) \u00d7 100 for the short leg. "
         "Changes every time price or time-to-expiry changes, even with no clicks."
)
cols[4].metric(
    "Total equity", f"${snap['total_equity']:,.2f}", delta=f"{return_pct:.2f}%",
    help="Cash + Unrealized P&L. What you'd walk away with if you closed "
         "everything right now. The % is versus your starting cash."
)
cols[5].metric(
    "Breakeven (combined)", f"${breakeven:,.2f}" if breakeven else "\u2014",
    help="Long strike + long entry price \u2212 premium collected so far "
         "(realized P&L \u00f7 100). Only moves when a short leg actually settles/closes "
         "-- not while it's still open. Stock needs to close above this by the "
         "long call's expiry for the overall position to be profitable."
)
st.divider()

# ---------------------------------------------------------------------
# Position cards
# ---------------------------------------------------------------------

long_col, short_col = st.columns(2)

with long_col:
    st.subheader("Long leg")
    if snap["long_leg"] is None:
        _render_leg_picker(session, snap, side="long", default_max_expiry_days=400)
    else:
        leg = snap["long_leg"]
        stop = session.check_stop_loss()
        badge = "Safe" if not stop["triggered"] else "Stop-loss triggered"
        st.caption(badge)
        st.write(f"**${leg['strike']} call** \u00b7 exp {leg['expiry']} \u00b7 entry ${leg['entry_price']:.2f}")

        entry_notional = leg["entry_price"] * 100 * leg["quantity"]
        current_notional = (leg["current_price"] or 0) * 100 * leg["quantity"]
        unrealized = current_notional - entry_notional
        st.metric("Unrealized P&L", f"${unrealized:,.2f}")

        g = st.columns(4)
        g[0].metric("Delta", f"{leg['delta']:.3f}" if leg["delta"] is not None else "\u2014")
        g[1].metric("Gamma", f"{leg['gamma']:.4f}" if leg["gamma"] is not None else "\u2014")
        g[2].metric("Theta", f"{leg['theta']:.2f}" if leg["theta"] is not None else "\u2014")
        g[3].metric("IV", f"{leg['iv']*100:.1f}%" if leg["iv"] is not None else "\u2014")

        if st.button("Close long leg"):
            session.close_long()
            _record_equity()
            st.rerun()

with short_col:
    st.subheader("Short leg")
    if snap["short_leg"] is None:
        _render_leg_picker(session, snap, side="short", default_max_expiry_days=35,
                            default_strike_at_spot=True)
    else:
        leg = snap["short_leg"]
        st.write(f"**${leg['strike']} call** \u00b7 exp {leg['expiry']} \u00b7 entry ${leg['entry_price']:.2f}")

        entry_notional = leg["entry_price"] * 100 * leg["quantity"]
        current_notional = (leg["current_price"] or 0) * 100 * leg["quantity"]
        unrealized = entry_notional - current_notional  # short: gain when price falls
        st.metric("Unrealized P&L", f"${unrealized:,.2f}")

        g = st.columns(4)
        g[0].metric("Delta", f"{leg['delta']:.3f}" if leg["delta"] is not None else "\u2014")
        g[1].metric("Gamma", f"{leg['gamma']:.4f}" if leg["gamma"] is not None else "\u2014")
        g[2].metric("Theta", f"{leg['theta']:.2f}" if leg["theta"] is not None else "\u2014")
        g[3].metric("IV", f"{leg['iv']*100:.1f}%" if leg["iv"] is not None else "\u2014")

        if st.button("Close short leg"):
            session.close_short()
            _record_equity()
            st.rerun()

st.divider()

# ---------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------

st.subheader("Equity curve")
if len(st.session_state.equity_log) > 1:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[p["date"] for p in st.session_state.equity_log],
        y=[p["total_equity"] for p in st.session_state.equity_log],
        mode="lines+markers", name="Total equity",
    ))
    fig.update_layout(height=300, margin=dict(l=20, r=20, t=20, b=20))
    st.plotly_chart(fig, width="stretch")
else:
    st.caption("Not enough history yet -- open a position and advance time to see the curve.")

# ---------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------

st.subheader("Event log")
history = session.history()
if history:
    st.dataframe(list(reversed(history)), width="stretch")
else:
    st.caption("No events yet.")
