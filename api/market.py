from http.server import BaseHTTPRequestHandler
import json
import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://finance.yahoo.com",
}

# =============================================================================
# FALLBACK HOLDINGS — used only if Vercel KV (user:settings) is unreachable
# or has never been populated. Under normal operation market.py reads
# computed_holdings / cash_positions / contributions_cad directly from KV,
# which is written by the dashboard on every load and every transaction.
# To add a new position: log it via the dashboard — no code change needed.
# =============================================================================
_FALLBACK_HOLDINGS = [
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
    {"ticker": "AVGO",   "account": "Investment",  "shares": 22,   "cost_total": 9071.04,  "ccy": "USD"},
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
    {"ticker": "MU",     "account": "RRSP",         "shares": 7,    "cost_total": 7025.06,  "ccy": "USD"},
    # Fallback cash — these are the base values before any dashboard transactions
    {"ticker": "_CASH_USD", "account": "TFSA",       "shares": 1, "cost_total": 344.41,  "ccy": "USD", "cash": True},
    {"ticker": "_CASH_CAD", "account": "FHSA",       "shares": 1, "cost_total": 24.34,   "ccy": "CAD", "cash": True},
    {"ticker": "_CASH_USD", "account": "Investment", "shares": 1, "cost_total": 301.38,  "ccy": "USD", "cash": True},
    {"ticker": "_CASH_USD", "account": "RRSP",       "shares": 1, "cost_total": 653.26,  "ccy": "USD", "cash": True},
]

# Fallback constants — used only when KV is unavailable
_FALLBACK_CONTRIBUTIONS_CAD = {
    "TFSA":       44500.0,
    "Investment": 78000.0,
    "FHSA":       24000.0,
    "RRSP":       16132.0,
}
_FALLBACK_REALIZED_GAINS_CAD = 22193
_FALLBACK_USD_BOOK_RATE      = 1.3925

BENCHMARKS = {
    "sp500":        "^GSPC",
    "nasdaq":       "^IXIC",
    "tsx":          "^GSPTSE",
    "dow":          "^DJI",
    "vix":          "^VIX",
    "usdcad":       "USDCAD=X",
    "treasury_10y": "^TNX",
}

# =============================================================================
# Vercel KV — read user:settings (written by dashboard on load + every trade)
# Cached for 5 minutes to avoid a KV round-trip on every 12-second price refresh.
# =============================================================================
KV_URL   = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")

_kv_cache    = {}
_kv_cache_ts = 0.0
_KV_TTL      = 300   # 5 minutes

_bm_cache    = {}
_bm_cache_ts = 0.0


def _kv_get_benchmark_state() -> dict:
    """Return benchmark:state from KV (written nightly by snapshot.py), 5-min cache."""
    global _bm_cache, _bm_cache_ts
    now = time.time()
    if _bm_cache and (now - _bm_cache_ts) < _KV_TTL:
        return _bm_cache
    if not KV_URL or not KV_TOKEN:
        return {}
    try:
        r = requests.post(
            KV_URL,
            headers={"Authorization": f"Bearer {KV_TOKEN}",
                     "Content-Type":  "application/json"},
            json=["GET", "benchmark:state"],
            timeout=5,
        )
        if not r.ok:
            return _bm_cache   # return stale on transient error
        raw = r.json().get("result")
        if raw is None:
            return {}
        state = json.loads(raw) if isinstance(raw, str) else (raw or {})
        _bm_cache    = state
        _bm_cache_ts = now
        return state
    except Exception:
        return _bm_cache   # return stale on exception


