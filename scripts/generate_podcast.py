#!/usr/bin/env python3
"""
Portfolio Pulse — Weekly Podcast Generator
==========================================
Runs via cron-job.org every Monday at 6:00 AM UTC.

Pipeline:
  1. Load intelligence.json, the KV snapshot and settings, past scripts and the
     show's memory (data/podcast_memory.json).
  2. Plan the episode in code: one main story, a holding spotlight and quick news
     items (all distinct, none repeated from recent weeks), the portfolio desk
     (money in vs market, trades, dividends, sector mix, accounts, the house goal),
     last week's scenario scorecard, this week's fixed scenario frame and the
     learning topic promised the week before.
  3. Groq (gpt-oss-120b) writes six segments, each from its own material only.
     Segments with problems are rewritten (at most twice); what still cannot air
     is cut. Paced and budgeted to stay inside the free tier.
  4. Audio: cold open → intro theme → episode → outro theme. Microsoft voices
     first, Kokoro then Piper as fallbacks.
  5. Save podcast_epNNN.mp3/.txt, podcast_meta.json and the show's memory.

Voices: en-US-AndrewMultilingualNeural (Alex), en-US-AvaMultilingualNeural (Sam)
Format: a weekly magazine show — cold open, portfolio review, one main story,
around the portfolio, a learning segment, scored scenarios, next-week tease.
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
import podcast_desk as _desk                                  # noqa: E402
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
If the stock market moves by this much, the effect on the portfolio is the figure below. The leverage is already built in:
never apply a multiplier to it, and never explain leverage to the listener.
  {idx_line}
  (Same amount up or down.)
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
- At most 4 numbers in any one turn; about 12 per half; never more than 18. State each figure ONCE.
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
    "ZSP.TO": "BMO S&P 500 ETF", "KGS": "Kodiak Gas Services", "COST": "Costco",
    "NFLX": "Netflix", "SOXL": "Semiconductor 3x ETF",
}

# What each holding IS, so the script never has to guess. The redone episode 22
# called Kodiak Gas Services "the Canadian oil services firm" with "rigs"; it is a
# US natural-gas compression company. A holding is described only this way.
HOLDING_PROFILES = {
    "FNGU": "3x fund on the FANG+ index of ten big US tech names",
    "SPXL": "3x fund on the S&P 500", "UDOW": "3x fund on the Dow Jones Industrial Average",
    "SOXL": "3x fund on US semiconductor stocks", "TQQQ": "3x fund on the Nasdaq-100",
    "NVDA": "US chip designer, AI accelerators", "AVGO": "US chips and infrastructure software",
    "TSM": "Taiwanese contract chipmaker", "QCOM": "US mobile and wireless chip designer",
    "MSFT": "US software and cloud", "AAPL": "US consumer devices and services",
    "TXF.TO": "Canadian-listed ETF of large US tech stocks that sells covered calls for income",
    "TSLA": "US electric vehicles and energy storage", "BYDDF": "Chinese electric vehicles and batteries",
    "NFLX": "US streaming", "LYV": "US live concerts and ticketing",
    "COST": "US membership warehouse retailer",
    "CM.TO": "Canadian bank (CIBC)", "RY.TO": "Canadian bank (Royal Bank of Canada)",
    "BMO.TO": "Canadian bank (Bank of Montreal)", "IBKR": "US online brokerage",
    "V": "US card-payments network",
    "ENB.TO": "Canadian oil and gas pipelines and gas utility", "ET": "US pipelines and storage partnership",
    "SHEL": "British integrated oil and gas major", "KGS": "US natural-gas compression services",
    "MSTR": "US software company that holds large amounts of bitcoin", "GBTC": "fund that holds bitcoin",
}


# ============================================================
# SCRIPT PROMPT — PART 1: Welcome + Recap + Deep Dive 1
# ============================================================
# ============================================================
# THE SHOW — six segments, each written by its own call
# ============================================================
# Until episode 22 the script was written in two (then four) big calls that each
# saw EVERYTHING: all the news, the whole portfolio page, the currency box and both
# deep-dive subjects. So every part talked about every subject — ep22 explained the
# exchange rate twice, restated its watch list five times, deep-dived the same Saudi
# oil-storage story twice, and used seven analogies. Now each segment is given only
# its own material, is told what the other segments cover, and is checked on its own.
#
# Run of show (about 17-20 minutes):
#   1 opening      cold open, [THEME], welcome, last week's scorecard, run-down
#   2 desk         the portfolio this week: money in vs market, trades, dividends,
#                  sector mix, movers, accounts, the house goal
#   3 main         ONE main story, with the other side argued
#   4 around       two quick news items + one holding spotlight
#   5 learning     the learning segment (topic promised the week before)
#   6 scenarios    three fixed-threshold cases for the coming week, the close,
#                  and what is coming next week
# The intro theme plays at [THEME]; the outro theme plays after the last line.

SHOW_BIBLE = """You are writing ONE segment of "Portfolio Pulse Weekly", a weekly podcast about one real portfolio, made for its owner.
{editor_notes}
THE LISTENER: Christopher, 24, Toronto. Base currency CAD. High risk tolerance. Saving for a GTA home: the FHSA and the RRSP are the down-payment money (target about $90,000). He has heard every episode — never re-explain a basic idea from scratch.
THE HOSTS: ALEX is the analyst — calm, precise, explains cause and effect in plain words. SAM is the co-host — curious and practical, asks what a listener would ask, pushes back, sometimes disagrees, and ties things to Christopher's goals. They are friends: light, natural banter is fine; catchphrases are not.
THE SOUND: a well-produced weekly show. Conversational and specific. Most turns are one to four sentences; about a quarter are under fifteen words. Vary how turns begin.
FORMAT: every line begins with "ALEX:" or "SAM:" — no headings, bullets, numbered lists, markdown or stage directions. The only exceptions are marker lines this segment asks for, written exactly as shown.
FACTS: use only facts in this prompt. Never invent a number, price, date, event, cause, company announcement, earnings result or statistic. If no reason is given for a move, say it moved with the market.
NUMBERS: at most 4 in any turn. Round them and write them as digits — "about $14,000", "about 9.5%" — never "$14,386"; the voice reads digits aloud properly. No arithmetic out loud. Say each figure once. Never state a level for the S&P 500 or any index — you are not given one.
NEVER explain how leveraged or 3x funds work — no amplifying, no daily reset, no decay. The listener knows. Say what they did.
A company is a holding only if it is in WHAT WE OWN, and it is described only as WHAT WE OWN describes it — never guess what a company does, where it is based, or confuse one holding with another.
NO ADVICE: never tell Christopher to buy, sell, add, trim or hold anything. Give perspective, not instructions.
The portfolio is ONLY what WHAT WE OWN lists, at the weights shown: it holds no bonds, no cash plan and nothing else. Never call a small holding "core".
{currency_rule}
SAY IT ONCE: this segment covers only its own subject. Do not recap earlier segments or preview later ones (apart from a one-line handoff where asked), and never restate a point in new words. This week the other segments cover: {elsewhere}. Leave those alone.
ANALOGIES: {analogy_rule}
Never say: "it's worth noting", "going forward", "at the end of the day", "landscape", "navigate", "tailwinds", "headwinds", "buckle up", "let's dive in", "in today's market", "in my own words", "what does that actually mean".
DATE: this episode airs {today}. {briefing_note}
WHAT WE OWN (and what each one is): {held_line}
{continuity}
"""

CURRENCY_RULE_OFF = ("CURRENCY: do not mention the exchange rate, the US or Canadian dollar getting stronger "
                     "or weaker, or currency effects at all. It is not this segment's subject this week.")
CURRENCY_RULE_ON = """CURRENCY (this segment owns it — at most two sentences, and get the direction right): every US-listed holding is held and priced in US dollars; the Canadian-dollar figures are only a translation. USD/CAD going UP means the US dollar got stronger and our US holdings are worth MORE in Canadian dollars; going DOWN means they are worth less. Never call a stronger US dollar a drag on us, and never say we hold US assets in Canadian dollars."""

SEG_OPENING = """━━━ SEGMENT 1 of 6 — THE OPENING ━━━
Write, in this order:
1. COLD OPEN — two or three short lines between ALEX and SAM BEFORE the theme music, on this week's most interesting fact: {hook} Say that fact and react to it — add nothing to it: no reason, no comparison, no record, no "all year", no "biggest ever". No greeting yet.
2. A line that is exactly: [THEME]
3. WELCOME — ALEX welcomes everyone to Portfolio Pulse Weekly; SAM says hello. Two short turns.
4. LAST TIME — {previously}
5. RUN-DOWN — one ALEX turn naming today's segments in order, plainly, without explaining them: {rundown}.
6. SAM hands over to the portfolio review in one line.
LENGTH: 260 to 340 words. Write segment 1 now:"""

SEG_DESK = """━━━ SEGMENT 2 of 6 — THE PORTFOLIO THIS WEEK ━━━
A proper weekly portfolio review, the way a good adviser would walk a client through it. These are the facts, already rounded — the only portfolio numbers you may use:
{desk}
NUMBERS IN THIS SEGMENT: at most TWO in any turn. Spread them across SAM's questions; never read a list of figures.
Cover these, in order, each ONCE:
1. The headline: start with what the portfolio is worth and how the week went. Then, in the next turns, how much of that change was new money Christopher put in versus what the market did, and total profit against everything he has contributed.
2. What he did: trades (new positions, additions, sales) and dividends received. If there were none, one line and move on. For a trade, SAM asks what it does to the portfolio's risk or balance; ALEX answers from the sector mix.
3. How the mix shifted: the sector changes listed — they come from price moves and trades together, so never say a trade caused a shift — and what they say about how concentrated the portfolio is.
4. Movers: the one that rose most and the one that fell most, with a reason only if the news gives one.
5. The accounts and the house: how the down-payment accounts are tracking against the target.
{fx_instruction}
End with ALEX handing over to today's main story in one line.
LENGTH: 560 to 700 words. Write segment 2 now:"""

SEG_MAIN = """━━━ SEGMENT 3 of 6 — THE MAIN STORY: {title} ━━━
What the news says — your only facts about it:
{brief}
Keep its certainty: if it says something "may" happen or is being "evaluated", say exactly that — never that it happened. Add no details it does not give: who, how many, what kind, prices, or market reactions.
What we own that it touches: {exposure}
Structure:
1. SAM sets up the question the story raises, in a line or two.
2. ALEX explains what happened and why, one step at a time.
3. What it means for THIS portfolio — name the holdings it touches and say how, in words.
4. The other side: SAM makes the strongest case that it matters less (or more) than it seems; ALEX answers honestly, including what we do not know yet.
5. One thing to watch next, said once — only an event named in the news, or the type of event with no invented date.
End with SAM moving on to the rest of the week's news in one line.
LENGTH: 750 to 920 words. Write segment 3 now:"""

SEG_AROUND = """━━━ SEGMENT 4 of 6 — AROUND THE PORTFOLIO ━━━
{count} short item{plural}, three to five turns each, each introduced so the listener knows a new item has started (vary how). None of them is the main story.
{items}
For a news item: what happened, then why it matters — or doesn't — for what we own.
For the holding spotlight: what the business does, why it is in the portfolio, how big the position is, and the news if there is any.
End with ALEX turning to the learning segment in one line.
LENGTH: 480 to 620 words. Write segment 4 now:"""

SEG_LEARNING = """━━━ SEGMENT 5 of 6 — THE LEARNING SEGMENT: {topic} ━━━
First line, exactly: [EDUCATION_TOPIC: {topic}]
Then teach {topic} over several turns: what it is, how it works step by step, and how an investor actually uses it. One worked example with small round numbers (like $100) — say each example number once. Use no current market data. SAM asks the practical question, and ALEX ties the idea to THIS portfolio in words, with no new figures.
The last line is SAM saying: "That's our learning segment for this week."
LENGTH: 440 to 560 words. Write segment 5 now:"""

SEG_SCENARIOS = """━━━ SEGMENT 6 of 6 — SCENARIOS AND THE CLOSE ━━━
SCENARIOS for the coming week. The three cases below are fixed — do not change them. You choose the odds: they add up to 100, and the base case is between 45 and 55.
{frame}
For EACH case, two to four turns, in this order:
 a. ALEX opens it with a line beginning exactly "Base case — NN percent:" (or "Bull case — NN percent:", "Bear case — NN percent:") and says what would have to happen, using only these drivers from this week's news: {drivers}
 b. The signpost: the one thing during the week that would tell us it is happening.
 c. What it would mean for the portfolio, using the figure given for that case — said once.
 d. SAM asks how Christopher should think about it; ALEX gives perspective tied to his goals and time horizon — never "buy" or "sell".
