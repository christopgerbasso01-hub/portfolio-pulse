"""
Portfolio Pulse — Intraday Alert Engine
========================================
POST /api/intraday  (requires CRON_SECRET Bearer token)
Called every 30 min during market hours Mon–Fri by GitHub Actions.

Alerts fired (once per day each, deduplicated in KV):
  #12  Individual holding crash  > -5% from yesterday's close
  #13  Individual holding spike  > +5% from yesterday's close
  #23  Major index drop          > -2% (S&P 500, NASDAQ, TSX, Dow Jones)

Dedup key: notify:intraday:{YYYY-MM-DD}  (TTL 3 days)
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests
from datetime import datetime, timezone

try:
    from pywebpush import webpush, WebPushException
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
KV_URL        = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN      = os.environ.get("KV_REST_API_TOKEN", "")
CRON_SECRET   = os.environ.get("CRON_SECRET", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = "mailto:noreply@portfoliopulse.app"
MARKET_API    = "https://portfolio-pulse-dun.vercel.app/api/market"

CRASH_THRESHOLD  = -5.0    # % — individual holding
SPIKE_THRESHOLD  =  5.0    # %
INDEX_THRESHOLD  = -2.0    # % — major indices

INDICES = {
    "^GSPC":   "S&P 500",
    "^IXIC":   "NASDAQ",
    "^GSPTSE": "TSX",
    "^DJI":    "Dow Jones",
}

DEDUP_TTL = 3 * 86400   # 3 days


# ── KV helpers ────────────────────────────────────────────────────────────────

def _kv(cmd: list) -> dict:
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


def kv_set(key: str, value, ttl: int = None):
    if ttl:
        _kv(["SET", key, json.dumps(value), "EX", ttl])
    else:
        _kv(["SET", key, json.dumps(value)])


def get_subs() -> list:
    return kv_get("push:subs") or []


KEY_HISTORY = "notify:history"
MAX_HISTORY = 15

def _append_history(notifs: list, timestamp: str):
    try:
        history   = kv_get(KEY_HISTORY) or []
        new_items = [{"title": n["title"], "body": n.get("body", ""),
                      "tag": n.get("tag", ""), "timestamp": timestamp}
                     for n in notifs]
        kv_set(KEY_HISTORY, (new_items + history)[:MAX_HISTORY], ttl=365 * 86400)
    except Exception as exc:
        print(f"  [intraday] history write error: {exc}")


def get_fired(date_str: str) -> list:
    return kv_get(f"notify:intraday:{date_str}") or []


def mark_fired(date_str: str, fired: list):
    kv_set(f"notify:intraday:{date_str}", fired, ttl=DEDUP_TTL)


# ── Data fetchers ─────────────────────────────────────────────────────────────

def get_holdings_prices() -> dict:
    """Fetch current holdings prices from the market API."""
    r = requests.get(MARKET_API, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("holdings", {})


def get_index_change(ticker: str) -> float | None:
    """Fetch today's % change for a major index via Yahoo Finance."""
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&range=2d")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        curr = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("regularMarketPreviousClose")
        if curr and prev and prev != 0:
            return round((curr - prev) / prev * 100, 2)
    except Exception as exc:
        print(f"  [intraday] index fetch error {ticker}: {exc}")
    return None


# ── Push sender ───────────────────────────────────────────────────────────────

def send_push(sub: dict, payload: dict) -> bool:
    if not PUSH_AVAILABLE or not VAPID_PRIVATE:
        return False
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
        return True
    except WebPushException as exc:
        status = exc.response.status_code if exc.response else 0
        print(f"  [intraday] WebPushException {status}: {exc}")
        return False
    except Exception as exc:
        print(f"  [intraday] send error: {exc}")
        return False


def broadcast(payload: dict, subs: list) -> int:
    return sum(1 for s in subs if send_push(s, payload))


# ── Alert logic ───────────────────────────────────────────────────────────────

