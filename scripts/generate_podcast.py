#!/usr/bin/env python3
"""
Portfolio Pulse — Weekly Podcast Generator
==========================================
Runs via cron-job.org every Monday at 6:00 AM UTC.

Pipeline:
  1. Load intelligence.json + KV snapshot + live holdings + past scripts
  2. Groq preprocessing call → deep topic registry (topics, tickers, education used)
  3. Groq (Llama 3.3 70B) × 2 → full podcast script (3,500–4,300 words)
  4. edge-tts → MP3 segments per speaker turn
  5. Merge segments → podcast_epNNN.mp3
  6. Save script text → podcast_epNNN.txt (used by future episodes)
  7. Groq summary call → update podcast_meta.json

Voices: en-US-AndrewMultilingualNeural (Alex), en-US-AvaMultilingualNeural (Sam)
Style: NPR/Bloomberg deep-dive — mechanisms, genuine push-back, learning segment
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# Shared with the daily briefing. Added to the path explicitly so it imports whether
# this runs as a script, from another directory, or under a test.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fx_guard import (classify as _fx_classify,            # noqa: E402
                      held_in_cad_reason as _fx_held_in_cad,
                      split_sentences as _split_sentences,
                      scrub_fx_sentences)

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"
# Groq shut down llama-3.3-70b-versatile and llama-3.1-8b-instant on
# 2026-08-16. These are Groq's own recommended replacements.
GROQ_MODEL    = "openai/gpt-oss-120b"
SNAPSHOT_API  = "https://portfolio-pulse-dun.vercel.app/api/snapshot"
SETTINGS_API  = "https://portfolio-pulse-dun.vercel.app/api/settings"
CRON_SECRET   = os.environ.get("CRON_SECRET", "")

# edge-tts voices (Kokoro had persistent install issues — revisit later)
VOICE_ALEX = "en-US-AndrewMultilingualNeural"   # Male — warm, analytical
VOICE_SAM  = "en-US-AvaMultilingualNeural"      # Female — curious, challenges

SPEECH_RATE_SHORT  = "+14%"   # Short reactions < 60 chars
SPEECH_RATE_MEDIUM = "+8%"    # Normal turns 60-200 chars
SPEECH_RATE_LONG   = "+3%"    # Detailed explanations > 200 chars

DATA_DIR     = Path("data")
PODCAST_META = DATA_DIR / "podcast_meta.json"
INTEL_FILE   = DATA_DIR / "intelligence.json"
MAX_EPISODES = 4


# ============================================================
# PORTFOLIO CONTEXT — built dynamically from KV on every run.
# This static fallback is only used if the KV fetch fails.
# ============================================================
_PORTFOLIO_CONTEXT_FALLBACK = """
INVESTOR: Christopher, 24, Toronto. HIGH risk tolerance. Base currency: CAD.
GOAL: a GTA home purchase; the FHSA and RRSP are the down-payment money. Returns to Canada March 2027.

LIVE PORTFOLIO DATA IS UNAVAILABLE THIS WEEK. You therefore have NO dollar figures and NO weights.
Do not state, estimate or illustrate any figure about the portfolio, any holding, or its size. Keep
the episode qualitative: explain the ideas, and say plainly that the numbers are not available.

WHAT WE BROADLY OWN: leveraged 3x funds (FANG+, S&P 500, Dow, semiconductors), big tech including
Nvidia, Broadcom and Taiwan Semi, Canadian banks, and a little energy. Nothing else is owned; ideas
called "picks" are only ideas.

CURRENCY: the base currency is CAD. US-listed holdings are HELD IN US DOLLARS and only translated to
CAD for display. USD/CAD going UP means a stronger US dollar and our US holdings are worth MORE in
Canadian dollars; going DOWN means they are worth LESS. A stronger US dollar is never a "drag".
No lists, no invented statistics, no invented dates.
"""


# ── Tickers that carry 3× leverage (for math anchor calculation) ─────────────
_LEVERAGE_3X = {"FNGU", "SPXL", "UDOW", "TQQQ", "SOXL"}

# Used to compute sector weights for the prompt rather than let the script
# characterise the book from memory.
_SECTORS = {
    "Leveraged 3x": {"FNGU", "SPXL", "UDOW", "TQQQ", "SOXL"},
    "Tech":         {"NVDA", "AVGO", "TSM", "MSFT", "AAPL", "QCOM", "TXF.TO",
                     "NFLX", "MSTR", "BYDDF"},
    "Financials":   {"CM.TO", "RY.TO", "BMO.TO", "IBKR", "V"},
    "Energy":       {"ENB.TO", "ET", "SHEL", "CNQ.TO", "KGS"},
}


# Friendly names for the leveraged funds. The context used to hardcode a list of
# three ("FANG+ 3x, S&P500 3x, Dow 3x") and so left out the semiconductor fund,
# which is a fourth of the sleeve — ep18 then said those three "together make up
# $164,363" when they were about $150K and the semiconductor fund was the rest.
_LEVERAGE_NAMES = {
    "FNGU": "FANG+ 3x", "SPXL": "S&P 500 3x", "UDOW": "Dow 3x",
    "SOXL": "Semiconductor 3x", "TQQQ": "Nasdaq 3x",
}

# Lookup-table steps. The model used to be told to "derive every dollar estimate
# from the anchors", so it did arithmetic on air — and in ep18 it applied the 3x
# leverage twice, pricing a 0.5% market drop at $7,397 in one breath and, minutes
# later, correctly at $2,466. It reads the answer off a table now.
_INDEX_STEPS = (0.5, 1, 2, 3)      # market move, percent
_FX_STEPS    = (1, 2, 3, 5)        # USD/CAD move, cents

DOWN_PAYMENT_TARGET = 90_000       # the FHSA + RRSP HBP goal quoted in the context


def _round_sig(v: float, sig: int = 2) -> float:
    """Round to `sig` significant figures: 14,386 -> 14,000; 1,722 -> 1,700."""
    if not v:
        return 0.0
    from math import floor, log10
    return round(v, -int(floor(log10(abs(v)))) + (sig - 1))


def _say_money(v: float) -> str:
    """A figure as it should be SAID — rounded, no cents, no false precision."""
    return f"${_round_sig(abs(v)):,.0f}"


def _say_pct(p: float) -> str:
    p = abs(p)
    return f"{p:.1f}%" if p < 10 else f"{p:.0f}%"


def _fetch_cash_positions() -> list[dict]:
    """Cash balances from KV. The prompt used to hardcode 'RRSP Cash: ~$7,685 USD
    idle', which was false (the RRSP holds about $2.9K CAD) and was read out as
    fact in ep16 and ep18."""
    try:
        r = requests.get(SETTINGS_API, timeout=10)
        r.raise_for_status()
        cash = r.json().get("cash_positions", []) or []
        print(f"  ✓ {len(cash)} cash positions from KV")
        return cash
    except Exception as exc:
        print(f"  ⚠ cash_positions fetch failed: {exc}")
        return []


def _fetch_computed_holdings() -> list[dict]:
    """Fetch current computed holdings from KV.
    The dashboard saves these on every page load and every transaction change,
    so this always reflects the exact current portfolio state.
    """
    try:
        r = requests.get(SETTINGS_API, timeout=10)
        r.raise_for_status()
        data     = r.json()
        holdings = data.get("computed_holdings", [])
        updated  = data.get("holdings_updated_at", "unknown")
        if holdings:
            print(f"  ✓ {len(holdings)} holdings from KV (updated {updated[:16]})")
        else:
            print("  ⚠ computed_holdings not in KV yet — open dashboard once to populate it")
        return holdings
    except Exception as exc:
        print(f"  ⚠ computed_holdings fetch failed: {exc}")
        return []


WEEK_BASELINE_DAYS = 7


def _week_baseline_date(sorted_dates: list[str]) -> str:
    """The date a week's change should be measured from.

    This used to be sorted_dates[0] — the OLDEST snapshot still retained, which
    is up to 90 days back. Every "weekly" figure was therefore a quarter's move
    wearing a weekly label: the 2026-09-14 episode compared against 2026-06-19,
    an 87-day span, and reported +8,565 CAD when the real week was -7,366.

    Picks the snapshot nearest seven days before the latest one. Ties break
    toward the older date so a week is never understated, and the latest
    snapshot is never chosen as its own baseline unless it is all there is.
    """
    if len(sorted_dates) < 2:
        return sorted_dates[-1]
    latest = datetime.fromisoformat(sorted_dates[-1]).date()
    target = latest - timedelta(days=WEEK_BASELINE_DAYS)
    return min(
        sorted_dates[:-1],
        key=lambda d: (abs((datetime.fromisoformat(d).date() - target).days), d),
    )


def _build_portfolio_context(holdings: list[dict], snapshots: dict,
                             cash: list = None) -> tuple[str, dict]:
    """Build a fully dynamic portfolio context string.
    Combines live share counts (from KV) with live prices (from snapshots)
    to produce exact values, weekly movers, and math anchors.
    Falls back to static context if either source is missing.
    """
    if not holdings or not snapshots:
        # Two values, like every other path — the caller unpacks this. Empty
        # facts switch figure verification off, which is correct here: there is
        # nothing to verify against, and validating a script against numbers we
        # do not have would either pass everything or block the episode.
        return _PORTFOLIO_CONTEXT_FALLBACK, {}

    # A snapshot without holdings_prices carries only account totals — one is
    # written intraday, before the close job fills the prices in. Taking it as
    # `latest` silently zeroes every per-holding figure: leverage, USD exposure,
    # movers and positions all vanish, while total_value still reads correctly
    # and usdcad quietly falls back to a constant. A context in that state told
    # episode 16's listeners, three times, that the portfolio holds no leveraged
    # exposure at all — beside a $149K leveraged book. Only consider snapshots
    # that can actually price the holdings.
    sorted_dates = [d for d in sorted(snapshots)
                    if (snapshots[d] or {}).get("holdings_prices")]
    if not sorted_dates:
        return _PORTFOLIO_CONTEXT_FALLBACK, {}

    latest_date   = sorted_dates[-1]
    latest        = snapshots[latest_date]
    baseline_date = _week_baseline_date(sorted_dates)
    prev          = snapshots[baseline_date]

    prices      = latest.get("holdings_prices", {})
    prev_prices = prev.get("holdings_prices",   {})
    usdcad      = float(latest.get("usdcad")    or 1.38)
    accounts    = latest.get("accounts",        {})
    prev_accts  = prev.get("accounts",          {})
    acct_cost   = latest.get("account_cost",    {})

    leverage_cad = 0.0
    usd_exp_cad  = 0.0
    movers       = []
    positions    = {}   # ticker -> aggregated size across accounts

    for h in holdings:
        ticker = h.get("ticker", "")
        if not ticker or ticker.startswith("CASH"):
            continue
        ccy    = h.get("ccy", "USD")
        shares = float(h.get("shares") or 0)
        if shares <= 0:
            continue

        px    = prices.get(ticker, {})
        price = float(px.get("price") or 0)
        if price <= 0:
            continue

        fx     = usdcad if ccy == "USD" else 1.0
        mv_cad = price * shares * fx

        if ccy == "USD":
            usd_exp_cad += mv_cad
        if ticker in _LEVERAGE_3X:
            leverage_cad += mv_cad

        # Per-holding size, summed across accounts. Scripts asserted position
        # sizes that were out by up to 7.8x ("about $14,000 CAD of Energy
        # Transfer" against a real $1,791) and named the wrong account for them,
        # with nothing in the context to check against.
        pos = positions.setdefault(ticker, {"name": h.get("name", ticker),
                                            "cad": 0.0, "accounts": []})
        pos["cad"] += mv_cad
        acct_name = h.get("account", "")
        if acct_name and acct_name not in pos["accounts"]:
            pos["accounts"].append(acct_name)

        # Weekly price move → dollar impact on this exact position
        ppx    = prev_prices.get(ticker, {})
        pprice = float(ppx.get("price") or price)
        wk_pct = (price - pprice) / pprice * 100 if pprice > 0 else 0.0
        wk_cad = (price - pprice) * shares * fx

        movers.append({
            "name":   h.get("name", ticker),
            "ticker": ticker,
            "acct":   h.get("account", ""),
            "wk_pct": wk_pct,
            "wk_cad": wk_cad,
        })

    # One ticker is held in several accounts, so the per-holding list put the
    # same ETF in the top six three times over (ep014's movers were FNGU, FNGU,
    # Nvidia, FNGU, Tesla, Broadcom). That crowded out genuinely distinct movers
    # and invited the script to count one ETF's move repeatedly. Merge first: the
    # percentage move is a property of the price, so it is shared, and only the
    # dollar impact adds up.
    by_ticker = {}
    for m in movers:
        agg = by_ticker.get(m["ticker"])
        if agg is None:
            by_ticker[m["ticker"]] = {"name": m["name"], "ticker": m["ticker"],
                                      "accts": [m["acct"]] if m["acct"] else [],
                                      "wk_pct": m["wk_pct"], "wk_cad": m["wk_cad"]}
        else:
            agg["wk_cad"] += m["wk_cad"]
            if m["acct"] and m["acct"] not in agg["accts"]:
                agg["accts"].append(m["acct"])

    merged = list(by_ticker.values())
    for m in merged:
        m["acct"] = ", ".join(m["accts"])
    merged.sort(key=lambda x: abs(x["wk_cad"]), reverse=True)
    top_movers = merged[:6]

    # Portfolio-level totals come from the snapshot (calculated by the same
    # market API the dashboard uses — guaranteed to match what the user sees)
    total_val = float(latest.get("total_value") or 0)
    roi_pct   = float(latest.get("roi_pct")     or 0)
    wk_start  = float(prev.get("total_value")   or total_val)
    wk_gain   = total_val - wk_start
    wk_pct    = (wk_gain / wk_start * 100) if wk_start > 0 else 0.0

    # Prices can be present and still partial — SOXL had no price for fifteen
    # straight days in August, quietly dropping a whole sleeve from the anchors.
    # Account-level cash explains roughly 3.5% of the total; far below that and
    # the context is understating exposure with nothing on the surface to show
    # it, so fall back rather than publish confident, incomplete anchors.
    priced_cad = sum(p["cad"] for p in positions.values())
    if total_val > 0 and priced_cad < total_val * 0.80:
        print(f"  ⚠ only ${priced_cad:,.0f} of ${total_val:,.0f} could be priced — "
              f"portfolio context would understate exposure; using fallback")
        return _PORTFOLIO_CONTEXT_FALLBACK, {}

    # ── Cash ─────────────────────────────────────────────────────────────────
    cash_total, cash_by_acct = 0.0, {}
    for c in (cash or []):
        cad = float(c.get("amount") or 0) * (usdcad if c.get("ccy") == "USD" else 1.0)
        cash_total += cad
        a = c.get("account") or "?"
        cash_by_acct[a] = cash_by_acct.get(a, 0.0) + cad

    # ── Lookup tables ────────────────────────────────────────────────────────
    per_1pct_sp  = leverage_cad * 0.03   # 3x leverage: ALREADY includes the multiplier
    # usd_exp_cad is already in CAD. A 1-cent move in USD/CAD acts on the USD
    # NOTIONAL, so the swing is notional x 0.01 — not the CAD value x 0.01.
    usd_notional = (usd_exp_cad / usdcad) if usdcad else 0.0
    per_1cent_fx = usd_notional * 0.01
    lev_pct      = leverage_cad / total_val * 100 if total_val else 0
    usd_pct      = usd_exp_cad  / total_val * 100 if total_val else 0
    index_table  = {str(x): round(per_1pct_sp * x) for x in _INDEX_STEPS}
    fx_table     = {str(c): round(per_1cent_fx * c) for c in _FX_STEPS}

    # Computed so the script never has to characterise the book from memory.
    # Episode 16 called this "a portfolio that's half-energy, half-tech" when
    # energy was about 3.5% of it.
    sector_cad = {}
    for tkr, p in positions.items():
        for sector, members in _SECTORS.items():
            if tkr in members:
                sector_cad[sector] = sector_cad.get(sector, 0.0) + p["cad"]
                break
        else:
            sector_cad["Other"] = sector_cad.get("Other", 0.0) + p["cad"]
    _tot_for_pct = sum(p["cad"] for p in positions.values()) or 1.0
    sector_line = " | ".join(
        f"{s} {v / _tot_for_pct * 100:.0f}%" for s, v in
        sorted(sector_cad.items(), key=lambda kv: -kv[1]))
    held_line   = ", ".join(sorted(positions.keys()))
    n_positions = len(positions)
    lev_funds   = [_LEVERAGE_NAMES.get(t, positions[t]["name"])
                   for t in sorted(positions) if t in _LEVERAGE_3X]
    lev_names   = ", ".join(lev_funds) or "none"

    # State the real span. The old label said "Weekly change" regardless of how
    # far back the baseline actually was, which is how an 87-day move reached
    # the script as a weekly one.
    span_days = (datetime.fromisoformat(latest_date).date()
                 - datetime.fromisoformat(baseline_date).date()).days
    period    = f"{baseline_date} → {latest_date} ({span_days} days)"

    wk_dir = "up" if wk_gain >= 0 else "down"
    def _mline(m):
        return (f"  {_LEVERAGE_NAMES.get(m['ticker']) or COMPANY_NAMES.get(m['ticker'], m['name'])}: "
                f"{'up' if m['wk_pct'] >= 0 else 'down'} about {_say_pct(m['wk_pct'])} "
                f"(about {_say_money(m['wk_cad'])} {'gained' if m['wk_cad'] >= 0 else 'lost'} for us)")

    # The recap asks for the biggest mover AND one that fell, but the top movers by
    # dollars are usually all risers, so the model had nothing to name and made a
    # reason up. State the real biggest faller explicitly.
    fallers = sorted((m for m in merged if m["wk_cad"] < 0), key=lambda m: m["wk_cad"])
    faller  = fallers[0] if fallers else None
    movers_str = "\n".join(_mline(m) for m in top_movers[:3]) or "  (price moves unavailable this week)"
    if faller is not None and faller not in top_movers[:3]:
        movers_str += "\nBiggest faller:\n" + _mline(faller)
    elif faller is None:
        movers_str += "\n  (Nothing of note fell this week.)"
    cash_line = (f"Cash (not invested): about {_say_money(cash_total)} CAD in total — a small slice"
                 if cash else "Cash balances: not provided — never state one.")
    idx_line = " | ".join(f"{x}% move -> about {_say_money(v)}"
                          for x, v in zip(_INDEX_STEPS, index_table.values()))
    fx_line  = " | ".join(f"{c} cent{'s' if c > 1 else ''} -> about {_say_money(v)}"
                          for c, v in zip(_FX_STEPS, fx_table.values()))

    # The prompt used to carry hand-typed facts here: "RRSP Cash: ~$7,685 USD
    # idle", "Nvidia +1,776%", a three-fund leveraged list, "implied beta 1.8x".
    # Every one was stale or false, and ep16 and ep18 read them out as fact. This
    # block is computed; nothing in it is typed by hand except the investor's
    # circumstances.
    text = f"""INVESTOR: Christopher, 24, Toronto. HIGH risk tolerance. Base currency: CAD.
