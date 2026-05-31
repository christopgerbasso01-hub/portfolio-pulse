"""
Portfolio Pulse — Daily Snapshot Endpoint
=========================================
Called by GitHub Actions at 9:30 PM UTC (5:30 PM ET) Mon–Fri after market close.
Stores a full portfolio state snapshot in Vercel KV (Upstash Redis) for:
  • Weekly push notification (Mon open vs Fri close performance)
  • Friday podcast enhancement (real weekly P&L data)
  • Future: charting historical portfolio value over time

Routes
  POST /api/snapshot   — take a new snapshot (requires CRON_SECRET header)
  GET  /api/snapshot   — retrieve last 8 days of snapshots (requires CRON_SECRET header)
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
KV_URL      = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN    = os.environ.get("KV_REST_API_TOKEN", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

# Call our own market API — serverless functions can't import each other directly
MARKET_API      = "https://portfolio-pulse-dun.vercel.app/api/market"
SNAPSHOT_TTL    = 95 * 86400   # 95 days in seconds (~3 months of daily history)


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


# ── Snapshot logic ─────────────────────────────────────────────────────────────

def take_snapshot() -> dict:
    """
    Fetch live market data from /api/market and store a dated snapshot in KV.
    Key format: snapshot:YYYY-MM-DD
    """
    r = requests.get(MARKET_API, timeout=20)
    r.raise_for_status()
    data = r.json()

    portfolio = data.get("portfolio", {})
    holdings  = data.get("holdings", {})   # {ticker: {price, change, change_pct, prev}}
    usdcad    = float(data.get("usdcad") or 1.37)
    today     = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
        "holdings_prices":  holdings,
    }

    # Store keyed by date
    kv_set(f"snapshot:{today}", snapshot)

    # Keep a "latest" pointer (expires in 2 days — refreshed daily)
    kv_set("snapshot:latest", {"date": today}, ttl_seconds=2 * 86400)

    print(
        f"  [snapshot] snapshot:{today} | "
        f"total=${portfolio.get('total_value', 0):,.0f} | "
        f"daily={portfolio.get('daily_change', 0):+,.0f}"
    )
    return snapshot


def get_recent_snapshots(days: int = 8) -> dict:
    """
    Return up to `days` days of snapshots, keyed by date string.
    Used by the Friday podcast generator and weekly summary notification.
    """
    today  = datetime.now(timezone.utc).date()
    result = {}
    for i in range(days):
        d   = (today - timedelta(days=i)).isoformat()
        key = f"snapshot:{d}"
        snap = kv_get(key)
        if snap:
            result[d] = snap
    return result


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

    start_val  = oldest.get("total_value") or 0
    end_val    = newest.get("total_value") or 0
    week_gain  = end_val - start_val
    week_pct   = (week_gain / start_val * 100) if start_val else 0

    # Per-account weekly change
    acct_start = oldest.get("accounts", {})
    acct_end   = newest.get("accounts", {})
    acct_delta = {
        acct: round((acct_end.get(acct, 0) - acct_start.get(acct, 0)), 0)
        for acct in acct_end
    }

    return {
        "period_start":    sorted_dates[0],
        "period_end":      sorted_dates[-1],
        "days_tracked":    len(sorted_dates),
        "start_value":     round(start_val),
        "end_value":       round(end_val),
        "week_gain_cad":   round(week_gain),
        "week_gain_pct":   round(week_pct, 2),
        "account_deltas":  acct_delta,
        "start_usdcad":    oldest.get("usdcad"),
        "end_usdcad":      newest.get("usdcad"),
    }


# ── Request handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        """Take a new snapshot — called by GitHub Actions cron."""
        if not self._auth():
            return
        try:
            snap = take_snapshot()
            self._respond(200, {
                "ok":          True,
                "date":        snap["date"],
                "total_value": snap["total_value"],
            })
        except Exception as exc:
            print(f"  [snapshot] POST error: {exc}")
            self._respond(500, {"error": str(exc)})

    def do_GET(self):
        """Return recent snapshots for charts — public, no auth required.
        Returns up to 90 days of daily data for the portfolio value/ROI charts.
        """
        try:
            snaps   = get_recent_snapshots(days=90)
            summary = compute_weekly_summary(snaps)
            # Compact chart-ready array sorted oldest→newest
            chart_points = [
                {
                    "date":         d,
                    "total_value":  snaps[d].get("total_value"),
                    "roi_pct":      snaps[d].get("roi_pct"),
                    "daily_change": snaps[d].get("daily_change"),
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
        """Verify CRON_SECRET Bearer token. Returns True if authorized."""
        if not CRON_SECRET:
            # No secret configured — allow all (dev mode)
            return True
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