def check_alerts(today_str: str, holdings: dict, subs: list) -> list:
    """
    Check all intraday thresholds. Fire deduped alerts.
    Returns list of alert IDs that fired this call.
    """
    fired_today = get_fired(today_str)
    newly_fired = []
    sent_total  = 0

    # ── #12 & #13: Individual holdings ───────────────────────────────────────
    for ticker, data in holdings.items():
        pct = data.get("change_pct")
        if pct is None:
            continue
        price = data.get("price", 0)

        if pct <= CRASH_THRESHOLD:
            alert_id = f"crash_{ticker}"
            if alert_id not in fired_today:
                payload = {
                    "title": f"📉 {ticker} Crash",
                    "body":  f"{ticker} down {pct:+.1f}% today  ·  ${price:.2f}",
                    "tag":   f"stock-crash-{ticker}",
                }
                sent_total += broadcast(payload, subs)
                newly_fired.append(alert_id)
                print(f"  [intraday] crash alert: {ticker} {pct:+.1f}%")

        elif pct >= SPIKE_THRESHOLD:
            alert_id = f"spike_{ticker}"
            if alert_id not in fired_today:
                payload = {
                    "title": f"🚀 {ticker} Spiking!",
                    "body":  f"{ticker} up {pct:+.1f}% today  ·  ${price:.2f}",
                    "tag":   f"stock-spike-{ticker}",
                }
                sent_total += broadcast(payload, subs)
                newly_fired.append(alert_id)
                print(f"  [intraday] spike alert: {ticker} {pct:+.1f}%")

    # ── #23: Major indices ────────────────────────────────────────────────────
    alert_id = "index_drop"
    if alert_id not in fired_today:
        down_indices = []
        for ticker, name in INDICES.items():
            pct = get_index_change(ticker)
            if pct is not None and pct <= INDEX_THRESHOLD:
                down_indices.append(f"{name}: {pct:+.1f}%")

        if down_indices:
            payload = {
                "title": "⚠️ Market Selloff",
                "body":  "\n".join(down_indices),
                "tag":   "index-drop",
            }
            sent_total += broadcast(payload, subs)
            newly_fired.append(alert_id)
            print(f"  [intraday] index drop: {', '.join(down_indices)}")

    # Persist dedup list and history
    if newly_fired:
        mark_fired(today_str, fired_today + newly_fired)
        # Collect payloads that fired for history storage
        fired_payloads = []
        for fid in newly_fired:
            if fid.startswith("crash_"):
                t = fid[6:]
                d = holdings.get(t, {})
                fired_payloads.append({"title": f"📉 {t} Crash",
                    "body": f"{t} down {d.get('change_pct',0):+.1f}% today · ${d.get('price',0):.2f}",
                    "tag": f"stock-crash-{t}"})
            elif fid.startswith("spike_"):
                t = fid[6:]
                d = holdings.get(t, {})
                fired_payloads.append({"title": f"🚀 {t} Spiking!",
                    "body": f"{t} up {d.get('change_pct',0):+.1f}% today · ${d.get('price',0):.2f}",
                    "tag": f"stock-spike-{t}"})
            elif fid == "index_drop":
                fired_payloads.append({"title": "⚠️ Market Selloff",
                    "body": "Major index(es) down >2%", "tag": "index-drop"})
        if fired_payloads:
            _append_history(fired_payloads, datetime.now(timezone.utc).isoformat())

    return newly_fired


# ── Request handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if not self._auth():
            return
        try:
            now       = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")

            subs = get_subs()
            if not subs:
                self._respond(200, {"ok": True, "alerts": 0, "message": "No subscribers"})
                return

            holdings   = get_holdings_prices()
            fired_list = check_alerts(today_str, holdings, subs)

            self._respond(200, {
                "ok":     True,
                "date":   today_str,
                "alerts": len(fired_list),
                "fired":  fired_list,
            })

        except Exception as exc:
            print(f"  [intraday] POST error: {exc}")
            self._respond(500, {"error": str(exc)})

    def _auth(self) -> bool:
        if not CRON_SECRET:
            return True
        if self.headers.get("Authorization", "") != f"Bearer {CRON_SECRET}":
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
