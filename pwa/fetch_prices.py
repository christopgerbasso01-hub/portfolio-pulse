"""
Fetches live prices for all portfolio holdings via yfinance.
Run by GitHub Actions every 30 minutes — output saved to prices.json.
"""
import json
import yfinance as yf
from datetime import datetime, timezone

TICKERS = [
    'SPXL', 'FNGU', 'NVDA', 'TXF.TO', 'TSLA', 'CM.TO', 'UDOW', 'ENB.TO',
    'TSM', 'RY.TO', 'IBKR', 'AVGO', 'COST', 'BMO.TO', 'RDS', 'LYV', 'NFLX',
    'GBTC', 'V', 'ET', 'AAPL', 'QCOM', 'MSFT', 'MSTR', 'BYDDF', 'USDCAD=X'
]

prices = {}
usdcad = 1.37  # fallback

print(f"Fetching {len(TICKERS)} tickers...")

for symbol in TICKERS:
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = getattr(info, 'last_price', None)
        prev  = getattr(info, 'previous_close', None)
        curr  = getattr(info, 'currency', None)

        if price is None:
            # fallback to history
            hist = ticker.history(period='2d')
            if not hist.empty:
                price = float(hist['Close'].iloc[-1])
                prev  = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price

        if price is not None:
            if symbol == 'USDCAD=X':
                usdcad = float(price)
                print(f"  USD/CAD: {usdcad:.4f}")
            else:
                prices[symbol] = {
                    'price':     float(price),
                    'prevClose': float(prev) if prev is not None else float(price),
                    'currency':  curr or ('CAD' if symbol.endswith('.TO') else 'USD'),
                }
                print(f"  {symbol}: {prices[symbol]['currency']} {price:.2f}")
        else:
            print(f"  {symbol}: no price data")

    except Exception as e:
        print(f"  {symbol}: ERROR — {e}")

output = {
    'prices':  prices,
    'usdcad':  usdcad,
    'updated': datetime.now(timezone.utc).isoformat(),
    'count':   len(prices),
}

with open('prices.json', 'w') as f:
    json.dump(output, f, indent=2)

print(f"\nDone. {len(prices)}/{len(TICKERS)-1} prices saved. USD/CAD = {usdcad:.4f}")
