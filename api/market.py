from http.server import BaseHTTPRequestHandler
import json
import time
from datetime import datetime, timezone

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

# =============================================================================
# PORTFOLIO HOLDINGS — last snapshot 2026-05-12
# cost_basis_total = market_value - unrealized (native currency of holding)
# =============================================================================
HOLDINGS = [
    # TFSA
    {"ticker": "FNGU",   "account": "TFSA",       "shares": 495,  "cost_total": 13070.64, "ccy": "USD"},
    {"ticker": "NVDA",   "account": "TFSA",        "shares": 40,   "cost_total": 645.60,   "ccy": "USD"},
    {"ticker": "TXF.TO", "account": "TFSA",        "shares": 320,  "cost_total": 7033.29,  "ccy": "CAD"},
    {"ticker": "SPXL",   "account": "TFSA",        "shares": 23,   "cost_total": 5573.74,  "ccy": "USD"},
    {"ticker": "TSLA",   "account": "TFSA",        "shares": 14,   "cost_total": 4768.11,  "ccy": "USD"},
    {"ticker": "UDOW",   "account": "TFSA",        "shares": 84,   "cost_total": 4245.40,  "ccy": "USD"},
    {"ticker": "CM.TO",  "account": "TFSA",        "shares": 45,   "cost_total": 2957.00,  "ccy": "CAD"},
    {"ticker": "AVGO",   "account": "TFSA",        "shares": 8,    "cost_total": 4376.37,  "ccy": "USD"},
    {"ticker": "COST",   "account": "TFSA",        "shares": 3,    "cost_total": 1237.40,  "ccy": "USD"},
    {"ticker": "NFLX",   "account": "TFSA",        "shares": 20,   "cost_total": 1448.10,  "ccy": "USD"},
    {"ticker": "MSFT",   "account": "TFSA",        "shares": 2,    "cost_total": 489.94,   "ccy": "USD"},
    {"ticker": "AAPL",   "account": "TFSA",        "shares": 4,    "cost_total": 642.26,   "ccy": "USD"},
    {"ticker": "QCOM",   "account": "TFSA",        "shares": 5,    "cost_total": 640.05,   "ccy": "USD"},
    {"ticker": "SHEL",   "account": "TFSA",        "shares": 22,   "cost_total": 1018.21,  "ccy": "USD"},
    {"ticker": "ET",     "account": "TFSA",        "shares": 60,   "cost_total": 1067.57,  "ccy": "USD"},
    {"ticker": "BMO.TO", "account": "TFSA",        "shares": 15,   "cost_total": 1767.36,  "ccy": "CAD"},
    # Non-Reg Investment
    {"ticker": "SPXL",   "account": "Investment",  "shares": 75,   "cost_total": 13363.23, "ccy": "USD"},
    {"ticker": "FNGU",   "account": "Investment",  "shares": 665,  "cost_total": 18137.79, "ccy": "USD"},
    {"ticker": "CM.TO",  "account": "Investment",  "shares": 50,   "cost_total": 6674.45,  "ccy": "CAD"},
    {"ticker": "TXF.TO", "account": "Investment",  "shares": 221,  "cost_total": 4955.93,  "ccy": "CAD"},
    {"ticker": "TSM",    "account": "Investment",  "shares": 9,    "cost_total": 3697.84,  "ccy": "USD"},
    {"ticker": "RY.TO",  "account": "Investment",  "shares": 19,   "cost_total": 3895.45,  "ccy": "CAD"},
    {"ticker": "IBKR",   "account": "Investment",  "shares": 40,   "cost_total": 3195.14,  "ccy": "USD"},
    {"ticker": "V",      "account": "Investment",  "shares": 4,    "cost_total": 1250.66,  "ccy": "USD"},
    {"ticker": "LYV",    "account": "Investment",  "shares": 11,   "cost_total": 1307.80,  "ccy": "USD"},
    {"ticker": "MSTR",   "account": "Investment",  "shares": 4,    "cost_total": 1780.74,  "ccy": "USD"},
    {"ticker": "GBTC",   "account": "Investment",  "shares": 25,   "cost_total": 2608.25,  "ccy": "USD"},
    {"ticker": "BYDDF",  "account": "Investment",  "shares": 3,    "cost_total": 74.00,    "ccy": "USD"},
    # FHSA
    {"ticker": "SPXL",   "account": "FHSA",        "shares": 53,   "cost_total": 7096.23,  "ccy": "USD"},
    {"ticker": "TXF.TO", "account": "FHSA",        "shares": 434,  "cost_total": 11411.13, "ccy": "CAD"},
    {"ticker": "UDOW",   "account": "FHSA",        "shares": 86,   "cost_total": 5329.56,  "ccy": "USD"},
    {"ticker": "FNGU",   "account": "FHSA",        "shares": 157,  "cost_total": 4159.25,  "ccy": "USD"},
    {"ticker": "ENB.TO", "account": "FHSA",        "shares": 82,   "cost_total": 4499.84,  "ccy": "CAD"},
    # RRSP
    {"ticker": "TXF.TO", "account": "RRSP",        "shares": 284,  "cost_total": 6184.11,  "ccy": "CAD"},
    {"ticker": "TSM",    "account": "RRSP",         "shares": 6,    "cost_total": 3232.08,  "ccy": "USD"},
    {"ticker": "UDOW",   "account": "RRSP",         "shares": 36,   "cost_total": 2554.37,  "ccy": "USD"},
    {"ticker": "FNGU",   "account": "RRSP",         "shares": 56,   "cost_total": 1491.56,  "ccy": "USD"},
    # RRSP cash position
    {"ticker": "_CASH_CAD", "account": "RRSP",     "shares": 1,    "cost_total": 10531.00, "ccy": "CAD", "cash": True},
]

