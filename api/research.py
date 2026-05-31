"""
Portfolio Pulse — Stock Research Endpoint
==========================================
Yahoo Finance v10/quoteSummary is blocked from Vercel cloud IPs (requires
crumb/cookie auth). This version uses FMP as the primary data source for
fundamentals, and Yahoo Finance v8/chart (proven to work) for price charts.

GET /api/research?ticker=MU            → full research data (KV-cached 15 min)
GET /api/research?ticker=MU&type=chart&range=1y → OHLC chart (KV-cached 5 min)
GET /api/research?ticker=MU&type=price → live price only (no cache, 15s updates)

Data sources:
  FMP (parallel, all cached):
    /v3/quote/{ticker}                      → live price, volume, 52W range, PE, EPS
    /v3/profile/{ticker}                    → description, sector, industry, beta, divs
    /v3/analyst-stock-recommendations/{t}  → strong buy / buy / hold / sell counts
    /v3/price-target-consensus/{t}          → mean / high / low analyst price targets
    /v3/earnings-surprises/{t}              → EPS actual vs estimate history
    /v3/stock_news?tickers={t}              → recent news headlines
  Yahoo Finance v8/chart (proven reliable from Vercel):
    chart/{ticker}?interval=…&range=…      → OHLCV for all time ranges
    chart/{ticker}?interval=1m&range=1d    → fast live-price update
"""
from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ── FMP helpers ───────────────────────────────────────────────────────────────

def _fmp_sym(ticker: str) -> str:
    """Strip exchange suffix for FMP (CM.TO → CM)."""
    return ticker.replace(".TO", "").replace(".V", "").replace(".CN", "")