THE CLOSE:
 - One question for Christopher to think about this week — about investing itself, not about data.
 - NEXT WEEK: {next_week}
 - A warm, brief sign-off from both hosts.
LENGTH: 540 to 680 words. Write segment 6 now:"""


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
    "Dollar-Cost Averaging Versus Lump Sums",
    "What An Earnings Report Contains",
    "How Portfolio Rebalancing Works",
    "Real Versus Nominal Returns",
    "Tax-Loss Harvesting Basics",
]
# Deliberately absent: leveraged-ETF reset/decay and leverage-ratio topics. The listener
# already knows them and asked not to hear them again.


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
# ── Groq free-tier guard ────────────────────────────────────────────────────
# Limits for openai/gpt-oss-120b on the free plan, per ORGANISATION (shared with
# the daily briefing): 8,000 tokens/minute (max_tokens counts toward it), 200,000
# tokens/day, 1,000 requests/day. The pacer below keeps every rolling minute
# under the TPM limit, and the run budget keeps one podcast run to about half the
# daily allowance, so the briefing and an occasional manual re-run still fit.
GROQ_TPM_LIMIT        = 7_600     # a little under 8,000 for estimate error
GROQ_RUN_TOKEN_BUDGET = 110_000   # one weekly run, all attempts included
_GROQ_RECENT: list = []           # (time, tokens charged) in the last minute
_GROQ_USED = {"tokens": 0, "calls": 0}


class GroqDailyLimit(RuntimeError):
    """Groq's tokens-per-day allowance is spent; waiting minutes will not help."""


def _estimate_tokens(text: str) -> int:
    return int(len(text) / 3.6) + 50      # deliberately high: ~4 chars per token in practice


def groq_tokens_used() -> int:
    return _GROQ_USED["tokens"]


def groq_budget_allows(extra_tokens: int) -> bool:
    return _GROQ_USED["tokens"] + extra_tokens <= GROQ_RUN_TOKEN_BUDGET


def _pace(charge: int) -> None:
    """Sleep until this request fits inside the rolling one-minute token window."""
    import time
    while True:
        now = time.time()
        _GROQ_RECENT[:] = [(t, c) for t, c in _GROQ_RECENT if now - t < 61]
        if not _GROQ_RECENT or sum(c for _, c in _GROQ_RECENT) + charge <= GROQ_TPM_LIMIT:
            _GROQ_RECENT.append((now, charge))
            return
        wait = 61 - (now - _GROQ_RECENT[0][0]) + 0.5
        print(f"     pacing {wait:.0f}s to stay under Groq's per-minute limit")
        time.sleep(max(1.0, wait))


def _safe_max_tokens(prompt: str, wanted: int) -> int:
    """prompt + max_tokens must stay under 8,000 or Groq rejects the request (413)."""
    return max(1200, min(wanted, 7_900 - _estimate_tokens(prompt)))


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

    max_tokens = _safe_max_tokens(prompt, max_tokens)
    daily_waited = False
    for attempt in range(max_attempts):
        try:
            _pace(_estimate_tokens(prompt) + max_tokens)
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
                body = r.text.lower()
                if "per day" in body or "tpd" in body or "rpd" in body:
                    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", body)
                    wait = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
                            + float(m.group(3))) if m else 99_999
                    if wait <= 1_200 and not daily_waited:
                        daily_waited = True
                        print(f"  ⚠ {label}: Groq daily allowance momentarily full — waiting {wait:.0f}s")
                        time.sleep(wait + 5)
                        continue
                    raise GroqDailyLimit(f"{label}: Groq daily allowance used up — {r.text[:200]}")
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
            body = r.json()
            used = int((body.get("usage") or {}).get("total_tokens") or 0)
            _GROQ_USED["tokens"] += used or (_estimate_tokens(prompt) + max_tokens)
            _GROQ_USED["calls"] += 1
            text = body["choices"][0]["message"]["content"].strip()
            if text:
                print(f"  ✓ {label} with {primary} ({len(text.split()):,} words, "
                      f"{used:,} tokens; run total {_GROQ_USED['tokens']:,})")
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

        except GroqDailyLimit:
            raise
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
def _choose_deep_dive_1(intel: dict, clean, recent_themes=frozenset(), exclude_titles=()) -> dict:
    """The most important macro story that is NOT a theme covered in the last few
    episodes. Taking macro[0] blindly put the dollar and the CAD in ep15, ep17 and
    ep18 in a row."""
    cands = []
    for m in intel.get("macro", []) or []:
        if m.get("title"):
            cands.append((_themes_of(m["title"]),
                          {"title": str(m["title"]).strip(),
                           "body": clean(m.get("body", ""), 420),
                           "bull": clean(m.get("bull", ""), 160),
                           "bear": clean(m.get("bear", ""), 160)}))
    for n in intel.get("news", []) or []:
        if n.get("headline"):
            cands.append((_themes_of(n["headline"]),
                          {"title": str(n["headline"]).strip(),
                           "body": clean(n.get("body", ""), 420), "bull": "", "bear": ""}))
    # Never a story a recent episode already told.
    cands = [c for c in cands if not any(_desk.same_story(c[1]["title"], x) for x in exclude_titles if x)] or cands
    pick = _pick_fresh(cands, frozenset(recent_themes))
    return pick or {"title": "the biggest market story of the week", "body": "", "bull": "", "bear": ""}


def _choose_deep_dive_2(intel: dict, facts: dict, recently_spotlighted: list, clean,
                        avoid_themes=frozenset(), exclude_titles=()):
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

    cands = []
    for n in intel.get("news", []) or []:
        head = str(n.get("headline") or "")
        # Ep22 spotlit Enbridge on the very Saudi oil-storage story the main segment had
        # just told. The spotlight's news must be a different story on a different theme.
        if (any(_desk.same_story(head, x) for x in exclude_titles if x)
                or (_themes_of(head) & frozenset(avoid_themes))):
            continue
        for tkr in re.findall(r"\b[A-Z]{1,5}(?:\.TO)?\b", str(n.get("exposure") or "")):
            if tkr in positions and tkr not in _LEVERAGE_3X and tkr.upper() not in spot:
                cands.append((_themes_of(head), (tkr, n)))
                break
    chosen = _pick_fresh(cands, frozenset(avoid_themes))
    if chosen:
        return pick(chosen[0], chosen[1])
    cands = sorted(((p["cad"], t) for t, p in positions.items()
                    if t not in _LEVERAGE_3X and t.upper() not in spot), reverse=True)
    return pick(cands[0][1]) if cands else None


def _deep_dive_briefs(dd1: dict, dd2) -> tuple:
    """(dd1_title, dd1_brief, dd2_title, dd2_brief) as the prompts print them."""
    b1 = f"What the briefing says: {dd1['body']}" if dd1["body"] else ""
    # Only when the story touches the dollar: sentences about the currency were removed
    # from the briefing because they were wrong, so say where the right version is.
    if "currency" in _themes_of(dd1["title"] + " " + dd1["body"]):
        b1 += ("\nFor how the dollar affects us, use the CURRENCY box above — not the briefing's "
               "wording about it.")
    # The briefing's own "if it goes well / badly" lines are deliberately left out: they are
    # speculation, and one called a deeper slowdown "going well" because of "ENB's hedged
    # pipeline business". Fewer claims, fewer invented causal chains.

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


def _groq_part(api_key: str, build, label: str, editor_notes: str, min_words: int,
               max_tokens: int = 3200) -> str:
    """One segment. A segment that comes back short is asked for again, once, with the
    shortfall stated, but only while the run's Groq budget allows. The longer draft is kept."""
    text = _groq_call(api_key, build(editor_notes), label, max_tokens=max_tokens)
    n = len(text.split())
    if n >= min_words:
        return text
    if not groq_budget_allows(SEGMENT_COST_ESTIMATE):
        print(f"     {label} is {n:,} words, but the run's Groq budget is reserved — keeping it")
        return text
    print(f"     {label} is {n:,} words, under {min_words:,} — asking for a fuller version")
    note = (f"LENGTH FIX: your last draft of this segment was only {n:,} words. Write at least "
            f"{min_words + 100:,} words: slow the explanation down, give SAM more of the listener's "
            f"questions, and go one step deeper. Do NOT add figures, dates or events to get there.\n")
    try:
        again = _groq_call(api_key, build((editor_notes or "") + "\n" + note), label + " (fuller)",
                           max_tokens=max_tokens)
    except GroqDailyLimit:
        raise
    except Exception as exc:  # noqa: BLE001  the shorter draft is still usable
        print(f"     ⚠ fuller {label} failed ({str(exc)[:80]}) — keeping the first draft")
        return text
    return again if len(again.split()) > n else text


# ── Episode plan: everything decided BEFORE the model writes a word ─────────
MEMORY_FILE = DATA_DIR / "podcast_memory.json"

# (key, template, min words, max_tokens). A segment that comes back under its
# minimum is asked for once more (budget permitting); max_tokens leaves room for
# the answer while keeping each request well inside Groq's 8,000-token limit.
SEGMENTS = (
    ("opening",   SEG_OPENING,   230, 1800),
    ("desk",      SEG_DESK,      480, 2600),
    ("main",      SEG_MAIN,      650, 2800),
    ("around",    SEG_AROUND,    420, 2400),
    ("learning",  SEG_LEARNING,  380, 2200),
    ("scenarios", SEG_SCENARIOS, 470, 2600),
)
SEGMENT_KEYS = tuple(s[0] for s in SEGMENTS)
SEGMENT_TITLES = {"opening": "the opening", "desk": "the portfolio this week",
                  "main": "the main story", "around": "around the portfolio",
                  "learning": "the learning segment", "scenarios": "scenarios and the close"}
ANALOGY_SEGMENTS = ("main", "learning")      # the only places a comparison is allowed
REPAIR_ROUNDS    = 2
SEGMENT_COST_ESTIMATE = 6_000                 # tokens for one segment call, measured high