def _kv_get_settings() -> dict:
    """Return user:settings from KV, with a 5-minute in-process cache."""
    global _kv_cache, _kv_cache_ts
    now = time.time()
    if _kv_cache and (now - _kv_cache_ts) < _KV_TTL:
        return _kv_cache
    if not KV_URL or not KV_TOKEN:
        return {}
    try:
        r = requests.post(
            KV_URL,
            headers={"Authorization": f"Bearer {KV_TOKEN}",
                     "Content-Type":  "application/json"},
            json=["GET", "user:settings"],
            timeout=5,
        )
        if not r.ok:
            return _kv_cache   # return stale on transient error
        raw = r.json().get("result")
        if raw is None:
            return {}
        settings = json.loads(raw) if isinstance(raw, str) else raw
        _kv_cache    = settings
        _kv_cache_ts = now
        return settings
    except Exception:
        return _kv_cache   # return stale on exception


# =============================================================================
# Price fetching
# =============================================================================
_price_cache    = {}
_price_cache_ts = 0.0
PRICE_CACHE_TTL = 12


def _safe_float(val, default=None):
    try:
        f = float(val)
        return None if (f != f) else f
    except (TypeError, ValueError):
        return default


_fetch_errors = {}


def _fetch_one(session, ticker):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    try:
        r = session.get(url, headers=_YF_HEADERS, timeout=5)
        if not r.ok:
            _fetch_errors[ticker] = f"HTTP {r.status_code}"
            return ticker, None
        data   = r.json()
        result = data["chart"]["result"][0]
        meta   = result["meta"]
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes if c is not None]
        curr = _safe_float(meta.get("regularMarketPrice") or (closes[-1] if closes else None))
        prev = _safe_float(
            meta.get("chartPreviousClose")
            or meta.get("previousClose")
            or (closes[-2] if len(closes) >= 2 else None)
        )
        if not curr:
            _fetch_errors[ticker] = "no price in response"
            return ticker, None
        prev = prev or curr
        # If the last trade timestamp is not from today (holiday / weekend),
        # no trading has occurred today — zero out the daily change.
        last_trade_ts = meta.get("regularMarketTime")
        if last_trade_ts:
            try:
                # Use US/Eastern as the reference timezone for market date
                from datetime import timedelta
                # Yahoo regularMarketTime is UTC epoch; shift to ET (UTC-4 DST / UTC-5 STD)
                # Use a simple offset: if month 3-11 assume EDT (-4h), else EST (-5h)
                now_utc = datetime.now(timezone.utc)
                et_offset = -4 if 3 <= now_utc.month <= 11 else -5
                today_et = (now_utc + timedelta(hours=et_offset)).date()
                trade_dt_et = (datetime.fromtimestamp(last_trade_ts, tz=timezone.utc) + timedelta(hours=et_offset)).date()
                if trade_dt_et != today_et:
                    prev = curr   # market hasn't traded today — show 0 change
            except Exception:
                pass
        is_fx = ticker.endswith('=X') or ticker.endswith('-CAD') or ticker.endswith('-USD')
        decimals = 4 if is_fx else 2
        return ticker, {
            "price":      round(curr, decimals),
            "prev":       round(prev, decimals),
            "change":     round(curr - prev, decimals),
            "change_pct": round((curr - prev) / prev * 100, 2),
        }
    except Exception as e:
        _fetch_errors[ticker] = str(e)
        return ticker, None


def fetch_prices(extra_tickers: set = None) -> dict:
    """
    Fetch live prices for all portfolio tickers + benchmarks.
    extra_tickers: additional tickers from KV holdings not in _FALLBACK_HOLDINGS.
    """
    global _price_cache, _price_cache_ts
    now = time.time()
    # Only use cache if no new extra tickers are being requested
    if _price_cache and (now - _price_cache_ts) < PRICE_CACHE_TTL and not extra_tickers:
        return _price_cache

    base_tickers  = {h["ticker"] for h in _FALLBACK_HOLDINGS if not h.get("cash")}
    bench_tickers = set(BENCHMARKS.values())
    all_tickers   = base_tickers | bench_tickers | (extra_tickers or set())

    _fetch_errors.clear()
    prices  = {}
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(_fetch_one, session, t): t for t in all_tickers}
        for future in as_completed(futures):
            ticker, data = future.result()
            if data:
                prices[ticker] = data

    _price_cache    = prices
    _price_cache_ts = now
    return prices


