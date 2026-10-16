"""Streamlit manual iron-condor replay, using the shared multi-leg engine."""

from datetime import date, timedelta
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from simulator.data.option_pricer import BlackScholesSource, OptionRight
from simulator.data.price_history import fetch_price_history
from simulator.engine.multi_leg_session import MultiLegSession
from simulator.engine.portfolio import Portfolio
from simulator.strategies.iron_condor import CONDOR_ROLES


LABELS = {"lower_put": "Buy lower put", "upper_put": "Sell upper put",
          "lower_call": "Sell lower call", "upper_call": "Buy upper call"}


def _save(session, store, session_id):
    try:
        store.create(session_id + ':condor', session)
    except Exception as exc:
        st.warning(f"Could not save session; replay continues locally: {exc}")


def _act(fn, session, store, session_id):
    try:
        fn()
        _save(session, store, session_id)
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.rerun()


def _contract_label(c):
    return f"${c.strike:g} · bid {c.bid:.2f} / ask {c.ask:.2f} · Δ {c.delta:.3f}"


def _picker(session, chain, snap, store, session_id):
    version = f"{session.current_date}_{len(session.history())}"
    st.subheader("Build or roll an iron condor")
    expiry_dates = sorted({c.expiry for c in chain})
    if not expiry_dates:
        st.info("No available contracts.")
        return
    expiry = st.selectbox("Common expiry", expiry_dates,
                          index=min(range(len(expiry_dates)),
                                    key=lambda i: abs((expiry_dates[i] - session.current_date).days - 30)),
                          key=f"ic_expiry_{version}")
    contracts = [c for c in chain if c.expiry == expiry]
    quantity = st.number_input("Contracts per leg", min_value=1, value=1, step=1, key='ic_quantity')
    selections = {}
    targets = dict(zip(CONDOR_ROLES, (0.90, 0.95, 1.05, 1.10)))
    for col, (role, (right, action)) in zip(st.columns(4), CONDOR_ROLES.items()):
        candidates = sorted((c for c in contracts if c.right == right), key=lambda c: c.strike)
        if not candidates:
            st.info(f"No {right.value.lower()} contracts for this expiry.")
            return
        with col:
            selections[role] = st.selectbox(
                LABELS[role], candidates,
                index=min(range(len(candidates)),
                          key=lambda i: abs(candidates[i].strike - session.spot_price * targets[role])),
                format_func=_contract_label, key=f"ic_{role}_{version}_{expiry}")
    st.dataframe(pd.DataFrame([
        {"Leg": LABELS[r], "Strike": c.strike, "Expiry": c.expiry,
         "Bid": c.bid, "Ask": c.ask, "Delta": c.delta, "Gamma": c.gamma,
         "Theta / day": c.theta, "Vega / vol point": c.vega, "Model IV": c.iv}
        for r, c in selections.items()]), hide_index=True, width='stretch')
    try:
        risk = session.preview_condor(selections, quantity)
    except ValueError as exc:
        st.info(str(exc))
        valid = False
    else:
        valid = True
        st.caption(f"Entry credit after fees: ${risk['entry_credit']:,.2f} · "
                   f"Max loss: ${risk['max_loss']:,.2f} · "
                   f"Expiry breakevens: ${risk['lower_breakeven']:.2f} / ${risk['upper_breakeven']:.2f}")
    if snap['legs']:
        st.caption("Roll closes all remaining legs at bid/ask, then opens the selected four legs. "
                   "Previous realized P&L remains in the session totals.")
        if st.button("Roll position to selected contracts", disabled=not valid, type='primary'):
            _act(lambda: session.roll_condor(selections, quantity), session, store, session_id)
    elif st.button("Open iron condor", disabled=not valid, type='primary'):
        _act(lambda: session.open_condor(selections, quantity), session, store, session_id)


