"""
Portfolio Pulse — Quote Fetcher
================================
GET /api/quote?tickers=MU,AAPL,BNS.TO

Fetches live price + previous close for tickers not in the main holdings list
(i.e. newly added via the transaction log). Uses the same Yahoo Finance v8/chart
API as market.py so it works on Vercel without any extra dependencies.
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import time
import requests

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://finance.yahoo.com",
}

_cache    = {}
_cache_ts = {}
CACHE_TTL = 12   # seconds — match market.py


def _safe_float(val, default=None):
    try:
        f = float(val)
        return None if (f != f) else f
    except (TypeError, ValueError):
        return default


def _fetch_one(session, ticker):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    try:
        r = session.get(url, headers=_YF_HEADERS, timeout=8)
        if not r.ok:
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
            return ticker, None
        prev = prev or curr
        is_fx = ticker.endswith("=X") or ticker.endswith("-CAD") or ticker.endswith("-USD")
        dp = 4 if is_fx else 2
        return ticker, {
            "price":      round(curr, dp),
            "prev":       round(prev, dp),
            "change":     round(curr - prev, dp),
            "change_pct": round((curr - prev) / prev * 100, 2),
        }
    except Exception:
        return ticker, None


def fetch_quotes(tickers: list) -> dict:
    now    = time.time()
    result = {}
    to_fetch = []

    for t in tickers:
        if t in _cache and (now - _cache_ts.get(t, 0)) < CACHE_TTL:
            result[t] = _cache[t]
        else:
            to_fetch.append(t)

    if not to_fetch:
        return result

    session = requests.Session()
    with ThreadPoolExecutor(max_workers=min(8, len(to_fetch))) as ex:
        futures = {ex.submit(_fetch_one, session, t): t for t in to_fetch}
        for future in as_completed(futures):
            ticker, data = future.result()
            if data:
                result[ticker]    = data
                _cache[ticker]    = data
                _cache_ts[ticker] = now

    return result


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        params  = parse_qs(urlparse(self.path).query)
        raw     = params.get("tickers", [""])[0]
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]

        prices = fetch_quotes(tickers) if tickers else {}

        body = json.dumps(prices).encode()
        self.send_response(200)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",  "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
