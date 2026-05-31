"""
Portfolio Pulse — Push Notification Subscription Manager
=========================================================
POST   /api/push  — save a Web Push subscription to KV
DELETE /api/push  — remove a subscription from KV
GET    /api/push  — return subscription count (used to sync bell state)
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests

KV_URL   = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")

SUBS_KEY = "push:subs"
SUBS_TTL = 400 * 86400   # ~13 months — refreshed on every subscribe


# ── KV helpers ────────────────────────────────────────────────────────────────

def _kv(cmd: list) -> dict:
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


def kv_get(key: str):
    result = _kv(["GET", key])
    raw = result.get("result")
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def kv_set(key: str, value, ttl: int = SUBS_TTL):
    _kv(["SET", key, json.dumps(value), "EX", ttl])


def get_subs() -> list:
    return kv_get(SUBS_KEY) or []


def save_subs(subs: list):
    kv_set(SUBS_KEY, subs)


# ── Handler ───────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        """Subscribe: upsert subscription in KV list (dedup by endpoint)."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return

        endpoint = body.get("endpoint", "")
        if not endpoint:
            self._respond(400, {"error": "Missing endpoint"})
            return

        try:
            subs = get_subs()
            # Remove any existing entry with the same endpoint
            subs = [s for s in subs if s.get("endpoint") != endpoint]
            subs.append(body)
            # Cap at 10 devices
            subs = subs[-10:]
            save_subs(subs)
            print(f"  [push] subscribed — total subs: {len(subs)}")
            self._respond(200, {"ok": True, "count": len(subs)})
        except Exception as exc:
            print(f"  [push] POST error: {exc}")
            self._respond(500, {"error": str(exc)})

    def do_DELETE(self):
        """Unsubscribe: remove matching endpoint from KV list."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return

        endpoint = body.get("endpoint", "")
        try:
            subs = get_subs()
            before = len(subs)
            subs = [s for s in subs if s.get("endpoint") != endpoint]
            save_subs(subs)
            print(f"  [push] unsubscribed — removed {before - len(subs)}, remaining: {len(subs)}")
            self._respond(200, {"ok": True, "count": len(subs)})
        except Exception as exc:
            print(f"  [push] DELETE error: {exc}")
            self._respond(500, {"error": str(exc)})

    def do_GET(self):
        """Return subscription count + notification history for the panel."""
        try:
            subs    = get_subs()
            history = kv_get("notify:history") or []
            self._respond(200, {"count": len(subs), "history": history})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    # ── Helpers ───────────────────────────────────────────────────────────────

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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        pass
