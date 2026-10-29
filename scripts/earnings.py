"""
Earnings on the stocks we hold — computed once a day by the intelligence run, read by
the dashboard and the podcast.

For every holding that reports earnings (not ETFs or 3x funds):
  recent   — reported in the last RECENT_DAYS days: EPS against the estimate
             (beat / miss / in line), revenue against the previous quarter, the
             stock's move on the report, what that move was worth to us, our
             position and its weight, whether the company is also inside FNGU,
             and a three-point summary of what the company said, written ONLY
             from that week's news coverage and checked number by number.
  upcoming — reporting in the next UPCOMING_DAYS days: date, before or after the
             market, our position, the EPS estimate to watch.

Data: Yahoo Finance quoteSummary (dates, EPS, revenue — covers .TO listings too) and
Yahoo daily closes for the reaction; Finnhub company news for the summary. Every
network call is injectable, and every failure is local: one ticker failing never
stops the others, and the whole step failing never stops the briefing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

RECENT_DAYS = 14
UPCOMING_DAYS = 14
BEAT_PCT = 2.0                     # EPS surprise within +/-2% is "in line"
MAX_NEW_SUMMARIES = 4              # Groq calls per run; earlier summaries are reused
try:                                # New York time, with daylight saving
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                   # pragma: no cover — no tz database
    ET = timezone(timedelta(hours=-5))

# Funds and trusts do not report earnings.
NON_REPORTING = frozenset({"FNGU", "SPXL", "UDOW", "SOXL", "TQQQ", "TECL", "LABU", "FNGA",
                           "TXF.TO", "GBTC", "ZSP.TO", "ZAG.TO"})
# Stocks that are standing members of the NYSE FANG+ index FNGU tracks (the tenth
# seat rotates, so it is left out rather than guessed).
FANG_PLUS = frozenset({"META", "AAPL", "AMZN", "NFLX", "GOOGL", "MSFT", "NVDA", "TSLA", "AVGO"})

_YF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


# ── Network (injectable) ─────────────────────────────────────────────────────

def yahoo_session():
    import requests
    s = requests.Session()
    s.headers.update(_YF_HEADERS)
    try:
        s.get("https://fc.yahoo.com", timeout=10)
    except Exception:
        pass
    crumb = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10).text.strip()
    return s, crumb


def fetch_summary(session, crumb, ticker):
    r = session.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                    params={"modules": "calendarEvents,earnings,quoteType,price", "crumb": crumb}, timeout=12)
    r.raise_for_status()
    res = (r.json().get("quoteSummary") or {}).get("result") or []
    return res[0] if res else None


def fetch_closes(session, ticker):
    """{YYYY-MM-DD: close} for the last two months."""
    r = session.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
                    params={"interval": "1d", "range": "2mo"}, timeout=12)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    out = {}
    for ts, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d")] = float(c)
    return out


def fetch_news(finnhub_key, ticker, day_from, day_to):
    import requests
    r = requests.get("https://finnhub.io/api/v1/company-news",
                     params={"symbol": ticker, "from": day_from, "to": day_to, "token": finnhub_key}, timeout=12)
    if r.status_code != 200:
        return []
    return [{"headline": a.get("headline", ""), "summary": a.get("summary", "")}
            for a in (r.json() or [])[:12] if a.get("headline")]


# ── Pure logic ───────────────────────────────────────────────────────────────

def _raw(obj, *keys):
    for k in keys:
        obj = (obj or {}).get(k) if isinstance(obj, dict) else None
    if isinstance(obj, dict):
        return obj.get("raw")
    return obj


def timing_of(ts: int) -> str:
    """'before the open', 'after the close' or 'during the day', from a Unix time."""
    t = datetime.fromtimestamp(ts, tz=ET)
    minutes = t.hour * 60 + t.minute
    if minutes < 9 * 60 + 30:
        return "before the open"
    if minutes >= 16 * 60:
        return "after the close"
    # Almost nobody reports mid-session; a midday stamp is Yahoo's placeholder for a
    # date whose release time has not been announced.
    return "time not confirmed"


def parse_summary(ticker: str, qs: dict, now: datetime) -> dict:
    """{'type': 'EQUITY'|..., 'last': {...}|None, 'next': {...}|None} from quoteSummary."""
    out = {"type": _raw(qs, "quoteType", "quoteType") or (qs.get("quoteType") or {}).get("quoteType"),
           "name": (qs.get("price") or {}).get("shortName") or ticker, "last": None, "next": None}
    earn = qs.get("earnings") or {}
    chart = (earn.get("earningsChart") or {}).get("quarterly") or []
    revs = {q.get("date"): _raw(q, "revenue") for q in ((earn.get("financialsChart") or {}).get("quarterly") or [])}
    now_ts = now.timestamp()
    reported = [q for q in chart if _raw(q, "reportedDate") and _raw(q, "reportedDate") <= now_ts]
    if reported:
        q = max(reported, key=lambda x: _raw(x, "reportedDate"))
        actual, est = _raw(q, "actual"), _raw(q, "estimate")
        surprise = ((actual - est) / abs(est) * 100) if (actual is not None and est not in (None, 0)) else None
        order = [x.get("date") for x in ((earn.get("financialsChart") or {}).get("quarterly") or [])]
        key = q.get("date")
        rev = revs.get(key)
        prev = revs.get(order[order.index(key) - 1]) if key in order and order.index(key) > 0 else None
        ts = int(_raw(q, "reportedDate"))
        out["last"] = {
            "quarter": key, "reported_ts": ts,
            "reported_at": datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d"),
            "timing": timing_of(ts),
            "eps_actual": actual, "eps_estimate": est,
            "surprise_pct": round(surprise, 1) if surprise is not None else None,
            "verdict": (None if surprise is None else "beat" if surprise >= BEAT_PCT
                        else "miss" if surprise <= -BEAT_PCT else "in line"),
            "revenue": rev, "revenue_prev": prev,
            "revenue_qoq_pct": round((rev / prev - 1) * 100, 1) if rev and prev else None,
        }
    cal = (qs.get("calendarEvents") or {}).get("earnings") or {}
    # Yahoo sometimes leaves earningsDate on the last report (RY.TO showed 27 Aug in
    # late October) while earningsCallDate already holds the next one, so take the
    # earliest FUTURE date from both.
    future = sorted(int(d["raw"]) for d in (cal.get("earningsDate") or []) + (cal.get("earningsCallDate") or [])
                    if isinstance(d, dict) and d.get("raw") and d["raw"] > now_ts)
    if future:
        ts = future[0]
        out["next"] = {"date": datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d"), "ts": ts,
                       "timing": timing_of(ts), "estimated": bool(cal.get("isEarningsDateEstimate")),
                       "eps_estimate": _raw(cal, "earningsAverage"),
                       "revenue_estimate": _raw(cal, "revenueAverage")}
    return out


def reaction_pct(closes: dict, reported_at: str, timing: str):
    """The stock's move on the news: for an after-the-close report, the next session
    against the report day; otherwise the report day against the session before."""
    days = sorted(closes)
    if reported_at not in days:
        later = [d for d in days if d > reported_at]
        if not later:
            return None
        base_i = days.index(later[0]) - 1
        if base_i < 0:
            return None
        a, b = days[base_i], later[0]
    else:
        i = days.index(reported_at)
        if timing == "after the close":
            if i + 1 >= len(days):
                return None                       # the reaction session has not closed yet
            a, b = days[i], days[i + 1]
        else:
            if i == 0:
                return None
            a, b = days[i - 1], days[i]
    return round((closes[b] / closes[a] - 1) * 100, 2)


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set:
    return {float(n.replace(",", "")) for n in _NUM.findall(text or "")}


def check_summary(bullets: list, sources: str, card: dict) -> list:
    """Keep only bullets whose every number appears in the coverage or the card's own
    figures — a summary must not add a statistic nobody reported."""
    allowed = _numbers(sources)
    for k in ("eps_actual", "eps_estimate", "surprise_pct", "revenue_qoq_pct", "reaction_pct"):
        v = card.get(k)
        if isinstance(v, (int, float)):
            allowed |= {round(abs(v), 2), round(abs(v), 1), round(abs(v))}
    kept = []
    for b in bullets:
        b = str(b).strip()
        if not b:
            continue
        if all(any(abs(n - a) <= max(0.011, a * 0.005) for a in allowed) for n in _numbers(b)):
            kept.append(b)
    return kept[:3]


def summary_prompt(card: dict, articles: list) -> str:
    src = "\n".join(f"- {a['headline']}. {a.get('summary', '')[:300]}" for a in articles[:10])
    return f"""Summarise {card['name']}'s latest earnings report ({card['quarter']}, released {card['reported_at']}) for a long-term investor who owns the stock.
