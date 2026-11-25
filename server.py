#!/usr/bin/env python3
"""
Portfolio Pulse — one small web server for hosts that are not Vercel (Render, Koyeb, a Raspberry Pi…).

It does what vercel.json does: serves the static site from this folder and runs each
api/*.py function at /api/<name>. The API modules are used unchanged — each defines a
`handler(BaseHTTPRequestHandler)` class, exactly as Vercel expects.

    python3 server.py            # listens on $PORT (default 8000)

Secrets come from environment variables (see render.yaml), never from files in the repo.
"""
import importlib.util
import json
import mimetypes
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8000"))

# ── API routes: the same table vercel.json uses ───────────────────────────────
def _load_routes() -> dict:
    routes = {}
    try:
        cfg = json.loads((ROOT / "vercel.json").read_text())
        for r in cfg.get("routes", []):
            src, dest = r.get("src", ""), r.get("dest", "")
            if src.startswith("/api/") and dest.startswith("/api/") and dest.endswith(".py"):
                routes[src] = dest[len("/api/"):-3]
    except Exception as exc:          # pragma: no cover - fall back to file names
        print("vercel.json not readable, routing by file name:", exc, file=sys.stderr)
    for f in (ROOT / "api").glob("*.py"):   # /api/<file> works even without a route entry
        routes.setdefault(f"/api/{f.stem}", f.stem)
    return routes


ROUTES = _load_routes()
_modules = {}


def _api_handler_class(name: str):
    """Import api/<name>.py once and return its `handler` class."""
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"pp_api_{name}", ROOT / "api" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _modules[name] = mod.handler
    return _modules[name]


# ── Static files ──────────────────────────────────────────────────────────────
# Everything Vercel would serve, minus code, tests, secrets and backups.
_BLOCKED_DIRS = {"api", "scripts", "tests", "backups", ".git", ".github", ".vercel", ".claude",
                 "node_modules", "voice-samples", "__pycache__"}
_BLOCKED_SUFFIX = {".py", ".md", ".yml", ".yaml", ".env", ".local", ".test", ".xlsx", ".csv", ".txt"}
_NO_CACHE = {"index.html", "sw.js", "manifest.json"}
_BLOCKED_NAMES = {"vercel.json", "render.yaml", "requirements.txt", "package.json"}


class Static(BaseHTTPRequestHandler):
    server_version = "PortfolioPulse"
    # One request per connection (like the API handlers): the router chooses the handler from the
    # FIRST request line, so a kept-alive connection would send later /api calls to the static handler.
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _resolve(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/healthz":
            return "health"
        if path.endswith("/"):
            path += "index.html"
        parts = [p for p in path.split("/") if p]
        if any(p in (".", "..") or p.startswith(".") and p != ".nojekyll" for p in parts):
            return None
        if parts and parts[0] in _BLOCKED_DIRS:
            return None
        f = (ROOT / "/".join(parts)).resolve() if parts else ROOT / "index.html"
        try:
            f.relative_to(ROOT)
        except ValueError:
            return None
        if f.name in _BLOCKED_NAMES:
            return None
        if f.name.startswith(".env") or f.suffix in _BLOCKED_SUFFIX or not f.is_file():
            # the app's own data files in data/ are the one exception to the suffix rule
            if not (f.is_file() and parts and parts[0] == "data" and f.suffix in {".json", ".txt", ".mp3"}):
                return None
        return f

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None, head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _serve(self, head=False):
        f = self._resolve()
        if f == "health":
            return self._send(200, b"ok", head=head)
        if f is None:
            return self._send(404, b"Not found", head=head)
        size = f.stat().st_size
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        cache = "no-cache" if f.name in _NO_CACHE else "public, max-age=300"
        hdr = {"Cache-Control": cache, "Accept-Ranges": "bytes"}
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")          # audio players seek with Range requests
        if rng and rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else max(0, size - int(b))
                end = int(b) if (a and b) else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"}, head=head)
                code = 206
                hdr["Content-Range"] = f"bytes {start}-{end}/{size}"
            except ValueError:
                start, end, code = 0, size - 1, 200
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        for k, v in hdr.items():
            self.send_header(k, v)
        self.end_headers()
        if head:
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve(head=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def finish_request(self, request, client_address):
        """Pick the handler class from the request line, then let it run the request."""
        try:
            request.settimeout(30)
            first = request.recv(4096, socket.MSG_PEEK).split(b"\r\n", 1)[0].decode("latin-1")
            target = first.split(" ")[1] if first.count(" ") >= 2 else "/"
        except Exception:
            target = "/"
        route = urlsplit(target).path.rstrip("/") if target.startswith("/api/") else None
        cls = Static
        if route is not None:
            name = ROUTES.get(route)
            if name:
                try:
                    cls = _api_handler_class(name)
                except Exception as exc:
                    print(f"[server] cannot load api/{name}: {exc}", file=sys.stderr)
                    cls = None
            else:
                cls = None
            if cls is None:
                body = b'{"error":"no such api"}'
                request.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n"
                                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                return
        cls(request, client_address, self)


if __name__ == "__main__":
    print(f"Portfolio Pulse listening on :{PORT} — {len(ROUTES)} API routes", flush=True)
    Server(("0.0.0.0", PORT), Static).serve_forever()
