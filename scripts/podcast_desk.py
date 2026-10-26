"""
The portfolio desk and the show's memory — pure, deterministic, unit-tested.

Everything the podcast says about the portfolio's week is computed here, not left
to the model: what changed in value and how much of that was new money versus the
market, what was bought and sold, which dividends landed, how the sector mix moved,
how each account did, and how far along the home down-payment is. Episodes 21 and
22 said almost none of this because the model was only ever handed a total and a
list of movers.

The memory file (data/podcast_memory.json) is what makes the episodes one show
instead of unrelated broadcasts: each episode records its scenarios, what it
promised for next week, its subjects and the analogies it used, so the next one
can score last week's call, keep the promise, and avoid repeating itself.

Stdlib only.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ACCOUNTS = ("TFSA", "Investment", "FHSA", "RRSP")
ACCOUNT_LABEL = {"TFSA": "TFSA", "Investment": "non-registered account", "FHSA": "FHSA", "RRSP": "RRSP"}
SECTOR_SHIFT_MIN_PP = 0.5          # a sector move smaller than this is noise
FX_MENTION_MIN_CENTS = 1.0         # USD/CAD must move at least a cent to be worth a word
SCENARIO_BAND_PCT = 1.5            # base case: the S&P 500 within +/-1.5% over the week
MEMORY_KEEP = 12


def _d(s: str):
    return datetime.fromisoformat(str(s)[:10]).date()


def _cad(amount: float, ccy: str, usdcad: float) -> float:
    return float(amount or 0) * (usdcad if (ccy or "CAD") == "USD" else 1.0)


# ── The week, computed ───────────────────────────────────────────────────────

def portfolio_changes(settings: dict, snapshots: dict, baseline_date: str, latest_date: str) -> dict:
    """Everything that changed in the portfolio between two snapshot dates.

    settings  — the dashboard's KV document (transactions, computed_holdings, ...)
    snapshots — {date: snapshot}; both dates must carry holdings_prices.
    """
    then, now = snapshots.get(baseline_date) or {}, snapshots.get(latest_date) or {}
    fx_now  = float(now.get("usdcad") or 1.38)
    fx_then = float(then.get("usdcad") or fx_now)
    px_now  = now.get("holdings_prices") or {}
    px_then = then.get("holdings_prices") or {}
    holdings = [h for h in (settings.get("computed_holdings") or [])
                if h.get("ticker") and not str(h["ticker"]).startswith("CASH")]
    txs = [t for t in (settings.get("transactions") or []) if t.get("date")]
    # Transactions that happened AFTER the baseline close, up to and including the latest.
    window = [t for t in txs if baseline_date < t["date"] <= latest_date]

    # ── Positions now and at the baseline ────────────────────────────────────
    info, shares_now = {}, {}
    for h in holdings:
        k = (h["ticker"], h.get("account", ""))
        shares_now[k] = shares_now.get(k, 0.0) + float(h.get("shares") or 0)
        info.setdefault(h["ticker"], {"name": h.get("name") or h["ticker"],
                                      "sector": h.get("sector") or "Other",
                                      "ccy": h.get("ccy") or "USD"})
    shares_then = dict(shares_now)
    for t in window:
        k = (t.get("ticker"), t.get("account", ""))
        if t.get("type") == "BUY":
            shares_then[k] = shares_then.get(k, 0.0) - float(t.get("shares") or 0)
        elif t.get("type") == "SELL":
            shares_then[k] = shares_then.get(k, 0.0) + float(t.get("shares") or 0)
            info.setdefault(t["ticker"], {"name": t.get("name") or t["ticker"],
                                          "sector": t.get("sector") or "Other",
                                          "ccy": t.get("ccy") or "USD"})

    def value(shares: dict, prices: dict, fx: float) -> dict:
        by_ticker = {}
        for (tkr, _acct), sh in shares.items():
            if sh <= 1e-6 or tkr not in info:
                continue
            p = float((prices.get(tkr) or {}).get("price") or 0)
            if p <= 0:
                continue
            by_ticker[tkr] = by_ticker.get(tkr, 0.0) + _cad(p * sh, info[tkr]["ccy"], fx)
        return by_ticker

    val_now, val_then = value(shares_now, px_now, fx_now), value(shares_then, px_then, fx_then)

    def weights(vals: dict) -> dict:
        tot = sum(vals.values()) or 1.0
        out = {}
        for tkr, v in vals.items():
            s = info[tkr]["sector"]
            out[s] = out.get(s, 0.0) + v / tot * 100
        return out

    w_now, w_then = weights(val_now), weights(val_then)
    shifts = sorted(((s, w_then.get(s, 0.0), w_now.get(s, 0.0)) for s in set(w_now) | set(w_then)),
                    key=lambda x: -abs(x[2] - x[1]))
    sector_shifts = [{"sector": s, "then": round(a, 1), "now": round(b, 1), "pp": round(b - a, 1)}
                     for s, a, b in shifts if abs(b - a) >= SECTOR_SHIFT_MIN_PP][:3]

    # ── Trades ───────────────────────────────────────────────────────────────
    held_before = {tkr for (tkr, _a), sh in shares_then.items() if sh > 1e-6}
    held_after  = {tkr for (tkr, _a), sh in shares_now.items() if sh > 1e-6}
    trades = []
    for t in window:
        if t.get("type") not in ("BUY", "SELL"):
            continue
        tkr, acct = t.get("ticker"), t.get("account", "")
        amount = _cad(float(t.get("price") or 0) * float(t.get("shares") or 0),
                      t.get("ccy") or info.get(tkr, {}).get("ccy", "USD"), float(t.get("rate") or fx_now))
        if t["type"] == "BUY":
            kind = "new position" if tkr not in held_before else "added to"
        else:
            kind = "sold out of" if shares_now.get((tkr, acct), 0) <= 1e-6 else "trimmed"
        trades.append({"date": t["date"], "kind": kind, "ticker": tkr, "account": acct,
                       "name": info.get(tkr, {}).get("name", tkr),
                       "shares": float(t.get("shares") or 0), "cad": round(amount)})
    new_tickers    = sorted({x["ticker"] for x in trades if x["kind"] == "new position"})
    exited_tickers = sorted(held_before - held_after)

    # ── Dividends and contributions ──────────────────────────────────────────
    divs = [t for t in window if t.get("type") == "DIVIDEND"]
    div_by = {}
    for t in divs:
        div_by[t["ticker"]] = div_by.get(t["ticker"], 0.0) + _cad(t.get("amount"), t.get("ccy") or "CAD",
                                                                  float(t.get("rate") or fx_now))
    contrib_then = sum(float(v or 0) for v in (then.get("account_cost") or {}).values())
    contrib_now  = sum(float(v or 0) for v in (now.get("account_cost") or {}).values())
    contrib_added = contrib_now - contrib_then
    contrib_tx = [t for t in window if t.get("type") == "CONTRIBUTION"]

    total_now  = float(now.get("total_value") or 0)
    total_then = float(then.get("total_value") or total_now)
    change     = total_now - total_then
    market     = change - contrib_added

    # ── Accounts and the house goal ──────────────────────────────────────────
    acc_now, acc_then = now.get("accounts") or {}, then.get("accounts") or {}
    acc_cost = now.get("account_cost") or {}
    accounts = {a: {"value": round(float(acc_now.get(a) or 0)),
                    "change": round(float(acc_now.get(a) or 0) - float(acc_then.get(a) or acc_now.get(a) or 0)),
                    "contributed": round(float(acc_cost.get(a) or 0))}
                for a in ACCOUNTS if acc_now.get(a) is not None}
    house_now = sum(float(acc_now.get(a) or 0) for a in ("FHSA", "RRSP"))

    return {
        "baseline_date": baseline_date, "latest_date": latest_date,
        "days": (_d(latest_date) - _d(baseline_date)).days,
        "total_now": round(total_now), "total_then": round(total_then), "change": round(change),
        "change_pct": round(change / total_then * 100, 2) if total_then else 0.0,
        "contrib_total": round(contrib_now), "contrib_added": round(contrib_added),
        "market_change": round(market),
        "market_pct": round(market / total_then * 100, 2) if total_then else 0.0,
        "pnl_total": round(total_now - contrib_now),
        "pnl_pct": round((total_now - contrib_now) / contrib_now * 100, 1) if contrib_now else 0.0,
        "contrib_tx": [{"date": t["date"], "account": t.get("account"), "amount": float(t.get("amount") or 0)}
                       for t in contrib_tx],
        "trades": trades, "new_tickers": new_tickers, "exited_tickers": exited_tickers,
        "dividends": {"count": len(divs), "total": round(sum(div_by.values()), 2),
                      "top": max(div_by.items(), key=lambda kv: kv[1])[0] if div_by else None,
                      "by_ticker": {k: round(v, 2) for k, v in div_by.items()}},
        "sector_weights": {s: round(v, 1) for s, v in sorted(w_now.items(), key=lambda kv: -kv[1])},
        "sector_shifts": sector_shifts,
        "accounts": accounts,
        "house": {"value": round(house_now)},
        "usdcad_then": round(fx_then, 4), "usdcad_now": round(fx_now, 4),
        "fx_cents": round((fx_now - fx_then) * 100, 1),
        "positions": {t: round(v) for t, v in sorted(val_now.items(), key=lambda kv: -kv[1])},
    }


def fx_is_newsworthy(changes: dict) -> bool:
    return abs(float(changes.get("fx_cents") or 0)) >= FX_MENTION_MIN_CENTS


# ── Scenarios and last week's scorecard ──────────────────────────────────────

def scenario_frame(index_table: dict) -> list:
    """The three cases for the coming week. The thresholds are fixed so next week's
    episode can say, from data, which one happened. The model writes the story and
    picks the odds; it does not get to move the goalposts."""
    small = index_table.get("1") or index_table.get(1)
    two   = index_table.get("2") or index_table.get(2)
    band  = SCENARIO_BAND_PCT
    return [
        {"case": "base", "label": "Base case",
         "condition": f"the S&P 500 ends the week within {band:g} percent of where it starts",
         "effect": small, "effect_note": "a 1 percent move either way"},
        {"case": "bull", "label": "Bull case",
         "condition": f"the S&P 500 rises more than {band:g} percent this week",
         "effect": two, "effect_note": "a 2 percent rise"},
        {"case": "bear", "label": "Bear case",
         "condition": f"the S&P 500 falls more than {band:g} percent this week",
         "effect": two, "effect_note": "a 2 percent fall"},
    ]


def score_last_week(prev: dict, sp_now: float):
    """Which of last episode's cases played out, from the S&P 500 close. None if unknown."""
    sc = (prev or {}).get("scenarios") or {}
    sp_then = sc.get("sp500_close")
    if not sp_then or not sp_now:
        return None
    move = (float(sp_now) / float(sp_then) - 1) * 100
    band = float(sc.get("band_pct") or SCENARIO_BAND_PCT)
    case = "base" if abs(move) <= band else ("bull" if move > 0 else "bear")
    probs = sc.get("probabilities") or {}
    return {"move_pct": round(move, 2), "case": case,
            "label": {"base": "base case", "bull": "bull case", "bear": "bear case"}[case],
            "probability": probs.get(case), "from_date": sc.get("sp500_date")}