def _fmp_get(path: str, params: dict, timeout: int = 8):
    """Make a GET request to FMP, return parsed JSON or None."""
    if not FMP_API_KEY:
        return None
    try:
        r = requests.get(
            f"https://financialmodelingprep.com/api/v3/{path}",
            params={**params, "apikey": FMP_API_KEY},
            timeout=timeout,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


def fmp_quote(ticker: str) -> dict:
    """Live quote — price, change, volume, 52W range, PE, EPS, earnings date."""
    data = _fmp_get(f"quote/{_fmp_sym(ticker)}", {})
    return data[0] if data else {}


def fmp_profile(ticker: str) -> dict:
    """Company profile — description, sector, industry, beta, dividends, exchange."""
    data = _fmp_get(f"profile/{_fmp_sym(ticker)}", {})
    return data[0] if data else {}


def fmp_analyst_recs(ticker: str) -> dict:
    """Most recent analyst rating breakdown — strongBuy/buy/hold/sell counts."""
    data = _fmp_get(f"analyst-stock-recommendations/{_fmp_sym(ticker)}", {"limit": 1})
    return data[0] if data else {}


def fmp_price_targets(ticker: str) -> dict:
    """Analyst price target consensus — mean / high / low."""
    data = _fmp_get(f"price-target-consensus/{_fmp_sym(ticker)}", {})
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def fmp_earnings_history(ticker: str) -> list:
    """EPS actual vs estimate — last 8 quarters."""
    data = _fmp_get(f"earnings-surprises/{_fmp_sym(ticker)}", {"limit": 8})
    if not data:
        return []
    return [
        {"date": d.get("date", ""), "actual": d.get("actualEarningResult"), "estimate": d.get("estimatedEarning")}
        for d in data[:8]
    ]


def fmp_news(ticker: str) -> list:
    """Recent news headlines from FMP."""
    data = _fmp_get("stock_news", {"tickers": _fmp_sym(ticker), "limit": 5})
    if not data:
        return []
    return [
        {
            "title":     n.get("title", ""),
            "publisher": n.get("site", ""),
            "published": n.get("publishedDate"),
            "link":      n.get("url", ""),
        }
        for n in data[:5]
        if n.get("title")
    ]


# ── Yahoo Finance helpers (chart only) ────────────────────────────────────────

def yf_chart(ticker: str, yf_range: str, interval: str) -> dict | None:
    """Fetch OHLCV chart data from Yahoo Finance (works from Vercel)."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={yf_range}"
    try:
        r = requests.get(url, headers=_YF_HEADERS, timeout=12)
        if not r.ok:
            return None
        res = r.json().get("chart", {}).get("result")
        if not res:
            return None
        res = res[0]
        ts   = res.get("timestamp", [])
        q    = res.get("indicators", {}).get("quote", [{}])[0]
        meta = res.get("meta", {})
        points = []
        for i, t in enumerate(ts):
            closes = q.get("close", [])
            c = closes[i] if i < len(closes) else None
            if c is None:
                continue
            opens  = q.get("open",   [])
            highs  = q.get("high",   [])
            lows   = q.get("low",    [])
            vols   = q.get("volume", [])
            points.append({
                "t": t,
                "c": round(c, 2),
                "o": round(opens[i],  2) if i < len(opens)  and opens[i]  else round(c, 2),
                "h": round(highs[i],  2) if i < len(highs)  and highs[i]  else round(c, 2),
                "l": round(lows[i],   2) if i < len(lows)   and lows[i]   else round(c, 2),
                "v": int(vols[i] or 0)   if i < len(vols)                  else 0,
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
    """Fast live price via Yahoo Finance chart API (works from Vercel)."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
    try:
        r    = requests.get(url, headers=_YF_HEADERS, timeout=5)
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


# ── Main data builder ─────────────────────────────────────────────────────────

def build_quote(ticker: str) -> dict:
    """
    Fetch all research data in parallel from FMP.
    All calls are parallel via ThreadPoolExecutor.
    """
    if not FMP_API_KEY:
        return {"error": "FMP_API_KEY not configured. Add it in Vercel environment variables.", "ticker": ticker}

    # Parallel fetch — all 6 calls fire simultaneously
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_quote    = ex.submit(fmp_quote,          ticker)
        f_profile  = ex.submit(fmp_profile,        ticker)
        f_analyst  = ex.submit(fmp_analyst_recs,   ticker)
        f_targets  = ex.submit(fmp_price_targets,  ticker)
        f_earnings = ex.submit(fmp_earnings_history, ticker)
        f_news     = ex.submit(fmp_news,           ticker)

    quote    = f_quote.result()    or {}
    profile  = f_profile.result()  or {}
    analyst  = f_analyst.result()  or {}
    targets  = f_targets.result()  or {}
    eps_hist = f_earnings.result() or []
    news     = f_news.result()     or []

    if not quote and not profile:
        return {"error": f"'{ticker}' not found. Check the ticker symbol and try again.", "ticker": ticker}

    # ── Normalise analyst consensus key ──────────────────────────────────────
    rec_raw = (analyst.get("analystRatingsConsensus") or "").lower().replace(" ", "_")

    # ── Earnings date (ISO string from FMP) ──────────────────────────────────
    earn_raw = quote.get("earningsAnnouncement", "")
    earn_date = earn_raw[:10] if earn_raw else None  # "2024-12-18T21:00:00.000+0000" → "2024-12-18"
    if earn_date:
        try:
            earn_date = datetime.fromisoformat(earn_date).strftime("%b %d, %Y")
        except Exception:
            pass

    # ── Ex-dividend date ──────────────────────────────────────────────────────
    ex_div_raw  = profile.get("lastDiv")          # dollars per share (not a date from profile)
    ex_div_date = None  # FMP profile doesn't give ex-div date directly

    # ── Dividend string ───────────────────────────────────────────────────────
    div_rate  = profile.get("lastDiv") or 0
    price_now = quote.get("price") or profile.get("price") or 1
    div_yield = (div_rate / price_now) if div_rate and price_now else 0
    div_str   = f"${div_rate:.2f} ({div_yield*100:.2f}%)" if div_rate else "N/A"

    return {
        "ticker":   ticker,
        "name":     profile.get("companyName") or quote.get("name") or ticker,
        "exchange": profile.get("exchangeShortName") or profile.get("exchange") or quote.get("exchange") or "—",
        "currency": profile.get("currency") or "USD",
        "quoteType": "EQUITY",

        # Live price (from FMP quote)
        "price":          quote.get("price"),
        "priceChange":    quote.get("change"),
        "priceChangePct": quote.get("changesPercentage"),
        "previousClose":  quote.get("previousClose"),
        "open":           quote.get("open"),
        "dayHigh":        quote.get("dayHigh"),
        "dayLow":         quote.get("dayLow"),
        "volume":         quote.get("volume"),
        "volumeFmt":      None,
        "marketCap":      quote.get("marketCap"),
        "marketCapFmt":   None,

        # Key stats
        "fiftyTwoWeekHigh": quote.get("yearHigh"),
        "fiftyTwoWeekLow":  quote.get("yearLow"),
        "avgVolume":        quote.get("avgVolume"),
        "avgVolumeFmt":     None,
        "beta":             profile.get("beta"),
        "trailingPE":       quote.get("pe"),
        "forwardPE":        None,
        "trailingEps":      quote.get("eps"),
        "priceToBook":      None,

        # Dividend / earnings
        "dividendStr":   div_str,
        "exDividendDate": ex_div_date,
        "earningsDate":   earn_date,

        # Analyst (from FMP)
        "targetHigh":    targets.get("targetHigh"),
        "targetLow":     targets.get("targetLow"),
        "targetMean":    targets.get("targetConsensus"),
        "targetMedian":  targets.get("targetMedian"),
        "recommendationKey": rec_raw or "hold",
        "numAnalysts":   (
            analyst.get("analystRatingsStrongBuy", 0) +
            analyst.get("analystRatingsBuy",       0) +
            analyst.get("analystRatingsHold",      0) +
            analyst.get("analystRatingsSell",      0) +
            analyst.get("analystRatingsStrongSell",0)
        ) or None,
        "analystBreakdown": {
            "strongBuy":  analyst.get("analystRatingsStrongBuy",  0),
            "buy":        analyst.get("analystRatingsBuy",        0),
            "hold":       analyst.get("analystRatingsHold",       0),
            "sell":       analyst.get("analystRatingsSell",       0),
            "strongSell": analyst.get("analystRatingsStrongSell", 0),
            "total":      (
                analyst.get("analystRatingsStrongBuy",  0) +
                analyst.get("analystRatingsBuy",        0) +
                analyst.get("analystRatingsHold",       0) +
                analyst.get("analystRatingsSell",       0) +
                analyst.get("analystRatingsStrongSell", 0)
            ),
        },
        "grossMargins":  None,
        "profitMargins": None,

        # Company profile (from FMP)
        "description": (profile.get("description") or "")[:800],
        "industry":    profile.get("industry")   or "",
        "sector":      profile.get("sector")     or "",
        "website":     profile.get("website")    or "",
        "employees":   profile.get("fullTimeEmployees"),
        "country":     profile.get("country")    or "",

        # EPS history
        "epsHistory": eps_hist,

        # News
        "news": news,

        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


# ── Request handler ────────────────────────────────────────────────────────────

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
                # Fast live-price update every 15s (Yahoo Finance chart API — works from Vercel)
                data = yf_live_price(ticker)
                if not data:
                    # FMP fallback for price
                    q = fmp_quote(ticker)
                    if q and q.get("price"):
                        data = {"ticker": ticker, "price": q["price"],
                                "change": q.get("change", 0), "changePct": q.get("changesPercentage", 0)}
                    else:
                        self._respond(502, {"error": "Price fetch failed"})
                        return
                self._respond(200, data)

            elif req_type == "chart":
                cfg       = CHART_RANGES.get(range_key, CHART_RANGES["1y"])
                cache_key = f"research:chart:{ticker}:{range_key}"
                cached    = kv_get(cache_key)
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
                # Full research — cached 15 min
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