GOAL: a GTA home purchase. The FHSA and the RRSP are the down-payment money (a target of about $90K). Returns to Canada in March 2027.
Nvidia is a permanent hold in the TFSA.

━━━ THIS WEEK — already rounded; the only portfolio numbers you may say ━━━
Period:              {period}
Portfolio value:     about {_say_money(total_val)} CAD
This week:           {wk_dir} about {_say_money(wk_gain)} ({wk_dir} about {_say_pct(wk_pct)})
Since the start:     up about {roi_pct:.0f}% in total
Leveraged 3x funds:  {lev_names} — about {_say_money(leverage_cad)} together, about {lev_pct:.0f}% of the portfolio
US-dollar holdings:  about {_say_money(usd_notional)} US dollars' worth
{cash_line}
Biggest movers this week (mention at most two):
{movers_str}

━━━ HOW MUCH THINGS MOVE — read the answer off this table, NEVER calculate ━━━
If the stock market moves by this much, our leveraged funds move 3x as much, and the effect on the portfolio is:
  {idx_line}
  (This ALREADY includes the 3x leverage — never multiply it by 3 again. Same amount up or down.)
If USD/CAD moves by this many cents (it is about {usdcad:.2f} now), the effect on our US-dollar holdings is:
  {fx_line}
  (Same amount up or down.)
These are two SEPARATE effects. Never add them, subtract one from the other, or give a "net" figure. If you mention both, state each on its own.

━━━ CURRENCY — read carefully; this has been wrong before ━━━
Our base currency is CAD. Every US-listed holding (the leveraged funds, Nvidia, Tesla, ...) is HELD AND PRICED IN US DOLLARS. The Canadian-dollar numbers in the app are only a translation for display. NEVER say we hold them in Canadian dollars.
USD/CAD going UP (say 1.41 to 1.45) = the US dollar got STRONGER and the Canadian dollar WEAKER = our US holdings are worth MORE in Canadian dollars. That is GOOD for us.
USD/CAD going DOWN = the Canadian dollar got stronger = our US holdings are worth LESS in Canadian dollars. That is the currency RISK for us.
Say it like this: "When the US dollar gets stronger against the Canadian dollar, our US holdings are worth more in Canadian dollars, and the other way around."
A stronger US dollar is NEVER a "drag" or "headwind" on the Canadian-dollar value of our US holdings. (Separately, a strong dollar can weigh on US stock PRICES — if you mention that, say it is about stock prices, not about our currency translation.)
Canadian-listed holdings (.TO) are priced in CAD and ignore the exchange rate.

━━━ WHAT WE OWN ({n_positions} positions) ━━━
{held_line}
Weights: {sector_line}
Anything not on that list is NOT owned. Ideas called "picks" (for example Novo Nordisk or Toronto-Dominion) are only ideas.

━━━ NUMBER BUDGET — strict ━━━
- At most 3 numbers in any one turn; about 12 per half; never more than 18.
- Use only numbers from this page and from the news items. Round as shown: say "about fourteen thousand dollars", never "$14,386".
- No arithmetic on air: no "4 times 1,722", no "net effect", no adding or subtracting two dollar impacts.
- Prefer words to numbers: "a big week", "roughly half the portfolio", "a small slice".
- Never invent a statistic, price, yield, VIX level, earnings result, announcement, date or cause. If it is not on this page or in the news, explain the idea without it."""

    # Machine-checkable record of every portfolio-level figure the model was
    # given. verify_script_figures() validates the finished script against this,
    # because the prompt's "do not cite figures not present in this context"
    # instruction was not honoured: ep016 asserted +23,927 CAD (+8.6%) when
    # neither number appeared anywhere in the text above.
    facts = {
        "latest_date":   latest_date,
        "baseline_date": baseline_date,
        "span_days":     span_days,
        "total_value":   round(total_val),
        "wk_gain":       round(wk_gain),
        "wk_pct":        round(wk_pct, 2),
        "roi_pct":       round(roi_pct, 2),
        "usdcad":        round(usdcad, 4),
        "leverage_cad":  round(leverage_cad),
        "usd_exposure":  round(usd_exp_cad),
        "accounts": {a: round(float(accounts.get(a) or 0))
                     for a in ("TFSA", "Investment", "FHSA", "RRSP")},
        "account_change": {
            a: round(float(accounts.get(a) or 0)
                     - float(prev_accts.get(a) or accounts.get(a) or 0))
            for a in ("TFSA", "Investment", "FHSA", "RRSP")
        },
        "movers": [{"ticker": m["ticker"], "pct": round(m["wk_pct"], 2),
                    "cad": round(m["wk_cad"])}
                   for m in top_movers + ([faller] if faller is not None and faller not in top_movers else [])],
        "usd_notional": round(usd_notional),
        "index_table": index_table,
        "fx_table": fx_table,
        "cash_total": round(cash_total),
        "cash_by_account": {a: round(v) for a, v in cash_by_acct.items()},
        "leverage_funds": lev_funds,
        "down_payment_target": DOWN_PAYMENT_TARGET,
        # Switches on the strict "a dollar figure nobody supplied" check.
        "strict_figures": True,
        "positions": {t: {"name": p["name"], "cad": round(p["cad"]),
                          "accounts": list(p["accounts"])}
                      for t, p in positions.items()},
    }
    return text, facts

# Company name lookup for the script (tickers → names, for reference)
COMPANY_NAMES = {
    "FNGU": "FANG+ 3x ETF", "SPXL": "S&P 500 3x ETF", "UDOW": "Dow 3x ETF",
    "NVDA": "Nvidia", "AVGO": "Broadcom", "TSM": "Taiwan Semiconductor",
    "TXF.TO": "CI Tech Giants ETF", "MSFT": "Microsoft", "AAPL": "Apple",
    "QCOM": "Qualcomm", "TSLA": "Tesla", "IBKR": "Interactive Brokers",
    "CM.TO": "CIBC", "RY.TO": "Royal Bank", "BMO.TO": "Bank of Montreal",
    "ENB.TO": "Enbridge", "ET": "Energy Transfer", "SHEL": "Shell",
    "MSTR": "MicroStrategy", "GBTC": "Grayscale Bitcoin Trust",
    "BYDDF": "BYD", "V": "Visa", "LYV": "Live Nation",
    "ZSP.TO": "BMO S&P 500 ETF",
}


# ============================================================
# SCRIPT PROMPT — PART 1: Welcome + Recap + Deep Dive 1
# ============================================================
SCRIPT_PROMPT_PART1 = """You are writing the FIRST HALF of a weekly podcast, "Portfolio Pulse Weekly", for one listener: a smart beginner who owns this portfolio. Two hosts: ALEX explains; SAM asks the questions a beginner would ask.
{editor_notes}
EPISODE DATE: {today}     TRADING WEEK: {week_range}     MARKET MOOD: {mood}

{registry_context}

━━━ THE NEWS — your ONLY source of facts about the outside world ━━━
{briefing_note}Outlook: {outlook}

Macro themes:
{macro}

Market news:
{news}

━━━ THE PORTFOLIO — your ONLY source of facts about the portfolio ━━━
{portfolio}

━━━ THIS EPISODE'S FIXED SUBJECTS ━━━
Deep Dive 1: {dd1_title}
{dd1_brief}
Deep Dive 2 (the second half writes it): {dd2_title}
Learning segment (the second half writes it): {education_topic}
In the opening agenda name all three, exactly as written above. Never swap one for something else — the second half is already committed to them.

━━━ HOW TO TALK ━━━
Plain, calm and conversational — a smart friend explaining it over coffee. Short sentences, one idea at a time. Explain IDEAS and cause-and-effect, not figures. If a sentence works without a number, leave the number out.
Use ONE everyday analogy for the whole episode — not two.

