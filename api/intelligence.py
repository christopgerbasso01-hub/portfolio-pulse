"""
Portfolio Pulse — Intelligence KV Store
========================================
GET  /api/intelligence              — return latest intelligence JSON from KV
POST /api/intelligence  (CRON_SECRET) — store new intelligence JSON in KV
Called by generate_intelligence.py after Groq generation completes.
Bypasses Vercel CDN so the dashboard always gets the freshest data.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests

KV_URL       = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN     = os.environ.get("KV_REST_API_TOKEN", "")
CRON_SECRET  = os.environ.get("CRON_SECRET", "")
INTEL_KEY    = "intelligence:latest"
INTEL_TTL    = 7 * 86400   # 7 days


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


def kv_set(key: str, value, ttl: int = INTEL_TTL):
    _kv(["SET", key, json.dumps(value), "EX", ttl])


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        """Return latest intelligence from KV (always fresh — no CDN caching)."""
        try:
            data = kv_get(INTEL_KEY)
            if data:
                self._respond(200, data)
            else:
                self._respond(404, {"error": "No intelligence data in KV yet"})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def do_POST(self):
        """Store new intelligence JSON in KV (called by generate_intelligence.py)."""
        if not self._auth():
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return
        if not body.get("generated_at"):
            self._respond(400, {"error": "Missing generated_at field"})
            return
        try:
            kv_set(INTEL_KEY, body)
            print(f"  [intelligence] stored — generated_at: {body.get('generated_at')}")
            self._respond(200, {"ok": True, "generated_at": body.get("generated_at")})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def _auth(self) -> bool:
        if not CRON_SECRET:
            return True
        token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if token != CRON_SECRET:
            self._respond(401, {"error": "Unauthorized"})
            return False
        return True

    def _respond(self, code: int, body: dict):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control",  "no-store, no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(b)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        pass
