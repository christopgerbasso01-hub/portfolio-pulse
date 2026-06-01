"""
Portfolio Pulse — User Settings
================================
GET  /api/settings        — fetch settings (rrsp_limit, etc.)
POST /api/settings        — save settings
Stored in Vercel KV so they sync across all devices.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests

KV_URL   = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")
SETTINGS_KEY = "user:settings"
SETTINGS_TTL = 400 * 86400   # ~13 months


def _kv(cmd: list) -> dict:
    r = requests.post(
        KV_URL,
        headers={"Authorization": f"Bearer {KV_TOKEN}",
                 "Content-Type":  "application/json"},
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


def kv_set(key: str, value, ttl: int = SETTINGS_TTL):
    _kv(["SET", key, json.dumps(value), "EX", ttl])


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            settings = kv_get(SETTINGS_KEY) or {}
            self._respond(200, settings)
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return
        try:
            existing = kv_get(SETTINGS_KEY) or {}
            existing.update(body)           # merge — don't overwrite unrelated keys
            kv_set(SETTINGS_KEY, existing)
            self._respond(200, {"ok": True, "settings": existing})
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