━━━ THE FACT RULES (these matter more than anything else) ━━━
1. State only facts that appear above: the portfolio page, the lookup tables, and the news items. Never invent a statistic, price, yield, VIX level, earnings result, company announcement or cause. If you have no reason for a move, say "it moved with the market" — do not make one up.
2. Never do arithmetic on air. No multiplying, adding, subtracting or "netting" dollar figures. Read any dollar impact straight off the lookup table. The market table ALREADY includes the 3x leverage — never apply it again.
3. Round: "about fourteen thousand dollars", never "$14,386".
4. Call something a holding only if it is under "WHAT WE OWN". "Picks" are ideas, not owned.
5. Currency: follow the CURRENCY box exactly. We hold US assets in US dollars, never in Canadian dollars. A stronger US dollar RAISES the Canadian-dollar value of our US holdings.
6. No lists. Never write bullet points, numbered lists, or a sentence ending in a colon that introduces a list — say "first… second… third…" in full sentences. Every line starts with "ALEX:" or "SAM:".
7. No dates unless they appear in the news above. Otherwise say "at the next Fed meeting" or "when the next inflation report comes out".

━━━ STRUCTURE ━━━
[WELCOME — 45 seconds] ALEX welcomes listeners to Portfolio Pulse Weekly, introduces himself and Sam, then gives the agenda naming the three subjects above. End with ONE sentence hook — the most surprising idea in this week's story.

[RECAP — 2 minutes] The week in plain words. Say once whether the portfolio was up or down and by about how much. Name the biggest mover and the biggest faller listed on the portfolio page. Give a reason for a move ONLY if the news above supplies it — otherwise say honestly that it moved with the market. At most 3 numbers in the whole recap. One short callback to last episode, only if natural.

[DEEP DIVE 1 — about 5 minutes] on {dd1_title}.
- SAM opens with the puzzle.
- ALEX explains the mechanism in plain English, step by step.
- SAM pushes back twice, the way a beginner would ("hang on — why would that happen?").
- ALEX re-explains more simply each time.
- Connect it to a holding we actually own, by name.
- Look ahead: what we would watch next — only events named in the news above, or the TYPE of event with no invented date.

DIALOGUE: company names, not tickers. A quarter of turns under 20 words. Natural reactions ("Right.", "Hmm.", "Okay but…"). No two turns start with the same word. Avoid: "it's worth noting", "going forward", "as mentioned", "at the end of the day", "in today's market", "landscape", "navigate", "tailwinds", "headwinds".

LENGTH: aim for 1,300 to 1,800 words. If you run short, explain the mechanism more slowly with a simple everyday example. NEVER add dates, events, figures or details just to fill time.

THIS IS THE FIRST HALF ONLY — DO NOT CLOSE THE EPISODE.
Part 2 is written separately and is joined directly onto your final line, in the same episode. It contains Deep Dive 2, the learning segment, the scenarios and the closing. So do NOT write a sign-off, a wrap-up, "stay tuned", or "we'll be back next Monday". Stop mid-conversation on a Deep Dive 1 line so the second half continues straight out of it.

Write PART 1 now:"""


SCRIPT_PROMPT_PART2 = """You are writing the SECOND HALF of "Portfolio Pulse Weekly" for {today}. ALEX explains; SAM asks the questions a beginner would ask.
{editor_notes}
PART 1 IS ALREADY WRITTEN AND IS JOINED DIRECTLY ONTO YOUR FIRST LINE. The listener has just heard it:
{dive1_summary}

YOU ARE CONTINUING ONE EPISODE, NOT STARTING ANYTHING.
- Do NOT greet the listener or say "welcome back".
- Do NOT open with "let's pick up where we left off" or recap what Part 1 covered.
- Do NOT say the hosts "just walked through" or "just discussed" a topic unless it appears above as something actually discussed.
Begin directly with Deep Dive 2's hook.

━━━ THE NEWS — your ONLY source of facts about the outside world ━━━
{briefing_note}{news}

SUGGESTIONS from the briefing. These are NOT decisions, rules or plans we have made, and none of the
companies are owned. Never say "we have a rule", "we earmarked" or "our plan is" about them — at
most, "one idea floating around is…". Ideas:
{picks}
{strategy}

━━━ THE PORTFOLIO — your ONLY source of facts about the portfolio ━━━
{portfolio}

━━━ THIS HALF'S FIXED SUBJECTS ━━━
Deep Dive 2: {dd2_title}
{dd2_brief}
Learning segment: {education_topic}

━━━ THE FACT RULES (these matter more than anything else) ━━━
1. State only facts that appear above. Never invent a statistic, price, yield, VIX level, earnings result, company announcement, buyback, date or cause. For Deep Dive 2 the ONLY facts you may state about the company are in the brief; beyond that, explain how that kind of business works, in general terms.
2. Never do arithmetic on air. Read any dollar impact straight off the lookup table; the market table ALREADY includes the 3x leverage.
3. Round: "about fourteen thousand dollars".
4. Call something a holding only if it is under "WHAT WE OWN". Picks are ideas, not owned. If a company IS owned, never say it isn't.
5. Currency: follow the CURRENCY box exactly. We hold US assets in US dollars. A stronger US dollar RAISES the Canadian-dollar value of our US holdings.
6. No lists, no bullets, no sentence ending in a colon introducing a list. Every line starts with "ALEX:" or "SAM:" — except the [EDUCATION_TOPIC] marker line.
7. No dates unless they appear in the news above.
NUMBER BUDGET: at most 3 numbers in any one turn and about 12 in this half.

Use plain, calm, conversational language — a smart friend over coffee. Do not introduce a second analogy (one was used in Part 1). Company names, not tickers. No two turns start with the same word. Avoid: "it's worth noting", "going forward", "as mentioned", "at the end of the day", "in today's market", "landscape", "navigate", "tailwinds", "headwinds".

━━━ STRUCTURE ━━━
[DEEP DIVE 2 — about 4 minutes] on {dd2_title}.
- ALEX introduces it with a hook; SAM asks why it matters for us right now.
- ALEX explains how this business works and what the news means for it, using only the brief. SAM pushes back at least once.
- Say how big the holding is in plain terms, using the size in the brief ("a small slice, about 2% of the portfolio").
- Close by saying what we would watch next — only an event named in the news, or the type of event.

[LEARNING SEGMENT — about 2 minutes]
THE TOPIC IS ALREADY CHOSEN: {education_topic}. Write exactly that topic — the first half has already told the listener, by name, that this is what the segment covers.
(Covered in past episodes, not to be repeated: {education_topics_used})
Place this marker on its own line immediately BEFORE the segment starts (no speaker prefix; it is never read aloud):
[EDUCATION_TOPIC: {education_topic}]
Then:
- ALEX or SAM introduces: "Before we get to our scenarios, let's step back and learn something…"
- Teach the idea in plain language, with one simple everyday example using small round numbers (like $100), never this portfolio's figures.
- Use NO current market data — no yields, no index levels, no VIX numbers, no "historically, X% of the time" statistics.
- Connect it briefly to this investor where it fits. SAM asks one "but what does that actually mean in practice?" question.
- Close with: "Alright, that's our learning segment for this week. On to scenarios…"

[SCENARIOS — about 90 seconds]
Give exactly THREE cases for the next few weeks, as three separate turns, in this form:
  "Base case — 50 percent: [one plain sentence on what happens]. For us that is about [one figure from the lookup table, or the word 'small']."
  "Bull case — 30 percent: …"
  "Bear case — 20 percent: …"
Probabilities add to 100; the base case is 45 to 55. Each case moves ONE lever only — the stock market OR the currency, never both — so there is nothing to net. The bear case's effect must be at least as large as the bull case's. Read each figure straight off the lookup table; do not calculate. Never skip a case.

[CLOSING — about 60 seconds]
- ONE open question that leaves the listener thinking about investing itself, not about data.
- "One thing we're watching next week" — only an event named in the news above, or the type of event.
- A warm sign-off and a brief tease for next week.

LENGTH: aim for 1,200 to 1,700 words. If you run short, explain the ideas more slowly with a simple everyday example. NEVER add dates, events, figures or details just to fill time.

Write PART 2 now:"""


# ============================================================
# DATA HELPERS
# ============================================================
def _last_trading_day(ref: datetime) -> str:
    d = ref
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%A, %B %d, %Y")


def _week_trading_range(ref: datetime) -> str:
    mon = ref - timedelta(days=ref.weekday())
    fri = mon + timedelta(days=4)
    return f"{mon.strftime('%b %d')} – {fri.strftime('%b %d, %Y')}"


def _load_intel() -> dict:
    try:
        if INTEL_FILE.exists():
            return json.loads(INTEL_FILE.read_text())
    except Exception:
        pass
    return {}


def _fetch_snapshot() -> dict:
    try:
        r = requests.get(SNAPSHOT_API, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# ============================================================
# PAST EPISODE ANALYSIS — full script reading & topic registry
# ============================================================

def _load_past_scripts() -> dict:
    """Load all saved episode scripts from disk. Returns {episode_num: script_text}."""
    scripts = {}
    for path in sorted(DATA_DIR.glob("podcast_ep*.txt")):
        m = re.match(r"podcast_ep(\d+)\.txt$", path.name)
        if m:
            try:
                text = path.read_text(encoding="utf-8")
                scripts[int(m.group(1))] = text
                print(f"  ✓ Loaded script: {path.name} ({len(text.split()):,} words)")
            except Exception as exc:
                print(f"  ⚠ Could not load {path.name}: {exc}")
    return scripts


def _build_deep_topic_registry(old_meta: dict, past_scripts: dict, api_key: str) -> dict:
    """Build a structured topic registry from past episodes.
    Uses full scripts where available, meta summaries as fallback.
    Returns a dict with registry_text, recently_spotlighted_tickers, education_topics_used.
    """
    all_eps = []
    if old_meta.get("episode"):
        all_eps.append({
            "episode":         old_meta["episode"],
            "title":           old_meta.get("title", ""),
            "date":            old_meta.get("date", ""),
            "script":          past_scripts.get(old_meta["episode"], ""),
            "summary":         old_meta.get("summary", {}),
            "education_topic": old_meta.get("education_topic", ""),
        })
    for ep in old_meta.get("archive", [])[:MAX_EPISODES - 1]:
        ep_num = ep.get("episode", 0)
        all_eps.append({
            "episode":         ep_num,
            "title":           ep.get("title", ""),
            "date":            ep.get("date", ""),
            "script":          past_scripts.get(ep_num, ""),
            "summary":         ep.get("summary", {}),
            "education_topic": ep.get("education_topic", ""),
        })

    if not all_eps:
        return {
            "registry_text":               "No previous episodes — fresh start, no callbacks needed.",
            "recently_spotlighted_tickers": [],
            "education_topics_used":        [],
        }

    # ── Extract structured registry from full scripts via Groq ──────────────
    scripts_available = [(e["episode"], e["script"]) for e in all_eps if e["script"].strip()]
    extracted_registry = {}

    if scripts_available:
        print(f"  ✓ Extracting topic registry from {len(scripts_available)} saved script(s)...")
        combined = ""
        for ep_num, script_text in scripts_available:
            ep_meta = next((e for e in all_eps if e["episode"] == ep_num), {})
            combined += (f"\n\n### EPISODE {ep_num}: \"{ep_meta.get('title','')}\" "
                         f"({ep_meta.get('date','')})\n{script_text}")

        extract_prompt = f"""Read these past podcast episodes and extract a topic registry.
Return ONLY valid JSON — no explanation, no markdown fences.

{combined}

