"""
Portfolio Pulse — Stock Research Endpoint
==========================================
GET /api/research?ticker=AAPL          → full research data  (KV-cached 15 min)
GET /api/research?ticker=AAPL&type=chart&range=1y  → OHLC chart (KV-cached 5 min)
GET /api/research?ticker=AAPL&type=price           → live price only (no cache)

Data sources:
  Yahoo Finance Quote Summary API  — price, fundamentals, analyst consensus
  Financial Modeling Prep          — EPS actual vs estimate history
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

# ── Config ────────────────────────────────────────────────────────────────────
KV_URL      = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN    = os.environ.get("KV_REST_API_TOKEN", "")
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")

CACHE_QUOTE = 15 * 60   # 15 min for fundamentals
CACHE_CHART = 5  * 60   # 5 min for chart data

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "*/*",
    "Referer":    "https://finance.yahoo.com",
}

# chart range → (yf_range, yf_interval)
CHART_RANGES = {
    "1d":  ("1d",  "2m"),
    "1w":  ("5d",  "60m"),
    "1m":  ("1mo", "1d"),
    "3m":  ("3mo", "1d"),
    "ytd": ("ytd", "1d"),
    "1y":  ("1y",  "1d"),
    "5y":  ("5y",  "1wk"),
    "10y": ("10y", "1mo"),
    "all": ("max", "3mo"),
}


# ── KV helpers ────────────────────────────────────────────────────────────────

def _kv(cmd: list) -> dict:
    if not KV_URL or not KV_TOKEN:
        return {}
    r = requests.post(
        KV_URL,
        headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
        json=cmd, timeout=5,
    )
    r.raise_for_status()
    return r.json()

def kv_get(key: str):
    try:
        raw = _kv(["GET", key]).get("result")
        return json.loads(raw) if raw else None
    except Exception:
        return None

def kv_set(key: str, value, ttl: int):
    try:
        _kv(["SET", key, json.dumps(value), "EX", ttl])
    except Exception:
        pass


# ── Yahoo Finance helpers ─────────────────────────────────────────────────────

def yf_quote_summary(ticker: str) -> dict | None:
    """Fetch comprehensive fundamentals + analyst data from Yahoo Finance."""
    mods = "price,summaryDetail,defaultKeyStatistics,financialData,recommendationTrend,assetProfile,calendarEvents,earnings"
    url  = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={mods}"
    try:
        r = requests.get(url, headers=_YF_HEADERS, timeout=10)
        if not r.ok:
            return None
        res = r.json().get("quoteSummary", {}).get("result")
        return res[0] if res else None
    except Exception as e:
        print(f"  [research] YF summary error: {e}")
        return None


def yf_chart(ticker: str, yf_range: str, interval: str) -> dict | None:
    """Fetch OHLCV chart points."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={yf_range}"
    try:
        r = requests.get(url, headers=_YF_HEADERS, timeout=12)
        if not r.ok:
            return None
        res = r.json().get("chart", {}).get("result")
        if not res:
            return None
        res = res[0]
        ts     = res.get("timestamp", [])
        q      = res.get("indicators", {}).get("quote", [{}])[0]
        closes = q.get("close", [])
        opens  = q.get("open", [])
        highs  = q.get("high", [])
        lows   = q.get("low", [])
        vols   = q.get("volume", [])
        meta   = res.get("meta", {})
        points = []
        for i, t in enumerate(ts):
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue
            points.append({
                "t": t,
                "c": round(c, 2),
                "o": round(opens[i],  2) if i < len(opens)  and opens[i]  else round(c, 2),
                "h": round(highs[i],  2) if i < len(highs)  and highs[i]  else round(c, 2),
                "l": round(lows[i],   2) if i < len(lows)   and lows[i]   else round(c, 2),
                "v": int(vols[i] or 0) if i < len(vols) else 0,
            })
        return {
            "points":        points,
            "currency":      meta.get("currency", "USD"),
            "previousClose": meta.get("chartPreviousClose") or meta.get("previousClose"),
        }
    except Exception as e:
        print(f"  [research] YF chart error: {e}")
        return None