# Average USD/CAD rate at which USD positions were originally purchased.
# Back-calculated from Google Sheet FX Conversion Gain/Loss figure.
# Update this if you significantly rebalance USD holdings at a different rate.
USD_BOOK_RATE = 1.3925

# Pre-computed total USD cost basis (sum of all USD-denominated holdings).
# Used for dynamic FX impact calculation.
USD_COST_BASIS = sum(
    h["cost_total"] for h in HOLDINGS
    if h.get("ccy") == "USD" and not h.get("cash")
)

# Static figures that don't change with market prices.
# Update REALIZED_GAINS_CAD whenever you close a position.
# Update DIVIDENDS_CAD whenever you receive a dividend payment.
REALIZED_GAINS_CAD = 22193
DIVIDENDS_CAD      = 7399

BENCHMARKS = {
    "sp500":        "^GSPC",
    "nasdaq":       "^IXIC",
    "tsx":          "^GSPTSE",
    "dow":          "^DJI",
    "vix":          "^VIX",
    "usdcad":       "USDCAD=X",
    "treasury_10y": "^TNX",
}

_cache = {}
_cache_ts = 0
CACHE_TTL = 60


def _safe_float(val, default=None):
    try:
        f = float(val)
        return None if (f != f) else f  # NaN check
    except (TypeError, ValueError):
        return default


