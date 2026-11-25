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
    # Snapshot of the dashboard's computed holdings on 2026-11-22. Used ONLY when KV is
    # unreachable; responses then carry portfolio.source = "fallback" so the dashboard
    # can say its figures are not live.
    # TFSA
    {"ticker": "FNGU",    "account": "TFSA",        "shares": 495, "cost_total": 9435.62, "ccy": "USD"},
    {"ticker": "NVDA",    "account": "TFSA",        "shares": 40, "cost_total": 471.15, "ccy": "USD"},
    {"ticker": "TXF.TO",  "account": "TFSA",        "shares": 320, "cost_total": 7033.29, "ccy": "CAD"},
    {"ticker": "SPXL",    "account": "TFSA",        "shares": 23, "cost_total": 4067.61, "ccy": "USD"},
    {"ticker": "TSLA",    "account": "TFSA",        "shares": 14, "cost_total": 3479.76, "ccy": "USD"},
    {"ticker": "UDOW",    "account": "TFSA",        "shares": 84, "cost_total": 3098.21, "ccy": "USD"},
    {"ticker": "CM.TO",   "account": "TFSA",        "shares": 54, "cost_total": 4378.96, "ccy": "CAD"},
    {"ticker": "AVGO",    "account": "TFSA",        "shares": 8, "cost_total": 3223.61, "ccy": "USD"},
    {"ticker": "COST",    "account": "TFSA",        "shares": 3, "cost_total": 903.03, "ccy": "USD"},
    {"ticker": "NFLX",    "account": "TFSA",        "shares": 20, "cost_total": 1056.79, "ccy": "USD"},
    {"ticker": "MSFT",    "account": "TFSA",        "shares": 2, "cost_total": 357.55, "ccy": "USD"},
    {"ticker": "AAPL",    "account": "TFSA",        "shares": 4, "cost_total": 468.71, "ccy": "USD"},
    {"ticker": "QCOM",    "account": "TFSA",        "shares": 5, "cost_total": 467.1, "ccy": "USD"},
    {"ticker": "SHEL",    "account": "TFSA",        "shares": 22, "cost_total": 1018.21, "ccy": "USD"},
    {"ticker": "ET",      "account": "TFSA",        "shares": 60, "cost_total": 803.28, "ccy": "USD"},
    {"ticker": "BMO",    "account": "TFSA",        "shares": 15, "cost_total": 1269.88, "ccy": "USD"},
    # Investment
    {"ticker": "SPXL",    "account": "Investment",  "shares": 75, "cost_total": 9785.05, "ccy": "USD"},
    {"ticker": "FNGU",    "account": "Investment",  "shares": 665, "cost_total": 13266.68, "ccy": "USD"},
    {"ticker": "CM.TO",   "account": "Investment",  "shares": 50, "cost_total": 6674.45, "ccy": "CAD"},
    {"ticker": "TXF.TO",  "account": "Investment",  "shares": 415, "cost_total": 10287.3, "ccy": "CAD"},
    {"ticker": "RY.TO",   "account": "Investment",  "shares": 19, "cost_total": 3895.45, "ccy": "CAD"},
    {"ticker": "IBKR",    "account": "Investment",  "shares": 40, "cost_total": 2331.75, "ccy": "USD"},
    {"ticker": "TSM",     "account": "Investment",  "shares": 9, "cost_total": 2652.27, "ccy": "USD"},
    {"ticker": "LYV",     "account": "Investment",  "shares": 11, "cost_total": 954.41, "ccy": "USD"},
    {"ticker": "V",       "account": "Investment",  "shares": 4, "cost_total": 912.71, "ccy": "USD"},
    {"ticker": "SOXL",    "account": "Investment",  "shares": 245, "cost_total": 18196.84, "ccy": "USD"},
    {"ticker": "ZWB.TO",  "account": "Investment",  "shares": 237, "cost_total": 7345.1, "ccy": "CAD"},
    # FHSA
    {"ticker": "SPXL",    "account": "FHSA",        "shares": 53, "cost_total": 5180.21, "ccy": "USD"},
    {"ticker": "TXF.TO",  "account": "FHSA",        "shares": 434, "cost_total": 11411.13, "ccy": "CAD"},
    {"ticker": "UDOW",    "account": "FHSA",        "shares": 86, "cost_total": 3889.41, "ccy": "USD"},
    {"ticker": "FNGU",    "account": "FHSA",        "shares": 157, "cost_total": 2995.88, "ccy": "USD"},
    {"ticker": "ENB.TO",  "account": "FHSA",        "shares": 82, "cost_total": 4499.84, "ccy": "CAD"},
    # RRSP
    {"ticker": "TXF.TO",  "account": "RRSP",        "shares": 395, "cost_total": 9121.13, "ccy": "CAD"},
    {"ticker": "TSM",     "account": "RRSP",        "shares": 6, "cost_total": 2390.38, "ccy": "USD"},
    {"ticker": "UDOW",    "account": "RRSP",        "shares": 36, "cost_total": 1807.06, "ccy": "USD"},
    {"ticker": "FNGU",    "account": "RRSP",        "shares": 56, "cost_total": 1068.64, "ccy": "USD"},
    {"ticker": "SOXL",    "account": "RRSP",        "shares": 63, "cost_total": 7570.08, "ccy": "USD"},
    # Cash
    {"ticker": "_CASH_USD", "account": "TFSA", "shares": 1, "cost_total": 15.56, "ccy": "USD", "cash": True},
    {"ticker": "_CASH_CAD", "account": "FHSA", "shares": 1, "cost_total": 1198.67, "ccy": "CAD", "cash": True},
    {"ticker": "_CASH_CAD", "account": "Investment", "shares": 1, "cost_total": 25.0, "ccy": "CAD", "cash": True},
    {"ticker": "_CASH_CAD", "account": "TFSA", "shares": 1, "cost_total": 146.15, "ccy": "CAD", "cash": True},
    {"ticker": "_CASH_CAD", "account": "RRSP", "shares": 1, "cost_total": 112.36, "ccy": "CAD", "cash": True},
]

