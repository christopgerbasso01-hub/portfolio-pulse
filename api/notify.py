"""
Portfolio Pulse — End-of-Day Notification Engine
=================================================
POST /api/notify  (requires CRON_SECRET Bearer token)
Called by GitHub Actions ~90 min after market close Mon–Fri.

Notifications fired:
  #1  Daily close summary (always)          — P&L + best/worst mover
  #2  Weekly summary (Fridays)              — Mon→Fri performance
  #3  Monthly summary (1st of month)        — prev-month P&L + YTD ROI
  #5  New all-time high                     — first time total crosses prior peak
  #6  Dollar milestones ($275K, $300K …)    — fires once per milestone
  #7  ROI milestones (90%, 100%, 125% …)    — fires once per milestone
  #8  Best single-day gain record           — new personal-best daily gain
  #9  Big down day  (< -3%)
  #10 Big up day    (> +3%)
  #11 Drawdown 10% from peak
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import requests
from datetime import datetime, timezone, timedelta

try:
    from pywebpush import webpush, WebPushException
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False
    print("  [notify] pywebpush not installed — push disabled")

# ── Config ────────────────────────────────────────────────────────────────────
KV_URL        = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN      = os.environ.get("KV_REST_API_TOKEN", "")
CRON_SECRET   = os.environ.get("CRON_SECRET", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = "mailto:noreply@portfoliopulse.app"

# Thresholds
DOWN_THRESHOLD  = -3.0    # % — triggers "big down day"
UP_THRESHOLD    =  3.0    # % — triggers "big up day"
DRAWDOWN_PCT    = 10.0    # % below ATH — triggers drawdown alert

# KV state keys (no TTL — permanent records)
KEY_PEAK       = "notify:peak"        # {"value": float, "date": str}
KEY_RECORD     = "notify:record"      # {"gain": float, "pct": float, "date": str}
KEY_MILESTONES = "notify:milestones"  # {"dollar": [int,...], "roi": [float,...]}

DOLLAR_MILESTONES = [
    225_000, 250_000, 275_000, 300_000, 325_000, 350_000,
    375_000, 400_000, 425_000, 450_000, 500_000, 600_000,
    700_000, 800_000, 900_000, 1_000_000,
]
ROI_MILESTONES = [50, 60, 70, 75, 80, 85, 90, 95, 100, 110, 125, 150, 175, 200, 250, 300]


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
    """Set a KV value. Pass ttl=None for permanent (no expiry)."""
    if ttl:
        _kv(["SET", key, json.dumps(value), "EX", ttl])
    else:
        _kv(["SET", key, json.dumps(value)])


def get_subs() -> list:
    return kv_get("push:subs") or []


KEY_HISTORY  = "notify:history"
MAX_HISTORY  = 15

def _append_history(notifs: list, timestamp: str):
    """Prepend fired notifications to the shared history list (max 15)."""
    try:
        history   = kv_get(KEY_HISTORY) or []
        new_items = [{"title": n["title"], "body": n.get("body", ""),
                      "tag": n.get("tag", ""), "timestamp": timestamp}
                     for n in notifs]
        kv_set(KEY_HISTORY, (new_items + history)[:MAX_HISTORY], ttl=365 * 86400)
    except Exception as exc:
        print(f"  [notify] history write error: {exc}")


def get_snapshot(date_str: str):
    return kv_get(f"snapshot:{date_str}")


# ── Formatting ────────────────────────────────────────────────────────────────

def _fmt(v: float) -> str:
    """Compact signed CAD value: +$1.2K or -$850"""
    sign  = "+" if v >= 0 else "-"
    abs_v = abs(v)
    if abs_v >= 1000:
        return f"{sign}${abs_v / 1000:.1f}K"
    return f"{sign}${abs_v:.0f}"


# ── Notification builders ─────────────────────────────────────────────────────

def _best_worst(holdings_prices: dict):
    """Return (best_str, worst_str) from holdings price dict."""
    valid = {t: d for t, d in holdings_prices.items() if d.get("change_pct") is not None}
    if not valid:
        return "—", "—"
    best  = max(valid.items(), key=lambda x: x[1]["change_pct"])
    worst = min(valid.items(), key=lambda x: x[1]["change_pct"])
    return (
        f"{best[0]} {best[1]['change_pct']:+.1f}%",
        f"{worst[0]} {worst[1]['change_pct']:+.1f}%",
    )


def build_notifications(snap: dict, today: datetime, week_snap, prev_month_snap) -> list:
    """
    Check all end-of-day conditions and return a list of notification payloads.
    Each payload: {"title": str, "body": str, "tag": str}
    """
    total     = snap.get("total_value")  or 0
    daily     = snap.get("daily_change") or 0
    daily_pct = snap.get("daily_change_pct") or 0
    roi_pct   = snap.get("roi_pct")      or 0
    today_str = today.strftime("%Y-%m-%d")
    is_friday = today.weekday() == 4
    is_first  = today.day == 1

    holdings_prices = snap.get("holdings_prices", {})
    best_str, worst_str = _best_worst(holdings_prices)
    arrow = "↑" if daily >= 0 else "↓"

    # Load / initialise persistent state
    peak       = kv_get(KEY_PEAK)
    record     = kv_get(KEY_RECORD)
    milestones = kv_get(KEY_MILESTONES) or {"dollar": [], "roi": []}
    first_run  = (peak is None)

    if first_run:
        # Seed state silently — don't fire special alerts on first run
        kv_set(KEY_PEAK, {"value": total, "date": today_str})
        kv_set(KEY_RECORD, {"gain": daily, "pct": daily_pct, "date": today_str})
        milestones["dollar"] = [m for m in DOLLAR_MILESTONES if m <= total]
        milestones["roi"]    = [m for m in ROI_MILESTONES    if m <= roi_pct]
        kv_set(KEY_MILESTONES, milestones)

    peak_val   = (peak or {}).get("value", total)
    record_gain = (record or {}).get("gain", 0)

    notifs = []

    # ── #1 Daily close + #4 best/worst mover (always) ────────────────────────
    notifs.append({
        "title": "Portfolio Pulse 📊",
        "body":  f"{arrow} {_fmt(daily)} ({daily_pct:+.2f}%)  ·  ${total/1000:.1f}K\n🏆 {best_str}  ·  💔 {worst_str}",
        "tag":   "portfolio-daily",
    })

    # ── #2 Weekly summary (Fridays only) ─────────────────────────────────────
    if is_friday and week_snap:
        ws    = week_snap.get("total_value") or total
        wgain = total - ws
        wpct  = (wgain / ws * 100) if ws else 0
        notifs.append({
            "title": "📅 Weekly Summary",
            "body":  f"This week: {_fmt(wgain)} ({wpct:+.2f}%)  ·  ${total/1000:.1f}K",
            "tag":   "portfolio-weekly",
        })

    # ── #3 Monthly summary (1st of month) ────────────────────────────────────
    if is_first and prev_month_snap:
        pm    = prev_month_snap.get("total_value") or total
        mgain = total - pm
        mpct  = (mgain / pm * 100) if pm else 0
        prev_month_name = (today - timedelta(days=1)).strftime("%B")
        notifs.append({
            "title": f"📆 {prev_month_name} Summary",
            "body":  f"{prev_month_name}: {_fmt(mgain)} ({mpct:+.2f}%)  ·  ROI {roi_pct:+.1f}%",
            "tag":   "portfolio-monthly",
        })

    if first_run:
        return notifs   # Don't check special conditions on first run

    # ── #5 New all-time high ──────────────────────────────────────────────────
    if total > peak_val:
        notifs.append({
            "title": "🎉 New All-Time High!",
            "body":  f"Portfolio hit ${total:,.0f}",
            "tag":   "portfolio-ath",
        })
        kv_set(KEY_PEAK, {"value": total, "date": today_str})
        peak_val = total     # Use updated peak for drawdown check below

    # ── #6 Dollar milestones ─────────────────────────────────────────────────
    fired_d    = milestones.get("dollar", [])
    new_d      = [m for m in DOLLAR_MILESTONES if m <= total and m not in fired_d]
    if new_d:
        m = max(new_d)
        notifs.append({
            "title": f"🏁 ${m // 1000}K Milestone!",
            "body":  f"Portfolio crossed ${m:,}",
            "tag":   "portfolio-dollar-milestone",
        })
        milestones["dollar"] = fired_d + new_d
        kv_set(KEY_MILESTONES, milestones)

    # ── #7 ROI milestones ────────────────────────────────────────────────────
    fired_r   = milestones.get("roi", [])
    new_r     = [m for m in ROI_MILESTONES if m <= roi_pct and m not in fired_r]
    if new_r:
        m = max(new_r)
        notifs.append({
            "title": f"📈 {m}% ROI Milestone!",
            "body":  f"Total return reached {roi_pct:.1f}%",
            "tag":   "portfolio-roi-milestone",
        })
        milestones["roi"] = fired_r + new_r
        kv_set(KEY_MILESTONES, milestones)

    # ── #8 Best single-day gain record ───────────────────────────────────────
    if daily > 0 and daily > record_gain:
        notifs.append({
            "title": "🚀 Best Day Ever!",
            "body":  f"{_fmt(daily)} ({daily_pct:+.2f}%) today  ·  Previous record: {_fmt(record_gain)}",
            "tag":   "portfolio-best-day",
        })
        kv_set(KEY_RECORD, {"gain": daily, "pct": daily_pct, "date": today_str})

    # ── #9 Big down day ───────────────────────────────────────────────────────
    if daily_pct < DOWN_THRESHOLD:
        notifs.append({
            "title": "📉 Big Down Day",
            "body":  f"{_fmt(daily)} ({daily_pct:+.2f}%) today  ·  ${total/1000:.1f}K",
            "tag":   "portfolio-down-day",
        })

    # ── #10 Big up day ───────────────────────────────────────────────────────
    if daily_pct > UP_THRESHOLD:
        notifs.append({
            "title": "🚀 Big Up Day!",
            "body":  f"{_fmt(daily)} ({daily_pct:+.2f}%) today  ·  ${total/1000:.1f}K",
            "tag":   "portfolio-up-day",
        })

    # ── #11 Drawdown from peak ───────────────────────────────────────────────
    if peak_val > 0:
        dd_pct = (total - peak_val) / peak_val * 100
        if dd_pct <= -DRAWDOWN_PCT:
            notifs.append({
                "title": "⚠️ Portfolio Drawdown",
                "body":  f"Down {dd_pct:.1f}% from ${peak_val/1000:.1f}K peak  ·  Now ${total/1000:.1f}K",
                "tag":   "portfolio-drawdown",
            })

    return notifs


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
        print(f"  [notify] WebPushException {status}: {exc}")
        return False
    except Exception as exc:
        print(f"  [notify] send error: {exc}")
        return False


def broadcast(notifs: list, subs: list) -> dict:
    """Send each notification to every subscriber."""
    sent = failed = 0
    for notif in notifs:
        for sub in subs:
            if send_push(sub, notif):
                sent += 1
            else:
                failed += 1
    return {"sent": sent, "failed": failed, "notifications": len(notifs), "subs": len(subs)}


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
            # ── Podcast-ready notification ────────────────────────────────────
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}") if length else {}

            if body.get("type") == "test":
                notif = {
                    "title": body.get("title", "🔔 Portfolio Pulse Test"),
                    "body":  body.get("body", "Push notifications are working correctly!"),
                    "tag":   "test-notification",
                }
                subs = get_subs()
                now  = datetime.now(timezone.utc)
                results = broadcast([notif], subs)
                print(f"  [notify] test notification — sent={results.get('sent',0)}")
                self._respond(200, {"ok": True, "type": "test", **results})
                return

            if body.get("type") == "podcast":
                ep_num = body.get("episode", "?")
                title  = body.get("title", f"Episode #{ep_num}")
                notif  = {
                    "title": "Portfolio Pulse 🎙️",
                    "body":  f"Ep #{ep_num} ready: {title}",
                    "tag":   "podcast-ready",
                }
                subs = get_subs()
                now  = datetime.now(timezone.utc)
                _append_history([notif], now.isoformat())
                results = broadcast([notif], subs)
                print(f"  [notify] podcast ep#{ep_num} — sent={results['sent']}")
                self._respond(200, {"ok": True, "type": "podcast", **results})
                return

            now       = datetime.now(timezone.utc)
            today_str = now.strftime("%Y-%m-%d")

            snap = get_snapshot(today_str)
            if not snap:
                self._respond(404, {"error": f"No snapshot for {today_str}"})
                return

            subs = get_subs()
            if not subs:
                self._respond(200, {"ok": True, "sent": 0, "message": "No subscribers"})
                return

            # ── Weekly: find Monday's snapshot ────────────────────────────────
            week_snap = None
            if now.weekday() == 4:   # Friday
                for days_back in range(4, 8):
                    d = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
                    ws = get_snapshot(d)
                    if ws:
                        week_snap = ws
                        break

            # ── Monthly: find a snapshot from ~30 days ago ────────────────────
            prev_month_snap = None
            if now.day == 1:
                for days_back in range(28, 36):
                    d = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
                    pm = get_snapshot(d)
                    if pm:
                        prev_month_snap = pm
                        break

            notifs  = build_notifications(snap, now, week_snap, prev_month_snap)
            results = broadcast(notifs, subs)
            _append_history(notifs, now.isoformat())

            print(f"  [notify] {today_str} — {len(notifs)} notif(s), "
                  f"sent={results['sent']} failed={results['failed']}")

            self._respond(200, {"ok": True, "date": today_str, **results})

        except Exception as exc:
            print(f"  [notify] POST error: {exc}")
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
