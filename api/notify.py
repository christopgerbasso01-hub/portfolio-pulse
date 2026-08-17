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
FMP_API_KEY   = os.environ.get("FMP_API_KEY", "")   # dividend payment dates
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


def _observed_max(default=None):
    """
    Highest total_value across the daily snapshots still held in KV.

    Returns None when the history cannot be read at all. That is deliberately
    distinct from a low value: the caller must not "correct" a stored peak
    downward on the basis of evidence it failed to load.
    """
    try:
        from snapshot import get_recent_snapshots
        vals = [
            s.get("total_value") for s in get_recent_snapshots(days=95).values()
            if s.get("total_value")
        ]
        return max(vals) if vals else default
    except Exception as exc:
        print(f"  [notify] observed-max lookup failed: {exc}")
        return default


# A stored peak is allowed to sit this far above anything ever actually
# recorded before it is treated as corrupt. Snapshots can lag the true intraday
# high slightly, so a small margin is legitimate; tens of percent is not.
PEAK_TRUST_MARGIN = 1.02

# A single day cannot plausibly add this much, even with 3x leveraged holdings.
# Rejecting such jumps stops one bad reading from poisoning the peak forever.
PEAK_MAX_DAILY_JUMP = 1.20


def _validated_peak(peak: dict, total: float, today_str: str) -> float:
    """
    Return a trustworthy all-time high.

    KEY_PEAK is a permanent high-water mark with no TTL, written whenever a
    total exceeds it. That makes it a one-way ratchet: a single corrupt reading
    — a bad price, a double-applied FX rate — raises it forever, and nothing
    can bring it back down. The drawdown alert then measures against a level
    the portfolio never reached and fires against a phantom loss.

    So the stored value is cross-checked against what the snapshot history
    actually recorded. If it claims a high no snapshot corroborates, it is
    corrected to the highest real observation and rewritten.
    """
    stored = (peak or {}).get("value") or 0
    if stored <= 0:
        return total

    history = _observed_max()
    if not history:
        # History unreadable. Today's value alone is not evidence the stored
        # peak is wrong — every day sits below a real peak — so leave it be.
        return stored

    observed = max(history, total)
    if stored > observed * PEAK_TRUST_MARGIN:
        print(f"  [notify] stored peak ${stored:,.0f} exceeds the highest value ever "
              f"recorded (${observed:,.0f}) — treating as corrupt and correcting")
        kv_set(KEY_PEAK, {"value": observed, "date": today_str, "corrected_from": stored})
        return observed

    return stored


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

    peak_val   = _validated_peak(peak, total, today_str)
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
        # Guard the write. Without this a single corrupt total is recorded as
        # the all-time high permanently, since the peak only ever ratchets up.
        if peak_val > 0 and total > peak_val * PEAK_MAX_DAILY_JUMP:
            print(f"  [notify] ignoring implausible ATH ${total:,.0f} — "
                  f"{total / peak_val - 1:+.0%} above the prior peak ${peak_val:,.0f}")
        else:
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



# ══════════════════════════════════════════════════════════════════════════════
# DIVIDEND AUTOPILOT
# Served at /api/dividends (routed to this file — Vercel Hobby caps the project
# at 12 serverless functions, and this module already owns KV + VAPID + push).
#
# Detects dividends paid on current holdings and queues them for approval.
# NOTHING is booked automatically: the dashboard confirms each entry through the
# normal DIVIDEND transaction path, so there is exactly one booking code path.
# ══════════════════════════════════════════════════════════════════════════════
SETTINGS_KEY  = "user:settings"
AUTOPILOT_KEY = "dividend:autopilot"
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

# Past this many days after the ex-date, payment has certainly landed, so an
# estimated payment date is no longer a reason to distrust the entry. Without
# this, every dividend from a source lacking payment dates reads as LOW.
PAYMENT_CERTAIN_DAYS = 45

# On the very first scan the queue is seeded from this far back only. Scanning
# a full year would flood the queue with dividends already booked by hand.
FIRST_RUN_LOOKBACK_DAYS = 30

# A single distribution worth more than this share of the position is almost
# certainly bad data — a split misread as a dividend, or a units error.
ANOMALY_PCT_OF_POSITION = 0.05


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
    """{ex_date: payment_date} from FMP. Empty dict when unavailable.

    Uses the /stable/dividends endpoint. The old
    /api/v3/historical-price-full/stock_dividend path now returns 403
    ("Legacy Endpoint ... no longer supported"), which is why payment dates
    silently went missing for every ticker.

    Coverage on the current plan is US common stocks only; Canadian (.TO)
    listings and leveraged ETFs return 402 Premium, so those fall back to the
    estimated payment date.
    """
    if not FMP_API_KEY:
        return {}
    try:
        r = requests.get("https://financialmodelingprep.com/stable/dividends",
                         params={"symbol": ticker, "apikey": FMP_API_KEY}, timeout=12)
        if not r.ok:                       # 402 = not on plan, 403 = retired path
            return {}
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("historical", []) or []
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
    """estimated_pay here means 'estimated AND recent enough to be uncertain' —
    the caller clears it once the ex-date is far enough back that payment is
    certain, so an old dividend is not marked LOW purely for lacking a feed date.
    """
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

