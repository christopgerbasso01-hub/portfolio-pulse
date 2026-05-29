from http.server import BaseHTTPRequestHandler
import json
import time
from urllib.parse import urlparse, parse_qs

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

_cache = {}
_cache_ts = {}
CACHE_TTL = 60


def _safe_float(val):
    try:
        f = float(val)
        return None if (f != f) else f
    except (TypeError, ValueError):
        return None


def fetch_quotes(tickers):
    now = time.time()
    results = {}
    to_fetch = []

    for t in tickers:
        if t in _cache and (now - _cache_ts.get(t, 0)) < CACHE_TTL:
            results[t] = _cache[t]
        else:
            to_fetch.append(t)

    if not to_fetch or not HAS_YF:
        return results

    try:
        raw = yf.download(
            tickers=" ".join(to_fetch),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        for ticker in to_fetch:
            try:
                df = raw["Close"].dropna() if len(to_fetch) == 1 else raw[ticker]["Close"].dropna()
                if len(df) >= 2:
                    curr = _safe_float(df.iloc[-1])
                    prev = _safe_float(df.iloc[-2])
                    if curr and prev:
                        entry = {
                            "price": round(curr, 2),
                            "prev": round(prev, 2),
                            "change": round(curr - prev, 2),
                            "change_pct": round((curr - prev) / prev * 100, 2),
                        }
                        results[ticker] = entry
                        _cache[ticker] = entry
                        _cache_ts[ticker] = now
                elif len(df) == 1:
                    curr = _safe_float(df.iloc[-1])
                    if curr:
                        entry = {"price": round(curr, 2), "prev": None, "change": None, "change_pct": None}
                        results[ticker] = entry
                        _cache[ticker] = entry
                        _cache_ts[ticker] = now
            except Exception:
                pass
    except Exception:
        # Fallback: fetch individually
        for ticker in to_fetch:
            try:
                t_obj = yf.Ticker(ticker)
                fi = t_obj.fast_info
                curr = _safe_float(fi.last_price)
                prev = _safe_float(fi.previous_close)
                if curr:
                    entry = {
                        "price": round(curr, 2),
                        "prev": round(prev, 2) if prev else None,
                        "change": round(curr - prev, 2) if prev else None,
                        "change_pct": round((curr - prev) / prev * 100, 2) if prev else None,
                    }
                    results[ticker] = entry
                    _cache[ticker] = entry
                    _cache_ts[ticker] = now
            except Exception:
                pass

    return results


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        raw_tickers = params.get("tickers", [""])[0]
        tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]

        prices = fetch_quotes(tickers) if tickers else {}

        body = json.dumps(prices).encode()
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