# Fallback constants — used only when KV is unavailable
_FALLBACK_CONTRIBUTIONS_CAD = {
    "TFSA": 44500.0,
    "Investment": 106000.0,
    "FHSA": 24000.0,
    "RRSP": 18763.0,
}
_FALLBACK_REALIZED_GAINS_CAD = 18982
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
PRICE_CACHE_TTL = 20   # every open tab shares one Yahoo round per 20 s


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
        session_date, last_prev = None, None
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
                session_date = trade_dt_et.isoformat()
                if trade_dt_et != today_et:
                    # Market hasn't traded today: keep the last session's move
                    # (shown as e.g. "Fri") but report 0 for *today*.
                    last_prev = prev
                    prev = curr
            except Exception:
                pass
        is_fx = ticker.endswith('=X') or ticker.endswith('-CAD') or ticker.endswith('-USD')
        decimals = 4 if is_fx else 2
        return ticker, {
            "price":      round(curr, decimals),
            "prev":       round(prev, decimals),
            "change":     round(curr - prev, decimals),
            "change_pct": round((curr - prev) / prev * 100, 2),
            "session":    session_date,
            "last_change_pct": round((curr - last_prev) / last_prev * 100, 2) if last_prev else None,
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
    bench_tickers = set(BENCHMARKS.values()) | set(BENCH_FUNDS.values())
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
# Contribution-weighted benchmark
# -----------------------------------------------------------------------------
# Every contribution "buys" each benchmark on its own date, at that day's
# dividend-adjusted close, so the benchmark is a true like-for-like: same
# dollars, same dates, dividends reinvested. Value today = Σ amount × P_now / P_then.
#
# Amounts are CAD and are applied to each fund's own-currency return, which is
# the same as converting at one rate (today's) on both ends — FX is left out
# by design. Funds track the indexes the dashboard names:
#   S&P 500 → SPY · S&P/TSX Composite → XIC.TO · NASDAQ-100 → QQQ
#
# Units (CAD per adjusted price point) are recomputed once a day, because
# Yahoo rescales adjusted history after every distribution; between those
# recomputes the live value is units × live price, with no compounding drift.
# =============================================================================
BENCH_FUNDS = {"sp500": "SPY", "tsx": "XIC.TO", "nasdaq": "QQQ"}

# Net deposits made before the transaction log began (2026-06-04): every
# month-to-month change in the dashboard's contribution history through
# 2026-05, INCLUDING withdrawals (negative — e.g. the $9,026 moved out of the
# non-registered account in Aug 2024). Dated the 15th; anything before Jan 2021
# is booked to Jan 2021, the earliest month on record. Everything after comes
# from CONTRIBUTION transactions in KV with exact dates. A withdrawal sells
# benchmark units at that day's price, exactly as a deposit buys them.
PRELOG_CONTRIBUTIONS = [
    ("2021-01", "TFSA", 15000), ("2021-02", "TFSA", 1000), ("2021-03", "TFSA", 1000),
    ("2021-04", "TFSA", 1000), ("2021-08", "Investment", 15000), ("2022-01", "TFSA", 3000),
    ("2023-01", "TFSA", 2000), ("2023-07", "FHSA", 8000), ("2024-01", "FHSA", 8000),
    ("2024-04", "TFSA", 7000), ("2024-06", "TFSA", 7500), ("2024-08", "Investment", -9026),
    ("2024-10", "RRSP", 9026), ("2024-12", "FHSA", 8000), ("2024-12", "TFSA", 7000),
    ("2025-05", "Investment", 17394), ("2025-05", "RRSP", 7106), ("2025-06", "Investment", 4000),
    ("2025-09", "Investment", -8000), ("2025-10", "Investment", 24000), ("2026-03", "Investment", 15000),
    ("2026-04", "Investment", 6632),
]


def contribution_events(transactions: list) -> list:
    """Dated contributions: the pre-log history plus every logged CONTRIBUTION."""
    ev = [{"date": f"{m}-15", "account": a, "amount": float(x)} for m, a, x in PRELOG_CONTRIBUTIONS]
    for t in transactions or []:
        if t.get("type") == "CONTRIBUTION" and t.get("date") and t.get("amount"):
            ev.append({"date": t["date"][:10], "account": t.get("account", ""), "amount": float(t["amount"])})
    ev.sort(key=lambda e: e["date"])
    return ev


def _adj_history(session, ticker: str) -> list:
    """[(YYYY-MM-DD, adjusted close)] daily since 2020, oldest first."""
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1=1577836800&period2={int(time.time())}&interval=1d&events=div")
    r = session.get(url, headers=_YF_HEADERS, timeout=10)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts  = res.get("timestamp") or []
    ind = res.get("indicators", {})
    adj = (ind.get("adjclose") or [{}])[0].get("adjclose") or (ind.get("quote") or [{}])[0].get("close") or []
    out = []
    for t, a in zip(ts, adj):
        if a is None:
            continue
        out.append((datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), float(a)))
    return out


