"""
Portfolio Pulse — Podcast Listened State
Backed by Vercel KV (Upstash Redis REST API).

GET  /api/pod_listened          → { "listened": [1, 2, 3] }
POST /api/pod_listened          → body: { "listened": [1, 2, 3] }
                                → saves list, returns { "listened": [1, 2, 3] }

Environment variables (auto-injected by Vercel when Upstash for Redis is connected):
  UPSTASH_REDIS_REST_URL    — Upstash Redis REST endpoint
  UPSTASH_REDIS_REST_TOKEN  — Upstash Redis bearer token
"""
import json
import os
import requests
from http.server import BaseHTTPRequestHandler

KV_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "")
KV_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
KV_KEY   = "pod_listened"


def kv_get() -> list:
    """Read the listened episode list from KV. Returns [] on any error."""
    if not KV_URL or not KV_TOKEN:
        return []
    try:
        r = requests.post(
            KV_URL,
            headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
            json=["GET", KV_KEY],
            timeout=5,
        )
        raw = r.json().get("result")
        return json.loads(raw) if raw else []
    except Exception:
        return []


def kv_set(listened: list) -> bool:
    """Write the listened episode list to KV. Returns True on success."""
    if not KV_URL or not KV_TOKEN:
        return False
    try:
        r = requests.post(
            KV_URL,
            headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
            json=["SET", KV_KEY, json.dumps(listened)],
            timeout=5,
        )
        return r.ok
    except Exception:
        return False


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        listened = kv_get()
        self._json(200, {"listened": listened})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            return self._error(400, "Invalid JSON body")

        listened = body.get("listened")
        if not isinstance(listened, list):
            return self._error(400, "listened must be an array")

        # Sanitise: keep only positive integers
        listened = sorted({int(ep) for ep in listened if isinstance(ep, (int, float)) and ep > 0})

        kv_set(listened)
        self._json(200, {"listened": listened})

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
