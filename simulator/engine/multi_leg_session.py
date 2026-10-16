"""Manual multi-leg replay. Trades use exact quotes; the clock never looks ahead.

Condor construction is a template layered on ordinary leg operations, so the
ledger and replay can also support a reverse condor without another engine.
"""

from datetime import date
import math

from simulator.data.option_pricer import filter_contracts
from simulator.engine.portfolio import Portfolio, LegAction
from simulator.strategies.iron_condor import CONDOR_ROLES, validate_condor, expiry_risk


class MultiLegSession:
    def __init__(self, underlying, data_source, price_history, portfolio=None,
                 start_date=None, commission_per_contract=0.0):
        if not price_history:
            raise ValueError("Price history is empty.")
        if not math.isfinite(commission_per_contract) or commission_per_contract < 0:
            raise ValueError("Commission must be nonnegative.")
        self.underlying = underlying
        self._data_source = data_source
        self.price_history = price_history
        self._portfolio = portfolio or Portfolio(100_000)
        requested = start_date or min(price_history)
        dates = sorted(d for d in price_history if d >= requested)
        if not dates:
            raise ValueError("No trading dates on or after the start date.")
        self.current_date = dates[0]
        self.commission_per_contract = commission_per_contract
        self._roles = {}  # role -> leg ID; persists across closing/replacing a leg
        self._log = []
        self._equity_history = []
        self._record_equity()

    @property
    def spot_price(self):
        return self.price_history[self.current_date]

    @property
    def price_history_bounds(self):
        return min(self.price_history), max(self.price_history)

    def history(self):
        return list(self._log)

    def equity_history(self):
        return [dict(point) for point in self._equity_history]

    def view_chain(self, right=None, **filters):
        return filter_contracts(self._data_source.get_chain(
            self.underlying, self.current_date, self.spot_price), right=right, **filters)

    def _quote(self, leg):
        quote = self._portfolio.find_matching_contract(
            leg, self.current_date, self._data_source, self.underlying, self.spot_price)
        if quote is None:
            raise ValueError(f"Missing quote for {leg.right.value} {leg.strike} {leg.expiry}.")
        return quote

    def _available(self, selections):
        # Opening must reference a listed contract, not arbitrary theoretical inputs.
        chain = {(c.right, c.strike, c.expiry): c for c in self.view_chain()}
        result = {}
        for role, c in selections.items():
            quote = chain.get((c.right, c.strike, c.expiry))
            if quote is None or quote.expiry <= self.current_date:
                raise ValueError("Selected contract is unavailable on the replay date.")
            if any(not math.isfinite(v) for v in (quote.bid, quote.ask)) or not 0 <= quote.bid <= quote.ask:
                raise ValueError("Invalid bid/ask quote.")
            result[role] = quote
        return result

    def _prepare_condor(self, selections, quantity):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Quantity must be a positive integer.")
        contracts = self._available(selections)
        validate_condor(contracts)
        credit = sum((c.bid if CONDOR_ROLES[r][1] == LegAction.SELL else -c.ask)
                     * 100 * quantity for r, c in contracts.items())
        credit -= 4 * self.commission_per_contract * quantity
        if credit <= 0:
            raise ValueError("Iron condor must collect a positive credit after entry fees.")
        risk = expiry_risk(contracts, quantity, credit)
        narrow_width = min(contracts['upper_put'].strike - contracts['lower_put'].strike,
                           contracts['upper_call'].strike - contracts['lower_call'].strike) * 100 * quantity
        if credit >= narrow_width:
            raise ValueError("Entry credit must be less than either wing width.")
        return contracts, risk

    def preview_condor(self, selections, quantity=1):
        return self._prepare_condor(selections, quantity)[1]

    def _open(self, role, contract, quantity):
        right, action = CONDOR_ROLES[role]
        leg = self._portfolio.open_leg(
            right, action, contract.strike, contract.expiry, self.current_date,
            contract.ask if action == LegAction.BUY else contract.bid,
            quantity, fee=self.commission_per_contract * quantity)
        self._roles[role] = leg.id
        self._log.append({"date": self.current_date, "event": "opened", "role": role,
                          "leg_id": leg.id, "right": right.value, "action": action.value,
                          "strike": leg.strike, "expiry": leg.expiry,
                          "quantity": quantity, "price": leg.entry_price, "fee": leg.entry_fee})

    def open_condor(self, selections, quantity=1):
        if self._portfolio.open_legs():
            raise ValueError("Close or roll the current position before opening another.")
        contracts, risk = self._prepare_condor(selections, quantity)
        if risk["max_loss"] > self.snapshot()["total_equity"]:
            raise ValueError("Insufficient equity to cover maximum loss.")
        for role, contract in contracts.items():
            self._open(role, contract, quantity)
        self._record_equity()
        return self.snapshot()

    def _close_prices(self, legs):
        prices = {}
        for leg in legs:
            quote = self._quote(leg)
            prices[leg.id] = quote.bid if leg.action == LegAction.BUY else quote.ask
        return prices

    def _close(self, legs, prices):
        roles = {v: k for k, v in self._roles.items()}
        for leg in legs:
            pnl = self._portfolio.close_leg(leg, self.current_date, prices[leg.id],
                                           fee=self.commission_per_contract * leg.quantity)
            self._log.append({"date": self.current_date, "event": "closed",
                              "role": roles.get(leg.id), "leg_id": leg.id,
                              "price": prices[leg.id], "fee": leg.exit_fee, "pnl": pnl})

    def close_legs(self, leg_ids=None):
        self.snapshot()  # Validate valuations of retained legs before mutating.
        legs = self._portfolio.open_legs()
        if leg_ids is not None:
            ids = set(leg_ids)
            if not ids <= {l.id for l in legs}:
                raise ValueError("Selected leg is not open.")
            legs = [l for l in legs if l.id in ids]
        if not legs:
            raise ValueError("No open legs selected.")
        prices = self._close_prices(legs)  # validate every quote before mutating
        self._close(legs, prices)
        self._record_equity()
        return self.snapshot()

    def roll_condor(self, selections, quantity=1):
        legs = self._portfolio.open_legs()
        if not legs:
            raise ValueError("No open position to roll.")
        contracts, risk = self._prepare_condor(selections, quantity)
        prices = self._close_prices(legs)
        liquidation_equity = self._portfolio.cash + sum(
            (1 if l.action == LegAction.BUY else -1) * prices[l.id] * 100 * l.quantity
            - self.commission_per_contract * l.quantity for l in legs)
        if risk['max_loss'] > liquidation_equity:
            raise ValueError("Insufficient equity for the replacement position.")
        self._close(legs, prices)
        for role, contract in contracts.items():
            self._open(role, contract, quantity)
        self._log.append({"date": self.current_date, "event": "rolled_position"})
        self._record_equity()
        return self.snapshot()

    def replace_leg(self, role, contract):
        """Roll one role, or reopen a previously closed role, preserving its quantity.

        A roll can change expiry. Condor-specific risk is suppressed until all
        four legs again form a same-expiry condor; the UI flags this explicitly.
        """
        if role not in CONDOR_ROLES or role not in self._roles:
            raise ValueError("Open a condor before replacing an individual role.")
        all_legs = self._portfolio.open_legs() + self._portfolio.closed_legs()
        old = next(l for l in all_legs if l.id == self._roles[role])
        fresh = self._available({role: contract})[role]
        if fresh.right != CONDOR_ROLES[role][0]:
            raise ValueError("Replacement must have the same option type.")
        # Keep the strike ordering for remaining legs; expiry may differ.
        strikes = {r: next(l.strike for l in all_legs if l.id == lid)
                   for r, lid in self._roles.items()}
        strikes[role] = fresh.strike
        values = [strikes[r] for r in CONDOR_ROLES]
        print(f"[DEBUG replace_leg] Ordered strikes: {values}", flush=True)
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError("Replacement must preserve the four-leg strike ordering.")
        # Do not remove protection while the corresponding short remains open.
        if CONDOR_ROLES[role][1] == LegAction.BUY:
            short_role = 'upper_put' if role == 'lower_put' else 'lower_call'
            short = next(l for l in all_legs if l.id == self._roles[short_role])
            if short.is_open and fresh.expiry < short.expiry:
                raise ValueError("Protection must expire on or after the corresponding short.")
        else:
            long_role = 'lower_put' if role == 'upper_put' else 'upper_call'
            long = next(l for l in all_legs if l.id == self._roles[long_role])
            if not long.is_open or long.expiry < fresh.expiry:
                raise ValueError("Replacement short requires protection through its expiry.")
        prices = self._close_prices([old]) if old.is_open else {}
        # Conservative reserve for individual adjustments, including calendars:
        # total wing widths, rather than claiming a same-expiry margin offset.
        reserve = ((strikes['upper_put'] - strikes['lower_put'])
                   + (strikes['upper_call'] - strikes['lower_call'])) * 100 * old.quantity
        equity = self.snapshot()['total_equity']
        replacement_cost = self.commission_per_contract * old.quantity
        # Equity already values the old leg at its executable closing price.
        # The new leg incurs its full spread when marked to its closing side.
        replacement_cost += (fresh.ask - fresh.bid) * 100 * old.quantity
        if old.is_open:
            replacement_cost += self.commission_per_contract * old.quantity
        if reserve > equity - replacement_cost:
            raise ValueError("Insufficient equity for the adjusted position's wing reserve.")
        if old.is_open:
            self._close([old], prices)
        self._open(role, fresh, old.quantity)
        self._log.append({"date": self.current_date, "event": "replaced_leg", "role": role})
        self._record_equity()
        return self.snapshot()

    def advance(self, to_date=None):
        dates = sorted(d for d in self.price_history if d > self.current_date)
        if to_date is None:
            if not dates:
                raise ValueError("Reached the end of available price history.")
            to_date = dates[0]
        if to_date <= self.current_date or to_date > max(self.price_history):
            raise ValueError("Advance within available history, after the current date.")
        crossed = [d for d in dates if d <= to_date]
        if not crossed:
            raise ValueError("No trading day in that interval.")
        # Missing historical data must not leave a requested time jump half done.
        for d in crossed:
            for leg in self._portfolio.open_legs():
                if leg.expiry > d:
                    quote = self._portfolio.find_matching_contract(
                        leg, d, self._data_source, self.underlying, self.price_history[d])
                    if quote is None:
                        raise ValueError(f"Missing quote on {d} for {leg.right.value} "
                                         f"{leg.strike} {leg.expiry}.")
        for d in crossed:
            for leg in self._portfolio.open_legs():
                if leg.expiry <= d:
                    # If expiry is a holiday/weekend, use the final available
                    # close on/before expiry, never the next day's stock price.
                    settlement_date = max(t for t in self.price_history if t <= leg.expiry)
                    pnl = self._portfolio.settle_leg(leg, leg.expiry, self.price_history[settlement_date])
                    self._log.append({"date": leg.expiry, "event": "settled", "leg_id": leg.id,
                                      "spot_date": settlement_date, "pnl": pnl})
            self.current_date = d
            self._record_equity()
        return self.snapshot()

    def snapshot(self):
        summary = self._portfolio.summary(self.current_date, self._data_source,
                                          self.underlying, self.spot_price)
        roles = {v: k for k, v in self._roles.items()}
        rows = []
        close_pnl = summary['realized_pnl']
        greeks = dict.fromkeys(('delta', 'gamma', 'theta', 'vega'), 0.0)
        for leg in self._portfolio.open_legs():
            q = self._quote(leg)
            sign = 1 if leg.action == LegAction.BUY else -1
            size = 100 * leg.quantity
            close_price = q.bid if sign == 1 else q.ask
            pnl = sign * (close_price - leg.entry_price) * size - leg.entry_fee
            close_pnl += pnl
            close_pnl -= self.commission_per_contract * leg.quantity
            for name in greeks:
                greeks[name] += sign * size * getattr(q, name)
            rows.append({"id": leg.id, "role": roles.get(leg.id), "right": leg.right.value,
                         "action": leg.action.value, "strike": leg.strike, "expiry": leg.expiry,
                         "quantity": leg.quantity, "entry_price": leg.entry_price,
                         "bid": q.bid, "ask": q.ask, "close_price": close_price, "iv": q.iv,
                         "delta": q.delta, "gamma": q.gamma, "theta": q.theta, "vega": q.vega,
                         "unrealized_pnl": pnl})
        risk = None
        legs = self._portfolio.open_legs()
        if len(legs) == 4 and len({l.quantity for l in legs}) == 1:
            contracts = {roles[l.id]: l for l in legs}
            try:
                validate_condor(contracts)
                credit = sum((1 if l.action == LegAction.SELL else -1)
                             * l.entry_price * 100 * l.quantity - l.entry_fee for l in legs)
                risk = expiry_risk(contracts, legs[0].quantity, credit)
            except ValueError:
                pass
        return {**summary, "date": self.current_date, "spot_price": self.spot_price,
                "legs": rows, "net_greeks": greeks, "condor_risk": risk,
                "total_pnl": summary['realized_pnl'] + summary['unrealized_pnl'],
                "pnl_if_closed": close_pnl,
                "starting_cash": self._portfolio.starting_cash}

    def _record_equity(self):
        summary = self._portfolio.summary(self.current_date, self._data_source,
                                          self.underlying, self.spot_price)
        self._equity_history.append({"date": self.current_date,
                                     "total_equity": summary['total_equity']})

    def to_dict(self):
        from simulator.engine.session import _encode_dates
        return {"session_type": "multi_leg", "underlying": self.underlying,
                "current_date": self.current_date.isoformat(),
                "commission_per_contract": self.commission_per_contract,
                "roles": dict(self._roles), "portfolio": self._portfolio.to_dict(),
                "log": _encode_dates(self._log),
                "equity_history": _encode_dates(self._equity_history)}

    @classmethod
    def from_dict(cls, data, data_source, price_history):
        from simulator.engine.session import _decode_dates
        session = cls(data['underlying'], data_source, price_history,
                      Portfolio.from_dict(data['portfolio']), date.fromisoformat(data['current_date']),
                      data.get('commission_per_contract', 0.0))
        session._roles = dict(data['roles'])
        session._log = _decode_dates(data['log'])
        session._equity_history = _decode_dates(data['equity_history'])
        return session
