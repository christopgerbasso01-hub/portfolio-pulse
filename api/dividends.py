"""
Portfolio Pulse — Dividend Autopilot
====================================
GET  /api/dividends          — pending dividends awaiting confirmation
POST /api/dividends          — resolve one entry, or run a scan

Resolve (no auth, same posture as /api/settings):
  { "id": "...", "action": "confirm" }                      accept as detected
  { "id": "...", "action": "adjust", "actual_amount": 9.9 }  accept a corrected amount
  { "id": "...", "action": "dismiss" }                       not received — drop it

Scan (requires Bearer CRON_SECRET), called by the Dividend Autopilot workflow:
  { "action": "scan" }             detect, queue, notify
  { "action": "scan", "dry_run": true }   detect and report only

Detection runs here rather than in GitHub Actions because KV, FMP, and VAPID
credentials live in Vercel only.

NOTHING is ever booked automatically. Detected dividends land in a pending
queue; the dashboard confirms them through the normal DIVIDEND transaction path
so there is exactly one booking code path. Adjusting records the true amount,
and once a (ticker, account) pair has 2+ samples the learned effective rate
replaces the statutory assumption — which is how holdings with unpredictable
withholding (MLPs, ADR fees, broker FX spreads) converge on accuracy.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta

import requests

try:
    from pywebpush import webpush
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False

KV_URL        = os.environ.get("KV_REST_API_URL", "")
KV_TOKEN      = os.environ.get("KV_REST_API_TOKEN", "")
FMP_API_KEY   = os.environ.get("FMP_API_KEY", "")
CRON_SECRET   = os.environ.get("CRON_SECRET", "")
VAPID_PRIVATE = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = "mailto:noreply@portfoliopulse.app"

SETTINGS_KEY  = "user:settings"
AUTOPILOT_KEY = "dividend:autopilot"
SUBS_KEY      = "push:subs"          # must match api/push.py
STATE_TTL     = 400 * 86400
MAX_SAMPLES   = 8                     # rolling window for the learned rate

# Instruments that never pay a distribution — never scanned.
# FNGU is an ETN; GBTC is a non-distributing trust.
NO_DIVIDEND = {"FNGU", "GBTC"}

# Statutory withholding, used only until a learned rate exists for a pair.
# RRSP is exempt from US withholding on US-listed securities under the
# Canada-US treaty; TFSA and FHSA get no such protection.
US_TREATY_EXEMPT_ACCOUNTS = {"RRSP"}
RATE_US_DEFAULT = 0.15
RATE_BY_TICKER  = {
    "TSM":  0.21,   # Taiwan statutory
    "SHEL": 0.00,   # UK withholds nothing; ADR fees are picked up by learning
    "ET":   0.37,   # MLP: IRC s.1446 on effectively-connected income. Highly
                    # variable — LOW confidence until the learned rate takes over.
}
LOW_CONFIDENCE_TICKERS = {"ET"}

# Used only to judge "has it been paid yet" when FMP has no payment date
# (weaker coverage on .TO listings). Flagged in the payload when applied.
ASSUMED_PAY_LAG_DAYS = 21

# A single distribution worth more than this share of the position is almost
# certainly bad data — a split misread as a dividend, or a units error.
ANOMALY_PCT_OF_POSITION = 0.05


# ── KV ────────────────────────────────────────────────────────────────────────

def _kv(cmd: list) -> dict:
    r = requests.post(
        KV_URL,
        headers={"Authorization": f"Bearer {KV_TOKEN}", "Content-Type": "application/json"},
        json=cmd, timeout=10,
    )
    r.raise_for_status()
    return r.json()


def kv_get(key: str):
    raw = _kv(["GET", key]).get("result")
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def kv_set(key: str, value, ttl: int = STATE_TTL):
    _kv(["SET", key, json.dumps(value), "EX", ttl])


# ── Market data ───────────────────────────────────────────────────────────────

def fetch_dividend_events(ticker: str) -> list:
    """Ex-dates + per-share amounts from Yahoo. Returns [{ex_date, amount, ccy}]."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
    try:
        r = requests.get(
            url,
            params={"interval": "1d", "range": "1y", "events": "div"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        r.raise_for_status()
        result = r.json()["chart"]["result"][0]
    except Exception:
        return []

    ccy  = result.get("meta", {}).get("currency", "USD")
    divs = (result.get("events") or {}).get("dividends") or {}
    out  = []
    for d in divs.values():
        try:
            ex = datetime.fromtimestamp(d["date"], tz=timezone.utc).strftime("%Y-%m-%d")
            out.append({"ex_date": ex, "amount": float(d["amount"]), "ccy": ccy})
        except Exception:
            continue
    return sorted(out, key=lambda x: x["ex_date"])


def fetch_payment_dates(ticker: str) -> dict:
    """{ex_date: payment_date} from FMP. Empty dict when unavailable."""
    if not FMP_API_KEY:
        return {}
    url = f"https://financialmodelingprep.com/api/v3/historical-price-full/stock_dividend/{ticker}"
    try:
        r = requests.get(url, params={"apikey": FMP_API_KEY}, timeout=12)
        r.raise_for_status()
        rows = r.json().get("historical", []) or []
    except Exception:
        return {}
    return {row["date"]: row["paymentDate"]
            for row in rows if row.get("date") and row.get("paymentDate")}


# ── Pure logic (unit-tested in tests/test_dividend_autopilot.py) ──────────────

def shares_at_ex_date(ticker: str, account: str, ex_date: str,
                      current_shares: float, txs: list) -> tuple:
    """Shares held at the close before ex_date.

    current_shares already reflects every transaction, so walk back the ones
    dated on or after the ex-date. A purchase settling ON the ex-date does not
    qualify for the dividend, hence >= rather than >.

    Returns (shares, saw_split); a split in the window makes the count
    unreliable, so the caller drops confidence.
    """
    shares, saw_split = current_shares, False
    for tx in txs:
        if tx.get("ticker") != ticker or tx.get("account") != account:
            continue
        d = tx.get("date") or ""
        if d < ex_date:
            continue                       # already baked into current_shares
        t = tx.get("type")
        if t == "BUY":
            shares -= float(tx.get("shares") or 0)
        elif t == "SELL":
            shares += float(tx.get("shares") or 0)
        elif t == "SPLIT":
            saw_split = True
    return max(shares, 0.0), saw_split


def resolve_rate(ticker: str, account: str, learned: dict) -> tuple:
    """Return (withholding_rate, basis). A learned rate wins once it has samples."""
    entry = (learned or {}).get(f"{ticker}|{account}") or {}
    if entry.get("samples", 0) >= 2 and entry.get("effective_net") is not None:
        return 1.0 - float(entry["effective_net"]), "learned"

    if ticker.endswith(".TO"):
        return 0.0, "statutory"            # Canadian-listed into a Canadian account
    if ticker in RATE_BY_TICKER:
        return RATE_BY_TICKER[ticker], "statutory"
    if account in US_TREATY_EXEMPT_ACCOUNTS:
        return 0.0, "statutory"            # treaty exemption, US-listed in RRSP
    return RATE_US_DEFAULT, "statutory"


def score_confidence(ticker: str, basis: str, saw_split: bool, estimated_pay: bool) -> str:
    if saw_split:
        return "LOW"
    if ticker in LOW_CONFIDENCE_TICKERS and basis != "learned":
        return "LOW"
    if basis == "learned":
        return "HIGH"
    if ticker.endswith(".TO"):
        return "HIGH"
    return "MEDIUM" if not estimated_pay else "LOW"


def record_learned_rate(rates: dict, entry: dict, actual: float) -> dict:
    """Fold the realised net/gross ratio into the rolling average for this pair."""
    gross = float(entry.get("gross_amount") or 0)
    if gross <= 0 or actual <= 0:
        return rates
    ratio = actual / gross
    if not (0.0 < ratio <= 1.0):           # nonsense input — ignore rather than poison
        return rates

    key = f"{entry.get('ticker')}|{entry.get('account')}"
    cur = rates.get(key) or {"effective_net": None, "samples": 0}
    n   = min(int(cur.get("samples") or 0), MAX_SAMPLES)
    avg = ratio if (n == 0 or cur.get("effective_net") is None) \
        else (float(cur["effective_net"]) * n + ratio) / (n + 1)
    rates[key] = {"effective_net": round(avg, 6), "samples": n + 1}
    return rates


# ── Detection ─────────────────────────────────────────────────────────────────

def detect(settings: dict, state: dict) -> list:
    """Return newly detected, not-yet-queued dividends."""
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    holdings = settings.get("computed_holdings") or []
    txs      = settings.get("transactions") or []
    seen     = state.get("seen") or {}
    learned  = state.get("rates") or {}
    queued   = {p.get("id") for p in (state.get("pending") or [])}

    positions = [h for h in holdings
                 if h.get("ticker") and h["ticker"] not in NO_DIVIDEND
                 and (h.get("shares") or 0) > 0]

    div_cache, pay_cache, found = {}, {}, []

    for h in positions:
        ticker, account = h["ticker"], h.get("account", "")
        if ticker not in div_cache:
            div_cache[ticker] = fetch_dividend_events(ticker)
            pay_cache[ticker] = fetch_payment_dates(ticker)

        for ev in div_cache[ticker]:
            ex_date = ev["ex_date"]
            key = f"{ticker}|{account}|{ex_date}"
            if key in seen or key in queued:
                continue

            pay_date = pay_cache[ticker].get(ex_date)
            estimated_pay = pay_date is None
            if estimated_pay:
                pay_date = (datetime.strptime(ex_date, "%Y-%m-%d")
                            + timedelta(days=ASSUMED_PAY_LAG_DAYS)).strftime("%Y-%m-%d")
            if pay_date > today:
                continue                              # not paid out yet

            shares, saw_split = shares_at_ex_date(
                ticker, account, ex_date, float(h.get("shares") or 0), txs)
            if shares <= 0:
                continue                              # not held on the ex-date

            rate, basis = resolve_rate(ticker, account, learned)
            gross = shares * ev["amount"]
            net   = round(gross * (1.0 - rate), 2)
            if net <= 0:
                continue

            cost = abs(float(h.get("cost_total") or 0))
            anomaly = cost > 0 and net > cost * ANOMALY_PCT_OF_POSITION
            confidence = "LOW" if anomaly else score_confidence(
                ticker, basis, saw_split, estimated_pay)

            found.append({
                "id": key, "ticker": ticker, "account": account,
                "ex_date": ex_date, "pay_date": pay_date,
                "shares": round(shares, 4), "per_share": ev["amount"], "ccy": ev["ccy"],
                "gross_amount": round(gross, 2), "withholding": round(rate, 4),
                "net_amount": net, "rate_basis": basis, "confidence": confidence,
                "estimated_pay": estimated_pay, "anomaly": anomaly,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })
            queued.add(key)
    return found


# ── Notification ──────────────────────────────────────────────────────────────

def notify(entry: dict, subs: list) -> int:
    if not PUSH_AVAILABLE or not VAPID_PRIVATE:
        return 0
    sym  = "$" if entry["ccy"] == "USD" else "C$"
    note = "estimate — confirm amount" if entry["confidence"] == "LOW" else "tap to confirm"
    payload = {
        "title": f"Dividend — {entry['ticker']}",
        "body":  (f"{entry['account']} · {sym}{entry['net_amount']:,.2f} {entry['ccy']}\n"
                  f"{entry['shares']:g} sh × {sym}{entry['per_share']:.4f} · {note}"),
        "tag":   f"dividend-{entry['id']}",
        "data":  {"type": "dividend_pending", "id": entry["id"]},
    }
    sent = 0
    for sub in subs:
        try:
            webpush(subscription_info=sub, data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE,
                    vapid_claims={"sub": VAPID_SUBJECT})
            sent += 1
        except Exception:
            pass
    return sent


# ── Handler ───────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            state = kv_get(AUTOPILOT_KEY) or {}
            self._respond(200, {"pending": state.get("pending") or [],
                                "rates":   state.get("rates") or {}})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return

        action = body.get("action")
        if action == "scan":
            self._handle_scan(body)
            return
        if action in ("confirm", "adjust", "dismiss"):
            self._handle_resolve(body, action)
            return
        self._respond(400, {"error": "action must be scan|confirm|adjust|dismiss"})

    # ── scan (cron-authenticated) ──
    def _handle_scan(self, body: dict):
        auth = self.headers.get("Authorization", "")
        if not CRON_SECRET or auth != f"Bearer {CRON_SECRET}":
            self._respond(401, {"error": "Unauthorized"})
            return
        try:
            settings = kv_get(SETTINGS_KEY) or {}
            if not settings.get("computed_holdings"):
                self._respond(200, {"ok": True, "detected": 0,
                                    "note": "no computed_holdings in KV"})
                return

            state = kv_get(AUTOPILOT_KEY) or {}
            found = detect(settings, state)

            if body.get("dry_run"):
                self._respond(200, {"ok": True, "dry_run": True,
                                    "detected": len(found), "entries": found})
                return

            if found:
                state["pending"] = (state.get("pending") or []) + found
                state.setdefault("seen", {})
                state.setdefault("rates", {})
                kv_set(AUTOPILOT_KEY, state)

            subs = kv_get(SUBS_KEY) or []
            sent = sum(notify(e, subs) for e in found) if subs else 0
            self._respond(200, {"ok": True, "detected": len(found),
                                "notifications_sent": sent,
                                "pending_total": len(state.get("pending") or []),
                                "entries": found})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    # ── resolve ──
    def _handle_resolve(self, body: dict, action: str):
        entry_id = body.get("id")
        if not entry_id:
            self._respond(400, {"error": "id required"})
            return
        try:
            state   = kv_get(AUTOPILOT_KEY) or {}
            pending = state.get("pending") or []
            seen    = state.get("seen") or {}
            rates   = state.get("rates") or {}

            entry = next((p for p in pending if p.get("id") == entry_id), None)
            if entry is None:
                # Already resolved on another device — succeed idempotently
                self._respond(200, {"ok": True, "already_resolved": True,
                                    "pending": pending})
                return

            now = datetime.now(timezone.utc).isoformat()
            if action == "dismiss":
                seen[entry_id] = {"resolved_at": now, "action": "dismiss"}
            else:
                actual = (float(body["actual_amount"])
                          if action == "adjust" and body.get("actual_amount") is not None
                          else float(entry.get("net_amount") or 0))
                rates = record_learned_rate(rates, entry, actual)
                seen[entry_id] = {"resolved_at": now, "action": action,
                                  "amount": actual, "ccy": entry.get("ccy")}

            state["pending"] = [p for p in pending if p.get("id") != entry_id]
            state["seen"]    = seen
            state["rates"]   = rates
            kv_set(AUTOPILOT_KEY, state)

            self._respond(200, {"ok": True, "id": entry_id, "action": action,
                                "pending": state["pending"], "rates": rates})
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        pass