# =============================================================================
# Portfolio computation
# =============================================================================

def _compute_from_kv(equity: list, cash: list, prices: dict, usdcad: float,
                     contributions: dict, realized_gains: float,
                     usd_book_rate: float) -> dict:
    """Compute portfolio totals from KV dynamic holdings + live prices."""
    accounts     = {k: 0.0 for k in contributions}
    total_value  = 0.0
    daily_change = 0.0
    usd_cost     = 0.0

    for h in equity:
        ticker = h.get("ticker", "")
        if not ticker or ticker.upper().startswith("CASH"):
            continue
        acct   = h.get("account", "")
        shares = h.get("shares", 0) or 0
        ccy    = h.get("ccy", "USD")
        cost   = h.get("cost_total", 0) or 0
        if ccy == "USD":
            usd_cost += cost
        p = prices.get(ticker)
        if not p:
            continue
        price    = p["price"]
        prev_p   = p.get("prev") or price
        val      = price * shares
        prev_val = prev_p * shares
        if ccy == "USD":
            val      *= usdcad
            prev_val *= usdcad
        if acct in accounts:
            accounts[acct] += val
        total_value  += val
        daily_change += val - prev_val

    for c in cash:
        acct   = c.get("account", "")
        ccy    = c.get("ccy", "USD")
        amount = c.get("amount", 0) or 0
        val    = amount if ccy == "CAD" else amount * usdcad
        if acct in accounts:
            accounts[acct] += val
        total_value += val

    total_cost = sum(contributions.values())
    total_pnl  = total_value - total_cost
    fx_impact  = round(usd_cost * (usdcad - usd_book_rate))
    unrealized = round(total_pnl - realized_gains - fx_impact)
    base_val   = (total_value - daily_change) if total_value != daily_change else total_value

    return {
        "total_value":       round(total_value),
        "total_cost":        round(total_cost),
        "total_pnl":         round(total_pnl),
        "unrealized_gain":   unrealized,
        "realized_gain":     round(realized_gains),
        "fx_impact":         fx_impact,
        "roi_pct":           round(total_pnl / total_cost * 100, 2) if total_cost else 0,
        "daily_change":      round(daily_change),
        "daily_change_pct":  round(daily_change / base_val * 100, 2) if base_val else 0,
        "accounts":          {k: round(v) for k, v in accounts.items()},
        "account_cost":      {k: round(v) for k, v in contributions.items()},
    }


def _compute_from_fallback(prices: dict, usdcad: float) -> dict:
    """Fallback: compute from hardcoded _FALLBACK_HOLDINGS when KV is unavailable."""
    accounts     = {"TFSA": 0.0, "FHSA": 0.0, "RRSP": 0.0, "Investment": 0.0}
    total_value  = 0.0
    daily_change = 0.0
    usd_cost     = 0.0

    for h in _FALLBACK_HOLDINGS:
        acct   = h["account"]
        shares = h["shares"]
        cost   = h["cost_total"]
        ccy    = h["ccy"]

        if h.get("cash"):
            val = cost if ccy == "CAD" else cost * usdcad
            accounts[acct] += val
            total_value    += val
            continue

        p = prices.get(h["ticker"])
        if not p:
            continue

        if ccy == "USD":
            usd_cost += cost

        price    = p["price"]
        prev_p   = p.get("prev") or price
        val      = price * shares
        prev_val = prev_p * shares
        if ccy == "USD":
            val      *= usdcad
            prev_val *= usdcad

        accounts[acct] += val
        total_value    += val
        daily_change   += val - prev_val

    total_cost = sum(_FALLBACK_CONTRIBUTIONS_CAD.values())
    total_pnl  = total_value - total_cost
    fx_impact  = round(usd_cost * (usdcad - _FALLBACK_USD_BOOK_RATE))
    unrealized = round(total_pnl - _FALLBACK_REALIZED_GAINS_CAD - fx_impact)
    base_val   = (total_value - daily_change) if total_value != daily_change else total_value

    return {
        "total_value":       round(total_value),
        "total_cost":        round(total_cost),
        "total_pnl":         round(total_pnl),
        "unrealized_gain":   unrealized,
        "realized_gain":     _FALLBACK_REALIZED_GAINS_CAD,
        "fx_impact":         fx_impact,
        "roi_pct":           round(total_pnl / total_cost * 100, 2) if total_cost else 0,
        "daily_change":      round(daily_change),
        "daily_change_pct":  round(daily_change / base_val * 100, 2) if base_val else 0,
        "accounts":          {k: round(v) for k, v in accounts.items()},
        "account_cost":      {k: round(v) for k, v in _FALLBACK_CONTRIBUTIONS_CAD.items()},
    }