def benchmark_units(events: list, histories: dict) -> dict:
    """{account: {bench: units}} — units = CAD amount / adjusted close on (or after) the date."""
    import bisect
    units = {}
    for bench, hist in histories.items():
        if not hist:
            continue
        dates = [d for d, _ in hist]
        for e in events:
            i = bisect.bisect_left(dates, e["date"])
            if i >= len(hist):
                i = len(hist) - 1          # contribution after the last close → today's price
            px = hist[i][1]
            if px > 0:
                units.setdefault(e["account"], {}).setdefault(bench, 0.0)
                units[e["account"]][bench] += e["amount"] / px
    return units


def month_end_closes(hist: list) -> dict:
    """{YYYY-MM: last adjusted close of that month}."""
    out = {}
    for d, a in hist:
        out[d[:7]] = a
    return out


_units_cache = {}


def get_benchmark_model(settings: dict):
    """Units per account and month-end closes, cached in memory and in KV for the day."""
    global _units_cache
    events = contribution_events(settings.get("transactions") or [])
    key = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "|" + str(round(sum(e["amount"] for e in events))) + "|" + str(len(events))
    if _units_cache.get("key") == key:
        return _units_cache
    try:
        cached = _kv_cmd(["GET", "benchmark:model"])
        if cached and cached.get("key") == key:
            _units_cache = cached
            return cached
    except Exception:
        pass
    try:
        session = requests.Session()
        with ThreadPoolExecutor(max_workers=3) as ex:
            futs = {b: ex.submit(_adj_history, session, t) for b, t in BENCH_FUNDS.items()}
            hists = {b: f.result() for b, f in futs.items()}
    except Exception as exc:
        _fetch_errors["benchmark_history"] = str(exc)
        return _units_cache or None
    model = {
        "key":     key,
        "as_of":   max((h[-1][0] for h in hists.values() if h), default=None),
        "funds":   BENCH_FUNDS,
        "units":   benchmark_units(events, hists),
        "monthly": {b: month_end_closes(h) for b, h in hists.items()},
        "events":  events,
    }
    _units_cache = model
    try:
        _kv_cmd(["SET", "benchmark:model", json.dumps(model), "EX", 3 * 86400])
    except Exception:
        pass
    return model


