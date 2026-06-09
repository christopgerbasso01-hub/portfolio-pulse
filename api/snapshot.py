"""
Portfolio Pulse — Daily Snapshot Endpoint
=========================================
Called by cron-job.org at 20:05 UTC (4:05 PM ET / market close) Mon–Fri.
Stores a full portfolio state snapshot in Vercel KV (Upstash Redis) for:
  • Weekly push notification (Mon open vs Fri close performance)
  • Podcast enhancement (real weekly P&L data)
  • Historical portfolio value chart

Routes
  POST /api/snapshot   — take a new snapshot (requires CRON_SECRET header)
  GET  /api/snapshot   — retrieve last 90 days of snapshots

Snapshot accuracy
-----------------
Holdings come from computed_holdings + cash_positions in KV (saved by the
dashboard on every load/trade — always reflects actual current positions).
Prices come from /api/market (Yahoo Finance).
Any ticker in KV that is NOT yet in market.py's HOLDINGS list gets a
supplemental price fetch directly from Yahoo Finance so new positions are
captured automatically without any code changes.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
KV_URL      = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN    = os.environ.get("KV_REST_API_TOKEN", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

MARKET_API   = "https://portfolio-pulse-dun.vercel.app/api/market"
SNAPSHOT_TTL = 95 * 86400   # 95 days (~3 months of daily history)

# ── Portfolio constants — keep in sync with api/market.py ─────────────────────
# Update CONTRIBUTIONS_CAD when adding fresh capital to any account.
# Update REALIZED_GAINS_CAD when closing a position.
# Update USD_BOOK_RATE only after a major rebalancing at a significantly different rate.
CONTRIBUTIONS_CAD = {
    "TFSA":       44500.0,
    "Investment": 78000.0,
    "FHSA":       24000.0,
    "RRSP":       16132.0,
}
REALIZED_GAINS_CAD = 22193
USD_BOOK_RATE      = 1.3925

# Fallback cash positions — used only if dashboard has never saved cash_positions to KV.
# Values reflect current state after June 4 2026 transactions.
_CASH_FALLBACK = [
    {"ticker": "CASH·USD", "account": "TFSA",       "ccy": "USD", "amount": 344.41},
    {"ticker": "CASH·CAD", "account": "FHSA",       "ccy": "CAD", "amount": 24.34},
    {"ticker": "CASH·USD", "account": "Investment", "ccy": "USD", "amount": 301.38},
    {"ticker": "CASH·USD", "account": "RRSP",       "ccy": "USD", "amount": 653.26},
]

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://finance.yahoo.com",
}


# ── Vercel KV (Upstash Redis) helpers ─────────────────────────────────────────

def _kv(cmd: list) -> dict:
    """Execute a single Redis command via Upstash REST API."""
    if not KV_URL or not KV_TOKEN:
        raise RuntimeError("KV_REST_API_URL / KV_REST_API_TOKEN not configured")
    r = requests.post(
        KV_URL,
        headers={"Authorization": f"Bearer {KV_TOKEN}",
                 "Content-Type":  "application/json"},
        json=cmd,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def kv_set(key: str, value: dict, ttl_seconds: int = SNAPSHOT_TTL) -> None:
    _kv(["SET", key, json.dumps(value), "EX", ttl_seconds])


def kv_get(key: str) -> dict | None:
    result = _kv(["GET", key])
    raw = result.get("result")
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


# ── Price helpers ──────────────────────────────────────────────────────────────

def _safe_float(val, default=None):
    try:
        f = float(val)
        return None if (f != f) else f
    except (TypeError, ValueError):
        return default


def _fetch_price(session, ticker: str):
    """Fetch current price + previous close from Yahoo Finance for a single ticker."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
    try:
        r = session.get(url, headers=_YF_HEADERS, timeout=5)
        if not r.ok:
            return ticker, None
        data = r.json()
        result = data["chart"]["result"][0]
        meta   = result["meta"]
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes if c is not None]
        curr = _safe_float(meta.get("regularMarketPrice") or (closes[-1] if closes else None))
        prev = _safe_float(
            (closes[-2] if len(closes) >= 2 else None)
            or meta.get("chartPreviousClose")
            or meta.get("previousClose")
        )
        if not curr:
            return ticker, None
        prev = prev or curr
        return ticker, {
            "price":      round(curr, 2),
            "prev":       round(prev, 2),
            "change":     round(curr - prev, 2),
            "change_pct": round((curr - prev) / prev * 100, 2),
        }
    except Exception:
        return ticker, None