def yf_live_price(ticker: str) -> dict | None:
    """Fast live-price-only fetch."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
    try:
        r = requests.get(url, headers=_YF_HEADERS, timeout=5)
        if not r.ok:
            return None
        meta = r.json()["chart"]["result"][0]["meta"]
        curr = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose") or curr
        if not curr:
            return None
        chg     = round(curr - prev, 2)
        chg_pct = round(chg / prev * 100, 2) if prev else 0
        return {"ticker": ticker, "price": round(curr, 2), "change": chg, "changePct": chg_pct}
    except Exception:
        return None


def yf_news(ticker: str) -> list:
    """Fetch 5 recent headlines from Yahoo Finance."""
    url = f"https://query2.finance.yahoo.com/v2/finance/news?symbols={ticker}&count=5"
    try:
        r = requests.get(url, headers=_YF_HEADERS, timeout=6)
        if not r.ok:
            return []
        data  = r.json()
        items = data.get("items", {}).get("result", []) or data.get("news", [])
        news  = []
        for n in items[:5]:
            title = n.get("title", "")
            if title:
                news.append({
                    "title":     title,
                    "publisher": n.get("publisher") or n.get("source", {}).get("label", ""),
                    "published": n.get("providerPublishTime") or n.get("pubDate"),
                    "link":      n.get("link") or n.get("url", ""),
                })
        return news
    except Exception:
        return []


# ── FMP helpers ───────────────────────────────────────────────────────────────

def fmp_earnings_history(ticker: str) -> list:
    """EPS actual vs estimate for last 8 quarters (FMP is more reliable than YF for this)."""
    if not FMP_API_KEY:
        return []
    try:
        sym = ticker.replace(".TO", "").replace(".V", "")
        r   = requests.get(
            f"https://financialmodelingprep.com/api/v3/earnings-surprises/{sym}",
            params={"apikey": FMP_API_KEY, "limit": 8},
            timeout=8,
        )
        if not r.ok:
            return []
        return [
            {
                "date":     d.get("date", ""),
                "actual":   d.get("actualEarningResult"),
                "estimate": d.get("estimatedEarning"),
            }
            for d in r.json()[:8]
        ]
    except Exception:
        return []


# ── Research data builder ─────────────────────────────────────────────────────

def _raw(obj, *keys, default=None):
    """Drill into nested dicts, return the 'raw' numeric value."""
    for k in keys:
        obj = obj.get(k) if isinstance(obj, dict) else None
    return (obj.get("raw") if isinstance(obj, dict) else obj) if obj is not None else default

def _fmt(obj, *keys, default=None):
    for k in keys:
        obj = obj.get(k) if isinstance(obj, dict) else None
    return (obj.get("fmt") if isinstance(obj, dict) else obj) if obj is not None else default

def _val(obj, *keys, default=None):
    for k in keys:
        obj = obj.get(k) if isinstance(obj, dict) else None
    return obj if obj is not None else default


def build_quote(ticker: str) -> dict:
    qs = yf_quote_summary(ticker)
    if not qs:
        return {"error": f"'{ticker}' not found or data unavailable.", "ticker": ticker}

    price    = qs.get("price",                 {})
    summary  = qs.get("summaryDetail",         {})
    keystats = qs.get("defaultKeyStatistics",  {})
    findata  = qs.get("financialData",         {})
    rectrd   = qs.get("recommendationTrend",   {})
    profile  = qs.get("assetProfile",          {})
    calendar = qs.get("calendarEvents",        {})
    earnings = qs.get("earnings",              {})

    # ── Analyst recommendation breakdown (most recent period) ─────────────
    rec_periods = rectrd.get("trend", [])
    rec_now     = rec_periods[0] if rec_periods else {}
    analyst_totals = {
        "strongBuy":  rec_now.get("strongBuy",  0),
        "buy":        rec_now.get("buy",        0),
        "hold":       rec_now.get("hold",       0),
        "sell":       rec_now.get("sell",       0),
        "strongSell": rec_now.get("strongSell", 0),
    }
    analyst_totals["total"] = sum(analyst_totals.values())

    # ── Earnings history from YF (fallback if FMP empty) ─────────────────
    yf_eps = []
    for q in (earnings.get("earningsChart", {}).get("quarterly") or [])[-8:]:
        yf_eps.append({
            "date":     _val(q, "date"),
            "actual":   _raw(q, "actual"),
            "estimate": _raw(q, "estimate"),
        })

    # ── Earnings/dividend dates ───────────────────────────────────────────
    earn_dates = _val(calendar, "earnings", "earningsDate") or []
    next_earn  = None
    for ed in earn_dates[:2]:
        ts = ed.get("raw") if isinstance(ed, dict) else ed
        if ts:
            next_earn = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")
            break

    ex_div_ts   = _raw(summary, "exDividendDate")
    ex_div_date = datetime.fromtimestamp(ex_div_ts, tz=timezone.utc).strftime("%b %d, %Y") if ex_div_ts and isinstance(ex_div_ts, (int, float)) else None

    div_rate  = _raw(summary, "dividendRate")
    div_yield = _raw(summary, "dividendYield")
    if div_rate and div_yield:
        div_str = f"${div_rate:.2f} ({div_yield*100:.2f}%)"
    else:
        div_str = _fmt(summary, "dividendRate") or "N/A"

    # ── News ─────────────────────────────────────────────────────────────
    news = yf_news(ticker)

    # ── EPS history (prefer FMP) ──────────────────────────────────────────
    fmp_eps = fmp_earnings_history(ticker)
    eps_history = fmp_eps if fmp_eps else yf_eps

    return {
        "ticker":          ticker,
        "name":            _val(price, "longName") or _val(price, "shortName") or ticker,
        "exchange":        _val(price, "exchangeName"),
        "currency":        _val(price, "currency") or "USD",
        "quoteType":       _val(price, "quoteType"),

        # Live price
        "price":           _raw(price, "regularMarketPrice"),
        "priceChange":     _raw(price, "regularMarketChange"),
        "priceChangePct":  _raw(price, "regularMarketChangePercent"),
        "previousClose":   _raw(price, "regularMarketPreviousClose"),
        "open":            _raw(price, "regularMarketOpen"),
        "dayHigh":         _raw(price, "regularMarketDayHigh"),
        "dayLow":          _raw(price, "regularMarketDayLow"),
        "volume":          _raw(price, "regularMarketVolume"),
        "volumeFmt":       _fmt(price, "regularMarketVolume"),
        "marketCap":       _raw(price, "marketCap"),
        "marketCapFmt":    _fmt(price, "marketCap"),

        # Key stats
        "fiftyTwoWeekHigh":  _raw(summary, "fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow":   _raw(summary, "fiftyTwoWeekLow"),
        "avgVolume":         _raw(summary, "averageVolume"),
        "avgVolumeFmt":      _fmt(summary, "averageVolume"),
        "beta":              _raw(summary, "beta"),
        "trailingPE":        _raw(summary, "trailingPE"),
        "forwardPE":         _raw(keystats, "forwardPE"),
        "trailingEps":       _raw(keystats, "trailingEps"),
        "priceToBook":       _raw(keystats, "priceToBook"),

        # Dividend / earnings
        "dividendStr":     div_str,
        "exDividendDate":  ex_div_date,
        "earningsDate":    next_earn,

        # Analyst
        "targetHigh":      _raw(findata, "targetHighPrice"),
        "targetLow":       _raw(findata, "targetLowPrice"),
        "targetMean":      _raw(findata, "targetMeanPrice"),
        "targetMedian":    _raw(findata, "targetMedianPrice"),
        "recommendationKey": _val(findata, "recommendationKey"),
        "numAnalysts":     _raw(findata, "numberOfAnalystOpinions"),
        "analystBreakdown": analyst_totals,
        "grossMargins":    _raw(findata, "grossMargins"),
        "profitMargins":   _raw(findata, "profitMargins"),

        # Company
        "description": (_val(profile, "longBusinessSummary") or "")[:800],
        "industry":    _val(profile, "industry") or "",
        "sector":      _val(profile, "sector")   or "",
        "website":     _val(profile, "website")  or "",
        "employees":   _val(profile, "fullTimeEmployees"),
        "country":     _val(profile, "country")  or "",

        # EPS history
        "epsHistory": eps_history,

        # News
        "news": news,

        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


# ── Handler ───────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        params    = parse_qs(urlparse(self.path).query)
        ticker    = (params.get("ticker",  [""])[0] or "").upper().strip()
        req_type  = (params.get("type",    ["quote"])[0])
        range_key = (params.get("range",   ["1y"])[0])

        if not ticker:
            self._respond(400, {"error": "ticker parameter required"})
            return

        try:
            if req_type == "price":
                # Fast live-price update — no KV cache (called every 15s)
                data = yf_live_price(ticker)
                if not data:
                    self._respond(502, {"error": "Price fetch failed"})
                    return
                self._respond(200, data)

            elif req_type == "chart":
                cfg        = CHART_RANGES.get(range_key, CHART_RANGES["1y"])
                cache_key  = f"research:chart:{ticker}:{range_key}"
                cached     = kv_get(cache_key)
                if cached:
                    self._respond(200, cached)
                    return
                data = yf_chart(ticker, cfg[0], cfg[1])
                if not data:
                    self._respond(404, {"error": "Chart data unavailable"})
                    return
                kv_set(cache_key, data, CACHE_CHART)
                self._respond(200, data)

            else:
                # Full research quote — cached 15 min
                cache_key = f"research:quote:{ticker}"
                cached    = kv_get(cache_key)
                if cached:
                    self._respond(200, cached)
                    return
                data = build_quote(ticker)
                if "error" not in data:
                    kv_set(cache_key, data, CACHE_QUOTE)
                self._respond(200, data)

        except Exception as exc:
            print(f"  [research] unhandled error: {exc}")
            self._respond(500, {"error": str(exc)})

    def _respond(self, code: int, body: dict):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(b)))
        self._cors()
        self.end_headers()
        self.wfile.write(b)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