def _kv_cmd(cmd: list):
    if not KV_URL or not KV_TOKEN:
        return None
    r = requests.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}",
                                       "Content-Type": "application/json"},
                      json=cmd, timeout=5)
    r.raise_for_status()
    raw = r.json().get("result")
    if isinstance(raw, str) and raw[:1] in "{[":
        return json.loads(raw)
    return raw




# =============================================================================
# Dividend history — trailing 12 months per held ticker, for the income forecast.
# Cached for the day in memory and KV (one Yahoo call per ticker per day).
# =============================================================================
_div_cache = {}


def _div_events(session, ticker: str) -> list:
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range=1y&interval=1d&events=div")
    r = session.get(url, headers=_YF_HEADERS, timeout=8)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    divs = ((res.get("events") or {}).get("dividends") or {}).values()
    cutoff = time.time() - 365 * 86400
    out = [{"date": datetime.fromtimestamp(d["date"], tz=timezone.utc).strftime("%Y-%m-%d"),
            "amount": round(float(d["amount"]), 6)}
           for d in divs if d.get("date", 0) >= cutoff and d.get("amount")]
    return sorted(out, key=lambda x: x["date"])


def get_dividend_model(tickers: set):
    global _div_cache
    tickers = sorted(t for t in tickers if t and not t.upper().startswith("CASH"))
    key = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "|" + ",".join(tickers)
    if _div_cache.get("key") == key:
        return _div_cache
    try:
        cached = _kv_cmd(["GET", "dividend:model"])
        if cached and cached.get("key") == key:
            _div_cache = cached
            return cached
    except Exception:
        pass
    session, hist, failed = requests.Session(), {}, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_div_events, session, t): t for t in tickers}
        for f in as_completed(futs):
            t = futs[f]
            try:
                hist[t] = f.result()
            except Exception:
                failed.append(t)
    model = {"key": key, "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
             "history": hist, "failed": sorted(failed)}
    if not failed:   # don't pin a partial result for the whole day
        try:
            _kv_cmd(["SET", "dividend:model", json.dumps(model), "EX", 2 * 86400])
        except Exception:
            pass
    _div_cache = model
    return model

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
        "source":            "kv",
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
        "source":            "fallback",
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

        # ── Contribution-weighted benchmark model (units per account) ────────
        try:
            benchmark_model = get_benchmark_model(_kv_get_settings())
        except Exception as exc:
            _fetch_errors["benchmark_model"] = str(exc)
            benchmark_model = None
        benchmark_funds = {b: prices[t] for b, t in BENCH_FUNDS.items() if t in prices}
        try:
            dividend_model = get_dividend_model({h.get("ticker", "") for h in
                                                 (_kv_get_settings().get("computed_holdings") or [])})
        except Exception as exc:
            _fetch_errors["dividend_model"] = str(exc)
            dividend_model = None

        # ── Holdings prices — all fetched tickers except benchmarks ──────────
        bench_set      = set(BENCHMARKS.values()) | set(BENCH_FUNDS.values())
        holdings_prices = {
            t: p for t, p in prices.items()
            if t not in bench_set
        }

        resp = {
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "usdcad":         _safe_float(prices.get("USDCAD=X", {}).get("price")) or 1.37,
            "benchmarks":     benchmarks,
            "benchmark_state": benchmark_state,
            "benchmark_model": benchmark_model,
            "benchmark_funds": benchmark_funds,
            "dividend_model":  dividend_model,
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