# ── Portfolio computation from KV data ────────────────────────────────────────

def _compute_portfolio_kv(equity: list, cash: list, prices: dict, usdcad: float,
                           contributions: dict, realized_gains: float,
                           usd_book_rate: float) -> dict:
    """
    Compute portfolio totals from KV holdings (dynamic) + live prices.

    equity         : list from computed_holdings KV — {ticker, account, shares, ccy, cost_total, ...}
    cash           : list from cash_positions KV   — {ticker, account, ccy, amount}
    prices         : {ticker: {price, prev, ...}} from /api/market
    usdcad         : live USD/CAD rate
    contributions  : {account: cad_amount} — true cash deposited per account
    realized_gains : total realized P&L in CAD (active holdings + closed positions)
    usd_book_rate  : weighted-avg USD/CAD rate at purchase time (for FX impact calc)
    """
    accounts     = {k: 0.0 for k in contributions}
    total_value  = 0.0
    daily_change = 0.0
    usd_cost     = 0.0

    # ── Equity positions ──────────────────────────────────────────────────────
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
            continue   # price unavailable — position skipped (logged by caller)

        price      = p["price"]
        prev_price = p.get("prev") or price
        val        = price * shares
        prev_val   = prev_price * shares

        if ccy == "USD":
            val      *= usdcad
            prev_val *= usdcad

        if acct in accounts:
            accounts[acct] += val
        total_value  += val
        daily_change += val - prev_val

    # ── Cash positions ────────────────────────────────────────────────────────
    for c in cash:
        acct   = c.get("account", "")
        ccy    = c.get("ccy", "USD")
        amount = c.get("amount", 0) or 0
        val    = amount if ccy == "CAD" else amount * usdcad
        if acct in accounts:
            accounts[acct] += val
        total_value += val

    # ── Derived metrics ───────────────────────────────────────────────────────
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


# ── Snapshot logic ─────────────────────────────────────────────────────────────