_PROB_LINE = re.compile(r"\b(Base|Bull|Bear)\s+case\b[^0-9\n]{0,30}?(\d{1,2})\s*(?:%|percent)", re.I)


def scenario_probabilities(script: str) -> dict:
    out = {}
    for name, p in _PROB_LINE.findall(script or ""):
        out.setdefault(name.lower(), int(p))
    return out


# ── Repetition across episodes ───────────────────────────────────────────────

_ART = r"(?:a|an|the|your|two|one)\s+"
_ANALOGY = re.compile(
    r"\b(?:think of (?:it|this|that|[\w'-]+(?:\s[\w'-]+)?)\s(?:like|as)\s(?:" + _ART + r")?"
    r"|it'?s (?:a bit |kind of |sort of |just )?like\s(?:\w+ing\s)?(?:on\s)?" + _ART +
    r"|(?:imagine|picture)\s(?:" + _ART + r"|you(?:'re| are| own| have)\s(?:" + _ART + r")?)"
    r"|think of\s" + _ART +
    r"|\b(?:is|are|was|were)\s(?:kind of |sort of |a bit |just )?like\s" + _ART +
    r"|(?:just like|similar to|much like)\s" + _ART + r")"
    r"((?:[\w'-]+\s+){0,5}[\w'-]+)", re.I)