def fetch_prices():
    global _cache, _cache_ts
    now = time.time()
    if _cache and (now - _cache_ts) < CACHE_TTL:
        return _cache

    if not HAS_YF:
        return {}

    portfolio_tickers = list({h["ticker"] for h in HOLDINGS if not h.get("cash")})
    bench_tickers = list(BENCHMARKS.values())
    all_tickers = portfolio_tickers + bench_tickers

    prices = {}
    try:
        raw = yf.download(
            tickers=" ".join(all_tickers),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        for ticker in all_tickers:
            try:
                if len(all_tickers) == 1:
                    df = raw["Close"].dropna()
                else:
                    df = raw[ticker]["Close"].dropna()
                if len(df) >= 2:
                    curr = _safe_float(df.iloc[-1])
                    prev = _safe_float(df.iloc[-2])
                    if curr and prev:
                        prices[ticker] = {
                            "price": round(curr, 2),
                            "prev": round(prev, 2),
                            "change": round(curr - prev, 2),
                            "change_pct": round((curr - prev) / prev * 100, 2),
                        }
                elif len(df) == 1:
                    curr = _safe_float(df.iloc[-1])
                    if curr:
                        prices[ticker] = {"price": round(curr, 2), "prev": None, "change": None, "change_pct": None}
            except Exception:
                pass
    except Exception:
        # Fallback: individual ticker calls (slower but more resilient)
        for ticker in all_tickers[:15]:
            try:
                t = yf.Ticker(ticker)
                fi = t.fast_info
                curr = _safe_float(fi.last_price)
                prev = _safe_float(fi.previous_close)
                if curr:
                    prices[ticker] = {
                        "price": round(curr, 2),
                        "prev": round(prev, 2) if prev else None,
                        "change": round(curr - prev, 2) if prev else None,
                        "change_pct": round((curr - prev) / prev * 100, 2) if prev else None,
                    }
            except Exception:
                pass

    _cache = prices
    _cache_ts = now
    return prices


# True CAD contributions per account — the denominator for all ROI calculations.
# These match the Google Sheet "Financials" contributions column exactly.
# Update these whenever you add fresh capital to an account.
CONTRIBUTIONS_CAD = {
    "TFSA":       44500.0,
    "Investment": 65000.0,
    "FHSA":       24000.0,
    "RRSP":       16132.0,
}


def compute_portfolio(prices):
    usdcad = _safe_float(prices.get("USDCAD=X", {}).get("price")) or 1.37
    accounts = {"TFSA": 0.0, "FHSA": 0.0, "RRSP": 0.0, "Investment": 0.0}
    total_value = 0.0
    daily_change = 0.0

    for h in HOLDINGS:
        acct = h["account"]
        shares = h["shares"]
        cost = h["cost_total"]
        ccy = h["ccy"]

        if h.get("cash"):
            val = cost if ccy == "CAD" else cost * usdcad
            accounts[acct] += val
            total_value += val
            continue

        p = prices.get(h["ticker"])
        if not p:
            continue

        price = p["price"]
        prev_price = p.get("prev") or price
        val = price * shares
        prev_val = prev_price * shares

        if ccy == "USD":
            val *= usdcad
            prev_val *= usdcad

        accounts[acct] += val
        total_value += val
        daily_change += val - prev_val

    # Cost basis = true CAD contributions (matches Google Sheet)
    total_cost = sum(CONTRIBUTIONS_CAD.values())   # 149,632
    acct_cost  = dict(CONTRIBUTIONS_CAD)

    total_pnl      = total_value - total_cost                          # Total P/L
    fx_impact      = round(USD_COST_BASIS * (usdcad - USD_BOOK_RATE))  # Dynamic FX drag/boost
    unrealized     = round(total_pnl - REALIZED_GAINS_CAD - fx_impact) # Unrealized only
    base_val       = total_value - daily_change if total_value != daily_change else total_value

    return {
        "total_value":       round(total_value),
        "total_cost":        round(total_cost),
        "total_pnl":         round(total_pnl),
        "unrealized_gain":   unrealized,
        "realized_gain":     REALIZED_GAINS_CAD,
        "fx_impact":         fx_impact,
        "roi_pct":           round(total_pnl / total_cost * 100, 2) if total_cost else 0,
        "daily_change":      round(daily_change),
        "daily_change_pct":  round(daily_change / base_val * 100, 2) if base_val else 0,
        "accounts":          {k: round(v) for k, v in accounts.items()},
        "account_cost":      {k: round(v) for k, v in acct_cost.items()},
    }


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        prices = fetch_prices()
        portfolio = compute_portfolio(prices)

        benchmarks = {}
        for name, ticker in BENCHMARKS.items():
            if ticker in prices:
                benchmarks[name] = prices[ticker]

        holdings_prices = {}
        for h in HOLDINGS:
            t = h["ticker"]
            if t in prices:
                holdings_prices[t] = prices[t]

        resp = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "usdcad": _safe_float(prices.get("USDCAD=X", {}).get("price")) or 1.37,
            "benchmarks": benchmarks,
            "holdings": holdings_prices,
            "portfolio": portfolio,
        }

        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=60")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