def render(store, session_id):
    st.title("Iron Condor Simulator")
    st.caption("Model-based daily-close replay · historical stock closes · synthetic Friday expiries "
               "and strikes · Black–Scholes with trailing realized volatility · synthetic bid/ask · "
               "cash settlement at expiry; dividends and early assignment are not modeled.")
    with st.sidebar:
        st.header("New iron-condor session")
        ticker = st.text_input("Stock symbol", 'AAPL', key='ic_ticker').strip().upper()
        start = st.date_input("Start date", date(2024, 1, 1), key='ic_start')
        end = st.date_input("History end date", date(2024, 12, 31), key='ic_end')
        cash = st.number_input("Starting cash", min_value=1.0, value=100_000.0,
                               step=1000.0, key='ic_cash')
        commission = st.number_input("Fee per contract per trade ($)", min_value=0.0,
                                     value=0.0, step=0.05, key='ic_commission')
        if st.button("Start iron-condor session", type='primary'):
            try:
                if not ticker or start >= end:
                    raise ValueError("Enter a symbol and an end date after the start date.")
                with st.spinner("Fetching historical stock prices..."):
                    # Separate warm-up from the replay start to avoid fallback volatility.
                    prices = fetch_price_history(ticker, start - timedelta(days=400), end)
                    if not any(d >= start for d in prices):
                        raise ValueError("No stock prices available within the replay period.")
                    source = BlackScholesSource(prices)
                    session = MultiLegSession(ticker, source, prices, Portfolio(cash), start, commission)
                    st.session_state.condor_session = session
                    _save(session, store, session_id)
            except Exception as exc:
                st.error(f"Could not start session: {exc}")
            else:
                st.rerun()
    session = st.session_state.get('condor_session')
    if session is None:
        st.info("Choose a stock and dates in the sidebar to start.")
        return
    try:
        snap = session.snapshot()
    except ValueError as exc:
        st.error(str(exc))
        return
    st.subheader(f"{session.underlying} · {session.current_date}")
    bar = st.columns([1, 1, 2, 1])
    bar[0].metric("Stock close", f"${snap['spot_price']:,.2f}")
    with bar[1]:
        if st.button("Next trading day", disabled=session.current_date >= session.price_history_bounds[1]):
            _act(session.advance, session, store, session_id)
    with bar[2]:
        target = st.date_input("Advance to", value=session.current_date,
                               min_value=session.current_date, max_value=session.price_history_bounds[1],
                               key=f"ic_advance_{session.current_date}")
    with bar[3]:
        if st.button("Advance to date", disabled=target <= session.current_date):
            _act(lambda: session.advance(target), session, store, session_id)
    cols = st.columns(5)
    for col, (label, field) in zip(cols, (
            ('Equity (bid/ask)', 'total_equity'), ('Realized P&L', 'realized_pnl'),
            ('Unrealized P&L (bid/ask)', 'unrealized_pnl'), ('Total P&L (bid/ask)', 'total_pnl'),
            ('Total P&L if closed now', 'pnl_if_closed'))):
        col.metric(label, f"${snap[field]:,.2f}")
    st.caption(f"Cash: ${snap['cash']:,.2f} · Positions are valued at closing bid/ask: "
               "longs at bid, shorts at ask. Close-now P&L also deducts closing fees. "
               "Daily-close fills are an explicit replay assumption.")
    greeks = snap['net_greeks']
    st.caption(f"Position Greeks × quantity × 100: Δ {greeks['delta']:.2f} · "
               f"Γ {greeks['gamma']:.3f} · Θ ${greeks['theta']:.2f}/day · "
               f"Vega ${greeks['vega']:.2f}/volatility point")
    if snap['condor_risk']:
        r = snap['condor_risk']
        breakevens = (f"${r['lower_breakeven']:.2f} / ${r['upper_breakeven']:.2f}"
                      if r['lower_breakeven'] is not None else 'none for the adjusted entry cost')
        st.caption(f"Current open condor at expiry: max profit ${r['max_profit']:,.2f} · "
                   f"max loss ${r['max_loss']:,.2f} · "
                   f"breakevens {breakevens}. "
                   "These figures exclude prior realized P&L and future closing fees.")
    elif snap['legs']:
        st.warning("The open legs no longer form a same-expiry iron condor. "
                   "Original condor risk limits and breakevens do not apply; "
                   "closing protection can leave an uncovered short option.")
    st.subheader("Open legs")
    if snap['legs']:
        st.dataframe(pd.DataFrame(snap['legs']).drop(columns=['id']), hide_index=True, width='stretch')
        labels = {l['id']: f"{LABELS[l['role']]} · ${l['strike']:g} · {l['expiry']}" for l in snap['legs']}
        selected = st.multiselect("Legs to close (select a leg or a spread)", list(labels),
                                  format_func=labels.get, key=f"ic_close_{len(session.history())}")
        a, b = st.columns(2)
        if a.button("Close selected legs", disabled=not selected):
            _act(lambda: session.close_legs(selected), session, store, session_id)
        if b.button("Close whole position"):
            _act(session.close_legs, session, store, session_id)
    else:
        st.caption("No open legs.")
    chain = session.view_chain()
    _picker(session, chain, snap, store, session_id)
    if session.history():
        with st.expander("Roll or reopen one leg"):
            st.caption("Rolling one leg may create different expiries. Use the full-position roll "
                       "to keep all four legs at the same expiry.")
            role = st.selectbox("Leg role", list(CONDOR_ROLES), format_func=LABELS.get)
            candidates = sorted((c for c in chain if c.right == CONDOR_ROLES[role][0]),
                                key=lambda c: (c.expiry, c.strike))
            expiries = sorted({c.expiry for c in candidates})
            if expiries:
                expiry = st.selectbox("Replacement expiry", expiries,
                                      key=f"ic_replace_exp_{session.current_date}_{role}")
                candidates = [c for c in candidates if c.expiry == expiry]
                contract = st.selectbox("Replacement contract", candidates, format_func=_contract_label,
                                        key=f"ic_replace_{session.current_date}_{role}_{expiry}")
                if st.button("Replace selected leg"):
                    _act(lambda: session.replace_leg(role, contract), session, store, session_id)
    with st.expander("Browse available calls and puts"):
        dte_range = st.slider("Days to expiry", 1, 730, (7, 60))
        tabs = st.tabs(['Calls', 'Puts'])
        for tab, right in zip(tabs, (OptionRight.CALL, OptionRight.PUT)):
            with tab:
                rows = [{"Strike": c.strike, "Expiry": c.expiry, "Bid": c.bid, "Ask": c.ask,
                         "Delta": c.delta, "Gamma": c.gamma, "Theta": c.theta,
                         "Vega": c.vega, "Model IV": c.iv}
                        for c in chain if c.right == right and dte_range[0] <= c.days_to_expiry <= dte_range[1]]
                st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')
    points = session.equity_history()
    st.subheader("Equity curve")
    fig = go.Figure(go.Scatter(x=[p['date'] for p in points],
                              y=[p['total_equity'] for p in points], mode='lines+markers'))
    fig.update_layout(height=300, yaxis_title='Equity ($)', margin=dict(l=20, r=20, t=20, b=20))
    st.plotly_chart(fig, width='stretch')
    with st.expander("Trade and settlement history"):
        st.dataframe(pd.DataFrame(session.history()), hide_index=True, width='stretch')