_ANALOGY_SKIP = frozenset("a an the your you having using driving getting being on in at of with and that "
                          "which who where for to some one this it".split())


_ANALOGY_NOISE = frozenset("set value looking plain why sanity stock stocks portfolio holdings company's earnings "
                           "market price prices number numbers".split())


def extract_analogies(text: str) -> list:
    """Short labels for the comparisons an episode used ("grocery store", "toll bridge")."""
    out = []
    for m in _ANALOGY.finditer(_plain(text)):
        words = [w for w in re.findall(r"[a-z'-]+", m.group(1).lower()) if w not in _ANALOGY_SKIP]
        label = " ".join(words[:2])
        # Keep real comparisons only ("toll road"), not fragments ("set giant", "oil value").
        if len(words) < 2 or words[0] in _ANALOGY_NOISE or words[1] in _ANALOGY_NOISE:
            continue
        if label and label not in out:
            out.append(label)
    return out


def analogy_count(text: str) -> int:
    return len(list(_ANALOGY.finditer(_plain(text))))


def _plain(text: str) -> str:
    return (text or "").replace("’", "'").replace("\u2011", "-").replace("\u2010", "-")


_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "the a an and or but of to in on for with at by from as is are was were be it its this that "
    "after over under amid into about says say said new more less than up down".split())


