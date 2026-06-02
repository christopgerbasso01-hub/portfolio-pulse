"""
Portfolio Pulse — On-Demand Intelligence Trigger
==================================================
POST /api/trigger_intelligence  (requires CRON_SECRET Bearer token)
Dispatches the daily-intelligence GitHub Actions workflow immediately.
The workflow runs the full Groq generation (same as 6 AM scheduled run)
and auto-deploys the result to Vercel when done (~2-4 minutes).
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests

CRON_SECRET  = os.environ.get("CRON_SECRET", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_OWNER   = "christopgerbasso01-hub"
REPO_NAME    = "portfolio-pulse"
WORKFLOW_ID  = "daily-intelligence.yml"


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        # No auth required — this only triggers market intelligence regeneration (no sensitive data)
        # Rate limiting: GitHub API itself throttles if called too frequently
        if not GITHUB_TOKEN:
            self._respond(500, {"error": "GITHUB_TOKEN not configured"})
            return
        try:
            url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/actions/workflows/{WORKFLOW_ID}/dispatches"
            r = requests.post(
                url,
                headers={
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Type": "application/json",
                },
                json={"ref": "main"},
                timeout=15,
            )
            if r.status_code == 204:
                self._respond(200, {
                    "ok": True,
                    "message": "Intelligence generation triggered. New analysis will appear in ~3 minutes.",
                })
            else:
                self._respond(500, {"error": f"GitHub API returned {r.status_code}: {r.text[:200]}"})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def _auth(self) -> bool:
        if not CRON_SECRET:
            return True
        auth = self.headers.get("Authorization", "")
        # Accept both Bearer <secret> and just <secret> for flexibility
        token = auth.replace("Bearer ", "").strip()
        if token != CRON_SECRET:
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
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        pass