def take_snapshot() -> dict:
    """
    Build a portfolio snapshot using:
      1. computed_holdings + cash_positions from KV (always current — set by dashboard)
      2. Live prices from /api/market (+ supplemental YF fetch for any new tickers)

    Fully dynamic: adding or selling a position in the dashboard is automatically
    reflected at the next snapshot without any code changes.

    Falls back to the market API's pre-computed portfolio only if KV has no holdings yet.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── 1. Live prices from market API ────────────────────────────────────────
    r = requests.get(MARKET_API, timeout=20)
    r.raise_for_status()
    market_data = r.json()
    prices  = dict(market_data.get("holdings", {}))   # copy — we may add to it
    usdcad  = float(market_data.get("usdcad") or 1.37)

    # ── 2. Current holdings from KV ───────────────────────────────────────────
    settings    = kv_get("user:settings") or {}
    kv_equity   = settings.get("computed_holdings", [])
    kv_cash     = settings.get("cash_positions", []) or _CASH_FALLBACK

    source = "kv_holdings"

    # ── 3. Read dynamic portfolio constants from KV (set by dashboard on every load) ──
    # Fall back to module-level hardcoded constants only on the very first run
    # before the dashboard has populated KV.
    contributions  = settings.get("contributions_cad") or CONTRIBUTIONS_CAD
    realized_gains = settings.get("realized_gains_cad")
    if realized_gains is None:
        realized_gains = float(REALIZED_GAINS_CAD)
    else:
        realized_gains = float(realized_gains)
    usd_book_rate  = float(settings.get("usd_book_rate") or USD_BOOK_RATE)

    if not kv_equity:
        # First run or KV never populated — fall back to market API totals
        print("  [snapshot] ⚠ No computed_holdings in KV — using market API fallback")
        portfolio = market_data.get("portfolio", {})
        source    = "market_api_fallback"
    else:
        # ── 4. Fetch prices for any new tickers not yet in market API ─────────
        covered      = set(prices.keys())
        need_prices  = {
            h["ticker"] for h in kv_equity
            if h.get("ticker")
            and not h["ticker"].upper().startswith("CASH")
            and h["ticker"] not in covered
        }
        if need_prices:
            print(f"  [snapshot] Fetching prices for {len(need_prices)} "
                  f"new ticker(s): {sorted(need_prices)}")
            session = requests.Session()
            with ThreadPoolExecutor(max_workers=6) as ex:
                futs = {ex.submit(_fetch_price, session, t): t for t in need_prices}
                for fut in as_completed(futs):
                    ticker, data = fut.result()
                    if data:
                        prices[ticker] = data
                    else:
                        print(f"  [snapshot] ⚠ No price for {ticker} — position excluded")

        # ── 5. Recompute portfolio from KV holdings + live prices + live constants ──
        portfolio = _compute_portfolio_kv(
            kv_equity, kv_cash, prices, usdcad,
            contributions, realized_gains, usd_book_rate,
        )

    # ── 6. Build snapshot ──────────────────────────────────────────────────────
    snapshot = {
        "date":             today,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "total_value":      portfolio.get("total_value"),
        "total_pnl":        portfolio.get("total_pnl"),
        "roi_pct":          portfolio.get("roi_pct"),
        "daily_change":     portfolio.get("daily_change"),
        "daily_change_pct": portfolio.get("daily_change_pct"),
        "usdcad":           usdcad,
        "accounts":         portfolio.get("accounts", {}),
        "account_cost":     portfolio.get("account_cost", {}),
        "holdings_prices":  prices,
        "source":           source,
    }

    # ── 7. Persist to KV ──────────────────────────────────────────────────────
    kv_set(f"snapshot:{today}", snapshot)
    kv_set("snapshot:latest", {"date": today}, ttl_seconds=2 * 86400)

    # Fridays: permanent weekly rollup (survives beyond 95-day daily TTL)
    dt = datetime.now(timezone.utc)
    if dt.weekday() == 4:
        iso_week = dt.isocalendar()
        week_key = f"snapshot:weekly:{iso_week[0]}-W{iso_week[1]:02d}"
        kv_set(week_key, snapshot, ttl_seconds=10 * 365 * 86400)

    # ── 8. Compound today's benchmark returns into benchmark:state ────────────
    try:
        bm_state = _update_benchmark_state(prices, today)
        kv_set("benchmark:state", bm_state, ttl_seconds=2 * 365 * 86400)
        print(
            f"  [snapshot] benchmark_state: "
            f"sp500={bm_state.get('sp500_val', 0):.0f} "
            f"tsx={bm_state.get('tsx_val', 0):.0f} "
            f"nasdaq={bm_state.get('nasdaq_val', 0):.0f}"
        )
    except Exception as exc:
        print(f"  [snapshot] ⚠ Failed to update benchmark_state: {exc}")

    print(
        f"  [snapshot] snapshot:{today} | "
        f"total=${portfolio.get('total_value', 0):,.0f} | "
        f"daily={portfolio.get('daily_change', 0):+,.0f} | "
        f"source={source}"
    )
    return snapshot


def get_recent_snapshots(days: int = 8) -> dict:
    """
    Return up to `days` days of snapshots keyed by date string.
    Used by the podcast generator and weekly push notification.
    """
    today  = datetime.now(timezone.utc).date()
    result = {}
    for i in range(days):
        d    = (today - timedelta(days=i)).isoformat()
        snap = kv_get(f"snapshot:{d}")
        if snap:
            result[d] = snap
    return result


def _update_benchmark_state(prices: dict, today: str) -> dict:
    """
    Compound today's EOD benchmark returns into the stored benchmark_state.
    Called once per day at market close via take_snapshot().

    benchmark:state holds contribution-weighted values — i.e., what a passive
    investor would have today if they had deployed the same contributions into
    each index instead.  Values start from a seeded baseline and compound daily
    via the EOD snapshot rather than being frozen constants.
    """
    try:
        current = kv_get("benchmark:state") or {}
    except Exception:
        current = {}

    # First-time seed: contribution-weighted values at $162,632 total contributions
    # (scaled from previous hardcoded values 191800/185400/196300 at $149,632).
    # After this initial seed the values compound nightly and never need manual updates.
    SEEDS = {
        "sp500_val":   208478.0,
        "tsx_val":     201435.0,
        "nasdaq_val":  213384.0,
        "ref_contrib": 162632.0,
    }

    state = dict(current) if current.get("ref_contrib") else dict(SEEDS)

    # YF ticker symbols used by market.py for each benchmark
    BENCH_TICKERS = {"sp500": "^GSPC", "tsx": "^GSPTSE", "nasdaq": "^IXIC"}
    VAL_KEYS      = {"sp500": "sp500_val", "tsx": "tsx_val", "nasdaq": "nasdaq_val"}

    for bench, ticker in BENCH_TICKERS.items():
        key = VAL_KEYS[bench]
        p   = prices.get(ticker)
        if p and p.get("change_pct") is not None:
            old_val = float(state.get(key) or SEEDS[key])
            state[key] = round(old_val * (1 + p["change_pct"] / 100), 2)

    state["last_updated"] = today
    return state


def compute_weekly_summary(snapshots: dict) -> dict:
    """
    Given a dict of {date: snapshot}, compute Mon–Fri weekly performance.
    Returns a summary dict suitable for push notification or podcast context.
    """
    if not snapshots:
        return {}

    sorted_dates = sorted(snapshots.keys())
    oldest = snapshots[sorted_dates[0]]
    newest = snapshots[sorted_dates[-1]]

    start_val = oldest.get("total_value") or 0
    end_val   = newest.get("total_value") or 0
    week_gain = end_val - start_val
    week_pct  = (week_gain / start_val * 100) if start_val else 0

    acct_start = oldest.get("accounts", {})
    acct_end   = newest.get("accounts", {})
    acct_delta = {
        acct: round(acct_end.get(acct, 0) - acct_start.get(acct, 0))
        for acct in acct_end
    }

    return {
        "period_start":   sorted_dates[0],
        "period_end":     sorted_dates[-1],
        "days_tracked":   len(sorted_dates),
        "start_value":    round(start_val),
        "end_value":      round(end_val),
        "week_gain_cad":  round(week_gain),
        "week_gain_pct":  round(week_pct, 2),
        "account_deltas": acct_delta,
        "start_usdcad":   oldest.get("usdcad"),
        "end_usdcad":     newest.get("usdcad"),
    }


# ── Request handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        """Take a new snapshot — called by cron-job.org via GitHub Actions."""
        if not self._auth():
            return
        try:
            snap = take_snapshot()
            self._respond(200, {
                "ok":          True,
                "date":        snap["date"],
                "total_value": snap["total_value"],
                "source":      snap.get("source"),
            })
        except Exception as exc:
            print(f"  [snapshot] POST error: {exc}")
            self._respond(500, {"error": str(exc)})

    def do_GET(self):
        """Return recent snapshots for charts — public, no auth required.
        Returns up to 90 days of daily data for the portfolio value/ROI charts.
        Injects today_snapshot from KV settings when the cron hasn't fired yet today,
        so charts update on every dashboard refresh rather than once per day.
        """
        try:
            snaps = get_recent_snapshots(days=90)

            # If no cron snapshot for today yet, inject the dashboard's live value
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today not in snaps:
                try:
                    settings = kv_get("user:settings") or {}
                    ts = settings.get("today_snapshot", {})
                    if ts and ts.get("date") == today and ts.get("total_value"):
                        snaps[today] = ts
                except Exception:
                    pass   # don't let a KV read failure break the whole GET

            summary = compute_weekly_summary(snaps)
            chart_points = [
                {
                    "date":         d,
                    "total_value":  snaps[d].get("total_value"),
                    "roi_pct":      snaps[d].get("roi_pct"),
                    "daily_change": snaps[d].get("daily_change"),
                    "accounts":     snaps[d].get("accounts", {}),
                }
                for d in sorted(snaps.keys())
            ]
            self._respond(200, {
                "snapshots":      snaps,
                "count":          len(snaps),
                "weekly_summary": summary,
                "chart_points":   chart_points,
            })
        except Exception as exc:
            print(f"  [snapshot] GET error: {exc}")
            self._respond(500, {"error": str(exc)})

    def _auth(self) -> bool:
        """Verify CRON_SECRET Bearer token."""
        if not CRON_SECRET:
            return True   # dev mode — no secret configured
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {CRON_SECRET}":
            self._respond(401, {"error": "Unauthorized"})
            return False
        return True

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        pass
