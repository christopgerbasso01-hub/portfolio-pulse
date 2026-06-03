"""
Portfolio Pulse — Public Cron Tick
====================================
POST /api/cron_tick?type=intraday   — run intraday alerts (every 10 min, market hours)
POST /api/cron_tick?type=eod        — run end-of-day snapshot + notify (4:10 PM ET daily)

Called by cron-job.org (free external cron service) — no auth required.
All secrets stay inside Vercel env vars; this endpoint just invokes internal logic.

Rate limited via KV: intraday max once per 8 minutes, eod max once per 30 minutes.
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import os
import requests
from datetime import datetime, timezone

KV_URL   = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")
BASE_URL = "https://portfolio-pulse-dun.vercel.app"
CRON_SECRET = os.environ.get("CRON_SECRET", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO         = "christopgerbasso01-hub/portfolio-pulse"

RATE_LIMITS = {
    "intraday":     480,   # 8 minutes between intraday calls
    "eod":         1800,   # 30 minutes between EOD calls
    "intelligence": 300,   # 5 minutes between intelligence triggers
    "podcast":     3600,   # 1 hour between podcast triggers
}


def _kv(cmd: list) -> dict:
    r = requests.post(
        KV_URL,
        headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
        json=cmd, timeout=10,
    )
    r.raise_for_status()
    return r.json()


def kv_get(key: str):
    result = _kv(["GET", key])
    raw = result.get("result")
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def kv_set(key: str, value, ttl: int = 3600):
    _kv(["SET", key, json.dumps(value), "EX", ttl])


def is_rate_limited(tick_type: str) -> bool:
    """Returns True if called too recently."""
    try:
        last = kv_get(f"cron_tick:{tick_type}:last")
        if last:
            elapsed = datetime.now(timezone.utc).timestamp() - float(last)
            if elapsed < RATE_LIMITS.get(tick_type, 300):
                return True
        kv_set(f"cron_tick:{tick_type}:last",
               str(datetime.now(timezone.utc).timestamp()),
               ttl=RATE_LIMITS.get(tick_type, 300) * 2)
        return False
    except Exception:
        return False  # If KV fails, allow the call


def call_endpoint(path: str) -> dict:
    """Call a Vercel API endpoint with CRON_SECRET auth."""
    headers = {"Content-Type": "application/json"}
    if CRON_SECRET:
        headers["Authorization"] = f"Bearer {CRON_SECRET}"
    try:
        r = requests.post(f"{BASE_URL}{path}", headers=headers, timeout=25)
        return {"status": r.status_code, "body": r.json() if r.ok else r.text[:200]}
    except Exception as exc:
        return {"status": 0, "error": str(exc)}


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        qs = parse_qs(urlparse(self.path).query)
        tick_type = qs.get("type", ["intraday"])[0]

        if tick_type not in ("intraday", "eod", "intelligence", "podcast"):
            self._respond(400, {"error": f"Unknown type: {tick_type}"})
            return

        if is_rate_limited(tick_type):
            self._respond(200, {"ok": True, "skipped": "rate_limited"})
            return

        if tick_type == "intraday":
            result = call_endpoint("/api/intraday")
            print(f"  [cron_tick] intraday → {result.get('status')} {result.get('body')}")
            self._respond(200, {"ok": True, "type": "intraday", "result": result})

        elif tick_type == "eod":
            snap  = call_endpoint("/api/snapshot")
            notif = call_endpoint("/api/notify")
            print(f"  [cron_tick] eod snapshot→{snap.get('status')} notify→{notif.get('status')}")
            self._respond(200, {"ok": True, "type": "eod",
                               "snapshot": snap, "notify": notif})

        elif tick_type in ("intelligence", "podcast"):
            # Trigger GitHub Actions workflow dispatch
            workflow = "daily-intelligence.yml" if tick_type == "intelligence" else "weekly-podcast.yml"
            if not GITHUB_TOKEN:
                self._respond(500, {"error": "GITHUB_TOKEN not configured"})
                return
            try:
                r = requests.post(
                    f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/dispatches",
                    headers={"Authorization": f"token {GITHUB_TOKEN}",
                             "Accept": "application/vnd.github.v3+json"},
                    json={"ref": "main"}, timeout=15,
                )
                ok = r.status_code == 204
                print(f"  [cron_tick] {tick_type} dispatch → {r.status_code}")
                self._respond(200, {"ok": ok, "type": tick_type, "status": r.status_code})
            except Exception as exc:
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
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