def _fetch_sp500() -> dict:
    """{YYYY-MM-DD: close} for the S&P 500 over the last two months, or {}."""
    try:
        r = requests.get("https://query2.finance.yahoo.com/v8/finance/chart/%5EGSPC",
                         params={"interval": "1d", "range": "2mo"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        out = {}
        for ts, c in zip(res["timestamp"], closes):
            if c:
                out[datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")] = float(c)
        return out
    except Exception as exc:
        print(f"  ⚠ S&P 500 history unavailable ({exc}) — no scorecard this week")
        return {}


def _close_on_or_before(series: dict, date: str):
    keys = [d for d in sorted(series) if d <= date]
    return (keys[-1], series[keys[-1]]) if keys else (None, None)


def _fetch_settings() -> dict:
    try:
        r = requests.get(SETTINGS_API, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        print(f"  ⚠ settings fetch failed: {exc}")
        return {}


def _clean_briefing(text, limit, sink: list):
    """The briefing is the model's only window on the world. Drop wrong-direction
    currency sentences and leverage explainers before the model sees them."""
    c, gone = scrub_fx_sentences(str(text or ""), strict_wording=True)
    sink.extend(gone)
    c = " ".join(x for x in _split_sentences(c) if not _LEVERAGE_TALK.search(x))
    return c[:limit]


def _intel_titles(intel: dict) -> list:
    return [str(x.get(k) or "") for src, k in (("macro", "title"), ("news", "headline"))
            for x in (intel or {}).get(src) or [] if x.get(k)]


def _recent_context(old_meta: dict, memory: dict, ep_num: int, past_scripts: dict,
                    intel_for_context: dict = None) -> dict:
    """What recent episodes covered — from the memory file, bootstrapped from the
    metadata archive and the saved transcripts for episodes made before it existed."""
    mem_recent = _desk.recent(memory, ep_num, 4)
    metas = ([old_meta] if old_meta.get("episode") else []) + list(old_meta.get("archive") or [])
    edu = [m.get("education_topic") for m in metas if m.get("education_topic")]
    edu += [e.get("learning_topic") for e in mem_recent if e.get("learning_topic")]
    themes, headlines, spotlit, analogies = set(), [], [], []
    for e in mem_recent[-3:]:
        themes |= set((e.get("main_story") or {}).get("themes") or [])
        headlines += [(e.get("main_story") or {}).get("title", "")] + list(e.get("quick_hits") or [])
        if e.get("spotlight"):
            spotlit.append(e["spotlight"])
    for m in metas[:3]:
        themes |= set(_themes_of(m.get("title", "")))
        for item in ((m.get("summary") or {}).get("position_spotlight") or []):
            for tkr, name in COMPANY_NAMES.items():
                if (tkr in item or name.lower() in item.lower()) and tkr not in spotlit:
                    spotlit.append(tkr)
    for e in mem_recent:
        analogies += [a for a in (e.get("analogies") or []) if a not in analogies]
    for n in sorted(past_scripts)[-4:]:
        analogies += [a for a in _desk.extract_analogies(past_scripts[n]) if a not in analogies]
    last = _desk.last_episode(memory, ep_num)
    # Episodes made before the memory file existed: a headline is "already told" if
    # nearly all of its distinctive words were spoken in one of the last two episodes
    # (ep22 read "Saudi Arabia to store oil in Singapore…" out in its agenda).
    # Only the opening, where the agenda names the episode's actual subjects — a word
    # mentioned in passing an hour in ("tariff talks") does not make a story "told".
    recent_text = [past_scripts[n][:1500].lower() for n in sorted(past_scripts)[-2:]]
    for title in _intel_titles(intel_for_context):
        words = [w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in _desk._STOPWORDS]
        if len(words) >= 4 and any(sum(w in t for w in words) / len(words) >= 0.8 for t in recent_text):
            headlines.append(title)
    return {"education_used": edu, "themes": frozenset(themes), "headlines": [h for h in headlines if h],
            "spotlit": spotlit, "analogies": analogies[:24], "last": last}


def _pick_quick_hits(intel: dict, exclude_titles: list, avoid_themes: frozenset, clean, n: int = 2) -> list:
    """Two short news items: none the same story as the main story, the spotlight
    or a recent episode's subjects, and not two on the same theme."""
    cands = []
    for src, tkey in (("news", "headline"), ("macro", "title")):
        for it in intel.get(src) or []:
            title = str(it.get(tkey) or "").strip()
            if not title or any(_desk.same_story(title, x) for x in exclude_titles):
                continue
            if "currency" in _themes_of(title):       # only the portfolio review talks currency
                continue
            cands.append({"title": title, "body": clean(it.get("body", ""), 260),
                          "exposure": str(it.get("exposure") or "")[:100], "themes": _themes_of(title)})
    picked, used = [], set(avoid_themes)
    for c in cands:                                   # fresh themes first
        if not (c["themes"] & used):
            picked.append(c)
            used |= c["themes"]
        if len(picked) == n:
            return picked
    for c in cands:                                   # then anything distinct
        if c not in picked and not any(_desk.same_story(c["title"], p["title"]) for p in picked):
            picked.append(c)
        if len(picked) == n:
            break
    return picked


def _desk_text(facts: dict, ch: dict, sp_week, fx_effect) -> str:
    """The portfolio segment's facts, rounded for speech."""
    def m(v):
        return _say_money(v)
    L = []
    d = "up" if ch["change"] >= 0 else "down"
    L.append(f"Portfolio value: about {m(ch['total_now'])} CAD (data to {ch['latest_date']}).")
    L.append(f"This week ({ch['days']} days): {d} about {m(ch['change'])} ({d} about {_say_pct(ch['change_pct'])}).")
    if ch["contrib_added"]:
        md = "up" if ch["market_change"] >= 0 else "down"
        L.append(f"New money Christopher added this week: about {m(ch['contrib_added'])}. "
                 f"So the market itself moved the portfolio {md} about {m(ch['market_change'])}.")
    else:
        L.append("New money added this week: none — the whole change was the market.")
    L.append(f"Everything contributed since the start: about {m(ch['contrib_total'])}. Total profit on top of "
             f"that: about {m(ch['pnl_total'])}, about {ch['pnl_pct']:.0f} percent.")
    if sp_week is not None:
        L.append(f"The S&P 500 this week: {'up' if sp_week >= 0 else 'down'} about {_say_pct(sp_week)}.")
    if ch["trades"]:
        for t in ch["trades"]:
            nm = _LEVERAGE_NAMES.get(t["ticker"]) or COMPANY_NAMES.get(t["ticker"], t["name"])
            acct = _desk.ACCOUNT_LABEL.get(t["account"], t["account"])
            L.append(f"Trade: {t['kind']} {nm} in the {acct} — about {m(t['cad'])}.")
    else:
        L.append("Trades this week: none.")
    dv = ch["dividends"]
    if dv["count"]:
        top = _LEVERAGE_NAMES.get(dv["top"]) or COMPANY_NAMES.get(dv["top"], dv["top"])
        L.append(f"Dividends received: {dv['count']}, about {m(dv['total'])} in total; the biggest from {top}.")
    else:
        L.append("Dividends received this week: none.")
    w = ch["sector_weights"]
    top = list(w.items())[:2]
    L.append("The two biggest slices: " + " and ".join(f"{_sector_name(s)} about {v:.0f}%" for s, v in top) + ".")
    if ch["sector_shifts"]:
        L.append("Biggest sector shifts this week: " + "; ".join(
            f"{_sector_name(s['sector'])} from about {s['then']:.0f}% to about {s['now']:.0f}%"
            for s in ch["sector_shifts"]) + ".")
    else:
        L.append("The sector mix barely moved this week.")
    mv = sorted(facts.get("movers") or [], key=lambda x: x["pct"])
    if mv:
        up, dn = mv[-1], mv[0]
        nm = lambda t: _LEVERAGE_NAMES.get(t) or COMPANY_NAMES.get(t, t)
        if up["pct"] > 0:
            L.append(f"Best mover: {nm(up['ticker'])}, up about {_say_pct(up['pct'])} (about {m(up['cad'])} for us).")
        if dn["pct"] < 0:
            L.append(f"Worst mover: {nm(dn['ticker'])}, down about {_say_pct(dn['pct'])} (about {m(dn['cad'])} for us).")
    acc = ch["accounts"]
    if acc:
        big = max(acc.items(), key=lambda kv: abs(kv[1]["change"]))
        L.append(f"The account that moved most: the {_desk.ACCOUNT_LABEL.get(big[0], big[0])}, "
                 f"{'up' if big[1]['change'] >= 0 else 'down'} about {m(big[1]['change'])}.")
    house, target = ch["house"]["value"], DOWN_PAYMENT_TARGET
    L.append(f"The house: FHSA and RRSP together about {m(house)} against the {m(target)} target — "
             f"about {house / target * 100:.0f}% of the way.")
    if fx_effect is not None:
        L.append(f"Currency: USD/CAD went from about {ch['usdcad_then']:.3f} to about {ch['usdcad_now']:.3f}, "
                 f"a move of about {abs(ch['fx_cents']):.1f} cents — "
                 f"worth about {m(fx_effect)} {'more' if ch['fx_cents'] > 0 else 'less'} in Canadian dollars "
                 f"for our US holdings.")
    return "\n".join("- " + x for x in L)


_SECTOR_SPOKEN = {"Leveraged ETF": "leveraged funds", "Technology": "technology", "Financials": "financials",
                  "Energy": "energy", "Consumer Disc.": "consumer discretionary",
                  "Comm. Services": "communication services", "Consumer Staples": "consumer staples",
                  "Crypto": "crypto", "Unknown": "other", "Other": "other"}


def _sector_name(s: str) -> str:
    return _SECTOR_SPOKEN.get(s, str(s).lower())


def build_episode_plan(intel, snapshot, settings, old_meta, memory, past_scripts, ep_num, now=None) -> dict:
    """Choose every subject and compute every number. The model only writes."""
    now = now or datetime.now(timezone.utc)
    removed_fx: list = []
    clean = lambda t, n=300: _clean_briefing(t, n, removed_fx)
    snaps = (snapshot or {}).get("snapshots", {})
    holdings = settings.get("computed_holdings") or []
    _, facts = _build_portfolio_context(holdings, snaps, settings.get("cash_positions") or [])
    rc = _recent_context(old_meta, memory, ep_num, past_scripts, intel)

    # ── The learning topic: keep last week's promise if there was one ──
    promised = (rc["last"] or {}).get("next_learning_topic")
    used = rc["education_used"]
    if promised and not any(str(promised).lower() == str(u).lower() for u in used):
        topic = promised
    else:
        topic = _choose_education_topic(used)
    next_topic = _choose_education_topic(used + [topic])

    # ── Subjects: one main story, a spotlight and two quick hits, all distinct ──
    # Currency is never the main story: the listener asked to hear less about the
    # exchange rate, and the portfolio review covers it whenever it actually moves.
    main = _choose_deep_dive_1(intel, clean, rc["themes"] | {"currency"}, exclude_titles=rc["headlines"])
    spot = _choose_deep_dive_2(intel, facts, rc["spotlit"], clean,
                               rc["themes"] | _themes_of(main["title"]),
                               exclude_titles=[main["title"]] + rc["headlines"])
    quick = _pick_quick_hits(intel, [main["title"]] + ([spot["headline"]] if spot and spot.get("headline") else [])
                             + rc["headlines"], _themes_of(main["title"]) | rc["themes"], clean)

    # ── The portfolio week ──
    dates = [d for d in sorted(snaps) if (snaps[d] or {}).get("holdings_prices")]
    ch = (_desk.portfolio_changes(settings, snaps, facts["baseline_date"], facts["latest_date"])
          if facts and len(dates) >= 2 else None)
    sp = _fetch_sp500()
    sp_week = None
    if sp and ch:
        _, a = _close_on_or_before(sp, ch["baseline_date"])
        _, b = _close_on_or_before(sp, ch["latest_date"])
        sp_week = round((b / a - 1) * 100, 2) if a and b else None
    sp_date, sp_close = _close_on_or_before(sp, (ch or {}).get("latest_date") or now.strftime("%Y-%m-%d"))
    fx_on = bool(ch) and _desk.fx_is_newsworthy(ch)
    fx_effect = None
    if fx_on and facts.get("fx_table"):
        per_cent = float(facts["usd_notional"]) * 0.01
        fx_effect = round(per_cent * abs(ch["fx_cents"]))
    main_is_fx = "currency" in _themes_of(main["title"] + " " + main["body"])
    fx_owner = "desk" if fx_on else ("main" if main_is_fx else None)

    # ── Last week, scored from data ──
    score = _desk.score_last_week(rc["last"], sp_close) if rc["last"] else None
    if score and score.get("probability") is not None:
        verb = "rose" if score["move_pct"] > 0 else "fell"
        previously = (f"Last episode set three scenarios for the week. What happened: the S&P 500 {verb} about "
                      f"{_say_pct(abs(score['move_pct']))}, so the {score['label']} played out — we had given it "
                      f"{score['probability']} percent. Give that verdict in one or two lines, honestly; if the call "
                      f"was wrong, say so with good humour. ")
    elif rc["last"]:
        previously = "Do not score last week's scenarios (no data). "
    else:
        previously = ("This is the first episode in the show's new format. In one line, say the show has a new "
                      "shape from this week: a proper look at the portfolio, one main story, quick hits from "
                      "around the portfolio, and scenarios that get scored the following week. ")
    if rc["last"] and rc["last"].get("next_learning_topic") == topic:
        previously += f"Then one line: last week we promised to explain {topic} — today we do."

    hook = _hook(ch, facts, main)
    rundown = ", ".join(["the portfolio this week", f"the main story — {_short(main['title'])}",
                         "around the portfolio" + (f", with a spotlight on {spot['name']}" if spot else ""),
                         f"the learning segment — {topic}", "and our scenarios for the week ahead"])
    frame = _desk.scenario_frame(facts.get("index_table") or {})
    frame_text = "\n".join(
        f"- {f['label']}: {f['condition']}. Effect on Christopher's PORTFOLIO (not the index): about "
        f"{_say_money(f['effect'])} for {f['effect_note']}."
        if f["effect"] else f"- {f['label']}: {f['condition']}. Effect on the portfolio: describe it as small."
        for f in frame) + "\nThe dollar figures are changes in the PORTFOLIO's value. Never state a level for the S&P 500."
    drivers = "; ".join([_short(main["title"])] + [_short(q["title"]) for q in quick]) or "the week's news"
    upcoming = _upcoming_event([main] + quick)
    next_week = (f"say in one or two lines what is coming next week: the learning segment will be on {next_topic}"
                 + (f", and we will see how {upcoming} turned out" if upcoming else "")
                 + ". Promise nothing else.")

    exposure = _exposure_line(main, intel, facts)
    items = []
    for i, q in enumerate(quick, 1):
        items.append(f"{i}. NEWS — {q['title']}: {q['body']}"
                     + (f" | What we own in it: {q['exposure']}" if q["exposure"] else " | We own nothing directly in it."))
    if spot:
        size = (f"we own about {_say_money(spot['cad'])} of it, about {_say_pct(spot['share'])} of the portfolio, "
                f"in the {' and '.join(_desk.ACCOUNT_LABEL.get(a, a) for a in spot['accounts']) or 'portfolio'}")
        news = (f"The news: {spot['headline']} — {spot['body']}" if spot["headline"]
                else "No news on it this week — explain the business and why we own it, nothing more.")
        items.append(f"{len(items) + 1}. HOLDING SPOTLIGHT — {spot['name']}: {size}. {news} "
                     f"Invent no announcement, buyback, number, date or earnings.")

    desk = _desk_text(facts, ch, sp_week, fx_effect) if ch else (
        "- Portfolio data is unavailable this week. Say so in one line and keep the review short; state no figures.")

    # Figures the checker must accept, because the prompt supplied them.
    if facts and ch:
        extra_money = [ch["contrib_added"], ch["market_change"], ch["contrib_total"], ch["pnl_total"],
                       ch["dividends"]["total"], ch["house"]["value"], DOWN_PAYMENT_TARGET,
                       *[t["cad"] for t in ch["trades"]], *[v["value"] for v in ch["accounts"].values()],
                       *[v["change"] for v in ch["accounts"].values()], *(f["effect"] for f in frame if f["effect"])]
        if fx_effect:
            extra_money.append(fx_effect)
        extra_pct = [ch["pnl_pct"], ch["market_pct"], ch["house"]["value"] / DOWN_PAYMENT_TARGET * 100,
                     *ch["sector_weights"].values(), *[s["then"] for s in ch["sector_shifts"]],
                     *[s["now"] for s in ch["sector_shifts"]]]
        if sp_week is not None:
            extra_pct.append(abs(sp_week))
        if score:
            extra_pct.append(abs(score["move_pct"]))
        facts["extra_money"] = [abs(float(x)) for x in extra_money if x]
        facts["extra_pct"] = [abs(float(x)) for x in extra_pct if x]

    try:
        bdate = datetime.fromisoformat(str(intel.get("generated_at")).replace("Z", "+00:00"))
        briefing_note = (f"The news was written on {bdate.strftime('%A, %B %d')}; \"today\" or \"tonight\" in it "
                         f"means that day, not now.")
    except Exception:
        briefing_note = ""

    plan = {
        "ep_num": ep_num, "today": now.strftime("%A, %B %d, %Y"), "briefing_note": briefing_note,
        "facts": facts, "changes": ch, "topic": topic, "next_topic": next_topic,
        "main": main, "spot": spot, "quick": quick, "fx_owner": fx_owner,
        "banned_analogies": rc["analogies"], "score": score,
        "sp500": {"close": sp_close, "date": sp_date, "week_pct": sp_week},
        "held_line": "; ".join(
            f"{_LEVERAGE_NAMES.get(t) or COMPANY_NAMES.get(t, (facts['positions'][t].get('name') or t))}"
            + (f" — {HOLDING_PROFILES[t]}" if t in HOLDING_PROFILES else "")
            + (f", about {_say_pct(facts['positions'][t]['cad'] / facts['total_value'] * 100)} of the portfolio"
               if facts.get("total_value") else "")
            for t in sorted((facts.get("positions") or {}).keys(),
                            key=lambda t: -facts["positions"][t]["cad"])) or "(unavailable)",
        "fields": {
            "opening":   {"hook": hook, "previously": previously, "rundown": rundown},
            "desk":      {"desk": desk, "fx_instruction": (
                "6. Currency — in no more than two sentences, from the Currency line above." if fx_owner == "desk" else "")},
            "main":      {"title": main["title"], "brief": main["body"] or "(no detail beyond the headline)",
                          "exposure": exposure},
            "around":    {"items": "\n".join(items) or "No other news this week: one short look at the biggest holding.",
                          "count": ["One", "One", "Two", "Three", "Four"][min(len(items), 4)],
                          "plural": "" if len(items) <= 1 else "s"},
            "learning":  {"topic": topic},
            "scenarios": {"frame": frame_text, "drivers": drivers, "next_week": next_week},
        },
    }
    shown = " ".join([desk, main["body"], exposure, plan["fields"]["around"]["items"], frame_text, hook])
    shown = re.sub(r"(?<=\d),(?=\d{3})", "", shown)
    if facts:
        facts["allowed_numbers"] = sorted({float(x) for x in re.findall(r"\d+(?:\.\d+)?", shown)}
                                          | set(facts.get("allowed_numbers") or []))
        # A briefing figure tied to a holding ("TSLA $7,480 CAD") is valid only in a
        # sentence naming that holding; the rest may be said anywhere.
        pairs = _position_pairs_in(plan["fields"]["around"]["items"] + " " + exposure)
        paired = {v for vs in pairs.values() for v in vs}
        facts["intel_positions"] = pairs
        facts["intel_money"] = [v for v in _money_values_in(main["body"], plan["fields"]["around"]["items"])
                                if v not in paired]
    if removed_fx:
        print(f"  ✓ Removed {len(set(removed_fx))} briefing sentence(s) with the currency direction wrong")
    print(f"     Main story:  {main['title'][:80]}")
    print(f"     Spotlight:   {spot['name'] if spot else '—'}")
    print(f"     Quick hits:  {' | '.join(q['title'][:50] for q in quick) or '—'}")
    print(f"     Learning:    {topic}  (next week: {next_topic})")
    print(f"     Currency:    {'segment ' + fx_owner if fx_owner else 'not discussed (moved under a cent)'}")
    if score:
        print(f"     Scorecard:   S&P {score['move_pct']:+.2f}% → {score['label']} (called {score.get('probability')}%)")
    return plan


def _short(title: str, n: int = 9) -> str:
    words = re.sub(r"\s+", " ", str(title or "")).strip().split(" ")
    return " ".join(words[:n]) + ("…" if len(words) > n else "")


def _hook(ch, facts, main) -> str:
    """The cold open's subject: the single most striking portfolio fact this week."""
    if not ch:
        return f"this week's main story — {_short(main['title'])}."
    opts = []
    if ch["contrib_added"] and (ch["change"] >= 0) != (ch["market_change"] >= 0):
        opts.append(f"the portfolio is {'up' if ch['change'] >= 0 else 'down'} on the week only because of new money "
                    f"— the market itself moved it {'up' if ch['market_change'] >= 0 else 'down'}.")
    if ch["new_tickers"]:
        opts.append(f"a brand-new name joined the portfolio this week: "
                    f"{', '.join(COMPANY_NAMES.get(t, t) for t in ch['new_tickers'])}.")
    if ch["exited_tickers"]:
        opts.append(f"something left the portfolio this week: {', '.join(COMPANY_NAMES.get(t, t) for t in ch['exited_tickers'])}.")
    mv = sorted(facts.get("movers") or [], key=lambda x: -abs(x["pct"]))
    if mv and abs(mv[0]["pct"]) >= 8:
        t = mv[0]
        opts.append(f"{_LEVERAGE_NAMES.get(t['ticker']) or COMPANY_NAMES.get(t['ticker'], t['ticker'])} "
                    f"{'jumped' if t['pct'] > 0 else 'dropped'} about {_say_pct(abs(t['pct']))} in a week.")
    if ch["sector_shifts"] and abs(ch["sector_shifts"][0]["pp"]) >= 1.5:
        s = ch["sector_shifts"][0]
        opts.append(f"{_sector_name(s['sector'])} went from about {s['then']:.0f}% to about {s['now']:.0f}% of the portfolio in a week.")
    return opts[0] if opts else f"this week's main story — {_short(main['title'])}."


def _upcoming_event(stories: list):
    """A story covered THIS episode whose news says something is still to come — so
    next week's tease follows up on something the listener has actually heard."""
    rx = re.compile(r"\b(?:next week|upcoming|due (?:on|next)|scheduled|will (?:report|release|announce|meet|decide))\b", re.I)
    for st in stories:
        if rx.search(str(st.get("body") or "")):
            return f"the {_short(st.get('title', ''), 7)} story"
    return None


def _exposure_line(main: dict, intel: dict, facts: dict) -> str:
    for it in (intel.get("news") or []):
        if str(it.get("headline") or "").strip() == main["title"] and it.get("exposure"):
            return str(it["exposure"])[:120]
    themes = _themes_of(main["title"] + " " + main["body"])
    hits = [COMPANY_NAMES.get(t, t) for t in (facts.get("positions") or {})
            if any(t in _SECTORS.get(s, ()) for s in _THEME_SECTORS.get(next(iter(themes), ""), ()))]
    return ", ".join(hits[:5]) or "nothing directly — explain the indirect link in words, or say there isn't one"


_THEME_SECTORS = {"oil & energy": ("Energy",), "banks & financials": ("Financials",),
                  "ai & semiconductors": ("Tech", "Leveraged 3x")}


# ── Writing a segment ────────────────────────────────────────────────────────

def _segment_prompt(plan: dict, key: str, notes: str, before_tail: str) -> str:
    template = dict((k, t) for k, t, _, _ in SEGMENTS)[key]
    elsewhere = []
    if key not in ("desk",):
        elsewhere.append("the portfolio review (values, trades, sector mix, accounts)")
    if key != "main":
        elsewhere.append(f"the main story ({_short(plan['main']['title'], 7)})")
    if key != "around":
        elsewhere.append("the quick news items and the holding spotlight")
    if key != "learning":
        elsewhere.append(f"the learning segment ({plan['topic']})")
    if key != "scenarios":
        elsewhere.append("the scenarios and what to watch")
    if key == "opening":
        elsewhere = ["everything — the opening only names the segments"]
    banned = ", ".join(plan["banned_analogies"][:16]) or "none yet"
    analogy_rule = (f"at most ONE short everyday comparison in this segment. Do not reuse any used in recent "
                    f"episodes: {banned}." if key in ANALOGY_SEGMENTS
                    else "none in this segment — say things directly, with no metaphors or comparisons.")
    bible = SHOW_BIBLE.format(
        editor_notes=notes or "", currency_rule=CURRENCY_RULE_ON if plan["fx_owner"] == key else CURRENCY_RULE_OFF,
        elsewhere="; ".join(elsewhere), analogy_rule=analogy_rule, today=plan["today"],
        briefing_note=plan["briefing_note"], held_line=plan["held_line"],
        continuity=(f"THE PREVIOUS SEGMENT ENDED WITH: {before_tail}\nContinue straight on from it with a new "
                    f"line of your own — do not greet, recap, repeat it, or open with \"Exactly\" or \"Right\"."
                    if before_tail else ""))
    return bible + "\n" + template.format(**plan["fields"][key])


def _segment_tail(text: str, n: int = 2) -> str:
    lines = [l.strip() for l in (text or "").split("\n") if l.strip().startswith(("ALEX:", "SAM:"))]
    return " ".join(lines[-n:])[-400:]


def write_segment(api_key: str, plan: dict, key: str, notes: str, before_tail: str) -> str:
    _, _, min_words, max_tokens = next(s for s in SEGMENTS if s[0] == key)
    text = _groq_part(api_key, lambda n: _segment_prompt(plan, key, n, before_tail),
                      f"Segment {SEGMENT_KEYS.index(key) + 1} ({SEGMENT_TITLES[key]})", notes, min_words,
                      max_tokens=max_tokens)
    return _normalize_script(text)


# ── Checking a segment on its own ────────────────────────────────────────────
_FX_TALK = re.compile(
    r"\b(?:exchange rate|usd\s*/\s*cad|loonie|greenback|currency|currencies"
    r"|(?:us|u\.s\.|american|canadian)\s+dollar\s+(?:has\s+|got\s+|gets\s+|is\s+|was\s+)?"
    r"(?:strong|weak|rose|rise|fell|fall|climb|slip|gain|los|firm|soft)\w*"
    r"|(?:strong|weak)\w*\s+(?:us|u\.s\.|american|canadian)\s+dollar)\b", re.I)
_TRADE_CAUSE = re.compile(r"\b(?:trade|addition|purchase|buying|bought|added)\b[^.?!]{0,60}\b(?:push|nudg|lower|rais|lift|"
                          r"shift|tilt|move|reduc|increas)\w*[^.?!]{0,50}\b(?:slice|share|weight|percent|exposure|mix)\b", re.I)
_EMBELLISH = re.compile(r"\b(?:all year|this year|ever|record|biggest|largest|worst|best|since \w+|single[- ]day|"
                       r"in (?:months|years|a decade)|because|after the|thanks to|due to)\b", re.I)
_WATCH = re.compile(r"\b(?:watch(?:ing)?|keep an eye|keep tabs|monitor(?:ing)?)\b", re.I)


def _turn_texts(text: str) -> list:
    return [t for k, _, t in _iter_script(text) if k == "turn"]


def segment_problems(key: str, text: str, plan: dict) -> list:
    """What is wrong with ONE segment, in words the model can act on."""
    out, turns = [], _turn_texts(text)
    sents = [s for t in turns for s in _split_sentences(t)]
    fx = [s for s in sents if _FX_TALK.search(s)]
    if plan["fx_owner"] != key and fx:
        out.append(f"talks about the exchange rate or currency, which is not this segment's subject — remove it: \"{fx[0][:100]}\"")
    elif plan["fx_owner"] == key and len(fx) > 2:
        out.append(f"spends {len(fx)} sentences on currency; two at most")
    n_an = _desk.analogy_count(text)
    if key not in ANALOGY_SEGMENTS and n_an:
        out.append("uses an analogy or comparison; this segment must say things directly")
    elif n_an > 1:
        out.append(f"uses {n_an} analogies; one at most")
    reused = set(_desk.extract_analogies(text)) & set(plan["banned_analogies"])
    if reused:
        out.append(f"reuses a comparison from a recent episode ({', '.join(sorted(reused))}); find a fresh one or none")
    if key not in ("main", "scenarios"):
        w = [s for s in sents if _WATCH.search(s)]
        if len(w) > 1:
            out.append(f"tells the listener what to watch {len(w)} times; that belongs in the main story and the scenarios")
    if key == "around":
        own = plan["fields"]["around"]["items"].lower()
        for tkr, aliases in _holding_aliases(plan["facts"]).items():
            said = [a for a in aliases if re.search(r"\b" + re.escape(a) + r"\b", text, re.I)]
            if said and not any(a.lower() in own for a in aliases):
                out.append(f"mentions {said[0]}, which is not one of this segment's items — check it is not "
                           f"calling one holding by another's name")
    cap = 2 if key == "desk" else FIGURES_PER_TURN_MAX
    heavy = [t for t in turns if len(_FIGURE.findall(t.replace("‑", "-"))) > cap]
    if heavy:
        out.append(f"{len(heavy)} turn(s) carry more than {cap} numbers — split them across SAM's questions: "
                   f"\"{heavy[0][:90]}\"")
    if key == "desk":
        cause = next((x for x in sents if _TRADE_CAUSE.search(x)), None)
        if cause:
            out.append(f"says a trade changed the sector mix — the shifts come from price moves and trades together: "
                       f"\"{cause[:100]}\"")
    if key == "opening" and "[THEME]" in text:
        cold = " ".join(_turn_texts(text.split("[THEME]", 1)[0]))
        hook = plan.get("fields", {}).get("opening", {}).get("hook", "").lower()
        extra = next((m for m in _EMBELLISH.finditer(cold) if m.group(0).lower() not in hook), None)
        if extra:
            out.append(f"the cold open adds \"{extra.group(0)}\" to the fact it was given — say the fact only")
    if key == "opening" and not any("[THEME]" in l for l in text.split("\n")):
        out.append("is missing the [THEME] line after the cold open")
    if key == "learning" and not re.search(r"\[EDUCATION_TOPIC:", text, re.I):
        out.append(f"is missing the first line [EDUCATION_TOPIC: {plan['topic']}]")
    if key == "scenarios":
        opened = {m.group(1).lower() for t in turns
                  for m in [re.match(r"^\W*(base|bull|bear)\s+case\b", t, re.I)] if m}
        missing = [f"{c} case" for c in ("base", "bull", "bear") if c not in opened]
        if missing:
            out.append(f"is missing the {', '.join(missing)} — each case must open its own turn, spoken by "
                       f"ALEX, starting \"Base case — NN percent:\" (or Bull / Bear)")
        probs = _desk.scenario_probabilities(text)
        if len(probs) == 3 and sum(probs.values()) != 100:
            out.append(f"the scenario odds add up to {sum(probs.values())}, not 100")
    return out


def _holding_aliases(facts: dict) -> dict:
    out = {}
    for tkr, p in (facts.get("positions") or {}).items():
        names = {COMPANY_NAMES.get(tkr, ""), str(p.get("name") or ""), _LEVERAGE_NAMES.get(tkr, "")}
        base = tkr.split(".")[0]
        if len(base) >= 3 and tkr not in ("ET",):
            names.add(base)
        out[tkr] = {n for n in names if n and len(n) >= 3}
    return out


def _locate(problem_msg: str, segs: dict):
    """Which segment a whole-script problem belongs to, from the sentence it quotes."""
    m = re.search(r"\"(.{12,}?)(?:\"|$)", problem_msg)
    if m:
        frag = re.sub(r"\s+", " ", m.group(1).strip("…").strip())[:50].lower()
        for k, t in segs.items():
            if frag and frag in re.sub(r"\s+", " ", t).lower():
                return k
    low = problem_msg.lower()
    if "scenario" in low:
        return "scenarios"
    if "agenda" in low or "opening" in low or "marker" in low:
        return "opening" if "agenda" in low or "opening" in low else "learning"
    return None


def assemble(segs: dict) -> str:
    """Join the segments, smoothing ONLY the seams: a sign-off or re-greeting at a join
    is dropped, and when one host would speak twice across a join the two lines become
    one turn. Turns inside a segment are never merged — merging them once glued three
    scenario lines into a single turn, which the per-turn figure cap then cut down."""
    turn = re.compile(r"^(ALEX|SAM):\s*(.*)$")
    out = []
    for k in SEGMENT_KEYS:
        new = [l.strip() for l in segs.get(k, "").split("\n") if l.strip()]
        if not new:
            continue
        if not out:
            out = new
            continue
        # _stitch_parts returns head + a blank line + tail, minus any seam chatter.
        head, tail = _stitch_parts("\n".join(out), "\n".join(new)).split("\n\n", 1)
        out = [l for l in head.split("\n") if l.strip()]
        new = [l for l in tail.split("\n") if l.strip()]
        first = turn.match(new[0]) if new else None
        last = turn.match(out[-1]) if out else None
        if (first and last and first.group(1) == last.group(1)
                and len(_FIGURE.findall(last.group(2) + " " + first.group(2))) <= FIGURES_PER_TURN_MAX):
            out[-1] = f"{last.group(1)}: {last.group(2)} {first.group(2)}"
            new = new[1:]
        out += new
    return "\n".join(out)


def produce_episode(api_key: str, plan: dict, history=None, log=print):
    """Write all six segments, then repair only the ones with problems.

    Returns (script, facts, segments) or None. A wrong number spoken with confidence
    is worse than a missing episode, so a falsehood that survives every repair still
    blocks publication; anything else is fixed or trimmed, never fatal."""
    facts, segs = plan["facts"], {}
    try:
        tail = ""
        for k in SEGMENT_KEYS:
            print(f"     Writing segment {SEGMENT_KEYS.index(k) + 1}/6 — {SEGMENT_TITLES[k]}...")
            segs[k] = write_segment(api_key, plan, k, "", tail)
            tail = _segment_tail(segs[k])
    except GroqDailyLimit as exc:
        log(f"ERROR: {exc}")
        return None
    except Exception as exc:
        log(f"ERROR: Script generation failed: {exc}")
        return None

    def problems_by_segment():
        script = assemble(segs)
        by = {k: [] for k in SEGMENT_KEYS}
        global_left = []
        for p in collect_script_problems(script, facts, history, MIN_SCRIPT_WORDS):
            k = _locate(p["msg"], segs)
            (by[k] if k else global_left).append(p)
        for k in SEGMENT_KEYS:
            by[k] += [{"kind": "segment", "fatal": False, "retry": True, "msg": m}
                      for m in segment_problems(k, segs[k], plan)]
        return script, by, global_left

    for rnd in range(1, REPAIR_ROUNDS + 1):
        script, by, left = problems_by_segment()
        st = figure_stats(script)
        n_fatal = sum(p["fatal"] for ps in by.values() for p in ps) + sum(p["fatal"] for p in left)
        log(f"  round {rnd}: {st['words']:,} words (~{st['words'] // 150} min), {st['total']} figures; "
            f"{n_fatal} factual problem(s); " + ", ".join(f"{k} {len(v)}" for k, v in by.items() if v))
        # A single repeated sentence is cheaper to cut than to rewrite a segment for.
        redo = [k for k, ps in by.items()
                if any(p["fatal"] or p["kind"] != "repeat" for p in ps) or sum(p["kind"] == "repeat" for p in ps) >= 2]
        redo = [k for k in redo if not all(p["kind"] == "length" for p in by[k])]
        if not redo:
            break
        for k in redo:
            for p in by[k][:5]:
                log(f"      • [{k}] {p['msg'][:150]}")
        for k in redo:
            if not groq_budget_allows(SEGMENT_COST_ESTIMATE):
                log(f"  ⚠ Groq budget reserved ({groq_tokens_used():,} tokens used) — repairing the rest by trimming")
                break
            before = _segment_tail(segs[SEGMENT_KEYS[SEGMENT_KEYS.index(k) - 1]]) if k != "opening" else ""
            notes = ("EDITOR'S NOTES — your previous draft of THIS segment was rejected. Fix exactly these, "
                     "and keep to every rule below:\n" + "\n".join(f"- {p['msg']}" for p in by[k][:6]) + "\n")
            try:
                new = write_segment(api_key, plan, k, notes, before)
            except Exception as exc:
                log(f"  ⚠ rewrite of {k} failed ({str(exc)[:100]}) — keeping the earlier draft")
                continue
            old_score = sum(10 if p["fatal"] else 1 for p in by[k])
            trial = dict(segs, **{k: new})
            new_score = (sum(10 if p["fatal"] else 1 for p in
                             [q for q in collect_script_problems(assemble(trial), facts, history)
                              if _locate(q["msg"], trial) == k])
                         + len(segment_problems(k, new, plan)))
            if new_score <= old_score:
                segs[k] = new
            else:
                log(f"  ⚠ rewrite of {k} was worse ({new_score} vs {old_score}) — keeping the earlier draft")

    script = assemble(segs)
    repaired = _final_repair(script, facts, history, log)
    if repaired is None:
        return None
    return repaired, facts, segs


def drop_orphan_questions(script: str) -> tuple:
    """A question left with no answer after a cut ("SAM: How are the accounts looking?"
    followed straight by another SAM line) is removed as well. Returns (script, removed)."""
    lines, out, removed = script.split("\n"), [], []
    turn = re.compile(r"^(ALEX|SAM):\s*(.*\S)\s*$")
    for i, line in enumerate(lines):
        m = turn.match(line.strip())
        nxt = next((turn.match(l.strip()) for l in lines[i + 1:] if l.strip()), None)
        if m and nxt and m.group(1) == nxt.group(1) and m.group(2).rstrip().endswith("?"):
            removed.append(m.group(2))
            continue
        out.append(line)
    return "\n".join(out), removed


def _final_repair(script: str, facts: dict, history, log=print):
    """Last resort, after the rewrites: cut what still cannot air."""
    problems = collect_script_problems(script, facts, history)
    if any(p["fatal"] for p in problems):
        script, removed = strip_unsafe_sentences(script, facts)
        if any(p["fatal"] for p in collect_script_problems(script, facts, history)):
            log("ERROR: the script still contains unsupported claims after repair — not publishing")
            return None
        log(f"  ⚠ Removed {len(removed)} unsafe sentence(s):")
        for s in removed:
            log(f"      - {s[:140]}")
    script, cut_a = strip_unwanted_sentences(script, history)
    script, cut_b = trim_heavy_turns(script)
    script, cut_q = drop_orphan_questions(script)
    cut_b += cut_q
    if cut_a or cut_b:
        log(f"  ⚠ Cut {len(cut_a) + len(cut_b)} repeated or overloaded sentence(s):")
        for x in cut_a + cut_b:
            log(f"      - {x[:140]}")
    left = collect_script_problems(script, facts, history, MIN_SCRIPT_WORDS)
    for p in left:
        log(f"  ⚠ {p['msg'][:150]}")
    if not left:
        log("  ✓ Script passes every check")
    return script


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
    # Two tries. On 2026-10-12 one failed parse fell through to the briefing's first
    # 60 characters, so the episode went out titled "The portfolio is positioned to
    # benefit from a strong USD, wh". A cut-off sentence is never a title.
    for _ in range(2):
        try:
            raw = _groq_call(api_key, prompt, "Summary", max_tokens=900)
            start, end = raw.find('{'), raw.rfind('}') + 1
            if start >= 0:
                return json.loads(raw[start:end])
        except Exception:  # noqa: BLE001
            continue
    return {}


def _usable_title(title) -> bool:
    """A real headline: a handful of words, no sentence punctuation, not cut off."""
    t = str(title or "").strip()
    n = len(t.split())
    return (2 <= n <= 12 and len(t) <= 80 and not t.endswith((",", ";", ":", "-", "–"))
            and not t.endswith(".") and not re.search(r"\b\w{1,2}$", t.split()[-1]) if t else False)


def episode_title(summary: dict, script: str, education_topic: str, ep_num: int) -> str:
    """The model's title if it is usable, otherwise one built from what was covered."""
    t = str((summary or {}).get("episode_title", "")).strip().strip('"')
    if _usable_title(t):
        return t
    if education_topic:
        return f"Portfolio Pulse Ep {ep_num}: {education_topic}"
    return f"Portfolio Pulse Ep {ep_num}"


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
_INDEX_LEVEL = re.compile(
    r"\b(?:s&p(?:\s*500)?|the index|the dow|nasdaq|tsx)\b(?:[^.?!]|(?<=\d)\.(?=\d)){0,90}?\b(?:at|to|around|near|about|reach\w*|"
    r"hover\w*|slip\w*|climb\w*|fall\w*|drop\w*|ris\w*|end\w*|sit\w*)\s+(?:about\s+|roughly\s+|around\s+)?"
    r"(?<!\$)(\d{1,2},\d{3}|\d{4,5})(?!\s*(?:%|percent|dollars|cad|usd))", re.I)
_HYPOTHETICAL_PLAIN = re.compile(r"\bif\b", re.I)

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

    allowed_money |= {abs(float(v)) for v in (facts.get("extra_money") or [])}

    allowed_pct = {abs(gain_pct), abs(float(facts.get("roi_pct") or 0))}
    allowed_pct |= {abs(float(v)) for v in (facts.get("extra_pct") or [])}
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
    # An index LEVEL. The script is never given one, so any level it states is made
    # up — the redone ep22 read the portfolio's dollar effects out as S&P levels
    # ("the index hovers around 4,700", "slipping to about 9,500").
    lvl = _INDEX_LEVEL.search(sentence)
    if lvl and not _HYPOTHETICAL_PLAIN.search(sentence[:lvl.start()][-12:]):
        probs.append(f"states an index level ({lvl.group(1)}) it was never given — the scenario figures are "
                     f"changes in the portfolio's value, not index levels — \"{sentence[:110]}\"")

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
            and not _SCENARIO_LINE.search(sentence) and not in_learning):
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
    opening = " ".join(turns[:14]).lower().replace("\u2011", "-")
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

MIN_SCRIPT_WORDS      = 2200   # about 15 minutes spoken; a short draft is retried, never blocked
FIGURES_PER_TURN_MAX  = 4      # the listener's limit: no turn may carry more
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


# ── Things the listener has asked not to hear ────────────────────────────────
# The listener already understands leveraged ETFs, the daily reset and decay, so
# explaining them is noise. Every phrase below appeared in a published episode:
#   "A 3x fund is built to move about three times as much as the index it follows"
#   "turned a modest 1.5% move into a 4.5% boost on the leveraged position"
_LEVERAGE_TALK = re.compile(
    r"\b(?:built|designed|engineered|structured)\s+to\s+(?:move|deliver|track|amplif\w*|return|multipl\w*)"
    r"|\b(?:daily\s+(?:reset|rebalanc\w*)|resets?\s+daily|rebalances?\s+daily)\b"
    r"|\b(?:volatility\s+(?:decay|drag)|beta\s+decay|decay\w*|compounding\s+(?:effect|drag))\b"
    r"|\b(?:three|3)[\s-]?(?:x|times)\b[^.?!]{0,50}\b(?:as\s+much|more\s+than|amplif\w*|multipl\w*|magnif\w*|swing\w*)"
    r"|\b(?:leverage|leveraged\s+(?:fund|etf)s?)\s+(?:amplif\w*|magnif\w*|multipl\w*|works?|means?)\b"
    r"|\bhow\s+(?:a\s+)?(?:3x|three[\s-]times|leveraged)\s+(?:fund|etf)s?\s+work"
    r"|\bturn(?:s|ed)?\s+(?:a|an)\s+(?:\w+\s+){0,2}[\d.]+\s?%\s+(?:move|gain|drop|rally|dip)\b[^.?!]{0,50}\binto\b", re.I)

# Lines that legitimately recur every week, so repeating them is not a defect.
_BOILERPLATE = re.compile(
    r"welcome (?:back )?to portfolio pulse|joined as always|as always i'?m joined|joined by sam|i'?m alex|"
    r"thanks for (?:listening|tuning)|catch you next|"
    r"take care, everyone|stay sharp|before we get to our scenarios|that's our learning segment|"
    r"on to scenarios|^(?:base|bull|bear) case", re.I)

_STOP = frozenset(
    "the a an and or but of to in on for with at by from as is are was were be been being it its this "
    "that these those we our us you your i they them their he she his her not no so if then than too "
    "very just about into out up down over more most some any all each can could would should will may "
    "might do does did have has had there here what which who whom how why when where also only even "
    "still".split())

# 0.6 from real episodes: it catches the same genuine carry-overs as 0.7 (including the stale
# "$7,685 idle cash" sentence) and the rephrased repeats 0.7 misses, while 0.5 starts matching
# unrelated sentences ("key watchlist" against "the playbook").
REPEAT_SIMILARITY    = 0.6
REPEAT_FIGURE_LIMIT  = 2      # a figure may be stated this many times, not more
REPEAT_MIN_WORDS     = 7
AGENDA_TURNS         = 4      # the opening turns that preview the episode


def _content_tokens(sentence: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", sentence.lower())
                     if len(w) > 2 and w not in _STOP)


def _similar(a: frozenset, b: frozenset) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= REPEAT_SIMILARITY


def _is_boilerplate(sentence: str) -> bool:
    return bool(_BOILERPLATE.search(sentence.replace("’", "'")))


def _history_sentences(history) -> list:
    """Token sets for every substantive sentence in the recent episodes."""
    out = []
    for text in (history or []):
        for kind, _, t in _iter_script(text):
            if kind != "turn":
                continue
            for s in _split_sentences(t):
                if len(s.split()) >= REPEAT_MIN_WORDS + 1 and not _is_boilerplate(s):
                    out.append(_content_tokens(s))
    return out


def _scan_unwanted(script: str, history=None) -> list:
    """Sentences that should not air, with why. One pass, in order, so that the
    checker and the repair (strip_unwanted_sentences) can never disagree.

      leverage - explaining how 3x funds work, the reset, or decay
      repeat   - a sentence already said earlier in this episode or in a recent one,
                 or a figure stated more than twice. Ep18 made its "$1,722 per cent"
                 point six times in different words, and carried "We have $7,685 USD
                 idle cash in the RRSP" over from an earlier week.
    """
    past = _history_sentences(history)
    seen, figs, out, ti = [], {}, [], -1
    learning = _in_learning_flags(script)
    for (kind, _, text), in_learning in zip(_iter_script(script), learning):
        if kind != "turn":
            continue
        ti += 1
        for si, sent in enumerate(_split_sentences(text)):
            if _LEVERAGE_TALK.search(sent):
                out.append({"turn": ti, "sent": si, "kind": "leverage", "sentence": sent,
                            "reason": "explains how leveraged funds work, their reset or their decay — "
                                      "the listener already knows; just say what the fund did"})
            toks = _content_tokens(sent)
            if len(sent.split()) >= REPEAT_MIN_WORDS and not _is_boilerplate(sent):
                if any(_similar(toks, t) for t in seen):
                    out.append({"turn": ti, "sent": si, "kind": "repeat", "sentence": sent,
                                "reason": "repeats a sentence already said earlier in this episode"})
                elif any(_similar(toks, t) for t in past):
                    out.append({"turn": ti, "sent": si, "kind": "repeat", "sentence": sent,
                                "reason": "repeats a sentence from a recent episode"})
            # Each scenario necessarily restates its threshold ("more than 1.5 percent");
            # counting it cut ep-redesign's bear case as a "repeat".
            # The learning example's "$100" is a practice number, not a fact to state once.
            for m in ([] if (_SCENARIO_LINE.search(sent) or in_learning)
                      else _FIGURE.finditer(sent.replace("‑", "-"))):
                key = re.sub(r"[\s,]", "", m.group(0)).lower()
                figs[key] = figs.get(key, 0) + 1
                if figs[key] > REPEAT_FIGURE_LIMIT and not any(
                        o["turn"] == ti and o["sent"] == si for o in out):
                    out.append({"turn": ti, "sent": si, "kind": "repeat", "sentence": sent,
                                "reason": f"states {m.group(0).strip()} again — it has now been said "
                                          f"{figs[key]} times; say each fact once"})
            # The opening turns announce what the episode covers, and the topic is then
            # introduced properly later; that overlap is the point of an agenda, not a
            # repeat, and must not get the real introduction deleted.
            if ti >= AGENDA_TURNS:
                seen.append(toks)
    return out


def strip_unwanted_sentences(script: str, history=None) -> tuple:
    """Last resort: drop every sentence _scan_unwanted flags. Returns (script, removed)."""
    flagged = {(u["turn"], u["sent"]) for u in _scan_unwanted(script, history)}
    if not flagged:
        return script, []
    out, removed, ti = [], [], -1
    for raw in script.split("\n"):
        m = re.match(r"^(ALEX|SAM):\s*(.*\S)\s*$", raw.strip())
        if not m:
            out.append(raw)
            continue
        ti += 1
        sents = _split_sentences(m.group(2))
        kept = [s for i, s in enumerate(sents) if (ti, i) not in flagged]
        if len(kept) == len(sents):
            out.append(raw)
            continue
        removed.extend(s for i, s in enumerate(sents) if (ti, i) in flagged)
        if kept:
            out.append(f"{m.group(1)}: " + " ".join(kept))
    return "\n".join(out), removed


def trim_heavy_turns(script: str, max_figs: int = None) -> tuple:
    """Guarantee the per-turn cap: while a turn carries more figures than allowed,
    drop its most number-heavy sentence. Returns (script, removed)."""
    cap = FIGURES_PER_TURN_MAX if max_figs is None else max_figs
    count = lambda s: len(_FIGURE.findall(s.replace("‑", "-")))
    out, removed = [], []
    for raw in script.split("\n"):
        m = re.match(r"^(ALEX|SAM):\s*(.*\S)\s*$", raw.strip())
        if not m:
            out.append(raw)
            continue
        sents = _split_sentences(m.group(2))
        if sum(count(s) for s in sents) <= cap:
            out.append(raw)
            continue
        while sents and sum(count(s) for s in sents) > cap:
            worst = max(range(len(sents)), key=lambda i: (count(sents[i]), i))
            removed.append(sents.pop(worst))
        if sents:
            out.append(f"{m.group(1)}: " + " ".join(sents))
    return "\n".join(out), removed


# ── Topic freshness ──────────────────────────────────────────────────────────
# The deep-dive subjects are chosen deterministically now, so nothing stopped them
# repeating week after week: ep15, ep17 and ep18 were all essentially "the dollar
# and the CAD". Coarse themes, matched on headlines and past episode titles only
# (a body mentioning "energy" in passing must not make a China story an oil story).
_THEMES = {
    "currency":            r"\b(?:fx|currency|dollar|greenback|loonie|usd|cad|exchange rate|carry trade)\b",
    "rates & inflation":   r"\b(?:fed|fomc|ecb|bank of canada|boc|central bank|interest rates?|rate (?:hike|cut|decision)|"
                           r"rates|inflation|cpi|pce|yield curve|treasur\w+|yields?)\b",
    "oil & energy":        r"\b(?:oil|crude|brent|wti|opec|energy|pipelines?|natural gas|lng|refin\w+)\b",
    "china & growth":      r"\b(?:china|chinese|pmi|gdp|recession|growth)\b",
    "ai & semiconductors": r"\b(?:ai|artificial intelligence|semiconductors?|chips?|nvidia|data cent\w+|foundr\w+)\b",
    "autos & ev":          r"\b(?:tesla|ev|evs|electric vehicles?|semi truck|autos?)\b",
    "banks & financials":  r"\b(?:banks?|financials?|credit|lending|mortgages?|visa)\b",
    "regulation & trade":  r"\b(?:regulat\w+|antitrust|tariffs?|trade war|sanctions?|ftc|sec)\b",
    "geopolitics":         r"\b(?:iran|israel|ukraine|russia|houthi|hormuz|war|conflict|truce|missile)\b",
    "healthcare":          r"\b(?:pharma\w*|drugs?|biotech|healthcare|wegovy|novo)\b",
    "crypto":              r"\b(?:bitcoin|crypto\w*)\b",
}
_THEME_RX = {name: re.compile(rx, re.I) for name, rx in _THEMES.items()}


def _themes_of(text) -> frozenset:
    t = str(text or "")
    return frozenset(n for n, rx in _THEME_RX.items() if rx.search(t))


def _pick_fresh(cands: list, avoid: frozenset):
    """cands: [(themes, item)] in order of importance. The first with no overlap with
    `avoid`; failing that, the one overlapping least (earliest wins a tie)."""
    best = None
    for i, (themes, item) in enumerate(cands):
        score = len(themes & avoid)
        if best is None or score < best[0]:
            best = (score, i, item)
        if score == 0:
            break
    return best[2] if best else None


def collect_script_problems(script: str, facts: dict, history=None, min_words: int = 0) -> list:
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
    for u in _scan_unwanted(script, history):
        out.append({"kind": u["kind"], "fatal": False, "retry": True,
                    "msg": f"{u['reason']} — \"{u['sentence'][:110]}\""})
    st = figure_stats(script)
    if min_words and st["words"] < min_words:
        out.append({"kind": "length", "fatal": False, "retry": True,
                    "msg": f"the episode is only {st['words']:,} words (about {st['words'] // 150} minutes); "
                           f"it needs at least {min_words:,}. Explain more slowly, add a second everyday example "
                           f"and more of SAM's questions — without adding any figures or invented facts"})
    if st["heavy_turns"]:
        out.append({"kind": "density", "fatal": False, "retry": True,
                    "msg": f"{len(st['heavy_turns'])} turn(s) carry more than {FIGURES_PER_TURN_MAX} "
                           f"figures (worst {st['max_turn']}) — at most {FIGURES_PER_TURN_MAX} numbers "
                           f"in any one turn"})
    return out


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


_NW_SMALL = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_NW_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_NW_SCALE = {"hundred": 100, "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_NW = "|".join(list(_NW_SMALL) + list(_NW_TENS) + list(_NW_SCALE))
_NUM_SPAN = re.compile(rf"\b(?:{_NW})(?:(?:[\s\u2011-]+(?:and[\s]+)?)(?:{_NW}|point))*\b"
                       r"(?P<unit>\s+(?:percent|per\s+cent|dollars|cents))?", re.I)


def _words_value(tokens: list):
    """Parse ["three", "hundred", "ten", "thousand"] -> 310000, or None if malformed."""
    if "point" in tokens:
        i = tokens.index("point")
        whole, frac = tokens[:i], tokens[i + 1:]
        if not frac or any(t not in _NW_SMALL or _NW_SMALL[t] > 9 for t in frac):
            return None
        w = _words_value(whole) if whole else 0
        return None if w is None else float(f"{w}." + "".join(str(_NW_SMALL[t]) for t in frac))
    total = current = 0
    last = None
    for t in tokens:
        if t in _NW_SMALL or t in _NW_TENS:
            kind = "small" if t in _NW_SMALL else "tens"
            if last == "small" or (last == "tens" and kind == "tens"):
                return None                       # "fifty fifty", "one two" — not one number
            if last == "tens" and _NW_SMALL.get(t, 0) >= 10:
                return None
            current += _NW_SMALL.get(t, _NW_TENS.get(t, 0))
            last = kind
        else:
            scale = _NW_SCALE[t]
            if scale == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * scale
                current = 0
            last = "scale"
    return total + current


def _digitize(text: str) -> str:
    """Spelled-out figures back to digits ("three hundred ten thousand dollars" ->
    "$310,000", "nine point five percent" -> "9.5%"). Every figure check reads digits,
    and the model spells numbers out, so for weeks the checks counted "0 figures" and
    could not see an invented one. The voice turns digits back into words anyway."""
    def rep(m):
        tokens = [t for t in re.split(r"[\s\u2011-]+", m.group(0).lower()) if t and t != "and"]
        unit = (m.group("unit") or "").strip().lower()
        if unit:
            tokens = tokens[: -len(unit.split())]
        has_scale = any(t in _NW_SCALE for t in tokens) or "point" in tokens
        if not (unit or has_scale or len(tokens) >= 2):
            return m.group(0)                     # "one thing", "two of them"
        v = _words_value(tokens)
        if v is None:
            return m.group(0)
        num = (f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}")
        if unit.startswith("per"):
            return f"{num}%"
        if unit == "dollars":
            return f"${num}"
        if unit == "cents":
            return f"{num} cents"
        return num
    return _NUM_SPAN.sub(rep, text)


_FAKE_LABEL = re.compile(r"^(\s*(?:(?:ALEX|SAM):\s*)?)([A-Z][A-Z .]{1,14}):\s+")


def _fix_label(line: str) -> str:
    """Repair garbled speaker labels the model sometimes emits mid-line
    ("SAM: A. L E X: KGS provides…", "SAM: AALIGN: A flat curve…"); the voice read them out."""
    m = _FAKE_LABEL.match(line)
    if not m:
        return line
    letters = re.sub(r"[^A-Z]", "", m.group(2))
    if letters in ("ALEX", "SAM") and not m.group(1):
        return line                                # an ordinary label
    if "LEX" in letters or letters.startswith("AL") or letters.startswith("AA"):
        who = "ALEX"
    elif "SAM" in letters:
        who = "SAM"
    elif m.group(1) and len(letters) <= 8:
        who = m.group(1).strip().rstrip(":")
    else:
        return line
    return f"{who}: " + line[m.end():]


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
        raw = _fix_label(_digitize(raw))
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
        if s.startswith("[") and s.endswith("]"):          # [THEME], [EDUCATION_TOPIC: …]
            out.append(s)
            continue
        if _HEADING.match(s):                               # "**SCENARIOS**", "## Close", "---"
            state["ok"] = False
            continue
        if state["parent"] is not None and len(s.split()) >= 3 and re.search(r"[a-z]", s):
            # Unlabelled speech. Episodes 21 and 22 wrote all three scenarios as bare
            # "Base case — 50 percent: …" lines; the check saw them, but parse_script
            # only speaks labelled lines, so the listener heard "On to scenarios…" and
            # then nothing. Give the line to the host who was speaking.
            speaker = out[state["parent"]].split(":", 1)[0].strip()
            out.append(f"{speaker}: {re.sub(r'[*_`#]+', '', s).strip()}")
            state["parent"], state["ok"] = len(out) - 1, True
            continue
        state["ok"] = False
        out.append(line)
    flush()
    return "\n".join(out)


_HEADING = re.compile(r"^(?:#{1,6}\s.*|[-*_=]{3,}|\*\*[^*]{1,60}\*\*:?|[A-Z][A-Z0-9 &/,'’—–-]{2,60}:?|.{1,40}:)$")


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


# ── Offline fallback voice (Piper) ───────────────────────────────────────────
# edge-tts is an unofficial Microsoft endpoint. On 2026-10-12 it began answering
# 403 to every request — from GitHub's runner and from a laptop alike, on the
# newest release — and the whole episode was lost after the script was already
# written. Piper runs on the runner itself with nothing external to refuse it, so
# audio no longer depends on a third party being willing.
PIPER_VOICE_DIR = Path(os.environ.get("PIPER_VOICE_DIR", Path.home() / ".cache" / "piper-voices"))
PIPER_VOICES    = {"ALEX": "en_US-ryan-medium", "SAM": "en_US-amy-medium"}
PIPER_BASE_URL  = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/{who}/medium/{name}.{ext}"
PIPER_LENGTH_SCALE = 0.93      # a touch faster than default, closer to the edge-tts pace
PIPER_PAUSE_SECS   = 0.28      # silence between speaker turns


def _ensure_piper_voices(attempts: int = 5) -> dict:
    """Return {speaker: model path}, downloading any voice file that is missing."""
    import time
    import urllib.request
    PIPER_VOICE_DIR.mkdir(parents=True, exist_ok=True)
    paths = {}
    for speaker, name in PIPER_VOICES.items():
        who = name.split("-")[1]
        for ext in ("onnx", "onnx.json"):
            dest = PIPER_VOICE_DIR / f"{name}.{ext}"
            if dest.exists() and dest.stat().st_size > 1000:
                continue
            url = PIPER_BASE_URL.format(who=who, name=name, ext=ext)
            for n in range(attempts):
                try:
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    urllib.request.urlretrieve(url, tmp)
                    tmp.replace(dest)
                    break
                except Exception as exc:  # noqa: BLE001
                    if n == attempts - 1:
                        raise RuntimeError(f"could not download {name}.{ext}: {exc}") from exc
                    time.sleep(3 * (n + 1))
        paths[speaker] = str(PIPER_VOICE_DIR / f"{name}.onnx")
    return paths


def _ffmpeg_exe() -> str:
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("ffmpeg is not available to encode the fallback audio") from exc


def synthesize_with_piper(turns: list, output: Path, tmp_dir: Path) -> None:
    """Speak every turn offline and write one MP3. Same turns, same order as edge-tts."""
    import subprocess
    import wave
    from piper import PiperVoice
    try:
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=PIPER_LENGTH_SCALE)
    except Exception:  # noqa: BLE001  older piper-tts without SynthesisConfig
        cfg = None

    voices = {sp: PiperVoice.load(path) for sp, path in _ensure_piper_voices().items()}
    wav_path = tmp_dir / "episode.wav"
    out = None
    for i, (speaker, text) in enumerate(turns):
        voice = voices["ALEX" if speaker == "ALEX" else "SAM"]
        part = tmp_dir / f"turn_{i:04d}.wav"
        with wave.open(str(part), "wb") as wf:
            if cfg is not None:
                voice.synthesize_wav(text, wf, syn_config=cfg)
            else:
                voice.synthesize_wav(text, wf)
        with wave.open(str(part), "rb") as wf:
            params, frames = wf.getparams(), wf.readframes(wf.getnframes())
        if out is None:
            out = wave.open(str(wav_path), "wb")
            out.setnchannels(params.nchannels)
            out.setsampwidth(params.sampwidth)
            out.setframerate(params.framerate)
        out.writeframes(frames)
        out.writeframes(b"\x00" * int(params.framerate * PIPER_PAUSE_SECS) * params.sampwidth * params.nchannels)
        part.unlink()
        if (i + 1) % 20 == 0 or i + 1 == len(turns):
            print(f"    Synthesized {i + 1}/{len(turns)} turns (offline voice)...")
    if out is None:
        raise RuntimeError("no turns to synthesize")
    out.close()
    try:
        import lameenc  # noqa: F401  pure wheel; ImportError selects the ffmpeg path
        import numpy as np
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32)
        if rate != AUDIO_RATE:        # Piper speaks at 22.05 kHz; the episode and themes are 24 kHz
            n = int(len(pcm) * AUDIO_RATE / rate)
            pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)
        Path(output).write_bytes(_encode_mp3(pcm.astype(np.int16).tobytes()))
    except ImportError:
        subprocess.run([_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(wav_path),
                        "-codec:a", "libmp3lame", "-b:a", "48k", "-ar", str(AUDIO_RATE), "-ac", "1", str(output)],
                       check=True)
    print(f"  ✓ MP3 created: {output} (offline voice)")


# Microsoft moved Edge's read-aloud voices to a new host in October 2026; the old
# one answers 403 to everything (rany2/edge-tts#490, fix proposed in #491). The
# same voices and token work through the new host, so it is tried first and the
# old host second, in case Microsoft swaps them back.
EDGE_HOSTS = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    "?Ocp-Apim-Subscription-Key={token}",
    "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"
    "?TrustedClientToken={token}",
)


