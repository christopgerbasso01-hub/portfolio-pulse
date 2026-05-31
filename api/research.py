"""
Portfolio Pulse — Stock Research Endpoint
==========================================
GET /api/research?ticker=MU            → full research data (KV-cached 15 min)
GET /api/research?ticker=MU&type=chart&range=1y → OHLC chart (KV-cached 5 min)
GET /api/research?ticker=MU&type=price → live price only (no cache, 15s updates)

Primary: Yahoo Finance quoteSummary with cookie+crumb auth
         (resolves the "blocked from cloud IPs" issue)
Fallback: FMP for fundamentals when YF crumb fails
          FMP always used for: EPS history, analyst breakdown, price targets
Charts:   Yahoo Finance v8/chart (proven reliable from Vercel)
"""
from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import requests
import xml.etree.ElementTree as ET
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
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer":    "https://finance.yahoo.com",
}

_YF_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":     "*/*",
    "Referer":    "https://finance.yahoo.com",
    "Origin":     "https://finance.yahoo.com",
}

# Cached crumb/session so we don't re-authenticate every call
_yf_session = None
_yf_crumb   = None

def _get_yf_crumb():
    """Obtain a Yahoo Finance crumb via cookie auth. Works from server IPs."""
    global _yf_session, _yf_crumb
    if _yf_session and _yf_crumb:
        return _yf_session, _yf_crumb
    try:
        s = requests.Session()
        s.headers.update(_YF_HEADERS)
        # Step 1: get cookies from Yahoo Finance
        s.get("https://finance.yahoo.com/", timeout=8)
        # Step 2: exchange cookies for a crumb
        cr = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                   headers={**_YF_API_HEADERS, "Accept": "text/plain"}, timeout=8)
        if cr.ok and cr.text and cr.text != "":
            _yf_session = s
            _yf_crumb   = cr.text.strip()
            print(f"  [research] YF crumb obtained (len={len(_yf_crumb)})")
            return _yf_session, _yf_crumb
    except Exception as e:
        print(f"  [research] crumb error: {e}")
    return None, None

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
        print(f"  [research] FMP_API_KEY is empty — cannot call {path}")
        return None
    try:
        r = requests.get(
            f"https://financialmodelingprep.com/api/v3/{path}",
            params={**params, "apikey": FMP_API_KEY},
            timeout=timeout,
        )
        if not r.ok:
            print(f"  [research] FMP {path} HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
        # Detect FMP error response (e.g. rate limit, invalid key)
        if isinstance(data, dict) and ("Error Message" in data or "message" in data):
            print(f"  [research] FMP {path} error: {data}")
            return None
        return data
    except Exception as e:
        print(f"  [research] FMP {path} exception: {e}")
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


def rss_news(ticker: str) -> list:
    """
    News via Yahoo Finance RSS — no auth required, returns real article links.
    URL: https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}
    """
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        r = requests.get(url, headers=_YF_API_HEADERS, timeout=8)
        if not r.ok:
            return []
        root = ET.fromstring(r.content)
        ns   = {"dc": "http://purl.org/dc/elements/1.1/"}
        news = []
        for item in root.findall(".//item")[:6]:
            title = item.findtext("title", "").strip()
            link  = item.findtext("link", "").strip()
            pub   = item.findtext("pubDate", "").strip()
            source = item.findtext("dc:creator", "", ns) or item.findtext("source", "")
            if title and link:
                news.append({
                    "title":     title,
                    "link":      link,
                    "publisher": source or "Yahoo Finance",
                    "published": pub,
                })
        return news
    except Exception as e:
        print(f"  [research] RSS news error: {e}")
        return []


def fmp_search(query: str) -> list:
    """
    Search tickers by company name or symbol via FMP.
    Returns list of {ticker, name, exchange}.
    """
    data = _fmp_get("search", {"query": query, "limit": 8})
    if not data or not isinstance(data, list):
        return []
    return [
        {
            "ticker":   d.get("symbol", ""),
            "name":     d.get("name", ""),
            "exchange": d.get("exchangeShortName", ""),
        }
        for d in data[:8]
        if d.get("symbol") and d.get("name")
    ]


# ── Yahoo Finance helpers (chart only) ────────────────────────────────────────

def yf_quote_summary(ticker: str) -> dict | None:
    """Fetch fundamentals from Yahoo Finance using cookie+crumb auth."""
    session, crumb = _get_yf_crumb()
    if not session:
        return None
    mods   = "price,summaryDetail,defaultKeyStatistics,financialData,recommendationTrend,assetProfile,calendarEvents,earnings"
    params = {"modules": mods, "crumb": crumb} if crumb else {"modules": mods}
    try:
        r = session.get(
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
            params=params, headers=_YF_API_HEADERS, timeout=12,
        )
        if not r.ok:
            print(f"  [research] YF quoteSummary HTTP {r.status_code} for {ticker}")
            return None
        result = r.json().get("quoteSummary", {}).get("result")
        return result[0] if result else None
    except Exception as e:
        print(f"  [research] YF quoteSummary error: {e}")
        return None


def yf_news(ticker: str) -> list:
    """News headlines — RSS first (includes links), API fallback."""
    # Try RSS first — works without auth and includes real article links
    rss = rss_news(ticker)
    if rss:
        return rss
    # Fallback to YF API
    try:
        session = _yf_session or requests.Session()
        r = session.get(
            f"https://query2.finance.yahoo.com/v2/finance/news?symbols={ticker}&count=5",
            headers=_YF_API_HEADERS, timeout=8,
        )
        if not r.ok:
            return []
        data  = r.json()
        items = data.get("items", {}).get("result", []) or data.get("news", [])
        return [
            {"title": n.get("title", ""), "publisher": n.get("publisher") or n.get("source", {}).get("label", ""),
             "published": n.get("providerPublishTime") or n.get("pubDate"), "link": n.get("link") or n.get("url", "")}
            for n in items[:5] if n.get("title")
        ]
    except Exception:
        return []


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
    Fetch research data.
    Strategy:
      1. Yahoo Finance quoteSummary (crumb auth) — primary for all fundamentals
      2. FMP — primary for EPS history + analyst breakdown (if key is valid)
         FMP is also used as fallback if YF fails
    All calls run in parallel.
    """
    # Kick off parallel fetches
    with ThreadPoolExecutor(max_workers=7) as ex:
        f_yf_qs    = ex.submit(yf_quote_summary,     ticker)       # YF primary
        f_earnings = ex.submit(fmp_earnings_history, ticker)       # FMP EPS
        f_analyst  = ex.submit(fmp_analyst_recs,     ticker)       # FMP analyst
        f_targets  = ex.submit(fmp_price_targets,    ticker)       # FMP targets
        f_fmp_q    = ex.submit(fmp_quote,            ticker)       # FMP price fallback
        f_fmp_p    = ex.submit(fmp_profile,          ticker)       # FMP profile fallback
        f_news     = ex.submit(fmp_news,             ticker)       # FMP news

    qs       = f_yf_qs.result()    # Yahoo Finance quoteSummary
    eps_hist = f_earnings.result() or []
    analyst  = f_analyst.result()  or {}
    targets  = f_targets.result()  or {}
    fmp_q    = f_fmp_q.result()    or {}
    fmp_p    = f_fmp_p.result()    or {}
    _fmp_news= f_news.result()     or []

    # If Yahoo Finance succeeded, use it as primary
    # If not, fall back to FMP
    using_yf = qs is not None

    if not using_yf and not fmp_q and not fmp_p:
        return {
            "error": f"'{ticker}' not found or data temporarily unavailable. Try again shortly.",
            "ticker": ticker,
        }

    # ── Build result from Yahoo Finance data ──────────────────────────────
    if using_yf:
        return _build_from_yf(ticker, qs, eps_hist, analyst, targets, _fmp_news)
    else:
        # FMP fallback path
        return _build_from_fmp(ticker, fmp_q, fmp_p, eps_hist, analyst, targets, _fmp_news)


def _raw(obj, *keys, default=None):
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


def _analyst_breakdown(analyst: dict) -> dict:
    sb  = analyst.get("analystRatingsStrongBuy",  0)
    b   = analyst.get("analystRatingsBuy",        0)
    h   = analyst.get("analystRatingsHold",       0)
    s   = analyst.get("analystRatingsSell",       0)
    ss  = analyst.get("analystRatingsStrongSell", 0)
    tot = sb + b + h + s + ss
    return {"strongBuy": sb, "buy": b, "hold": h, "sell": s, "strongSell": ss, "total": tot}


def _build_from_yf(ticker: str, qs: dict, eps_hist: list, analyst: dict, targets: dict, fmp_news_list: list = None) -> dict:
    """Build response dict from Yahoo Finance quoteSummary data."""
    price    = qs.get("price",                {})
    summary  = qs.get("summaryDetail",        {})
    keystats = qs.get("defaultKeyStatistics", {})
    findata  = qs.get("financialData",        {})
    rectrd   = qs.get("recommendationTrend",  {})
    profile  = qs.get("assetProfile",         {})
    calendar = qs.get("calendarEvents",       {})
    earnings = qs.get("earnings",             {})

    # Analyst from Yahoo (more complete than FMP for rec trend)
    rec_periods = rectrd.get("trend", [])
    rec_now     = rec_periods[0] if rec_periods else {}
    yf_ab = {
        "strongBuy":  rec_now.get("strongBuy",  0),
        "buy":        rec_now.get("buy",        0),
        "hold":       rec_now.get("hold",       0),
        "sell":       rec_now.get("sell",       0),
        "strongSell": rec_now.get("strongSell", 0),
    }
    yf_ab["total"] = sum(yf_ab.values())

    # Use FMP analyst if YF has no data
    ab = yf_ab if yf_ab["total"] > 0 else _analyst_breakdown(analyst)

    # Analyst targets — prefer FMP price-target-consensus, fallback to YF
    t_mean = targets.get("targetConsensus") or _raw(findata, "targetMeanPrice")
    t_high = targets.get("targetHigh")      or _raw(findata, "targetHighPrice")
    t_low  = targets.get("targetLow")       or _raw(findata, "targetLowPrice")

    # Recommendation key
    rec_key = _val(findata, "recommendationKey") or ""

    # Earnings date
    earn_dates = _val(calendar, "earnings", "earningsDate") or []
    earn_date  = None
    for ed in earn_dates[:2]:
        ts = ed.get("raw") if isinstance(ed, dict) else ed
        if ts:
            earn_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")
            break

    # Ex-dividend
    ex_div_ts   = _raw(summary, "exDividendDate")
    ex_div_date = datetime.fromtimestamp(ex_div_ts, tz=timezone.utc).strftime("%b %d, %Y") if ex_div_ts and isinstance(ex_div_ts, (int, float)) else None

    div_rate  = _raw(summary, "dividendRate")
    div_yield = _raw(summary, "dividendYield")
    div_str   = f"${div_rate:.2f} ({div_yield*100:.2f}%)" if div_rate and div_yield else "N/A"

    # EPS history — prefer FMP, fall back to YF earnings chart
    if not eps_hist:
        for q in (earnings.get("earningsChart", {}).get("quarterly") or [])[-8:]:
            eps_hist.append({"date": _val(q, "date"), "actual": _raw(q, "actual"), "estimate": _raw(q, "estimate")})

    # News — YF first, FMP as fallback
    news = yf_news(ticker) or fmp_news_list or []

    return {
        "ticker":   ticker,
        "name":     _val(price, "longName") or _val(price, "shortName") or ticker,
        "exchange": _val(price, "exchangeName") or "—",
        "currency": _val(price, "currency") or "USD",
        "quoteType": "EQUITY",
        "price":          _raw(price, "regularMarketPrice"),
        "priceChange":    _raw(price, "regularMarketChange"),
        "priceChangePct": _raw(price, "regularMarketChangePercent"),
        "previousClose":  _raw(price, "regularMarketPreviousClose"),
        "open":           _raw(price, "regularMarketOpen"),
        "dayHigh":        _raw(price, "regularMarketDayHigh"),
        "dayLow":         _raw(price, "regularMarketDayLow"),
        "volume":         _raw(price, "regularMarketVolume"),
        "volumeFmt":      _fmt(price, "regularMarketVolume"),
        "marketCap":      _raw(price, "marketCap"),
        "marketCapFmt":   _fmt(price, "marketCap"),
        "fiftyTwoWeekHigh": _raw(summary, "fiftyTwoWeekHigh"),
        "fiftyTwoWeekLow":  _raw(summary, "fiftyTwoWeekLow"),
        "avgVolume":      _raw(summary, "averageVolume"),
        "avgVolumeFmt":   _fmt(summary, "averageVolume"),
        "beta":           _raw(summary, "beta"),
        "trailingPE":     _raw(summary, "trailingPE"),
        "forwardPE":      _raw(keystats, "forwardPE"),
        "trailingEps":    _raw(keystats, "trailingEps"),
        "priceToBook":    _raw(keystats, "priceToBook"),
        "dividendStr":    div_str,
        "exDividendDate": ex_div_date,
        "earningsDate":   earn_date,
        "targetHigh":     t_high,
        "targetLow":      t_low,
        "targetMean":     t_mean,
        "targetMedian":   targets.get("targetMedian"),
        "recommendationKey": rec_key,
        "numAnalysts":    _raw(findata, "numberOfAnalystOpinions") or ab["total"] or None,
        "analystBreakdown": ab,
        "grossMargins":   _raw(findata, "grossMargins"),
        "profitMargins":  _raw(findata, "profitMargins"),
        "description":    (_val(profile, "longBusinessSummary") or "")[:800],
        "industry":       _val(profile, "industry")  or "",
        "sector":         _val(profile, "sector")    or "",
        "website":        _val(profile, "website")   or "",
        "employees":      _val(profile, "fullTimeEmployees"),
        "country":        _val(profile, "country")   or "",
        "epsHistory":     eps_hist,
        "news":           news,
        "fetchedAt":      datetime.now(timezone.utc).isoformat(),
    }


def _build_from_fmp(ticker: str, fmp_q: dict, fmp_p: dict, eps_hist: list, analyst: dict, targets: dict, fmp_news_list: list = None) -> dict:
    """Build response dict from FMP data (fallback when YF crumb fails)."""
    div_rate  = fmp_p.get("lastDiv") or 0
    price_now = fmp_q.get("price") or fmp_p.get("price") or 1
    div_yield = (div_rate / price_now) if div_rate and price_now else 0
    div_str   = f"${div_rate:.2f} ({div_yield*100:.2f}%)" if div_rate else "N/A"

    earn_raw  = fmp_q.get("earningsAnnouncement", "")
    earn_date = None
    if earn_raw:
        try:
            earn_date = datetime.fromisoformat(earn_raw[:10]).strftime("%b %d, %Y")
        except Exception:
            pass

    ab  = _analyst_breakdown(analyst)
    rec = (analyst.get("analystRatingsConsensus") or "hold").lower().replace(" ", "_")

    news = yf_news(ticker) or fmp_news_list or []

    return {
        "ticker":   ticker,
        "name":     fmp_p.get("companyName") or fmp_q.get("name") or ticker,
        "exchange": fmp_p.get("exchangeShortName") or fmp_p.get("exchange") or "—",
        "currency": fmp_p.get("currency") or "USD",
        "quoteType": "EQUITY",
        "price":          fmp_q.get("price"),
        "priceChange":    fmp_q.get("change"),
        "priceChangePct": fmp_q.get("changesPercentage"),
        "previousClose":  fmp_q.get("previousClose"),
        "open":           fmp_q.get("open"),
        "dayHigh":        fmp_q.get("dayHigh"),
        "dayLow":         fmp_q.get("dayLow"),
        "volume":         fmp_q.get("volume"),
        "volumeFmt":      None,
        "marketCap":      fmp_q.get("marketCap"),
        "marketCapFmt":   None,
        "fiftyTwoWeekHigh": fmp_q.get("yearHigh"),
        "fiftyTwoWeekLow":  fmp_q.get("yearLow"),
        "avgVolume":      fmp_q.get("avgVolume"),
        "avgVolumeFmt":   None,
        "beta":           fmp_p.get("beta"),
        "trailingPE":     fmp_q.get("pe"),
        "forwardPE":      None,
        "trailingEps":    fmp_q.get("eps"),
        "priceToBook":    None,
        "dividendStr":    div_str,
        "exDividendDate": None,
        "earningsDate":   earn_date,
        "targetHigh":     targets.get("targetHigh"),
        "targetLow":      targets.get("targetLow"),
        "targetMean":     targets.get("targetConsensus"),
        "targetMedian":   targets.get("targetMedian"),
        "recommendationKey": rec,
        "numAnalysts":    ab["total"] or None,
        "analystBreakdown": ab,
        "grossMargins":   None,
        "profitMargins":  None,
        "description":    (fmp_p.get("description") or "")[:800],
        "industry":       fmp_p.get("industry")  or "",
        "sector":         fmp_p.get("sector")    or "",
        "website":        fmp_p.get("website")   or "",
        "employees":      fmp_p.get("fullTimeEmployees"),
        "country":        fmp_p.get("country")   or "",
        "epsHistory":     eps_hist,
        "news":           news,
        "fetchedAt":      datetime.now(timezone.utc).isoformat(),
    }

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
        query     = (params.get("q",       [""])[0] or "").strip()

        # ── Company name / ticker search (no ticker param needed) ─────────────
        if req_type == "search":
            if not query:
                self._respond(400, {"error": "q parameter required"})
                return
            try:
                results = fmp_search(query)
                self._respond(200, {"results": results})
            except Exception as e:
                self._respond(500, {"error": str(e)})
            return

        if not ticker:
            self._respond(400, {"error": "ticker parameter required"})
            return

        try:
            if req_type == "news":
                # Fresh news fetch — no KV cache so headlines stay current
                news = rss_news(ticker) or fmp_news(ticker)
                self._respond(200, {"news": news})
                return

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
