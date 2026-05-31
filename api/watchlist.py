"""
Portfolio Pulse — Watchlist Manager
=====================================
GET    /api/watchlist          → {"tickers": ["AAPL","NVDA"]}
POST   /api/watchlist          → body: {"ticker": "AAPL"} → add ticker
DELETE /api/watchlist          → body: {"ticker": "AAPL"} → remove ticker
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests

KV_URL    = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN  = os.environ.get("KV_REST_API_TOKEN", "")
WATCH_KEY = "watchlist"
MAX_ITEMS = 20


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

def kv_get_list() -> list:
    try:
        raw = _kv(["GET", WATCH_KEY]).get("result")
        return json.loads(raw) if raw else []
    except Exception:
        return []

def kv_set_list(tickers: list):
    try:
        _kv(["SET", WATCH_KEY, json.dumps(tickers)])
    except Exception:
        pass


# ── Handler ───────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._respond(200, {"tickers": kv_get_list()})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            ticker = (body.get("ticker") or "").upper().strip()
            if not ticker:
                self._respond(400, {"error": "ticker required"})
                return
            tickers = kv_get_list()
            if ticker not in tickers:
                if len(tickers) >= MAX_ITEMS:
                    self._respond(400, {"error": f"Watchlist full ({MAX_ITEMS} max)"})
                    return
                tickers.append(ticker)
                kv_set_list(tickers)
            self._respond(200, {"tickers": tickers})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def do_DELETE(self):
        try:
            length  = int(self.headers.get("Content-Length", 0))
            body    = json.loads(self.rfile.read(length) or b"{}")
            ticker  = (body.get("ticker") or "").upper().strip()
            tickers = [t for t in kv_get_list() if t != ticker]
            kv_set_list(tickers)
            self._respond(200, {"tickers": tickers})
        except Exception as e:
            self._respond(500, {"error": str(e)})

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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