def _use_edge_host(template: str) -> None:
    import edge_tts.communicate as cm
    import edge_tts.constants as c
    c.WSS_URL = cm.WSS_URL = template.format(token=c.TRUSTED_CLIENT_TOKEN)


# ── Second choice: Kokoro (offline, natural-sounding; chosen by ear 2026-10-12) ──
KOKORO_VOICES   = {"ALEX": "am_adam", "SAM": "af_heart"}
KOKORO_DIR      = Path(os.environ.get("KOKORO_DIR", Path.home() / ".cache" / "kokoro"))
KOKORO_FILES    = {"kokoro-v1.0.int8.onnx": "kokoro-v1.0.int8.onnx", "voices-v1.0.bin": "voices-v1.0.bin"}
KOKORO_BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/{name}"
KOKORO_SPEED    = 1.05


def synthesize_with_kokoro(turns: list, output: Path, tmp_dir: Path) -> None:
    import time
    import urllib.request
    import numpy as np
    import lameenc
    from kokoro_onnx import Kokoro
    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name in KOKORO_FILES:
        dest = KOKORO_DIR / name
        for n in range(5):
            if dest.exists() and dest.stat().st_size > 1_000_000:
                break
            try:
                urllib.request.urlretrieve(KOKORO_BASE_URL.format(name=name), dest.with_suffix(".part"))
                dest.with_suffix(".part").replace(dest)
            except Exception:  # noqa: BLE001
                if n == 4:
                    raise
                time.sleep(3 * (n + 1))
    k = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.int8.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
    enc = None
    with open(output, "wb") as fh:
        for i, (speaker, text) in enumerate(turns):
            voice = KOKORO_VOICES["ALEX" if speaker == "ALEX" else "SAM"]
            samples, rate = k.create(text, voice=voice, speed=KOKORO_SPEED, lang="en-us")
            if enc is None:
                enc = lameenc.Encoder()
                enc.set_bit_rate(48); enc.set_in_sample_rate(rate); enc.set_channels(1); enc.set_quality(2)
            pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
            fh.write(enc.encode(pcm + b"\x00\x00" * int(rate * PIPER_PAUSE_SECS)))
            if (i + 1) % 20 == 0 or i + 1 == len(turns):
                print(f"    Synthesized {i + 1}/{len(turns)} turns (Kokoro)...")
        if enc is None:
            raise RuntimeError("no turns to synthesize")
        fh.write(enc.flush())
    print(f"  ✓ MP3 created: {output} (Kokoro)")