def compute_portfolio(prices: dict) -> dict:
    """
    Compute portfolio metrics + per-account values.

    Priority:
      1. KV user:settings (computed_holdings / cash_positions / contributions_cad)
         — always current because the dashboard writes it on every load and trade
      2. Hardcoded _FALLBACK_HOLDINGS — only used if KV is unreachable or empty
    """
    usdcad = _safe_float(prices.get("USDCAD=X", {}).get("price")) or 1.37

    try:
        settings    = _kv_get_settings()
        kv_equity   = settings.get("computed_holdings", [])
        kv_cash     = settings.get("cash_positions", [])
        kv_contribs = settings.get("contributions_cad")
        kv_realized = settings.get("realized_gains_cad")
        kv_rate     = settings.get("usd_book_rate")

        if kv_equity:
            contribs  = kv_contribs or _FALLBACK_CONTRIBUTIONS_CAD
            realized  = float(kv_realized) if kv_realized is not None else _FALLBACK_REALIZED_GAINS_CAD
            book_rate = float(kv_rate)     if kv_rate     is not None else _FALLBACK_USD_BOOK_RATE
            return _compute_from_kv(kv_equity, kv_cash or [], prices, usdcad,
                                    contribs, realized, book_rate)
    except Exception:
        pass   # fall through to hardcoded

    return _compute_from_fallback(prices, usdcad)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        # ── Determine any extra KV tickers to price-fetch ─────────────────────
        extra_tickers: set = set()
        try:
            settings  = _kv_get_settings()
            kv_equity = settings.get("computed_holdings", [])
            base_set  = {h["ticker"] for h in _FALLBACK_HOLDINGS if not h.get("cash")}
            extra_tickers = {
                h["ticker"] for h in kv_equity
                if h.get("ticker")
                and not h["ticker"].upper().startswith("CASH")
                and h["ticker"] not in base_set
            }
        except Exception:
            pass

        prices    = fetch_prices(extra_tickers if extra_tickers else None)
        portfolio = compute_portfolio(prices)

        # ── Benchmarks ────────────────────────────────────────────────────────
        benchmarks = {
            name: prices[ticker]
            for name, ticker in BENCHMARKS.items()
            if ticker in prices
        }

        # ── Benchmark state (contribution-weighted historical bases) ──────────
        # Written nightly by snapshot.py; read here so the report can use
        # live-compounded values instead of frozen hardcoded constants.
        benchmark_state = _kv_get_benchmark_state() or None

        # ── Holdings prices — all fetched tickers except benchmarks ──────────
        bench_set      = set(BENCHMARKS.values())
        holdings_prices = {
            t: p for t, p in prices.items()
            if t not in bench_set
        }

        resp = {
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "usdcad":         _safe_float(prices.get("USDCAD=X", {}).get("price")) or 1.37,
            "benchmarks":     benchmarks,
            "benchmark_state": benchmark_state,
            "holdings":       holdings_prices,
            "portfolio":      portfolio,
            "_debug_errors":  dict(_fetch_errors) if _fetch_errors else None,
        }

        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",  "public, max-age=0, must-revalidate")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