Return this exact JSON structure:
{{
  "episodes": [
    {{
      "episode": <number>,
      "topics_deep_dived": ["specific topic with brief description", ...],
      "tickers_spotlighted": ["TICKER", ...],
      "watch_items_stated": ["specific thing to watch mentioned in this episode", ...],
      "scenarios_bull": "brief description of bull scenario stated",
      "scenarios_bear": "brief description of bear scenario stated"
    }}
  ]
}}"""
        try:
            raw   = _groq_call(api_key, extract_prompt, "Topic registry extraction", max_tokens=1500)
            start = raw.find('{')
            end   = raw.rfind('}') + 1
            if start >= 0:
                parsed = json.loads(raw[start:end])
                for ep_data in parsed.get("episodes", []):
                    extracted_registry[ep_data["episode"]] = ep_data
        except Exception as exc:
            print(f"  ⚠ Registry extraction failed (using meta summaries): {exc}")

    # ── Build per-episode registry (extracted or summary fallback) ───────────
    registry_episodes = []
    for ep in all_eps:
        ep_num = ep["episode"]
        if ep_num in extracted_registry:
            reg = extracted_registry[ep_num]
        else:
            s = ep.get("summary", {})
            inferred_tickers = []
            for item in (s.get("position_spotlight") or []):
                for ticker, name in COMPANY_NAMES.items():
                    if ticker in item or name.lower() in item.lower():
                        if ticker not in inferred_tickers:
                            inferred_tickers.append(ticker)
            reg = {
                "episode":          ep_num,
                "topics_deep_dived": (s.get("market_context") or [])[:3],
                "tickers_spotlighted": inferred_tickers,
                "watch_items_stated": (s.get("watch_list") or [])[:2],
                "scenarios_bull":   "",
                "scenarios_bear":   "",
            }
        registry_episodes.append({
            "episode":         ep_num,
            "title":           ep["title"],
            "date":            ep["date"],
            "education_topic": ep["education_topic"],
            "registry":        reg,
        })

    registry_episodes.sort(key=lambda x: x["episode"], reverse=True)

    # ── Derived outputs ──────────────────────────────────────────────────────
    recently_spotlighted = []
    for ep_data in registry_episodes[:3]:
        for t in ep_data["registry"].get("tickers_spotlighted", []):
            if t and t not in recently_spotlighted:
                recently_spotlighted.append(t)

    education_topics_used = [
        ep_data["education_topic"]
        for ep_data in registry_episodes
        if ep_data.get("education_topic")
    ]

    # ── Human-readable registry text ─────────────────────────────────────────
    lines = ["=== PAST EPISODE TOPIC REGISTRY — you MUST read and respect this ==="]
    for ep_data in registry_episodes[:3]:
        reg = ep_data["registry"]
        lines.append(f"\n📌 Episode {ep_data['episode']}: \"{ep_data['title']}\" ({ep_data['date']})")
        topics  = reg.get("topics_deep_dived",  [])
        tickers = reg.get("tickers_spotlighted", [])
        watch   = reg.get("watch_items_stated",  [])
        bull    = reg.get("scenarios_bull", "")
        bear    = reg.get("scenarios_bear", "")
        if topics:
            lines.append(f"  Themes covered in depth: {' | '.join(str(t)[:100] for t in topics[:3])}")
        if tickers:
            lines.append(f"  Tickers spotlighted:     {', '.join(tickers[:6])}")
        if watch:
            lines.append(f"  Said to watch:           {' | '.join(str(w)[:80] for w in watch[:2])}")
        if bull:
            lines.append(f"  Bull scenario stated:    {bull[:120]}")
        if bear:
            lines.append(f"  Bear scenario stated:    {bear[:120]}")
        if ep_data.get("education_topic"):
            lines.append(f"  Education segment:       {ep_data['education_topic']}")

    lines.append("\nTOPIC FRESHNESS RULES:")
    lines.append("1. Do NOT use any of the above 'Themes covered in depth' as a Deep Dive topic this week.")
    lines.append("2. If a past 'Said to watch' item developed into news THIS WEEK, you MAY reference it —")
    lines.append("   but ONLY if there is material NEW information, and keep it brief.")
    lines.append("3. Make exactly ONE natural callback to a past episode. One. Not two. One.")

    return {
        "registry_text":               "\n".join(lines),
        "recently_spotlighted_tickers": recently_spotlighted,
        "education_topics_used":        education_topics_used,
    }


def _extract_education_topic(script: str) -> str:
    """Parse [EDUCATION_TOPIC: topic name] marker embedded in script."""
    m = re.search(r'\[EDUCATION_TOPIC:\s*([^\]]+)\]', script, re.IGNORECASE)
    return m.group(1).strip() if m else ""


# The learning-segment topic used to be chosen by Part 2, which runs AFTER Part 1
# has already announced the week's agenda. Episode 16 promised "forward-contract
# roll yields" in its opening and delivered "Short Interest Signals" — not a
# model error so much as a guaranteed consequence of the ordering. Choosing here,
# before either half is written, and handing the same topic to both makes the
# mismatch impossible rather than merely discouraged.
EDUCATION_TOPICS = [
    "3x ETF Daily Reset Decay",
    "Yield Curve Recession Signals",
    "What VIX Actually Measures",
    "Earnings Revisions Move Prices",
    "P/E Ratios In Practice",
    "Sector Rotation Through Cycles",
    "Short Interest Signals",
    "Insider Buying And Selling Data",
    "Implied Volatility Basics",
    "The Fed Balance Sheet And QT",
    "Currency Carry Trade Unwinds",
    "Reading 13F Filings",
    "Book Value Versus Market Cap",
    "Dividend Compounding Mathematics",
    "Leverage Ratio Versus Leverage Risk",
]


def _choose_education_topic(education_used: list) -> str:
    """First topic not yet covered; rotates once the list is exhausted."""
    used = {str(t).strip().lower() for t in (education_used or []) if t}

    def seen(topic: str) -> bool:
        # The registry records the topic as the model worded it ("Currency Carry
        # Trade"), which need not equal our list's wording ("Currency Carry Trade
        # Unwinds"), so match either way round rather than exactly.
        t = topic.lower()
        return any(t == u or (len(u) >= 6 and (u in t or t in u)) for u in used)

    for topic in EDUCATION_TOPICS:
        if not seen(topic):
            return topic
    return EDUCATION_TOPICS[len(used) % len(EDUCATION_TOPICS)]


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

_WEEKDAY_CLAIM = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
    r"(" + "|".join(_MONTHS) + r")\s+(\d{1,2})\b", re.I)


def _fix_weekday_claims(script: str, ref: datetime) -> tuple:
    """Correct weekday names that do not match their date.

    Episode 16 pointed at "the EIA report due Wednesday, September 18" when the
    18th was a Friday. The date is arithmetic, so this is repaired rather than
    flagged — a listener acting on the wrong day is a real cost, and refusing to
    publish over it would be disproportionate.
    """
    fixes = []

    def repl(m):
        stated, month, day = m.group(1), m.group(2), int(m.group(3))
        try:
            idx = [mo.lower() for mo in _MONTHS].index(month.lower()) + 1
            dt_ = datetime(ref.year, idx, day)
        except ValueError:
            return m.group(0)
        # A date far behind the run date is next year's (December -> January).
        if (dt_.date() - ref.date()).days < -180:
            try:
                dt_ = datetime(ref.year + 1, idx, day)
            except ValueError:
                return m.group(0)
        correct = dt_.strftime("%A")
        if correct.lower() == stated.lower():
            return m.group(0)
        fixes.append(f"{stated} {month} {day} -> {correct}")
        return m.group(0).replace(stated, correct, 1)

    return _WEEKDAY_CLAIM.sub(repl, script), fixes


def _clean_transcript(script: str) -> str:
    """Keep only spoken turns and the education marker.

    parse_script already drops anything that is not a speaker line, so section
    headers never reached the audio — but the raw script is what gets written to
    podcast_epNNN.txt, so "**PORTFOLIO RECAP**", stray "---" rules and a literal
    "**Metaphor** -" label were visible to anyone reading the transcript, and
    were fed back in as context when building the topic registry.
    """
    kept = []
    for line in script.split("\n"):
        s = line.strip()
        if s.startswith(("ALEX:", "SAM:")) or s.upper().startswith("[EDUCATION_TOPIC:"):
            kept.append(s)
    return "\n".join(kept) + "\n"


def _speaker_run_warnings(script: str, limit: int = 3) -> list:
    """Report runs of consecutive turns by one host. Reported, never rewritten —
    merging or reassigning dialogue would change meaning."""
    speakers = [l.strip()[:4].rstrip(":") for l in script.split("\n")
                if l.strip().startswith(("ALEX:", "SAM:"))]
    warnings, run, prev = [], 1, None
    for s in speakers:
        if s == prev:
            run += 1
            if run == limit:
                warnings.append(f"{s} speaks {limit}+ turns in a row")
        else:
            run, prev = 1, s
    return warnings


# ============================================================
# GROQ SCRIPT GENERATION
# ============================================================
def _groq_call(api_key: str, prompt: str, label: str, max_tokens: int = 4096) -> str:
    """Call Groq with patience for the free-tier per-minute token (TPM) budget.

    The big script-generation prompts momentarily exceed Groq's free-tier TPM
    limit, which returns HTTP 429 with a Retry-After header. The correct response
    is to WAIT the server-specified interval on the large 70B model — NOT to flip
    to the small 8B model, which rejects these prompts with 413 (its per-request
    cap is lower). The 8B model is therefore used only as a genuine outage
    fallback (5xx / connection errors), never for rate limits or payload size.
    This preserves output quality (same model, same prompt) while surviving the
    free-tier rate window that previously aborted the run.
    """
    import time
    primary      = GROQ_MODEL
    fallback     = "openai/gpt-oss-20b"
    max_attempts = 8
    last_err     = "unknown"

    for attempt in range(max_attempts):
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": primary, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens, "temperature": 0.85,
                      # gpt-oss are reasoning models and their chain of thought
                      # spends the same max_tokens budget, which silently
                      # truncates the script rather than raising. This wants
                      # prose, not deliberation.
                      "reasoning_effort": "low", "include_reasoning": False},
                timeout=120,
            )

            # 429 = per-minute token budget hit. Wait it out on the SAME model.
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After") or r.headers.get("retry-after")
                try:
                    wait = int(float(retry_after)) if retry_after else 0
                except (TypeError, ValueError):
                    wait = 0
                if wait <= 0:
                    wait = min(70, 10 * (attempt + 1))
                wait += 2  # small cushion past the rolling window
                last_err = "429 rate-limited"
                print(f"  ⚠ {label} attempt {attempt+1}: 429 on {primary} — waiting {wait}s for TPM window")
                time.sleep(wait)
                continue

            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                print(f"  ✓ {label} with {primary} ({len(text.split()):,} words)")
                return text
            last_err = "empty response"
            print(f"  ⚠ {label} attempt {attempt+1}: empty response")
            time.sleep(min(30, 5 * (attempt + 1)))

        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            last_err = f"HTTP {code}"
            print(f"  ⚠ {label} attempt {attempt+1} failed: {exc}")
            # Only a Groq-side outage (5xx) justifies trying the smaller model.
            if code is not None and 500 <= code < 600:
                try:
                    r2 = requests.post(
                        GROQ_URL,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": fallback, "messages": [{"role": "user", "content": prompt}],
                              "max_tokens": max_tokens, "temperature": 0.85,
                              "reasoning_effort": "low", "include_reasoning": False},
                        timeout=120,
                    )
                    r2.raise_for_status()
                    text = r2.json()["choices"][0]["message"]["content"].strip()
                    if text:
                        print(f"  ✓ {label} with {fallback} (outage fallback, {len(text.split()):,} words)")
                        return text
                except Exception as exc2:
                    print(f"  ⚠ {label} {fallback} outage-fallback also failed: {exc2}")
            time.sleep(min(30, 5 * (attempt + 1)))

        except Exception as exc:
            last_err = str(exc)
            print(f"  ⚠ {label} attempt {attempt+1} failed: {exc}")
            time.sleep(min(30, 5 * (attempt + 1)))

    raise RuntimeError(f"All Groq attempts failed for {label} (last: {last_err})")


_POS_PAIR = re.compile(r"\b([A-Z]{1,5}(?:\.TO)?)\s*~?\$\s?([\d,]*\d(?:\.\d+)?)\s*(K|M|B)?(?![A-Za-z])")


def _position_pairs_in(text: str) -> dict:
    """{"ENB.TO": [5493.0]} from briefing text like "ENB.TO $5,493 CAD, SHEL $2,973 CAD"."""
    out = {}
    for tkr, raw, suf in _POS_PAIR.findall(str(text or "")):
        try:
            out.setdefault(tkr, []).append(_claim_value(raw, suf or None))
        except (ValueError, KeyError):
            pass
    return out


def _money_values_in(*texts) -> list:
    """Every dollar amount (>= $1,000) that appears in the text the model was shown.
    Anything the briefing itself supplied is a figure the script is allowed to cite."""
    out = set()
    for t in texts:
        for raw, suffix in _CLAIM_MONEY.findall(str(t or "")):
            try:
                v = _claim_value(raw, suffix or None)
            except (ValueError, KeyError):
                continue
            if v >= 1_000:
                out.add(v)
    return sorted(out)


# ── Fixed subjects ───────────────────────────────────────────────────────────
# Part 1 used to pick Deep Dive 1 and write the agenda; Part 2 picked Deep Dive 2
# afterwards. Ep18's agenda promised "the Tesla Semi rollout" and Part 2 then
# delivered Broadcom — a company we already own, which it described as "not
# currently in the portfolio". Both subjects are fixed here, before either half is
# written, exactly as the learning topic is.
def _choose_deep_dive_1(intel: dict, clean) -> dict:
    for m in intel.get("macro", []) or []:
        if m.get("title"):
            return {"title": str(m["title"]).strip(),
                    "body": clean(m.get("body", ""), 420),
                    "bull": clean(m.get("bull", ""), 160),
                    "bear": clean(m.get("bear", ""), 160)}
    for n in intel.get("news", []) or []:
        if n.get("headline"):
            return {"title": str(n["headline"]).strip(), "body": clean(n.get("body", ""), 420),
                    "bull": "", "bear": ""}
    return {"title": "the biggest market story of the week", "body": "", "bull": "", "bear": ""}


def _choose_deep_dive_2(intel: dict, facts: dict, recently_spotlighted: list, clean):
    """A holding we actually own that this week's news actually touches.

    The news items name what we hold in them ("ENB.TO $5,493 CAD"), so the first
    one that names an owned, non-leveraged, not-recently-spotlighted holding is
    the subject, and its news item is the ONLY thing the model may say about it.
    Falls back to our largest such holding with no news at all.
    """
    positions = (facts or {}).get("positions") or {}
    if not positions:
        return None
    spot  = {str(t).upper() for t in (recently_spotlighted or [])}
    total = float((facts or {}).get("total_value") or 0) or sum(
        float(p["cad"]) for p in positions.values())

    def pick(tkr, news=None):
        p = positions[tkr]
        return {"ticker": tkr,
                "name": COMPANY_NAMES.get(tkr, p.get("name") or tkr),
                "cad": float(p["cad"]),
                "share": float(p["cad"]) / total * 100 if total else 0.0,
                "accounts": list(p.get("accounts") or []),
                "headline": str((news or {}).get("headline") or ""),
                "body": clean((news or {}).get("body", ""), 320) if news else ""}

    for n in intel.get("news", []) or []:
        for tkr in re.findall(r"\b[A-Z]{1,5}(?:\.TO)?\b", str(n.get("exposure") or "")):
            if tkr in positions and tkr not in _LEVERAGE_3X and tkr.upper() not in spot:
                return pick(tkr, n)
    cands = sorted(((p["cad"], t) for t, p in positions.items()
                    if t not in _LEVERAGE_3X and t.upper() not in spot), reverse=True)
    return pick(cands[0][1]) if cands else None


def _deep_dive_briefs(dd1: dict, dd2) -> tuple:
    """(dd1_title, dd1_brief, dd2_title, dd2_brief) as the prompts print them."""
    b1 = f"What the briefing says: {dd1['body']}" if dd1["body"] else ""
    # Sentences about the currency were removed from the briefing because they were
    # wrong, so say where the right version is.
    b1 += ("\nFor how the dollar affects us, use the CURRENCY box above — not the briefing's "
           "wording about it.")
    if dd1.get("bull"):
        b1 += f"\nIf it goes well: {dd1['bull']}"
    if dd1.get("bear"):
        b1 += f"\nIf it goes badly: {dd1['bear']}"

    if not dd2:
        return (dd1["title"], b1.strip(),
                "one of our holdings you have not heard about recently",
                "Pick a holding from WHAT WE OWN. Explain what the business does and why it is "
                "in the portfolio — nothing else. Invent no announcement, number, date or earnings.")
    accts = " and ".join(dd2["accounts"]) or "the portfolio"
    title = dd2["name"] + (f" — {dd2['headline']}" if dd2["headline"] else "")
    size  = (f"We own about {_say_money(dd2['cad'])} of it ({_say_pct(dd2['share'])} of the "
             f"portfolio), held in the {accts}.")
    if dd2["body"]:
        brief = (f"{size}\nThe news (your ONLY facts about this company): {dd2['body']}\n"
                 f"Beyond that, explain how this kind of business works. Invent no "
                 f"announcement, buyback, number, date or earnings.")
    else:
        brief = (f"{size}\nThere is no news item for it this week. Explain what the business "
                 f"does and why it is in the portfolio, and nothing else — invent no "
                 f"announcement, buyback, number, date or earnings.")
    return dd1["title"], b1.strip(), title, brief


def generate_script(intel: dict, snapshot: dict, old_meta: dict, api_key: str,
                    computed_holdings: list, registry: dict,
                    cash_positions: list = None, feedback: str = "") -> tuple[str, dict]:
    now     = datetime.now(timezone.utc)
    today   = now.strftime("%A, %B %d, %Y")
    week    = _week_trading_range(now)
    # Fixed before either half is written, so the agenda in Part 1 and the
    # segment in Part 2 cannot disagree.
    education_topic = _choose_education_topic(registry.get("education_topics_used", []))
    print(f"     Learning segment fixed up front: {education_topic}")

    # The briefing is the model's only window on the world, and it has contained
    # wrong-direction currency statements ("a stronger USD ... pressuring the
    # portfolio's USD-heavy exposure"), which the model then repeated. Drop those
    # sentences before the model sees them.
    removed_fx: list = []

    def clean(text, limit=300):
        c, gone = scrub_fx_sentences(str(text or ""), strict_wording=True)
        removed_fx.extend(gone)
        return c[:limit]

    mood    = intel.get("market_mood", "neutral").upper()
    outlook = clean(intel.get("daily_outlook", ""), 300)
    macro   = "\n".join(f"• {m['title']} [{m.get('impact','?')}]: {clean(m.get('body',''), 300)}"
                        for m in intel.get("macro", [])[:3])
    news    = "\n".join(f"• {n['headline']}: {clean(n.get('body',''), 250)} | "
                        f"What we own in it: {n.get('exposure','')[:100]}"
                        for n in intel.get("news", [])[:4])
    # Labelled explicitly as NOT owned. Episode 16 had Sam say "we've got a lot of
    # exposure to other energy names — Enbridge, Canadian Natural", but Canadian
    # Natural was a suggestion in this list, never a holding.
    picks   = "\n".join(f"• {p['ticker']} ({COMPANY_NAMES.get(p['ticker'], p['ticker'])}) "
                        f"— an IDEA, NOT OWNED: {clean(p.get('thesis',''), 200)}"
                        for p in intel.get("picks", [])[:3])
    strategy = "\n".join(f"• {clean(s['text'], 200)}" for s in intel.get("strategy_short", [])[:3])

    # Registry-derived context
    registry_context     = registry.get("registry_text", "No previous episodes — fresh start.")
    recently_spotlighted = registry.get("recently_spotlighted_tickers", [])
    education_used       = registry.get("education_topics_used", [])
    if education_used:
        education_topics_str = "\n".join(f"  • {t}" for t in education_used)
    else:
        education_topics_str = "  (none yet — first learning segment, all topics available)"

    # Build fully dynamic portfolio context — live holdings + snapshot prices + cash
    snaps = snapshot.get("snapshots", {})
    portfolio_ctx, portfolio_facts = _build_portfolio_context(
        computed_holdings, snaps, cash_positions)
    dd1 = _choose_deep_dive_1(intel, clean)
    dd2 = _choose_deep_dive_2(intel, portfolio_facts, recently_spotlighted, clean)
    dd1_title, dd1_brief, dd2_title, dd2_brief = _deep_dive_briefs(dd1, dd2)
    if portfolio_facts:
        # Dollar amounts the briefing itself supplied are figures the script may cite.
        # Those tied to a holding ("TSLA $7,480 CAD") count only where that holding is
        # named; the rest are free-standing.
        pairs  = _position_pairs_in(news)
        paired = {v for vs in pairs.values() for v in vs}
        portfolio_facts["intel_money"] = [v for v in _money_values_in(outlook, macro, news, picks, strategy)
                                          if v not in paired]
        portfolio_facts["intel_positions"] = pairs
        # Every number the model was shown, so a market statistic outside this set is
        # known to be invented. Thousands separators are removed first so "5,493"
        # is one number and not "5" and "493".
        shown = " ".join([portfolio_ctx, outlook, macro, news, picks, strategy,
                          dd1_brief, dd2_brief])
        shown = re.sub(r"(?<=\d),(?=\d{3})", "", shown)
        portfolio_facts["allowed_numbers"] = sorted({float(x) for x in re.findall(r"\d+(?:\.\d+)?", shown)})

    print(f"     Deep Dive 1 fixed: {dd1_title[:80]}")
    print(f"     Deep Dive 2 fixed: {dd2_title[:80]}")
    if removed_fx:
        print(f"  ✓ Removed {len(set(removed_fx))} briefing sentence(s) with the currency "
              f"direction wrong before prompting")

    editor_notes = feedback or ""
    # Ep18 told listeners "the CPI release is today". The briefing it read was written
    # on the Friday and the episode aired the following Monday, so "today" in the
    # briefing meant a different day. Say so.
    try:
        bdate = datetime.fromisoformat(str(intel.get("generated_at")).replace("Z", "+00:00"))
        briefing_note = (f"(This briefing was written on {bdate.strftime('%A, %B %d')}; this episode "
                         f"airs on {now.strftime('%A, %B %d')}. \"Today\", \"tonight\" and \"later today\" "
                         f"in it mean THAT day — never repeat them as if they were now.)\n")
    except Exception:
        briefing_note = ""

    # Brief cooldown to separate from the registry-extraction call that ran just
    # before this, keeping us clear of the free-tier per-minute token budget.
    import time
    time.sleep(20)

    print("     Generating Part 1 (Welcome + Recap + Deep Dive 1)...")
    part1 = _groq_call(api_key, SCRIPT_PROMPT_PART1.format(
        editor_notes=editor_notes, today=today, week_range=week, mood=mood,
        briefing_note=briefing_note,
        registry_context=registry_context, outlook=outlook, macro=macro, news=news,
        portfolio=portfolio_ctx,
        dd1_title=dd1_title, dd1_brief=dd1_brief, dd2_title=dd2_title,
        education_topic=education_topic,
    ), "Part 1", max_tokens=4096)

    # Summarise Part 1 for Part 2. This used to be the last 6 speaker lines
    # truncated to 400 characters. In ep016 that tail happened to be a list of
    # upcoming catalysts, so Part 2 opened by telling the listener the hosts had
    # "just walked through the Bank of Canada's upcoming rate call" — which was
    # one bullet in that list and was never discussed. Give it the agenda (what
    # the episode is actually about) as well as a longer tail.
    dive1_lines = [l for l in part1.split('\n') if l.strip().startswith(('ALEX:', 'SAM:'))]
    agenda      = next((l[5:].strip() for l in dive1_lines[:4]
                        if re.search(r"unpacking|covering|this week we", l, re.I)), "")
    tail        = ' '.join(l[5:].strip() for l in dive1_lines[-8:])
    dive1_last  = ((f"TOPICS PART 1 ACTUALLY COVERED: {agenda}\n" if agenda else "")
                   + f"HOW PART 1 ENDED — continue straight out of this: {tail[-900:]}")

    # Space out the two large generation calls so we stay under Groq's free-tier
    # per-minute token budget (the back-to-back calls were the root cause of the
    # 429 storm that aborted Part 2). This does not affect output — same prompt,
    # same model — it just lets the rolling TPM window reset first.
    print("     Cooling down 35s to reset Groq TPM window before Part 2...")
    time.sleep(35)

    print("     Generating Part 2 (Deep Dive 2 + Learning Segment + Scenarios + Close)...")
    part2 = _groq_call(api_key, SCRIPT_PROMPT_PART2.format(
        editor_notes=editor_notes, today=today, dive1_summary=dive1_last,
        briefing_note=briefing_note,
        news=news, picks=picks, strategy=strategy, portfolio=portfolio_ctx,
        dd2_title=dd2_title, dd2_brief=dd2_brief,
        education_topic=education_topic, education_topics_used=education_topics_str,
    ), "Part 2", max_tokens=4096)

    full = _stitch_parts(part1, part2)
    print(f"  ✓ Full script: {len(full.split()):,} words across both parts")
    return full, portfolio_facts


# ============================================================
# SCRIPT SUMMARY (for podcast metadata)
# ============================================================
def generate_summary(script: str, intel: dict, api_key: str) -> dict:
    turns  = [l for l in script.split('\n') if l.startswith(('ALEX:', 'SAM:'))]
    sample = '\n'.join(turns[:40])
    prompt = f"""Extract a structured JSON summary of this podcast episode.