def synthesize_episode(turns: list, output: Path, tmp_dir: Path) -> str:
    """The original Microsoft voices first, through each known host; then Kokoro;
    Piper only as the last resort. Returns the engine used; raises only if all fail."""
    def _clear():
        for stale in tmp_dir.glob("seg_*.mp3"):
            stale.unlink()

    for host in EDGE_HOSTS:
        try:
            _use_edge_host(host)
            seg_paths = asyncio.run(_generate_all_audio(turns, tmp_dir))
            if not seg_paths:
                raise RuntimeError("No segments produced")
            merge_mp3s(seg_paths, output)
            return "edge-tts"
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ edge-tts via {host.split('/')[2]} failed ({str(exc)[:100]})")
            _clear()
    for name, fn in (("kokoro", synthesize_with_kokoro), ("piper", synthesize_with_piper)):
        try:
            print(f"  → switching to {name}")
            fn(turns, output, tmp_dir)
            return name
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ {name} failed ({str(exc)[:120]})")
            _clear()
    raise RuntimeError("every voice engine failed")




# ── Theme music ──────────────────────────────────────────────────────────────
# Every engine writes 24 kHz mono 48 kbps MP3 (edge-tts's native format), and so do
# the themes, so the pieces join byte-for-byte and players read one steady stream.
AUDIO_RATE  = 24_000
THEME_DIR   = Path(__file__).resolve().parent / "assets"
THEME_INTRO = THEME_DIR / "theme_intro.mp3"
THEME_OUTRO = THEME_DIR / "theme_outro.mp3"


