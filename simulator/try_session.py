from datetime import date
from simulator.data.price_history import fetch_price_history
from simulator.data.option_pricer import BlackScholesSource
from simulator.engine.session import TradingSession

prices = fetch_price_history("AAPL", date(2024, 1, 1), date(2024, 12, 31))
source = BlackScholesSource(price_history=prices)
session = TradingSession("AAPL", source, prices)

session.open_long(target_delta=0.85, min_expiry_days=300)
print(session.view_chain(max_expiry_days=35, limit=5))
session.open_short(strike=session.spot_price * 1.05)
session.advance(days=25)
print(session.snapshot())
print(session.history())