def same_story(a: str, b: str) -> bool:
    """Two headlines about one event (ep22 ran the Saudi oil-storage story twice)."""
    ta = {w for w in _WORD.findall((a or "").lower()) if len(w) > 2 and w not in _STOPWORDS}
    tb = {w for w in _WORD.findall((b or "").lower()) if len(w) > 2 and w not in _STOPWORDS}
    if not ta or not tb:
        return False
    shared = ta & tb
    return len(shared) >= 3 or len(shared) / min(len(ta), len(tb)) >= 0.5


# ── Memory ───────────────────────────────────────────────────────────────────

def load_memory(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text())
        if isinstance(d, dict) and isinstance(d.get("episodes"), list):
            return d
    except Exception:
        pass
    return {"episodes": []}


def save_memory(path: Path, memory: dict, entry: dict) -> dict:
    eps = [e for e in memory.get("episodes", []) if e.get("episode") != entry.get("episode")]
    eps.append(entry)
    eps.sort(key=lambda e: e.get("episode", 0))
    memory = {"episodes": eps[-MEMORY_KEEP:]}
    Path(path).write_text(json.dumps(memory, indent=2))
    return memory


def last_episode(memory: dict, before: int):
    eps = [e for e in memory.get("episodes", []) if e.get("episode", 0) < before]
    return eps[-1] if eps else None


def recent(memory: dict, before: int, n: int = 4) -> list:
    return [e for e in memory.get("episodes", []) if e.get("episode", 0) < before][-n:]