def _encode_mp3(pcm16: bytes) -> bytes:
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(48)
    enc.set_in_sample_rate(AUDIO_RATE)
    enc.set_channels(1)
    enc.set_quality(2)
    return enc.encode(pcm16) + enc.flush()


def split_at_theme(script: str) -> tuple:
    """(cold-open turns, the rest) — split at the [THEME] line, if there is one."""
    if "[THEME]" not in script:
        return [], parse_script(script)
    head, tail = script.split("[THEME]", 1)
    return parse_script(head), parse_script(tail)


def synthesize_with_theme(script: str, output: Path, tmp_dir: Path) -> str:
    """cold open → intro theme → episode → outro theme. The music is optional: if a
    theme file is missing the episode still airs, just without it."""
    cold, rest = split_at_theme(script)
    parts, engine = [], None
    if cold:
        d = tmp_dir / "cold"
        d.mkdir(exist_ok=True)
        engine = synthesize_episode(cold, d / "cold.mp3", d)
        parts.append(d / "cold.mp3")
    if THEME_INTRO.exists():
        parts.append(THEME_INTRO)
    d = tmp_dir / "main"
    d.mkdir(exist_ok=True)
    engine = synthesize_episode(rest, d / "main.mp3", d) if engine is None else \
        _same_engine(engine, rest, d / "main.mp3", d)
    parts.append(d / "main.mp3")
    if THEME_OUTRO.exists():
        parts.append(THEME_OUTRO)
    with open(output, "wb") as out:
        for p in parts:
            out.write(Path(p).read_bytes())
    print(f"  ✓ Assembled: {'cold open + ' if cold else ''}{'theme + ' if THEME_INTRO.exists() else ''}"
          f"episode{' + outro' if THEME_OUTRO.exists() else ''}")
    return engine