Use ONLY the news coverage below. Give exactly three short bullet points: what the results showed, what the company said about the future (guidance) if the coverage mentions it, and the one surprise or concern. Do not add any number, date or fact that is not in the coverage. Do not give advice. If the coverage does not cover the report, return an empty list.

COVERAGE:
{src}

Return only JSON: {{"bullets": ["...", "...", "..."]}}"""


def groq_summary(api_key: str, prompt: str) -> list:
    import requests
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                      json={"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 700, "temperature": 0.3, "reasoning_effort": "low",
                            "include_reasoning": False, "response_format": {"type": "json_object"}},
                      timeout=60)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    return list((json.loads(txt[txt.find("{"):txt.rfind("}") + 1]) or {}).get("bullets") or [])


def positions(holdings: list) -> dict:
    """{ticker: {'shares', 'ccy', 'accounts', 'name'}} summed across accounts."""
    out = {}
    for h in holdings or []:
        t = h.get("ticker")
        if not t or str(t).startswith("CASH") or float(h.get("shares") or 0) <= 0:
            continue
        p = out.setdefault(t, {"shares": 0.0, "ccy": h.get("ccy") or "USD", "accounts": [], "name": h.get("name") or t})
        p["shares"] += float(h.get("shares") or 0)
        if h.get("account") and h["account"] not in p["accounts"]:
            p["accounts"].append(h["account"])
    return out


def build(holdings: list, now: datetime = None, *, session=None, crumb=None, usdcad: float = None,
          total_value: float = None,
          summary_fn=None, news_fn=None, previous: dict = None,
          fetch_summary_fn=fetch_summary, fetch_closes_fn=fetch_closes, log=print) -> dict:
    """The earnings block for intelligence.json. Never raises for a single ticker."""
    now = now or datetime.now(timezone.utc)
    pos = positions(holdings)
    prev_cards = {(c.get("ticker"), c.get("reported_at")): c for c in ((previous or {}).get("recent") or [])}
    recent, upcoming, errors, new_summaries = [], [], 0, 0
    for tkr, p in pos.items():
        if tkr in NON_REPORTING:
            continue
        try:
            qs = fetch_summary_fn(session, crumb, tkr)
            if not qs:
                continue
            info = parse_summary(tkr, qs, now)
            if info["type"] and info["type"] != "EQUITY":
                continue
            closes = {}
            try:
                closes = fetch_closes_fn(session, tkr)
            except Exception:
                pass
            last_px = closes[max(closes)] if closes else None
            fx = (usdcad or 1.38) if p["ccy"] == "USD" else 1.0
            value = (last_px or 0) * p["shares"] * fx
            base = {"ticker": tkr, "name": info["name"], "position_cad": round(value),
                    "accounts": p["accounts"], "in_fngu": tkr in FANG_PLUS}
            last = info["last"]
            if last and (now - datetime.fromtimestamp(last["reported_ts"], tz=timezone.utc)).days <= RECENT_DAYS:
                card = dict(base, **last)
                card["reaction_pct"] = reaction_pct(closes, last["reported_at"], last["timing"]) if closes else None
                # Reported this morning: the "reaction" is today's move so far, not a close.
                now_et = now.astimezone(ET)
                card["reaction_partial"] = (last["reported_at"] == now_et.strftime("%Y-%m-%d")
                                            and last["timing"] != "after the close"
                                            and now_et.hour * 60 + now_et.minute < 16 * 60)
                if card["reaction_pct"] is not None and value:
                    card["impact_cad"] = round(value - value / (1 + card["reaction_pct"] / 100))
                old = prev_cards.get((tkr, last["reported_at"]))
                if old and old.get("summary"):
                    card["summary"] = old["summary"]           # written on an earlier run
                elif summary_fn and news_fn and new_summaries < MAX_NEW_SUMMARIES:
                    try:
                        d0 = (datetime.fromisoformat(last["reported_at"]) - timedelta(days=1)).strftime("%Y-%m-%d")
                        d1 = (datetime.fromisoformat(last["reported_at"]) + timedelta(days=3)).strftime("%Y-%m-%d")
                        arts = [a for a in news_fn(tkr, d0, d1) if a.get("headline")]
                        if len(arts) >= 2:
                            new_summaries += 1
                            src = " ".join(a["headline"] + " " + a.get("summary", "") for a in arts)
                            card["summary"] = check_summary(summary_fn(summary_prompt(card, arts)), src, card)
                    except Exception as exc:
                        log(f"  ⚠ earnings summary for {tkr} skipped: {str(exc)[:100]}")
                recent.append(card)
            nxt = info["next"]
            if nxt and (datetime.fromtimestamp(nxt["ts"], tz=timezone.utc) - now).days <= UPCOMING_DAYS:
                upcoming.append(dict(base, **nxt))
        except Exception as exc:
            errors += 1
            log(f"  ⚠ earnings for {tkr} failed: {str(exc)[:100]}")
    # Weight against the WHOLE portfolio — the reporting stocks alone made Tesla look
    # like 7% of it. Without a total, no weight rather than a wrong one.
    total = total_value or 0
    for c in recent + upcoming:
        c["weight_pct"] = round(c["position_cad"] / total * 100, 1) if total else None
    recent.sort(key=lambda c: c["reported_ts"], reverse=True)
    upcoming.sort(key=lambda c: c["ts"])
    return {"generated_at": now.isoformat(), "recent": recent, "upcoming": upcoming, "errors": errors}


def legacy_upcoming(block: dict) -> list:
    """The old intelligence["upcoming_earnings"] shape the glance card reads."""
    return [{"symbol": c["ticker"], "date": c["date"], "eps_estimate": c.get("eps_estimate")}
            for c in (block or {}).get("upcoming", [])][:8]