def detect(settings: dict, state: dict) -> tuple:
    """Return (newly detected entries, diagnostics).

    Only considers ex-dates on or after state['start_date']. That boundary is
    seeded FIRST_RUN_LOOKBACK_DAYS back on the first scan so the queue is not
    flooded with a year of dividends that were already booked by hand.
    """
    now      = datetime.now(timezone.utc)
    today    = now.strftime("%Y-%m-%d")
    holdings = settings.get("computed_holdings") or []
    txs      = settings.get("transactions") or []
    seen     = state.get("seen") or {}
    learned  = state.get("rates") or {}
    queued   = {p.get("id") for p in (state.get("pending") or [])}

    start_date = state.get("start_date") or \
        (now - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    # Past this ex-date, payment has certainly landed even without a feed date
    certain_before = (now - timedelta(days=PAYMENT_CERTAIN_DAYS)).strftime("%Y-%m-%d")

    positions = [h for h in holdings
                 if h.get("ticker") and h["ticker"] not in NO_DIVIDEND
                 and (h.get("shares") or 0) > 0]

    div_cache, pay_cache, found = {}, {}, []
    diag = {"start_date": start_date, "tickers": 0, "fmp_ok": 0, "yahoo_ok": 0}

    for h in positions:
        ticker, account = h["ticker"], h.get("account", "")
        if ticker not in div_cache:
            div_cache[ticker] = fetch_dividend_events(ticker)
            pay_cache[ticker] = fetch_payment_dates(ticker)
            diag["tickers"]  += 1
            diag["yahoo_ok"] += 1 if div_cache[ticker] else 0
            diag["fmp_ok"]   += 1 if pay_cache[ticker] else 0

        for ev in div_cache[ticker]:
            ex_date = ev["ex_date"]
            if ex_date < start_date:
                continue                              # before autopilot was armed
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
            # An estimated payment date only casts doubt while the ex-date is
            # recent; past PAYMENT_CERTAIN_DAYS the money has certainly landed.
            pay_uncertain = estimated_pay and ex_date >= certain_before
            confidence = "LOW" if anomaly else score_confidence(
                ticker, basis, saw_split, pay_uncertain)

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
    return found, diag


def dividend_notif(entry: dict) -> dict:
    """Push payload for one detected dividend. Reuses broadcast()."""
    sym  = "$" if entry["ccy"] == "USD" else "C$"
    note = "estimate — confirm amount" if entry["confidence"] == "LOW" else "tap to confirm"
    return {
        "title": f"Dividend — {entry['ticker']}",
        "body":  (f"{entry['account']} · {sym}{entry['net_amount']:,.2f} {entry['ccy']}\n"
                  f"{entry['shares']:g} sh × {sym}{entry['per_share']:.4f} · {note}"),
        "tag":   f"dividend-{entry['id']}",
        "data":  {"type": "dividend_pending", "id": entry["id"]},
    }


# ── Request handler ────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    # /api/dividends is routed to this file; branch on path so the dividend
    # queue keeps its own auth posture (reads and resolves are unauthenticated,
    # matching /api/settings; only the scan requires CRON_SECRET).
    def _is_dividends(self) -> bool:
        return "/api/dividends" in (self.path or "")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._is_dividends():
            self._respond(404, {"error": "Not found"})
            return
        try:
            state = kv_get(AUTOPILOT_KEY) or {}
            self._respond(200, {"pending": state.get("pending") or [],
                                "rates":   state.get("rates") or {}})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def do_POST(self):
        if self._is_dividends():
            self._dividends_post()
            return
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

    # ── Dividend Autopilot ────────────────────────────────────────────────────

    def _dividends_post(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
            return

        action = body.get("action")
        if action == "scan":
            if not self._auth():          # scan is cron-only
                return
            self._dividends_scan(body)
        elif action in ("confirm", "adjust", "dismiss"):
            self._dividends_resolve(body, action)
        else:
            self._respond(400, {"error": "action must be scan|confirm|adjust|dismiss"})

    def _dividends_scan(self, body: dict):
        try:
            settings = kv_get(SETTINGS_KEY) or {}
            if not settings.get("computed_holdings"):
                self._respond(200, {"ok": True, "detected": 0,
                                    "note": "no computed_holdings in KV"})
                return

            state = kv_get(AUTOPILOT_KEY) or {}
            # One-time override for an intentional backfill:
            #   {"action":"scan","start_date":"2026-01-01"}
            if body.get("start_date"):
                state["start_date"] = body["start_date"]
            found, diag = detect(settings, state)

            if body.get("dry_run"):
                self._respond(200, {"ok": True, "dry_run": True, "diagnostics": diag,
                                    "detected": len(found), "entries": found})
                return

            state["pending"] = (state.get("pending") or []) + found
            state.setdefault("seen", {})
            state.setdefault("rates", {})
            # Pin the boundary on first run so later scans never reach further back
            state.setdefault("start_date", diag["start_date"])
            kv_set(AUTOPILOT_KEY, state, STATE_TTL)

            subs = get_subs()
            sent = broadcast([dividend_notif(e) for e in found], subs).get("sent", 0) \
                if (found and subs) else 0
            print(f"  [dividends] detected={len(found)} sent={sent} diag={diag}")
            self._respond(200, {"ok": True, "detected": len(found),
                                "notifications_sent": sent, "diagnostics": diag,
                                "pending_total": len(state.get("pending") or []),
                                "entries": found})
        except Exception as exc:
            self._respond(500, {"error": str(exc)})

    def _dividends_resolve(self, body: dict, action: str):
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
            kv_set(AUTOPILOT_KEY, state, STATE_TTL)

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
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        pass