SCRIPT SAMPLE:
{sample}

Return ONLY valid JSON:
{{
  "episode_title": "< 10-word punchy title for this specific episode >",
  "mood_summary": "one sentence on the market mood and portfolio outlook",
  "portfolio_snapshot": ["3-4 bullet strings about portfolio performance"],
  "market_context": ["3-4 bullet strings about macro themes covered"],
  "position_spotlight": ["2-3 strings naming specific holdings that got dedicated analysis"],
  "watch_list": ["2-3 specific things to watch next week with reason"],
  "action_items": ["2-3 specific portfolio actions discussed"],
  "education_topic": "the learning segment topic in 3-5 words, or empty string if none"
}}"""
    try:
        raw = _groq_call(api_key, prompt, "Summary", max_tokens=900)
        start, end = raw.find('{'), raw.rfind('}') + 1
        return json.loads(raw[start:end]) if start >= 0 else {}
    except Exception:
        return {"episode_title": intel.get("daily_outlook", "Weekly Update")[:60]}


# ============================================================
# AUDIO GENERATION — Kokoro TTS
# ============================================================
# ── Speech normalisation ──────────────────────────────────────────────────────
# The script goes to edge-tts verbatim, and the model writes money as
# "**$17,427 CAD**". The engine reads the symbol and the comma literally, giving
# "dollar sign four, two hundred twenty" instead of the amount. Percentages are
# written "6.2 %" with a space, and markdown asterisks are spoken too.
# Spelling the values out removes the ambiguity entirely.

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty",
         "sixty", "seventy", "eighty", "ninety")
_SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"))
_MAGNITUDE = {"K": "thousand", "M": "million", "B": "billion"}
_CURRENCY = {"CAD": "Canadian dollars", "USD": "US dollars"}


def _int_words(n: int) -> str:
    if n < 0:
        return "minus " + _int_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rem = divmod(n, 10)
        return _TENS[tens] + ("-" + _ONES[rem] if rem else "")
    if n < 1000:
        hund, rem = divmod(n, 100)
        return _ONES[hund] + " hundred" + (" " + _int_words(rem) if rem else "")
    for value, name in _SCALES:
        if n >= value:
            qty, rem = divmod(n, value)
            return _int_words(qty) + " " + name + (" " + _int_words(rem) if rem else "")
    return str(n)


def _num_words(raw: str) -> str:
    """'4,678' -> 'four thousand six hundred seventy-eight'; '6.2' -> 'six point two'."""
    raw = raw.replace(",", "").strip()
    if "." in raw:
        whole, _, frac = raw.partition(".")
        head = _int_words(int(whole)) if whole else "zero"
        digits = " ".join(_ONES[int(d)] for d in frac if d.isdigit())
        return f"{head} point {digits}" if digits else head
    return _int_words(int(raw)) if raw.isdigit() else raw


def _money_words(match) -> str:
    amount, magnitude, currency = match.group(1), match.group(2), match.group(3)
    words = _num_words(amount)
    if magnitude:
        words += " " + _MAGNITUDE[magnitude.upper()]
    unit = _CURRENCY.get((currency or "").upper(), "dollars")
    # "one dollar", not "one dollars" — only when it is exactly one unit
    if not magnitude and amount.replace(",", "") in ("1", "1.0"):
        unit = unit.replace("dollars", "dollar")
    return f"{words} {unit}"


def normalize_for_speech(text: str) -> str:
    """Rewrite symbols and figures the TTS engine mishandles into plain words."""
    # Markdown emphasis is spoken aloud by the engine
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)

    # $17,427 CAD · $1.2M · $500  (currency word wins over a bare "dollars")
    # The magnitude suffix must be attached to the digits; consuming whitespace
    # before it would swallow the separator and glue words together.
    text = re.sub(
        r"\$\s?(\d[\d,]*(?:\.\d+)?)([KMB])?(?:\s*(CAD|USD)\b)?",
        _money_words, text)

    # "6.2 %" and "23%"
    text = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*%",
                  lambda m: _num_words(m.group(1)) + " percent", text)

    # "3x leveraged"
    text = re.sub(r"\b(\d+)\s*[xX]\b",
                  lambda m: _num_words(m.group(1)) + " times", text)

    # Remaining comma-grouped figures ("12,000 shares"). Bare 4-digit numbers are
    # left alone so years still read naturally as "twenty twenty-seven".
    text = re.sub(r"\b(\d{1,3}(?:,\d{3})+)\b",
                  lambda m: _num_words(m.group(1)), text)

    return re.sub(r"\s{2,}", " ", text).strip()


# ── Figure verification ───────────────────────────────────────────────────────
# Episodes asserted portfolio-level numbers that appeared nowhere in their
# context and contradicted reality — ep016 opened with "+$23,927, an 8.6% weekly
# gain" when the week was actually -$7,366. The prompt already forbade this; the
# model ignored it, and nothing downstream checked. This does.
#
# Deliberately narrow. It only judges claims about the PORTFOLIO's total value
# and its change over the period. Per-holding maths ("a 1% S&P move is ±$4,480")
# is legitimate derivation and is left alone — policing every digit would fail
# constantly and the guard would be switched off.

# The suffix must be a real unit. It used to be `\s*([KMB])?`, which read the "b" of
# "$90, but" as billions — $90,000,000,000 — and the "b" of "boost" the same way.
_CLAIM_MONEY = re.compile(
    r"[-+]?\$\s?([\d,]*\d(?:\.\d+)?)\s*(K|M|B|thousand|million|billion)?(?![A-Za-z])", re.I)
_CLAIM_PCT   = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
# A sentence counts only when it predicates a result OF the book. Merely
# mentioning the portfolio is not enough: "our Enbridge position, which sits at
# roughly $12,000 CAD" names one holding, and policing every figure in sentences
# like that flagged sound episodes. A guard that blocks good scripts is a guard
# that gets switched off, so this stays deliberately narrow.
_PORTFOLIO_CLAIM = re.compile(
    r"\b(?:portfolio|the book|net worth|total value)\b[^.!?]{0,60}?"
    r"\b(?:gain(?:ed)?|lost|loss|jump(?:ed)?|rose|fell|climb(?:ed)?|drop(?:ped)?|"
    r"add(?:ed)?|shed|clos(?:ed)?|finish(?:ed)?|end(?:ed)?|return(?:ed)?|up|down)\b",
    re.I)

# Figures scoped to a single holding rather than to the whole book. ("worth" is
# deliberately absent — it would swallow "net worth".)
_POSITION_SCOPED = re.compile(
    r"\b(position|stake|holding|shares?|sits at|allocation|sleeve|exposure to)\b",
    re.I)

# Forecasts and sensitivities are not claims about what the period actually did.
_HYPOTHETICAL = re.compile(
    r"\b(if|would|could|should|might|expect\w*|forecast\w*|scenario|assum\w*|"
    r"project\w*|target\w*|next week|for every|per|imagine|"
    r"in that (?:environment|case|world))\b", re.I)

# "$7,000 – $9,000" is a projected band, never a statement of the period result.
_MONEY_RANGE = re.compile(
    r"\$\s?[\d,]+(?:\.\d+)?\s*(?:[-–—]|to)\s*\$?\s?[\d,]+", re.I)

# A sentence asserting how large a holding is, or where it sits. Distinct from
# _POSITION_SCOPED, which only asks "is this figure about one holding?" — this
# asks "is the script stating that holding's size?", which is checkable.
_SIZE_PHRASE = re.compile(
    r"\b(hold|holds|holding|own|owns|position|stake|sits? (?:at|in)|worth|"
    r"represent\w*|makes? up|allocation)\b", re.I)

MONEY_TOLERANCE_PCT = 0.06   # rounding/paraphrase ("roughly $24K")
MONEY_TOLERANCE_ABS = 750
PCT_TOLERANCE       = 0.6


def _claim_value(raw, suffix) -> float:
    v = float(raw.replace(",", ""))
    mult = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000,
            "b": 1_000_000_000, "billion": 1_000_000_000}
    return v * mult[suffix.lower()] if suffix else v


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(MONEY_TOLERANCE_ABS, abs(b) * MONEY_TOLERANCE_PCT)


_PART1_SIGNOFF = re.compile(
    r"\b(stay tuned|we['’]?ll be back|see you next|that['’]?s the play|reconvene|"
    r"thanks for (?:tuning|listening)|until next (?:week|time)|keep an eye on those|"
    r"that['’]?s (?:it|all) for (?:this|today)|catch you next)\b", re.I)

_PART2_REOPEN = re.compile(
    r"\b(welcome back|let['’]?s pick up|picking up where|we just walked through|"
    r"we just discussed|as we just|where we left off|back with you)\b", re.I)


def _stitch_parts(part1: str, part2: str) -> str:
    """Join the two generation passes without the seam showing.

    The halves come from separate model calls. Part 1 does not know anything
    follows, so it signs off; Part 2 does not know what Part 1 said, so it
    re-greets the listener and "recaps" something it never saw. Episode 16 closed
    a segment, printed a separator, then opened with "let's pick up where we left
    off — we just walked through the Bank of Canada's upcoming rate call", a topic
    that had only been named in a passing list of upcoming dates.

    The prompts now forbid both. This removes them as well, because a prompt rule
    is a request and this is a guarantee. It repairs rather than rejects: a seam
    is a blemish, never a reason to lose the week's episode.
    """
    p1, removed = part1.rstrip().split("\n"), 0
    while p1 and removed < 2:
        tail = p1[-1].strip()
        if not tail or tail in {"---", "***", "___"}:
            p1.pop()
            continue
        if tail.startswith(("ALEX:", "SAM:")) and _PART1_SIGNOFF.search(tail):
            p1.pop()
            removed += 1
            continue
        break

    p2, dropped = part2.lstrip().split("\n"), 0
    while p2 and dropped < 2:
        head = p2[0].strip()
        if not head or head in {"---", "***", "___"}:
            p2.pop(0)
            continue
        if head.startswith(("ALEX:", "SAM:")) and _PART2_REOPEN.search(head):
            p2.pop(0)
            dropped += 1
            continue
        break

    return "\n".join(p1).rstrip() + "\n\n" + "\n".join(p2).lstrip()


# ── Sentence-level machinery ─────────────────────────────────────────────────
# Everything below judges ONE sentence at a time. That is deliberate: the same
# function that flags a sentence is what removes it as a last resort, so the
# checker and the repair can never disagree about what is wrong.

# "Imagine you own a $10,000 position" is a teaching example, not a claim about
# this portfolio, so invented-figure checks stand down inside the learning segment.
_NOT_OWNED = re.compile(
    r"\b(?:isn't|is not|aren't|are not|wasn't|not)\s+(?:currently\s+|yet\s+)?(?:in|part of)\s+(?:the|our)\s+portfolio\b"
    r"|\b(?:don't|do not|doesn't|does not|didn't)\s+(?:currently\s+)?(?:own|hold|have)\b"
    r"|\bno\s+(?:direct\s+)?(?:exposure|position|stake)\s+(?:in|to)\b", re.I)

# Market statistics that are not dollar amounts: "VIX around 21.5", "the 2-year at
# 4.78%, the 10-year at 4.85%", "a 10bp flattening correlates with a 0.3% drop".
# Ep18 stated all three and none appeared anywhere in what the model was given.
_MARKET_WORDS = re.compile(
    # No bare "spread": "20% downside spread across the next few weeks" is not a bond spread.
    r"\b(?:vix|ovx|yields?|yield[\s-]spreads?|basis[\s-]points?|bps|inventor(?:y|ies)|barrels?|brent|wti|"
    r"crude|treasur(?:y|ies)|index level)\b|\b\d+[\s-]year\b", re.I)
_SCENARIO_LINE = re.compile(r"\b(?:base|bull|bear)\s+case\b|\bprobabilit", re.I)
_STAT_TOKEN = re.compile(r"\d+\.\d+|\d+(?:\.\d+)?\s*%")


_NOT_OWNED_EXEMPT = re.compile(r"\b(?:if|imagine|suppose|what if|unless|wish)\b", re.I)

DOWN_PAYMENT_ALLOWED = 90_000


def _strict_close(a: float, b: float) -> bool:
    """Tighter than _close: a figure read straight off a table, rounded to two
    significant figures, is within 5%. The looser ±$750 band let invented figures
    hide next to unrelated real ones."""
    return abs(a - b) <= max(150, abs(b) * 0.05)


def _iter_script(script: str):
    """Yield ("turn", speaker, text) / ("marker", None, text) / ("other", None, text)."""
    for raw in script.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if s.upper().startswith("[EDUCATION_TOPIC:"):
            yield ("marker", None, s)
            continue
        m = re.match(r"^(ALEX|SAM):\s*(.*\S)\s*$", s)
        if m:
            yield ("turn", m.group(1), m.group(2))
        else:
            yield ("other", None, s)


def _in_learning_flags(script: str) -> list:
    """For each item _iter_script yields, whether it sits inside the learning segment."""
    flags, inside = [], False
    for kind, _, text in _iter_script(script):
        if kind == "marker":
            inside = True
        flags.append(inside)
        low = text.lower()
        if inside and ("on to scenarios" in low or "learning segment for this week" in low):
            inside = False
    return flags


def _verify_ctx(facts: dict) -> dict:
    total    = float(facts.get("total_value") or 0)
    gain     = float(facts.get("wk_gain") or 0)
    gain_pct = float(facts.get("wk_pct") or 0)

    # Amounts the script may cite at portfolio level.
    allowed_money = {abs(total), abs(gain), float(facts.get("leverage_cad") or 0),
                     float(facts.get("usd_exposure") or 0)}
    allowed_money |= {abs(float(v)) for v in (facts.get("accounts") or {}).values()}
    allowed_money |= {abs(float(v)) for v in (facts.get("account_change") or {}).values()}
    allowed_money |= {abs(float(m.get("cad") or 0)) for m in (facts.get("movers") or [])}

    allowed_pct = {abs(gain_pct), abs(float(facts.get("roi_pct") or 0))}
    allowed_pct |= {abs(float(m.get("pct") or 0)) for m in (facts.get("movers") or [])}

    # Everything the script was actually GIVEN — the strict "invented figure"
    # check compares against this. Anything outside it was made up or computed
    # on air, which is where ep18's $7,397 / $14,793 / $10,355 came from.
    # Figures valid ANYWHERE in the script. Individual position sizes are
    # deliberately not here: a size is only valid in a sentence that names that
    # holding. Pooled together, ~27 position sizes plus the briefing's figures cover
    # so much of the number line that ep18's invented "$7,397" sat $83 from Tesla's
    # real $7,480 and passed as if it were a rounding of it.
    given = set(allowed_money)
    given |= {float(facts.get("usd_notional") or 0), float(facts.get("cash_total") or 0),
              float(facts.get("down_payment_target") or DOWN_PAYMENT_ALLOWED)}
    given |= {abs(float(v)) for v in (facts.get("cash_by_account") or {}).values()}
    given |= {abs(float(v)) for v in (facts.get("index_table") or {}).values()}
    given |= {abs(float(v)) for v in (facts.get("fx_table") or {}).values()}
    given |= {abs(float(v)) for v in (facts.get("intel_money") or [])}
    # Sizes tied to ONE holding, valid only where that holding is named.
    pos_extra = {t: {abs(float(p.get("cad") or 0))}
                 for t, p in (facts.get("positions") or {}).items()}
    for t, vals in (facts.get("intel_positions") or {}).items():
        pos_extra.setdefault(t, set()).update(abs(float(v)) for v in vals)

    index = []
    for tkr, p in (facts.get("positions") or {}).items():
        nm      = str(p.get("name") or tkr)
        aliases = {nm}
        if len(tkr) >= 3:           # skip "V"/"ET" — too short to match safely
            aliases.add(tkr)
        first = nm.split()[0] if nm.split() else ""
        if len(first) >= 5:         # "Shell" out of "Shell PLC"
            aliases.add(first)
        index.append((aliases, tkr, p))

    return {"total": total, "gain": gain, "gain_pct": gain_pct,
            "allowed_money": allowed_money, "allowed_pct": allowed_pct,
            "given": given, "pos_extra": pos_extra, "index": index,
            "strict": bool(facts.get("strict_figures")),
            "allowed_numbers": ([float(x) for x in facts["allowed_numbers"]]
                                if facts.get("allowed_numbers") else None),
            "span_days": facts.get("span_days")}


def _sentence_problems(sentence: str, ctx, in_learning: bool = False) -> list:
    """Every reason this one sentence cannot be aired. ctx may be None (no facts)."""
    probs = []

    # Currency direction — needs no facts, so it always runs.
    why = _fx_classify(sentence) or _fx_held_in_cad(sentence)
    if why:
        probs.append(f"{why} — \"{sentence[:110]}\"")
    if not ctx:
        return probs

    total, gain, gain_pct = ctx["total"], ctx["gain"], ctx["gain_pct"]
    flagged_amounts = set()

    # Portfolio-level claims: judged against the context.
    if (_PORTFOLIO_CLAIM.search(sentence)
            and not (_POSITION_SCOPED.search(sentence) or _HYPOTHETICAL.search(sentence)
                     or _MONEY_RANGE.search(sentence))):
        for raw, suffix in _CLAIM_MONEY.findall(sentence):
            amount = _claim_value(raw, suffix or None)
            if amount < 1_000:          # small change is almost always derived
                continue
            if not any(_close(amount, ok) for ok in ctx["allowed_money"] if ok):
                flagged_amounts.add(amount)
                probs.append(
                    f"${amount:,.0f} is not supported by the context "
                    f"(period change ${gain:,.0f}, total ${total:,.0f}) — \"{sentence[:110]}\"")
        for raw in _CLAIM_PCT.findall(sentence):
            pct = abs(float(raw))
            if pct == 0:
                continue
            if not any(abs(pct - ok) <= PCT_TOLERANCE for ok in ctx["allowed_pct"] if ok):
                probs.append(
                    f"{pct}% is not supported by the context "
                    f"(period change {gain_pct:+.1f}%) — \"{sentence[:110]}\"")

    # A single holding's size, the account it sits in, and "we don't own it".
    matched = [(tkr, p) for aliases, tkr, p in ctx["index"]
               if any(re.search(r"\b" + re.escape(a) + r"\b", sentence, re.I)
                      for a in aliases if a)]
    if matched and not _HYPOTHETICAL.search(sentence) and not _MONEY_RANGE.search(sentence):
        if _SIZE_PHRASE.search(sentence):
            # To BE a size claim the figure has to follow the size phrase closely.
            claim_amt = None
            for sm in _SIZE_PHRASE.finditer(sentence):
                mm = _CLAIM_MONEY.search(sentence, sm.end())
                if mm and mm.start() - sm.end() <= 30:
                    amt = _claim_value(mm.group(1), mm.group(2) or None)
                    if amt >= 1_000:
                        claim_amt = amt
                        break
            if claim_amt is not None and len(matched) == 1:
                tkr, p = matched[0]
                actual = float(p.get("cad") or 0)
                if actual > 0 and not _close(claim_amt, actual):
                    flagged_amounts.add(claim_amt)
                    probs.append(
                        f"{p.get('name') or tkr} is stated as ${claim_amt:,.0f} but the "
                        f"position is ${actual:,.0f} — \"{sentence[:110]}\"")
            for tkr, p in matched:
                held = {str(a).lower() for a in (p.get("accounts") or [])}
                if not held:
                    continue
                for acct in ("TFSA", "Investment", "FHSA", "RRSP"):
                    if (re.search(r"\b" + acct + r"\b", sentence, re.I)
                            and acct.lower() not in held):
                        probs.append(
                            f"{p.get('name') or tkr} is placed in the {acct} but it is held "
                            f"in {', '.join(p.get('accounts') or [])} — \"{sentence[:110]}\"")

    # Ep18 deep-dived Broadcom as "not currently in the portfolio" while holding about
    # $15,000 of it — in a sentence that also said "...could be redirected here". So
    # this must NOT stand down for hypothetical words ("could", "would"); only a real
    # supposition ("if we didn't own it") exempts it.
    if (len(matched) == 1 and _NOT_OWNED.search(sentence.replace("\u2019", "'"))
            and not _NOT_OWNED_EXEMPT.search(sentence)):
        tkr, p = matched[0]
        probs.append(
            f"says {p.get('name') or tkr} is not owned, but we hold about "
            f"${float(p.get('cad') or 0):,.0f} of it — \"{sentence[:110]}\"")

    # Calling a losing week a gain is the failure the user actually noticed.
    # Per sentence so a forecast is not mistaken for a claim about the period.
    if gain < 0 and not _HYPOTHETICAL.search(sentence):
        if re.search(r"\bportfolio\b[^.!?]{0,80}\b(gain(?:ed)?|jumped|rose|climbed|up)\b",
                     sentence, re.I):
            probs.append(
                f"script describes the portfolio as up, but the period change was "
                f"${gain:,.0f} ({gain_pct:+.1f}%) over {ctx.get('span_days')} days")

    # Strict: a dollar figure the script was never given. Computed-on-air arithmetic
    # and invented positions both land here.
    if ctx["strict"] and not in_learning:
        allowed = set(ctx["given"])
        for tkr, _p in matched:                    # sizes of holdings THIS sentence names
            allowed |= ctx["pos_extra"].get(tkr, set())
        for raw, suffix in _CLAIM_MONEY.findall(sentence):
            amount = _claim_value(raw, suffix or None)
            if amount < 1_000 or amount in flagged_amounts:
                continue
            if not any(_strict_close(amount, ok) for ok in allowed if ok):
                probs.append(
                    f"${amount:,.0f} is not a figure the script was given — it was invented or "
                    f"calculated on air; use only the supplied numbers — \"{sentence[:110]}\"")

    # A statistic about the market that nobody supplied. Every number the model was
    # shown is in allowed_numbers; anything else attached to VIX, yields, spreads,
    # inventories or crude was made up.
    # Scenario probabilities ("Base case — 50 percent") are the script's own judgement, not
    # market data, whatever words sit nearby.
    if (ctx.get("allowed_numbers") is not None and _MARKET_WORDS.search(sentence)
            and not _SCENARIO_LINE.search(sentence)):
        for tok in _STAT_TOKEN.findall(sentence):
            try:
                x = float(re.sub(r"[^\d.]", "", tok))
            except ValueError:
                continue
            # Rounding moves a statistic by a hundredth or two, never more, so a tight band:
            # 4.85 must not pass as a rounding of 4.78.
            if not any(abs(x - a) <= (0.06 if a < 100 else a * 0.005)
                       for a in ctx["allowed_numbers"]):
                probs.append(
                    f"cites the market figure {tok.strip()}, which is not in the news it was given "
                    f"— it was invented; leave the figure out — \"{sentence[:110]}\"")
                break
    return probs


def verify_script_figures(script: str, facts: dict) -> list[str]:
    """Every reason the script cannot be aired: wrong currency direction, portfolio
    and position claims the context contradicts, and (when the facts mark
    themselves strict) dollar figures nobody supplied."""
    ctx = _verify_ctx(facts) if facts else None
    problems, flags = [], _in_learning_flags(script)
    for (kind, _, text), in_learning in zip(_iter_script(script), flags):
        if kind == "marker":
            continue
        for sent in _split_sentences(text):
            problems.extend(_sentence_problems(sent, ctx, in_learning))

    seen, unique = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def strip_unsafe_sentences(script: str, facts: dict) -> tuple:
    """Last resort: drop every sentence verify_script_figures would flag.

    Used only after the regeneration attempts are spent. A slightly shorter
    episode is a better outcome than no episode, and no flagged sentence is ever
    aired. Only touches turns that actually contain one.
    """
    ctx = _verify_ctx(facts) if facts else None
    out, removed, inside = [], [], False
    for raw in script.split("\n"):
        s = raw.strip()
        if s.upper().startswith("[EDUCATION_TOPIC:"):
            inside = True
            out.append(raw)
            continue
        m = re.match(r"^(ALEX|SAM):\s*(.*\S)\s*$", s)
        if not m:
            out.append(raw)
            continue
        sents = _split_sentences(m.group(2))
        kept = [x for x in sents if not _sentence_problems(x, ctx, inside)]
        low = m.group(2).lower()
        if inside and ("on to scenarios" in low or "learning segment for this week" in low):
            inside = False
        if len(kept) == len(sents):
            out.append(raw)
            continue
        removed.extend(x for x in sents if x not in kept)
        if kept:
            out.append(f"{m.group(1)}: " + " ".join(kept))
    return "\n".join(out), removed


# ── Structure and density ────────────────────────────────────────────────────
def scenario_problems(script: str) -> list:
    """Ep18 announced "a roughly 50% base, 30% upside, and 20% downside" after
    giving only the base case, so the listener was told about two scenarios that
    were never spoken."""
    low = script.lower()
    return [f"the scenarios are incomplete — there is no {n}"
            for n in ("base case", "bull case", "bear case") if n not in low]


def agenda_problems(script: str) -> list:
    """The opening must announce the learning topic the episode then delivers.

    Ep16 promised "forward-contract roll yields" and delivered "Short Interest
    Signals". The topic is now fixed before either half is written, but the marker
    the model emits records what it actually wrote, so check the two agree."""
    m = re.search(r"\[EDUCATION_TOPIC:\s*([^\]]+)\]", script, re.I)
    if not m:
        return ["there is no [EDUCATION_TOPIC: …] marker, so the learning segment is unlabelled"]
    topic = m.group(1).strip()
    words = re.findall(r"[a-z]{4,}", topic.lower())
    if not words:
        return []
    # The opening is the first few turns BEFORE the segment itself; counting the
    # segment's own turns would let a mismatch vouch for itself.
    turns = []
    for kind, _, t in _iter_script(script):
        if kind == "marker":
            break
        if kind == "turn":
            turns.append(t)
    opening = " ".join(turns[:6]).lower().replace("\u2011", "-")
    hit = sum(1 for w in words if w in opening)
    if hit < max(1, round(0.6 * len(words))):
        return [f'the opening agenda does not name the learning topic "{topic}" — the episode '
                f"must announce what it goes on to deliver"]
    return []


def list_problems(script: str) -> list:
    """A turn ending in a colon introduces a list that was never spoken. Ep18 had
    four: "we should watch two things:", "the key watchlist is:", and so on."""
    return [f"a turn ends with a colon, introducing a list that was never spoken — "
            f"\"…{text[-80:]}\""
            for kind, _, text in _iter_script(script)
            if kind == "turn" and text.rstrip().endswith(":")]


# Money, percentages, cents/basis points, and bare decimals (rates, levels).
_FIGURE = re.compile(
    r"[-+]?\$\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:K|M|B|thousand|million|billion)(?![A-Za-z]))?"
    r"|\d+(?:\.\d+)?\s*%"
    r"|\d+(?:\.\d+)?[\s-]*(?:cents?|¢|basis[\s-]points?|bps?)\b"
    r"|(?<![\d.])\d+\.\d+(?!\d|%|\.\d)", re.I)

FIGURES_PER_TURN_MAX  = 4      # a turn above this is hard to follow by ear
DENSITY_TARGET_PER100 = 1.6    # aim: about one figure every 60 words
DENSITY_RETRY_PER100  = 2.6    # above this the draft is regenerated once more


def figure_stats(script: str) -> dict:
    """How many numbers the listener has to hold in their head."""
    words = total = 0
    heavy, mx = [], 0
    for kind, _, text in _iter_script(script):
        if kind != "turn":
            continue
        t = text.replace("‑", "-").replace("‐", "-")
        n = len(_FIGURE.findall(t))
        words += len(t.split())
        total += n
        mx = max(mx, n)
        if n > FIGURES_PER_TURN_MAX:
            heavy.append((n, t[:70]))
    return {"total": total, "words": words, "max_turn": mx, "heavy_turns": heavy,
            "per100": (total / words * 100) if words else 0.0}


def density_problems(script: str) -> list:
    """Ep18 had 112 figures in 2,608 words (4.3 per 100), and one turn carried 14."""
    st = figure_stats(script)
    if st["per100"] > DENSITY_RETRY_PER100:
        return [f"too many numbers: {st['total']} figures in {st['words']} words "
                f"({st['per100']:.1f} per 100; aim for about {DENSITY_TARGET_PER100}), and "
                f"one turn carries {st['max_turn']}. Say fewer, rounder numbers."]
    return []


def collect_script_problems(script: str, facts: dict) -> list:
    """Everything wrong with a draft, classed by what to do about it.

    fatal   - could air a falsehood. Regenerate; if still present, strip the
              sentence; if it somehow survives that, refuse to publish.
    retry   - worth another attempt, but never worth losing the episode over.
    """
    out = []
    for msg in verify_script_figures(script, facts):
        out.append({"kind": "fact", "fatal": True, "retry": True, "msg": msg})
    for msg in scenario_problems(script):
        out.append({"kind": "structure", "fatal": False, "retry": True, "msg": msg})
    for msg in list_problems(script) + agenda_problems(script):
        out.append({"kind": "structure", "fatal": False, "retry": True, "msg": msg})
    for msg in density_problems(script):
        out.append({"kind": "density", "fatal": False, "retry": True, "msg": msg})
    st = figure_stats(script)
    if st["heavy_turns"] and not density_problems(script):
        out.append({"kind": "density", "fatal": False, "retry": False,
                    "msg": f"{len(st['heavy_turns'])} turn(s) carry more than "
                           f"{FIGURES_PER_TURN_MAX} figures (worst {st['max_turn']})"})
    return out


def _editor_notes(problems: list, limit: int = 7) -> str:
    """Turn the problems into instructions for the next attempt. Telling the model
    exactly what was wrong works far better than regenerating blind."""
    lines = [f"- {p['msg']}" for p in problems[:limit]]
    return ("EDITOR'S NOTES — your previous draft of this episode was REJECTED. Fix exactly "
            "these, in whichever half they concern, and keep to every rule below:\n"
            + "\n".join(lines) + "\n")


# ── Repair: list lines and speaker labels ────────────────────────────────────
_LIST_ITEM = re.compile(r"^\s*(?:[-*\u2022\u2013]|\d{1,2}[.)])\s+(.*\S)\s*$")
_ORDINALS  = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth"]
# Lower-case an item's first word only when it is an ordinary word, never a name:
# "First, watch the print" but "First, Nvidia reports".
_COMMON_FIRST = {"watch", "keep", "track", "look", "expect", "the", "a", "an", "if", "when",
                 "whether", "how", "what", "our", "we", "it", "that", "this", "any", "each",
                 "see", "check", "notice", "remember", "consider", "think", "ask", "listen"}


def _decap(item: str) -> str:
    first = item.split(" ", 1)[0].strip(",.;:").lower()
    return item[0].lower() + item[1:] if first in _COMMON_FIRST else item


def _normalize_script(script: str) -> str:
    """Make the model's formatting safe for parse_script, and fold list lines in.

    1. "**ALEX:**" and similar become "ALEX:". parse_script only accepts a line
       that starts with the bare label, so the bold form was silently dropped.
    2. Bullet lines that follow a turn are folded back into it as spoken
       sentences. Ep18 had four turns like "we should watch two things:" with the
       two things on separate bullet lines; both the audio and the transcript
       discarded them, leaving an introduction and nothing after it.
    """
    out, items = [], []
    state = {"parent": None, "ok": False}

    def flush():
        if not items or state["parent"] is None:
            items.clear()
            return
        prev = out[state["parent"]].rstrip()
        had_colon = prev.endswith(":")
        if had_colon:
            prev = prev[:-1] + "."
        elif not prev.endswith((".", "!", "?")):
            prev += "."
        ordinals = had_colon or len(items) >= 2
        parts = []
        for i, it in enumerate(items):
            it = re.sub(r"[*_`]+", "", it).strip()      # no stray markdown mid-sentence
            if not it:
                continue
            it = it[0].upper() + it[1:]
            if not it.endswith((".", "!", "?")):
                it += "."
            if ordinals:
                it = f"{_ORDINALS[min(i, len(_ORDINALS) - 1)]}, {_decap(it)}"
            parts.append(it)
        if parts:
            out[state["parent"]] = prev + " " + " ".join(parts)
        items.clear()

    for raw in script.split("\n"):
        line = re.sub(r"^\s*[*_]*\s*(ALEX|SAM)\s*[*_]*\s*:\s*[*_]*\s*", r"\1: ",
                      raw.rstrip(), flags=re.I)
        line = re.sub(r"^(alex|sam):", lambda m: m.group(1).upper() + ":", line)
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith(("ALEX:", "SAM:")):
            flush()
            state["parent"], state["ok"] = len(out), True
            out.append(line)
            continue
        m = _LIST_ITEM.match(s)
        if m and state["ok"] and state["parent"] is not None:
            items.append(m.group(1))
            continue
        flush()
        state["ok"] = False
        out.append(line)
    flush()
    return "\n".join(out)


def produce_checked_script(generate, max_attempts: int = 3, log=print):
    """Draft, check, and if need be redraft the script. Returns (script, facts) or None.

    generate(feedback) -> (script, facts). Each rejected draft is regenerated WITH
    the specific reasons it failed, which works far better than regenerating blind.
    A wrong number spoken with confidence is worse than a missing episode, and it
    shipped four times before anyone noticed — so the order of preference is:

      1. a draft that passes every check;
      2. after the attempts are spent, the last draft with each flagged sentence
         removed (a slightly shorter episode, and nothing flagged is ever aired);
      3. no episode at all, only if a flagged sentence somehow survives removal.

    Structure and density problems trigger a redraft but never cost the episode.
    """
    feedback, script, facts, problems = "", "", {}, []
    for attempt in range(1, max_attempts + 1):
        try:
            script, facts = generate(feedback)
        except Exception as exc:
            log(f"ERROR: Script generation failed (attempt {attempt}): {exc}")
            return None
        script   = _normalize_script(script)
        problems = collect_script_problems(script, facts)
        st       = figure_stats(script)
        retry    = [p for p in problems if p["retry"]]
        log(f"  attempt {attempt}/{max_attempts}: "
            f"{sum(1 for p in problems if p['fatal'])} factual problem(s), "
            f"{sum(1 for p in problems if p['retry'] and not p['fatal'])} structural/density; "
            f"{st['total']} figures in {st['words']} words ({st['per100']:.1f} per 100)")
        if not retry:
            break
        for p in retry[:6]:
            log(f"      • [{p['kind']}] {p['msg']}")
        if attempt < max_attempts:
            feedback = _editor_notes(retry)
            log("  ↻ regenerating with the editor's notes…")

    removed = []
    if any(p["fatal"] for p in problems):
        script, removed = strip_unsafe_sentences(script, facts)
        problems = collect_script_problems(script, facts)
        if any(p["fatal"] for p in problems):
            log(f"ERROR: Script still contains unsupported claims after {max_attempts} "
                f"attempts and sentence removal — refusing to publish.")
            for p in problems:
                if p["fatal"]:
                    log(f"      • {p['msg']}")
            return None
        log(f"  ⚠ Published with {len(removed)} unsafe sentence(s) removed:")
        for sent in removed[:8]:
            log(f"      - {sent[:140]}")
    leftovers = [p for p in problems if p["retry"] and not p["fatal"]]
    for p in leftovers:
        log(f"  ⚠ {p['msg']}")
    if not removed and not leftovers:
        log("  ✓ Script passes every check")
    return script, facts


def parse_script(script: str) -> list[tuple[str, str]]:
    turns = []
    for line in script.strip().split("\n"):
        line = line.strip()
        if line.startswith("ALEX:"):
            text = line[5:].strip()
            if text: turns.append(("ALEX", normalize_for_speech(text)))
        elif line.startswith("SAM:"):
            text = line[4:].strip()
            if text: turns.append(("SAM", normalize_for_speech(text)))
    return turns


def split_long_text(text: str, max_chars: int = 400) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(current) + len(sentence) > max_chars and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = (current + " " + sentence).strip() if current else sentence
    if current:
        chunks.append(current.strip())
    return chunks or [text]


def _pick_rate(text: str) -> str:
    n = len(text)
    if n < 60:  return SPEECH_RATE_SHORT
    if n < 200: return SPEECH_RATE_MEDIUM
    return SPEECH_RATE_LONG


async def _synthesize_one(text: str, voice: str, path: str, retries: int = 3) -> None:
    import edge_tts
    rate = _pick_rate(text)
    for attempt in range(retries):
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate)
            await comm.save(path)
            return
        except Exception as exc:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))


async def _generate_all_audio(turns: list[tuple[str, str]], tmp_dir: Path) -> list[str]:
    """Synthesize all turns with edge-tts, return ordered list of mp3 paths."""
    paths, tasks = [], []
    for i, (speaker, text) in enumerate(turns):
        voice  = VOICE_ALEX if speaker == "ALEX" else VOICE_SAM
        chunks = split_long_text(text)
        for j, chunk in enumerate(chunks):
            path = str(tmp_dir / f"seg_{i:04d}_{j:02d}.mp3")
            paths.append(path)
            tasks.append(_synthesize_one(chunk, voice, path))

    batch = 8
    for start in range(0, len(tasks), batch):
        await asyncio.gather(*tasks[start:start + batch])
        if start + batch < len(tasks):
            await asyncio.sleep(0.3)
        done = min(start + batch, len(tasks))
        if done % 40 == 0 or done == len(tasks):
            print(f"    Synthesized {done}/{len(tasks)} segments...")

    return paths


def merge_mp3s(segment_paths: list[str], output: Path) -> None:
    """Concatenate MP3 segments using pure Python byte concatenation."""
    with open(output, "wb") as out:
        for path in segment_paths:
            with open(path, "rb") as seg:
                out.write(seg.read())
    print(f"  ✓ MP3 created: {output}")


# ============================================================
# METADATA
# ============================================================
def load_meta() -> dict:
    try:
        if PODCAST_META.exists():
            return json.loads(PODCAST_META.read_text())
    except Exception:
        pass
    return {"episode": 0, "archive": []}


def audio_duration(path: Path) -> tuple[str, int]:
    """Return (HH:MM, seconds) duration."""
    try:
        from mutagen.mp3 import MP3
        secs = int(MP3(str(path)).info.length)
    except Exception:
        secs = int(path.stat().st_size / (64_000 / 8))
    return f"{secs // 60}:{secs % 60:02d}", secs


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        print("ERROR: GROQ_API_KEY not set")
        return 1

    DATA_DIR.mkdir(exist_ok=True)

    # 1. Load all data sources
    print("1/4  Loading data sources...")
    intel             = _load_intel()
    snapshot          = _fetch_snapshot()
    old_meta          = load_meta()
    computed_holdings = _fetch_computed_holdings()   # live from KV — auto-synced by dashboard
    cash_positions    = _fetch_cash_positions()      # real balances, not a hand-typed line

    if not intel.get("generated_at"):
        print("  ⚠ No intelligence.json found — generating without weekly intel data")
    if not computed_holdings:
        print("  ⚠ No computed_holdings in KV — portfolio figures will use fallback context")

    # Load saved past scripts & build deep topic registry (Groq preprocessing call if scripts exist)
    print("     Loading past scripts & building topic registry...")
    past_scripts = _load_past_scripts()
    registry = _build_deep_topic_registry(old_meta, past_scripts, groq_key)
    if registry["recently_spotlighted_tickers"]:
        print(f"  ✓ Spotlighted recently: {', '.join(registry['recently_spotlighted_tickers'][:5])}")
    if registry["education_topics_used"]:
        print(f"  ✓ Education topics used: {'; '.join(registry['education_topics_used'])}")

    # 2. Generate script (registry preprocessing + two generation Groq calls)
    print("2/4  Generating script...")
    result = produce_checked_script(
        lambda fb: generate_script(intel, snapshot, old_meta, groq_key, computed_holdings,
                                   registry, cash_positions=cash_positions, feedback=fb))
    if result is None:
        return 1
    script, facts = result

    # Weekdays are arithmetic — correct them rather than let the listener act on
    # the wrong day. Episode 16 sent them to an EIA report on "Wednesday,
    # September 18" when the 18th was a Friday.
    script, weekday_fixes = _fix_weekday_claims(script, datetime.now(timezone.utc))
    for fix in weekday_fixes:
        print(f"  ✓ Corrected weekday: {fix}")
    for warning in _speaker_run_warnings(script):
        print(f"  ⚠ {warning}")

    turns = parse_script(script)
    if len(turns) < 20:
        print(f"ERROR: Only {len(turns)} speaker turns parsed — script too short")
        return 1
    print(f"  ✓ {len(turns)} speaker turns")

    # Extract education topic from script marker (before saving — used in meta)
    education_topic = _extract_education_topic(script)
    if education_topic:
        print(f"  ✓ Education topic: {education_topic}")
    else:
        print("  ⚠ No [EDUCATION_TOPIC: ...] marker found in script")

    # 3. Synthesize audio
    print("3/4  Synthesizing audio (edge-tts)...")
    ep_num   = (old_meta.get("episode", 0) or 0) + 1
    mp3_name = f"podcast_ep{ep_num:03d}.mp3"
    txt_name = f"podcast_ep{ep_num:03d}.txt"
    mp3_path = DATA_DIR / mp3_name
    txt_path = DATA_DIR / txt_name

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        try:
            seg_paths = asyncio.run(_generate_all_audio(turns, tmp_dir))
            if not seg_paths:
                raise RuntimeError("No segments produced")
            merge_mp3s(seg_paths, mp3_path)
        except Exception as exc:
            print(f"ERROR: Audio generation failed: {exc}")
            import traceback; traceback.print_exc()
            return 1

    duration_str, duration_secs = audio_duration(mp3_path)
    print(f"  ✓ Duration: {duration_str} ({mp3_path.stat().st_size / 1_048_576:.1f} MB)")

    # Save the cleaned script (used by future episodes for the deep topic
    # registry, and read directly by the user). Section headers and stray rules
    # never reached the audio — parse_script drops them — but they were visible
    # in the transcript and fed back in as registry context.
    try:
        txt_path.write_text(_clean_transcript(script), encoding="utf-8")
        print(f"  ✓ Script saved: {txt_name} ({len(script.split()):,} words)")
    except Exception as exc:
        print(f"  ⚠ Script save failed (non-fatal): {exc}")

    # 4. Generate summary + save metadata
    print("4/4  Generating summary & saving metadata...")
    summary = generate_summary(script, intel, groq_key)

    # Use education_topic from marker; fall back to what summary extracted
    if not education_topic:
        education_topic = summary.get("education_topic", "")

    now = datetime.now(timezone.utc)

    # Build archive — preserve education_topic in each archived entry
    archive = []
    if old_meta.get("episode") and old_meta.get("file"):
        prev_entry = {
            "episode":         old_meta["episode"],
            "title":           old_meta.get("title", ""),
            "date":            old_meta.get("date", ""),
            "display_date":    old_meta.get("display_date", ""),
            "duration":        old_meta.get("duration", ""),
            "mood":            old_meta.get("mood", ""),
            "mood_summary":    old_meta.get("mood_summary", ""),
            "file":            old_meta.get("file", ""),
            "education_topic": old_meta.get("education_topic", ""),
            "summary":         old_meta.get("summary", {}),
        }
        archive = [prev_entry] + (old_meta.get("archive", []))
    archive = archive[:MAX_EPISODES - 1]

    # Clean up old MP3s and .txt files not in archive
    keep_mp3 = {mp3_name} | {a["file"] for a in archive if a.get("file")}
    keep_ep_nums = {ep_num} | {a.get("episode", 0) for a in archive}
    for f in DATA_DIR.glob("podcast_ep*.mp3"):
        if f.name not in keep_mp3:
            f.unlink()
    for f in DATA_DIR.glob("podcast_ep*.txt"):
        m = re.match(r"podcast_ep(\d+)\.txt$", f.name)
        if m and int(m.group(1)) not in keep_ep_nums:
            f.unlink()

    mood_val = intel.get("market_mood", "neutral")
    mood_labels = {
        "risk-on": "Markets favouring growth — leveraged positions in tailwind",
        "risk-off": "Defensive positioning — reduce leverage exposure",
        "neutral":  "Mixed signals — stay disciplined",
        "mixed":    "Conflicting signals — watch volatility closely",
    }

    meta = {
        "episode":          ep_num,
        "file":             mp3_name,
        "date":             now.strftime("%Y-%m-%d"),
        "display_date":     now.strftime("%B %d, %Y"),
        "title":            summary.get("episode_title", f"Portfolio Pulse Ep {ep_num}"),
        "mood":             mood_val,
        "mood_summary":     mood_labels.get(mood_val, mood_val),
        "duration":         duration_str,
        "duration_seconds": duration_secs,
        "generated_at":     now.isoformat(),
        "education_topic":  education_topic,
        "archive":          archive,
        "summary":          summary,
    }
    PODCAST_META.write_text(json.dumps(meta, indent=2))

    print(f"\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  ✓ Episode {ep_num}: {meta['title']}")
    print(f"  ✓ Duration: {duration_str} | Archive: {len(archive)} previous")
    if education_topic:
        print(f"  ✓ Education: {education_topic}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
