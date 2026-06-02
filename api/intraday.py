"""
Portfolio Pulse — Intraday Alert Engine
========================================
POST /api/intraday  (requires CRON_SECRET Bearer token)
Called every 10 min during market hours Mon–Fri by GitHub Actions.

Alerts fired as soon as threshold is crossed (deduped so they fire once):
  #12  Individual holding crash  > -5% from yesterday's close  (once/day/ticker)
  #13  Individual holding spike  > +5% from yesterday's close  (once/day/ticker)
  #23  Major index drop          > -2% (S&P 500, NASDAQ, TSX, Dow Jones) (once/day)
  #6   Dollar milestone          Portfolio crosses $275K, $300K … $1M (once/milestone)
  #7   ROI milestone             Portfolio ROI crosses 90%, 100%, 125% … (once/milestone)
  #9   Big down day              Portfolio daily change < -3% (once/day)
  #10  Big up day                Portfolio daily change > +3% (once/day)
  #11  Drawdown alert            Portfolio > -10% from all-time peak (once/day)

Dedup keys:
  notify:intraday:{YYYY-MM-DD}  — per-day alerts (TTL 3 days)
  notify:milestones             — dollar + ROI milestones fired (permanent)
  notify:peak                   — all-time high value (permanent)
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
BIG_DOWN         = -3.0    # % — total portfolio daily change
BIG_UP           =  3.0    # %
DRAWDOWN_PCT     = 10.0    # % below ATH

INDICES = {
    "^GSPC":   "S&P 500",
    "^IXIC":   "NASDAQ",
    "^GSPTSE": "TSX",
    "^DJI":    "Dow Jones",
}

DOLLAR_MILESTONES = [
    275_000, 300_000, 325_000, 350_000, 375_000, 400_000,
    425_000, 450_000, 500_000, 600_000, 700_000, 800_000,
    900_000, 1_000_000,
]
ROI_MILESTONES = [90, 95, 100, 110, 125, 150, 175, 200, 250, 300]

KEY_MILESTONES = "notify:milestones"
KEY_PEAK       = "notify:peak"

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

def get_market_data() -> dict:
    """Fetch full market API response (holdings + portfolio summary)."""
    r = requests.get(MARKET_API, timeout=20)
    r.raise_for_status()
    return r.json()


def get_holdings_prices() -> dict:
    return get_market_data().get("holdings", {})


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

def _send_push_checked(sub: dict, payload: dict) -> str:
    """Returns 'ok', 'stale', or 'error'."""
    if not PUSH_AVAILABLE or not VAPID_PRIVATE:
        return 'error'
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
        return 'ok'
    except WebPushException as exc:
        status = exc.response.status_code if exc.response else 0
        if status in (404, 410):
            print(f"  [intraday] stale subscription (HTTP {status}), will remove")
            return 'stale'
        print(f"  [intraday] WebPushException {status}: {exc}")
        return 'error'
    except Exception as exc:
        print(f"  [intraday] send error: {exc}")
        return 'error'


def broadcast_checked(payload: dict, subs: list) -> tuple[int, list]:
    """Returns (sent_count, stale_endpoints_list)."""
    stale_eps = []
    sent = 0
    for s in subs:
        result = _send_push_checked(s, payload)
        if result == 'ok':
            sent += 1
        elif result == 'stale':
            stale_eps.append(s.get("endpoint", ""))
    return sent, stale_eps


def remove_stale_subs(stale_endpoints: list):
    """Purge expired/gone subscriptions from KV."""
    if not stale_endpoints:
        return
    try:
        subs = get_subs()
        cleaned = [s for s in subs if s.get("endpoint") not in stale_endpoints]
        if len(cleaned) < len(subs):
            kv_set("push:subs", cleaned, ttl=400 * 86400)
            print(f"  [intraday] removed {len(subs)-len(cleaned)} stale subscription(s)")
    except Exception as exc:
        print(f"  [intraday] cleanup error: {exc}")


# ── Alert logic ───────────────────────────────────────────────────────────────

def check_alerts(today_str: str, holdings: dict, subs: list) -> list:
    """
    Check all intraday thresholds. Fire deduped alerts.
    Returns list of alert IDs that fired this call.
    """
    fired_today = get_fired(today_str)
    newly_fired = []
    all_stale   = []

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
                sent, stale = broadcast_checked(payload, subs)
                all_stale.extend(stale)
                newly_fired.append(alert_id)
                print(f"  [intraday] crash alert: {ticker} {pct:+.1f}% → sent {sent}")

        elif pct >= SPIKE_THRESHOLD:
            alert_id = f"spike_{ticker}"
            if alert_id not in fired_today:
                payload = {
                    "title": f"🚀 {ticker} Spiking!",
                    "body":  f"{ticker} up {pct:+.1f}% today  ·  ${price:.2f}",
                    "tag":   f"stock-spike-{ticker}",
                }
                sent, stale = broadcast_checked(payload, subs)
                all_stale.extend(stale)
                newly_fired.append(alert_id)
                print(f"  [intraday] spike alert: {ticker} {pct:+.1f}% → sent {sent}")

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
            sent, stale = broadcast_checked(payload, subs)
            all_stale.extend(stale)
            newly_fired.append(alert_id)
            print(f"  [intraday] index drop: {', '.join(down_indices)} → sent {sent}")

    # Clean up stale subscriptions
    if all_stale:
        remove_stale_subs(list(set(all_stale)))

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


def check_portfolio_alerts(today_str: str, portfolio: dict, subs: list) -> list:
    """
    Check portfolio-level real-time alerts:
      - Dollar milestones (#6)
      - ROI milestones   (#7)
      - Big down day     (#9)
      - Big up day       (#10)
      - Drawdown alert   (#11)
    Returns list of alert IDs fired.
    """
    total      = portfolio.get("total_value", 0)
    roi_pct    = portfolio.get("roi_pct", 0)
    day_pct    = portfolio.get("daily_change_pct", 0)
    day_cad    = portfolio.get("daily_change", 0)

    if not total:
        return []

    fired_today = get_fired(today_str)
    newly_fired = []
    all_stale   = []
    fired_payloads = []

    # ── #6: Dollar milestones ─────────────────────────────────────────────────
    milestones = kv_get(KEY_MILESTONES) or {"dollar": [], "roi": []}
    fired_d    = milestones.get("dollar", [])
    new_d      = [m for m in DOLLAR_MILESTONES if m <= total and m not in fired_d]
    for m in new_d:
        payload = {
            "title": f"💰 ${m//1000}K Milestone!",
            "body":  f"Portfolio just crossed ${m:,} CAD  ·  +{roi_pct:.1f}% all-time ROI",
            "tag":   f"dollar-milestone-{m}",
        }
        sent, stale = broadcast_checked(payload, subs)
        all_stale.extend(stale)
        newly_fired.append(f"dollar_{m}")
        fired_payloads.append(payload)
        print(f"  [intraday] dollar milestone ${m:,} → sent {sent}")
    if new_d:
        milestones["dollar"] = fired_d + new_d
        kv_set(KEY_MILESTONES, milestones)

    # ── #7: ROI milestones ────────────────────────────────────────────────────
    fired_r = milestones.get("roi", [])
    new_r   = [m for m in ROI_MILESTONES if roi_pct >= m and m not in fired_r]
    for m in new_r:
        payload = {
            "title": f"📊 {m}% ROI Milestone!",
            "body":  f"Portfolio ROI just hit {roi_pct:.1f}%  ·  Total value ${total:,.0f} CAD",
            "tag":   f"roi-milestone-{m}",
        }
        sent, stale = broadcast_checked(payload, subs)
        all_stale.extend(stale)
        newly_fired.append(f"roi_{m}")
        fired_payloads.append(payload)
        print(f"  [intraday] ROI milestone {m}% → sent {sent}")
    if new_r:
        milestones["roi"] = fired_r + new_r
        kv_set(KEY_MILESTONES, milestones)

    # ── #9: Big down day ──────────────────────────────────────────────────────
    if day_pct <= BIG_DOWN and "big_dn" not in fired_today:
        payload = {
            "title": "📉 Big Down Day",
            "body":  f"Portfolio down {day_pct:+.1f}% today  ·  {day_cad:+,.0f} CAD",
            "tag":   "big-down-day",
        }
        sent, stale = broadcast_checked(payload, subs)
        all_stale.extend(stale)
        newly_fired.append("big_dn")
        fired_payloads.append(payload)
        print(f"  [intraday] big down day {day_pct:+.1f}% → sent {sent}")

    # ── #10: Big up day ───────────────────────────────────────────────────────
    if day_pct >= BIG_UP and "big_up" not in fired_today:
        payload = {
            "title": "📈 Big Up Day!",
            "body":  f"Portfolio up {day_pct:+.1f}% today  ·  +{day_cad:+,.0f} CAD",
            "tag":   "big-up-day",
        }
        sent, stale = broadcast_checked(payload, subs)
        all_stale.extend(stale)
        newly_fired.append("big_up")
        fired_payloads.append(payload)
        print(f"  [intraday] big up day {day_pct:+.1f}% → sent {sent}")

    # ── #11: Drawdown alert ───────────────────────────────────────────────────
    if "drawdown" not in fired_today:
        peak_data  = kv_get(KEY_PEAK)
        peak_val   = peak_data.get("value", 0) if isinstance(peak_data, dict) else 0
        if peak_val > 0:
            drawdown = (total - peak_val) / peak_val * 100
            if drawdown <= -DRAWDOWN_PCT:
                payload = {
                    "title": "⚠️ Portfolio Drawdown",
                    "body":  f"Portfolio is {drawdown:.1f}% below its peak of ${peak_val:,.0f}  ·  Current ${total:,.0f}",
                    "tag":   "drawdown-alert",
                }
                sent, stale = broadcast_checked(payload, subs)
                all_stale.extend(stale)
                newly_fired.append("drawdown")
                fired_payloads.append(payload)
                print(f"  [intraday] drawdown {drawdown:.1f}% → sent {sent}")
        # Update peak if new ATH
        if total > peak_val:
            kv_set(KEY_PEAK, {"value": total, "date": today_str})

    # Persist + cleanup
    if all_stale:
        remove_stale_subs(list(set(all_stale)))
    if newly_fired:
        mark_fired(today_str, fired_today + newly_fired)
    if fired_payloads:
        _append_history(fired_payloads, datetime.now(timezone.utc).isoformat())

    return newly_fired


# ── Request handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        """Send a test push notification to all subscribers (auth required)."""
        if not self._auth():
            return
        try:
            subs = get_subs()
            if not subs:
                self._respond(200, {"ok": True, "message": "No subscribers"})
                return
            payload = {
                "title": "🔔 Portfolio Pulse Test",
                "body":  "Push notifications are working correctly!",
                "tag":   "test-notification",
            }
            sent, stale = broadcast_checked(payload, subs)
            if stale:
                remove_stale_subs(list(set(stale)))
            self._respond(200, {"ok": True, "sent": sent, "stale_removed": len(set(stale))})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

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

            market_data = get_market_data()
            holdings    = market_data.get("holdings", {})
            portfolio   = market_data.get("portfolio", {})

            fired_holdings  = check_alerts(today_str, holdings, subs)
            fired_portfolio = check_portfolio_alerts(today_str, portfolio, subs)
            fired_list      = fired_holdings + fired_portfolio

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
