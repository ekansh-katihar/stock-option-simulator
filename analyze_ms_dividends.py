import yfinance as yf
import pandas as pd
import datetime

def analyze_dividends(ticker_symbol="MS", years=5):
    ticker = yf.Ticker(ticker_symbol)
    
    # Get historical market data
    end_date = datetime.datetime.now()
    start_date = end_date - datetime.timedelta(days=years*365)
    
    history = ticker.history(start=start_date, end=end_date)
    dividends = ticker.dividends
    
    # Ensure both indices are timezone-aware and match
    if history.index.tzinfo is not None and dividends.index.tzinfo is None:
        dividends.index = dividends.index.tz_localize('UTC').tz_convert(history.index.tzinfo)
    elif history.index.tzinfo is None and dividends.index.tzinfo is not None:
         history.index = history.index.tz_localize('UTC')

    
    # Filter dividends for the given period
    dividends = dividends[(dividends.index >= pd.to_datetime(start_date).tz_localize(dividends.index.tzinfo)) & (dividends.index <= pd.to_datetime(end_date).tz_localize(dividends.index.tzinfo))]
    
    results = []
    
    for ex_date, amount in dividends.items():
        try:
            # Get prices before ex-date
            before_ex_date_idx = history.index < ex_date
            before_prices = history.loc[before_ex_date_idx].tail(3)
            
            price_3d_before = before_prices.iloc[0]['Close'] if len(before_prices) >= 3 else None
            price_2d_before = before_prices.iloc[-2]['Close'] if len(before_prices) >= 2 else None
            price_1d_before = before_prices.iloc[-1]['Close'] if len(before_prices) >= 1 else None

            # Get prices on and after ex-date
            on_or_after_ex_date_idx = history.index >= ex_date
            after_prices = history.loc[on_or_after_ex_date_idx].head(4) # Ex-date + 3 days
            
            ex_date_price = after_prices.iloc[0]['Close'] if len(after_prices) >= 1 else None
            price_1d_after = after_prices.iloc[1]['Close'] if len(after_prices) >= 2 else None
            price_2d_after = after_prices.iloc[2]['Close'] if len(after_prices) >= 3 else None
            price_3d_after = after_prices.iloc[3]['Close'] if len(after_prices) >= 4 else None
            
            results.append({
                'Ex-Date': ex_date.strftime('%Y-%m-%d'),
                'Div': amount,
                '-3 Days': round(price_3d_before, 2) if price_3d_before else None,
                '-2 Days': round(price_2d_before, 2) if price_2d_before else None,
                '-1 Day': round(price_1d_before, 2) if price_1d_before else None,
                'Ex-Date Price': round(ex_date_price, 2) if ex_date_price else None,
                '+1 Day': round(price_1d_after, 2) if price_1d_after else None,
                '+2 Days': round(price_2d_after, 2) if price_2d_after else None,
                '+3 Days': round(price_3d_after, 2) if price_3d_after else None
            })
        except Exception as e:
            print(f"Error processing {ex_date}: {e}")
            
    df = pd.DataFrame(results)
    print("Morgan Stanley (MS) Dividend Analysis (Last 5 Years)\n")
    print(df.to_string())

if __name__ == "__main__":
    analyze_dividends()