def _same_engine(engine: str, turns: list, output: Path, tmp_dir: Path) -> str:
    """The rest of the episode in the voice the cold open used, so the hosts never
    change voice after the theme. Falls back through the chain only if that fails."""
    try:
        if engine == "kokoro":
            synthesize_with_kokoro(turns, output, tmp_dir)
            return engine
        if engine == "piper":
            synthesize_with_piper(turns, output, tmp_dir)
            return engine
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ {engine} failed after the cold open ({str(exc)[:100]}) — trying the full chain")
    return synthesize_episode(turns, output, tmp_dir)

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
    # Manual redo of the latest episode (workflow input replace_latest): generate as
    # if the latest episode had never aired. Nothing is removed up front — the new
    # episode simply takes the same number and overwrites its files and metadata
    # when, and only when, this run succeeds.
    if os.environ.get("REPLACE_LATEST") == "1" and old_meta.get("archive"):
        replaced = old_meta.get("episode")
        old_meta = {**old_meta["archive"][0], "archive": old_meta["archive"][1:]}
        print(f"  ↺ Redoing episode {replaced}: building on episode {old_meta.get('episode')}")
    settings          = _fetch_settings()            # holdings, transactions, cash — live from KV
    computed_holdings = settings.get("computed_holdings") or _fetch_computed_holdings()
    settings.setdefault("computed_holdings", computed_holdings)
    memory            = _desk.load_memory(MEMORY_FILE)
    ep_num            = (old_meta.get("episode", 0) or 0) + 1

    if not intel.get("generated_at"):
        print("  ⚠ No intelligence.json found — generating without weekly intel data")
    if not computed_holdings:
        print("  ⚠ No computed_holdings in KV — portfolio figures will use fallback context")

    past_scripts = _load_past_scripts()
    if os.environ.get("REPLACE_LATEST") == "1":
        past_scripts = {k: v for k, v in past_scripts.items() if k <= (old_meta.get("episode") or 0)}

    # 2. Plan the episode (every subject and number decided in code), then write it
    print("2/4  Planning and writing the episode...")
    plan = build_episode_plan(intel, snapshot, settings, old_meta, memory, past_scripts, ep_num)
    result = produce_episode(groq_key, plan, history=list(past_scripts.values()))
    if result is None:
        return 1
    script, facts, segments = result

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
    print("3/4  Synthesizing audio (Microsoft voices; Kokoro, then Piper, as fallbacks)...")
    mp3_name = f"podcast_ep{ep_num:03d}.mp3"
    txt_name = f"podcast_ep{ep_num:03d}.txt"
    mp3_path = DATA_DIR / mp3_name
    txt_path = DATA_DIR / txt_name

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        try:
            engine = synthesize_with_theme(script, mp3_path, tmp_dir)
            print(f"  ✓ Audio engine: {engine}")
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
        "title":            episode_title(summary, script, education_topic, ep_num),
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

    # The show's memory: next week scores these scenarios, keeps the learning-topic
    # promise, and avoids these subjects and comparisons.
    ch = plan.get("changes") or {}
    _desk.save_memory(MEMORY_FILE, memory, {
        "episode": ep_num, "date": meta["date"], "title": meta["title"],
        "main_story": {"title": plan["main"]["title"],
                       "themes": sorted(_themes_of(plan["main"]["title"] + " " + plan["main"]["body"]))},
        "spotlight": (plan.get("spot") or {}).get("ticker"),
        "quick_hits": [q["title"] for q in plan.get("quick") or []],
        "learning_topic": plan["topic"], "next_learning_topic": plan["next_topic"],
        "scenarios": {"sp500_close": plan["sp500"]["close"], "sp500_date": plan["sp500"]["date"],
                      "band_pct": _desk.SCENARIO_BAND_PCT,
                      "probabilities": _desk.scenario_probabilities(segments.get("scenarios", ""))},
        "analogies": _desk.extract_analogies(script),
        "portfolio": {"total_value": ch.get("total_now"), "contrib_total": ch.get("contrib_total"),
                      "pnl_total": ch.get("pnl_total"), "sector_weights": ch.get("sector_weights")},
    })
    print(f"  ✓ Show memory updated ({MEMORY_FILE.name})")

    print(f"\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  ✓ Episode {ep_num}: {meta['title']}")
    print(f"  ✓ Duration: {duration_str} | Archive: {len(archive)} previous")
    if education_topic:
        print(f"  ✓ Education: {education_topic}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
