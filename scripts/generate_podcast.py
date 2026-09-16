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
INVESTOR: Christopher, 24M, Toronto. $90K salary. HIGH risk tolerance.
GTA home purchase planned: FHSA + RRSP HBP = ~$90K down. Returns Canada March 2027.
TFSA/FHSA: no contributions in 2026. RRSP: eligible for new buys only.

NOTE: Live portfolio data unavailable — figures below may be outdated.
KEY HOLDINGS: Leveraged ETFs 49% (FANG+ 3x, S&P500 3x, Dow 3x), Nvidia (TFSA, +1776% NEVER SELL),
  Broadcom, Taiwan Semi, CI Tech Giants ETF, CIBC, Royal Bank, Bank of Montreal,
  Enbridge, Energy Transfer, Shell, MicroStrategy, Grayscale Bitcoin, BYD.
SENSITIVITIES: 3x leverage amplifies both ways. ~68% USD exposure.
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


def _build_portfolio_context(holdings: list[dict], snapshots: dict) -> tuple[str, dict]:
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

    # Per-account with week-over-week and all-time ROI
    acct_lines = []
    for acct in ["TFSA", "Investment", "FHSA", "RRSP"]:
        v_now  = float(accounts.get(acct)   or 0)
        v_prev = float(prev_accts.get(acct) or v_now)
        cost   = float(acct_cost.get(acct)  or 0)
        chg    = v_now - v_prev
        pct    = (chg  / v_prev * 100) if v_prev > 0 else 0.0
        a_roi  = ((v_now - cost) / cost * 100) if cost > 0 else 0.0
        acct_lines.append(
            f"  {acct:12s} ${v_now:>9,.0f} CAD  "
            f"WoW {chg:>+8,.0f} ({pct:>+5.1f}%)  "
            f"All-time ROI {a_roi:>+5.0f}%"
        )

    # Math anchors — grounded in actual position sizes
    per_1pct_sp  = leverage_cad * 0.03   # 3× leverage = 3× the market move
    # usd_exp_cad is ALREADY in CAD. A 1¢ move in USD/CAD acts on the USD
    # NOTIONAL, so the swing is notional × 0.01 — not the CAD value × 0.01.
    # Omitting this division overstated the anchor by the whole exchange rate
    # (~1.38×) in every episode, and the scripts repeated the wrong number
    # faithfully. The notional is published alongside it below so the model
    # cannot re-derive it the wrong way round.
    usd_notional = (usd_exp_cad / usdcad) if usdcad else 0.0
    per_1cent_fx = usd_notional * 0.01   # per 1¢ USD/CAD shift
    lev_pct      = leverage_cad / total_val * 100 if total_val else 0
    usd_pct      = usd_exp_cad  / total_val * 100 if total_val else 0

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
    _tot_for_pct = total_val_hint = sum(p["cad"] for p in positions.values()) or 1.0
    sector_line = "  " + " | ".join(
        f"{s} {v / _tot_for_pct * 100:.1f}%" for s, v in
        sorted(sector_cad.items(), key=lambda kv: -kv[1]))
    held_line = "  " + ", ".join(sorted(positions.keys()))
    n_positions = len(positions)

    movers_str = "\n".join(
        f"  {m['name']:<26} ({m['acct']})  "
        f"{m['wk_pct']:>+6.1f}%  →  {m['wk_cad']:>+9,.0f} CAD"
        for m in top_movers
    ) or "  (snapshot prices unavailable for this week)"

    # State the real span. The old label said "Weekly change" regardless of how
    # far back the baseline actually was, which is how an 87-day move reached
    # the script as a weekly one.
    span_days = (datetime.fromisoformat(latest_date).date()
                 - datetime.fromisoformat(baseline_date).date()).days
    period    = f"{baseline_date} → {latest_date} ({span_days} days)"

    text = f"""INVESTOR: Christopher, 24M, Toronto. $90K salary. HIGH risk tolerance.
GTA home purchase: FHSA + RRSP HBP = ~$90K down payment. Returns Canada March 2027.
TFSA/FHSA: no new contributions in 2026. RRSP: eligible for new buys only.

━━━ LIVE PORTFOLIO [{sorted_dates[-1]}] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total value:   ${total_val:>10,.0f} CAD
Change {period}: {wk_gain:>+10,.0f} CAD ({wk_pct:>+5.1f}%)
All-time ROI:  {roi_pct:>10.1f}%

ACCOUNTS (change over {period}):
{chr(10).join(acct_lines)}

USD/CAD: {usdcad:.4f}

━━━ MATH ANCHORS — every dollar estimate in this episode MUST derive from these ━━━
3× Leveraged exposure: ${leverage_cad:>9,.0f} CAD ({lev_pct:.0f}% of portfolio)
USD exposure:          ${usd_exp_cad:>9,.0f} CAD ({usd_pct:.0f}% of portfolio)
Per 1% index move    → ±${per_1pct_sp:>7,.0f} CAD on leveraged positions alone
USD notional:          ${usd_notional:>9,.0f} USD  ← an FX move acts on THIS, not the CAD value
Per 1¢ USD/CAD move  → ±${per_1cent_fx:>7,.0f} CAD on USD holdings
RULE: FX impact = USD notional × the cent move. Never multiply the CAD figure.
FX DIRECTION — do not invert this:
  USD/CAD RISING  = CAD weaker = our USD holdings are worth MORE in CAD.
  USD/CAD FALLING = CAD stronger = our USD holdings are worth LESS in CAD.
  So "1.3904 → 1.3950" is a WEAKER CAD, and "1.3904 → 1.3650" is a STRONGER CAD.
  Canadian-listed holdings (.TO) are priced in CAD and do not move on FX at all.

━━━ WHAT WE ACTUALLY HOLD ({n_positions} positions) ━━━━━━━━━━━━━━━━━━━━━━━━━
{held_line}
Anything not on that list is NOT owned. Never say "we hold", "our position in",
or "our exposure to" about a ticker absent from it.

SECTOR WEIGHTS (computed, not estimated):
{sector_line}
Describe the portfolio using these figures. Do not characterise it from memory.

SOURCING: every market statistic you cite — index levels, VIX, oil inventories,
central-bank dates — must come from the intelligence provided in this prompt. If
it is not there, discuss the mechanism without inventing a number or a date.
Portfolio implied β  ≈ 1.8× market (leverage concentration)
RULE: Bear case estimates must be at least as large as bull case estimates in absolute terms.
RULE: Never invent a dollar figure — use the anchors above and show your reasoning.

━━━ TOP MOVERS, {period} (your exact shares × price change) ━━━
{movers_str}

━━━ KEY HOLDINGS (use NAMES not tickers — 90% of the time) ━━━━━━━━━━━━━━━━━━
Leveraged 3× ({lev_pct:.0f}%): FANG+ 3×, S&P500 3×, Dow 3×
Tech: Nvidia (TFSA — NEVER SELL, permanently tax-free at +1,776%)
      Broadcom, Taiwan Semi, CI Tech Giants ETF, Microsoft, Apple, Qualcomm
CDN Financials: CIBC, Royal Bank, Bank of Montreal
Energy: Enbridge, Energy Transfer, Shell
Speculative: MicroStrategy, Grayscale Bitcoin, BYD
RRSP Cash: ~$7,685 USD idle → deploy to BMO S&P500 ETF

CRITICAL: Every % change you mention must match the weekly mover data above.
Do not cite performance figures not present in this context."""

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
                    "cad": round(m["wk_cad"])} for m in top_movers],
        "usd_notional": round(usd_notional),
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
SCRIPT_PROMPT_PART1 = """You are writing the FIRST HALF of a weekly financial podcast called "Portfolio Pulse Weekly."

EPISODE DATE: {today}
TRADING WEEK: {week_range}
MARKET MOOD: {mood}

{registry_context}

━━━ TICKER ROTATION (CRITICAL — read before picking Deep Dive topics) ━━━━━━━━━
{ticker_rotation}

PORTFOLIO PERFORMANCE THIS WEEK:
{live_portfolio}

THIS WEEK'S MACRO INTELLIGENCE:
Daily Outlook: {outlook}

Macro Themes:
{macro}

Market News:
{news}

PORTFOLIO CONTEXT:
{portfolio}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PODCAST STYLE — study this carefully:

TONE MODEL: Think NPR/Bloomberg deep-dive format. Not a data recital. A story.
Every number gets a mechanism. Every mechanism gets an implication. Every implication connects back to the portfolio.

CONTENT BALANCE (non-negotiable):
- Backward-looking (what happened): MAX 35% of content
- Forward-looking (what's coming, upcoming catalysts, what to watch): MIN 50% of content
- Every backward-looking statement should pivot: "...and here's why that matters for the next few weeks."
- Deep Dive 1 must be predominantly forward-looking — the market event is the context, not the story.

FIGURE RULE (non-negotiable):
Every number you state MUST be immediately followed by the mechanism that caused it or will cause it.
BAD: "The portfolio dropped $4,200 this week."
GOOD: "The portfolio dropped $4,200 — because when three of the five FANG+ basket names sold off together,
       the 3x leverage turned what was a 1.5% index move into a 4.5% hit on that position specifically."
Never state a figure without its mechanism. Not once. Not even in short recap lines.

METAPHOR RULE: You may use exactly ONE metaphor for the ENTIRE episode. One. Make it count.
Do not use it more than once. Do not introduce a second one. One metaphor, total.

OPENING STRUCTURE:
[WELCOME BACK — 60 seconds]
Alex welcomes listeners back warmly.
"Hey everyone, welcome back to Portfolio Pulse Weekly. I'm Alex, joined as always by Sam..."
Give a SHORT agenda teaser: "This week we're covering [Topic 1], [Topic 2], and in our
learning segment, [LEARNING SEGMENT TOPIC]."
THE LEARNING SEGMENT TOPIC FOR THIS EPISODE IS FIXED: {education_topic}
Name that exact topic in the teaser. Do not substitute your own — the second half
of the episode is already committed to writing it, and announcing anything else
promises the listener a segment that never arrives.
ONE sentence hook — a genuine "wait, what?" about the most counterintuitive thing this week.

[PORTFOLIO RECAP — 2.5 minutes]
Do NOT recite a scoreboard. Tell the STORY of what drove the portfolio's moves.
Structure:
- How did the overall portfolio do vs last week? (one honest sentence, mechanism first)
  BAD: "FANG+ 3x ETF was up 6.2%"
  GOOD: "The FANG+ 3x ETF ripped because Meta guided AI capital spending way higher — and when
         three of the five basket names move together, the 3x leverage turns that into something
         that really shows up in the numbers."
- Acknowledge ONE thing that underperformed or surprised, with a concrete reason
- ONE natural callback to the previous episode (one sentence — only if it adds value)

[DEEP DIVE 1 — 5 to 6 minutes]
Pick the single most important macro force impacting this portfolio THIS WEEK.
The topic MUST be fresh — check the topic registry above and do not repeat anything already covered.
Structure:
- SAM opens with the paradox/tension hook for this segment
- ALEX explains the mechanism (use the ONE metaphor here if anywhere)
- SAM pushes back TWICE with real challenges ("But hang on..." / "I need to push back here...")
- ALEX re-explains more clearly each time
- Connect explicitly to portfolio holdings by NAME: "Which means for us, Nvidia and Broadcom in particular..."
- The forward-looking pivot is MANDATORY: spend at least 3 of the 6 minutes on "and here's what this
  sets up for the next 2-4 weeks" — specific upcoming catalysts, dates, triggers to watch
- End with: "So going into next week specifically, here's what this means for the portfolio..."

DIALOGUE RULES (non-negotiable):
- 90% company NAMES, 10% tickers. "Nvidia" not "NVDA". "Broadcom" not "AVGO".
- Every % explained as a mechanism + dollar impact on the portfolio
- Short reaction turns mixed with longer explanations (min 25% of turns under 20 words)
- Natural filler: "Right.", "Yeah.", "Exactly.", "Hmm.", "Okay but...", "Ah — I see."
- NO banned phrases: "it's worth noting", "going forward", "as mentioned", "at the end of the day",
  "in today's market", "landscape", "navigate", "tailwinds", "headwinds"
- Every turn starts differently — never two consecutive turns with the same opening word

WORD COUNT: MINIMUM 1,900 words, TARGET 2,200 words for this half.
If running short, go DEEPER on the forward-looking component of Deep Dive 1 — more upcoming catalysts,
more specific dates/events, more portfolio implications. Do not pad with filler.
FORMAT: Every line starts with "ALEX:" or "SAM:" — no exceptions, no stage directions, no headers.

THIS IS THE FIRST HALF ONLY — DO NOT CLOSE THE EPISODE.
Part 2 is written separately and is joined directly onto your final line, in the
same episode. It contains Deep Dive 2, the learning segment, the scenarios and
the closing. So do NOT write a sign-off, a wrap-up, a "that's the play", a "stay
tuned", or a "we'll be back next Monday". Stop mid-conversation on a Deep Dive 1
line so the second half continues straight out of it.

Write PART 1 now (Welcome Back + Portfolio Recap + Deep Dive 1):"""


# ============================================================
# SCRIPT PROMPT — PART 2: Deep Dive 2 + Learning Segment + Scenarios + Close
# ============================================================
SCRIPT_PROMPT_PART2 = """You are writing the SECOND HALF of "Portfolio Pulse Weekly" for {today}.

PART 1 IS ALREADY WRITTEN AND IS JOINED DIRECTLY ONTO YOUR FIRST LINE.
The listener has just heard it. Here is what it contained:
{dive1_summary}

YOU ARE CONTINUING ONE EPISODE, NOT STARTING ANYTHING.
- Do NOT greet the listener or say "welcome back".
- Do NOT open with "let's pick up where we left off" or recap what Part 1 covered.
- Do NOT say the hosts "just walked through" or "just discussed" a topic unless it
  appears above as something actually discussed. A topic that was merely named in
  a passing list of upcoming events was NOT walked through, and claiming otherwise
  tells the listener they missed a segment that never happened.
Begin directly with Deep Dive 2's hook.

━━━ TICKER ROTATION (read before picking Deep Dive 2 subject) ━━━━━━━━━━━━━━━
{ticker_rotation}

THIS WEEK'S PICKS & STRATEGY:
{picks}
{strengths}
{concerns}
{strategy}

MARKET NEWS (for Deep Dive 2):
{news}

PORTFOLIO CONTEXT:
{portfolio}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Continue the podcast naturally from where Part 1 ended.

CONTENT BALANCE (non-negotiable):
- Backward-looking content (what happened): MAX 35% of all content in this half
- Forward-looking content (what's coming, upcoming events, catalysts): MIN 50%
- Every backward statement must pivot to its forward implication

FIGURE RULE (non-negotiable): every number must be immediately followed by its mechanism.
NO banned phrases: "it's worth noting", "going forward", "as mentioned", "at the end of the day",
"in today's market", "landscape", "navigate", "tailwinds", "headwinds"

[DEEP DIVE 2 — 5 to 6 minutes]
Pick a SPECIFIC holding or sector that is at a decision point or has a forward-looking catalyst.
The subject must NOT be a ticker spotlighted in recent episodes (see ticker rotation above).
Prioritize holdings that rarely get spotlight time — not the obvious FANG+ / Nvidia again.

Structure:
- ALEX introduces the specific story with a hook
- SAM asks "but why does this matter for us specifically RIGHT NOW?"
- ALEX explains the mechanism — NO new metaphor (the single episode metaphor was used in Part 1)
- At least ONE genuine push-back from SAM
- Explicit portfolio connection: "X dollars of our portfolio is directly exposed here..."
- End with: "Here's the specific catalyst or date we're watching on this one..."

[LEARNING SEGMENT — 2 to 3 minutes]
This segment steps back from this week's news to teach something genuinely useful.

THE TOPIC IS ALREADY CHOSEN FOR THIS EPISODE: {education_topic}

Write THAT topic. Do not pick a different one and do not broaden it. The first
half of this episode has already told the listener, by name, that this is what
the learning segment covers, so substituting anything else breaks the promise
the episode opened with.

(For reference, these were covered in past episodes and are not repeated:
{education_topics_used})

CRITICAL: Place the following marker on its own line immediately BEFORE Alex starts this segment
(no ALEX: or SAM: prefix — just the raw marker line, it won't be read aloud):
[EDUCATION_TOPIC: {education_topic}]

Then write the segment:
- ALEX or SAM introduces: "Before we get to our scenarios, let's step back and learn something..."
- Present the concept clearly — assume the listener is smart but not a professional
- Connect it briefly to this investor's portfolio where it naturally fits (don't force it)
- SAM asks at least one "okay but what does that actually mean in practice?" question
- Close with: "Alright, that's our learning segment for this week. On to scenarios..."

[SCENARIO FRAMEWORK — 2 minutes]
Three scenarios for the NEXT 2–4 WEEKS with specific probability.
Every scenario MUST state the portfolio impact in dollar terms derived from the math anchors.
Scenarios must be balanced — bear downside must be at least as large as bull upside in absolute dollars.

SHOW THE ARITHMETIC. For every dollar range, name the position and the percentage
move you applied to it, and make sure the total actually follows from them.
Episode 16 claimed a bear case of "$15,000 - $18,000" off drivers (Energy Transfer
-8%, a 0.015 CAD move) that add up to about $2,500 — roughly six times too large,
and nothing in the wording revealed the gap. If your drivers only justify a small
number, state the small number.

Format (use this exactly):
"Base case — [X]% probability: [specific mechanism that plays out] → portfolio impact: [dollar range]"
"Bull case — [X]% probability: [specific catalyst needed] → portfolio upside: [$X to $Y CAD]"
"Bear case — [X]% probability: [specific trigger] → portfolio downside: [$X to $Y CAD]"

Probabilities must sum to 100%. Base case should be 45-55%.

[CLOSING — 60 to 90 seconds]
- ONE open, unanswered question that leaves the listener thinking about something deeper
  (Not a data question — a "what does this mean about investing" type question)
- "One thing we're watching next week" — specific event, date if known, and why it matters
- Warm sign-off, brief tease for next week

WORD COUNT: MINIMUM 1,800 words, TARGET 2,100 words for this half.
If running short, deepen the learning segment or add more scenario nuance.
FORMAT: Every line starts with "ALEX:" or "SAM:" — EXCEPT the [EDUCATION_TOPIC: ...] marker line.
No stage directions, no headers, no section labels.
NAMES not tickers (90%). Short reactions mixed with explanations. Every turn opens differently.

Write PART 2 now (Deep Dive 2 + Learning Segment + Scenarios + Closing):"""


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
    for topic in EDUCATION_TOPICS:
        if topic.lower() not in used:
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


def generate_script(intel: dict, snapshot: dict, old_meta: dict, api_key: str,
                    computed_holdings: list, registry: dict) -> tuple[str, dict]:
    now     = datetime.now(timezone.utc)
    today   = now.strftime("%A, %B %d, %Y")
    week    = _week_trading_range(now)
    # Fixed before either half is written, so the agenda in Part 1 and the
    # segment in Part 2 cannot disagree.
    education_topic = _choose_education_topic(registry.get("education_topics_used", []))
    print(f"     Learning segment fixed up front: {education_topic}")
    mood    = intel.get("market_mood", "neutral").upper()
    outlook = intel.get("daily_outlook", "")[:300]
    macro   = "\n".join(f"• {m['title']} [{m.get('impact','?')}]: {m.get('body','')[:300]}"
                        for m in intel.get("macro", [])[:3])
    news    = "\n".join(f"• {n['headline']}: {n.get('body','')[:250]} | Exposure: {n.get('exposure','')[:100]}"
                        for n in intel.get("news", [])[:4])
    # Labelled explicitly as NOT owned. Episode 16 had Sam say "we've got a lot of
    # exposure to other energy names — Enbridge, Canadian Natural", but Canadian
    # Natural was a suggestion in this list, never a holding.
    picks   = "\n".join(f"• {p['ticker']} ({COMPANY_NAMES.get(p['ticker'], p['ticker'])}) "
                        f"— CANDIDATE, NOT OWNED: {p.get('thesis','')[:200]}"
                        for p in intel.get("picks", [])[:3])
    strengths = "\n".join(f"• {s['text'][:200]}" for s in intel.get("strengths", [])[:3])
    concerns  = "\n".join(f"• {c['text'][:200]}" for c in intel.get("concerns", [])[:3])
    strategy  = "\n".join(f"• {s['text'][:200]}" for s in intel.get("strategy_short", [])[:3])

    # Registry-derived context
    registry_context    = registry.get("registry_text", "No previous episodes — fresh start.")
    recently_spotlighted = registry.get("recently_spotlighted_tickers", [])
    education_used      = registry.get("education_topics_used", [])

    if recently_spotlighted:
        spotlighted_str = ", ".join(recently_spotlighted[:8])
        ticker_rotation = (
            f"These tickers were the Deep Dive focus in recent episodes — "
            f"do NOT spotlight them again in Deep Dive 2:\n  {spotlighted_str}\n"
            f"You may still reference them briefly in portfolio recap math or scenarios.\n"
            f"Choose a DIFFERENT holding for Deep Dive 2 this week — explore something from the\n"
            f"underexposed side of the portfolio: Broadcom, Taiwan Semi, CIBC, Royal Bank,\n"
            f"Bank of Montreal, Enbridge, Energy Transfer, Visa, Live Nation, BYD, Qualcomm, etc."
        )
    else:
        ticker_rotation = (
            "No rotation constraints yet — all holdings are eligible for Deep Dive 2.\n"
            "Consider a holding that rarely gets spotlight time beyond FANG+ and Nvidia."
        )

    if education_used:
        education_topics_str = "\n".join(f"  • {t}" for t in education_used)
    else:
        education_topics_str = "  (none yet — first learning segment, all topics available)"

    # Build fully dynamic portfolio context — live holdings + snapshot prices
    snaps         = snapshot.get("snapshots", {})
    portfolio_ctx, portfolio_facts = _build_portfolio_context(computed_holdings, snaps)
    live_port     = "(see LIVE PORTFOLIO section in portfolio context below)"

    # Brief cooldown to separate from the registry-extraction call that ran just
    # before this, keeping us clear of the free-tier per-minute token budget.
    import time
    time.sleep(20)

    print("     Generating Part 1 (Welcome + Recap + Deep Dive 1)...")
    part1 = _groq_call(api_key, SCRIPT_PROMPT_PART1.format(
        today=today, week_range=week, mood=mood,
        registry_context=registry_context,
        ticker_rotation=ticker_rotation,
        education_topic=education_topic,
        live_portfolio=live_port, outlook=outlook, macro=macro, news=news,
        portfolio=portfolio_ctx,
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
    import time
    print("     Cooling down 35s to reset Groq TPM window before Part 2...")
    time.sleep(35)

    print("     Generating Part 2 (Deep Dive 2 + Learning Segment + Scenarios + Close)...")
    part2 = _groq_call(api_key, SCRIPT_PROMPT_PART2.format(
        today=today, dive1_summary=dive1_last,
        ticker_rotation=ticker_rotation,
        education_topic=education_topic,
        education_topics_used=education_topics_str,
        picks=picks, strengths=strengths, concerns=concerns, strategy=strategy,
        news=news, portfolio=portfolio_ctx,
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

_CLAIM_MONEY = re.compile(r"[-+]?\$\s?([\d,]+(?:\.\d+)?)\s*([KMB])?", re.I)
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
    return v * {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix.lower()] if suffix else v


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(MONEY_TOLERANCE_ABS, abs(b) * MONEY_TOLERANCE_PCT)


_PART1_SIGNOFF = re.compile(
    r"\b(stay tuned|we'?ll be back|see you next|that'?s the play|reconvene|"
    r"thanks for (?:tuning|listening)|until next (?:week|time)|keep an eye on those|"
    r"that'?s (?:it|all) for (?:this|today)|catch you next)\b", re.I)

_PART2_REOPEN = re.compile(
    r"\b(welcome back|let'?s pick up|picking up where|we just walked through|"
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


def verify_script_figures(script: str, facts: dict) -> list[str]:
    """Return a list of portfolio-level claims the context does not support."""
    if not facts:
        return []

    total    = float(facts.get("total_value") or 0)
    gain     = float(facts.get("wk_gain") or 0)
    gain_pct = float(facts.get("wk_pct") or 0)

    # Amounts the script may legitimately cite at portfolio level.
    allowed_money = {abs(total), abs(gain), float(facts.get("leverage_cad") or 0),
                     float(facts.get("usd_exposure") or 0)}
    allowed_money |= {abs(float(v)) for v in (facts.get("accounts") or {}).values()}
    allowed_money |= {abs(float(v)) for v in (facts.get("account_change") or {}).values()}
    allowed_money |= {abs(float(m.get("cad") or 0)) for m in (facts.get("movers") or [])}

    allowed_pct = {abs(gain_pct), abs(float(facts.get("roi_pct") or 0))}
    allowed_pct |= {abs(float(m.get("pct") or 0)) for m in (facts.get("movers") or [])}

    problems  = []
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", script))
    for sentence in sentences:
        if not _PORTFOLIO_CLAIM.search(sentence):
            continue
        # Scoped to one holding, hypothetical, or a projected band — all of these
        # are legitimate things for the script to say and none is a period claim.
        if (_POSITION_SCOPED.search(sentence) or _HYPOTHETICAL.search(sentence)
                or _MONEY_RANGE.search(sentence)):
            continue

        for raw, suffix in _CLAIM_MONEY.findall(sentence):
            amount = _claim_value(raw, suffix or None)
            if amount < 1_000:          # small change is almost always derived
                continue
            if not any(_close(amount, ok) for ok in allowed_money if ok):
                problems.append(
                    f"${amount:,.0f} is not supported by the context "
                    f"(period change ${gain:,.0f}, total ${total:,.0f}) — \"{sentence[:110]}\"")

        for raw in _CLAIM_PCT.findall(sentence):
            pct = abs(float(raw))
            if pct == 0:
                continue
            if not any(abs(pct - ok) <= PCT_TOLERANCE for ok in allowed_pct if ok):
                problems.append(
                    f"{pct}% is not supported by the context "
                    f"(period change {gain_pct:+.1f}%) — \"{sentence[:110]}\"")

    # Position sizes and the account a holding sits in. The portfolio-level pass
    # above deliberately skips these, which is how "about $14,000 CAD of Energy
    # Transfer in the Investment account" shipped — the stake is $1,791 and it is
    # in the TFSA.
    positions = facts.get("positions") or {}
    if positions:
        index = []
        for tkr, p in positions.items():
            nm      = str(p.get("name") or tkr)
            aliases = {nm}
            if len(tkr) >= 3:           # skip "V"/"ET" — too short to match safely
                aliases.add(tkr)
            first = nm.split()[0] if nm.split() else ""
            if len(first) >= 5:         # "Shell" out of "Shell PLC"
                aliases.add(first)
            index.append((aliases, tkr, p))

        for sentence in sentences:
            if not _SIZE_PHRASE.search(sentence) or _HYPOTHETICAL.search(sentence):
                continue
            if _MONEY_RANGE.search(sentence):
                continue

            matched = [(tkr, p) for aliases, tkr, p in index
                       if any(re.search(r"\b" + re.escape(a) + r"\b", sentence, re.I)
                              for a in aliases if a)]
            if not matched:
                continue

            # Attribute a figure only when exactly one holding is named:
            # "Energy Transfer and Shell together represent $4,700" is a combined
            # number and belongs to neither of them alone.
            # To BE a size claim the figure has to follow the size phrase
            # closely. "sits at roughly $12,000" is one; "Nvidia's position in AI
            # is worth watching, and a 1% move is about $1,200" names a holding
            # and two size words while claiming no size at all — and policing
            # that would block an episode over a perfectly sound sentence.
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
                    problems.append(
                        f"{p.get('name') or tkr} is stated as ${claim_amt:,.0f} but the "
                        f"position is ${actual:,.0f} — \"{sentence[:110]}\"")

            for tkr, p in matched:
                held = {str(a).lower() for a in (p.get("accounts") or [])}
                if not held:
                    continue
                for acct in ("TFSA", "Investment", "FHSA", "RRSP"):
                    if (re.search(r"\b" + acct + r"\b", sentence, re.I)
                            and acct.lower() not in held):
                        problems.append(
                            f"{p.get('name') or tkr} is placed in the {acct} but it is held "
                            f"in {', '.join(p.get('accounts') or [])} — \"{sentence[:110]}\"")

    # Direction errors matter more than magnitude: calling a losing week a gain
    # is the failure the user actually noticed. Judged per sentence so a forecast
    # ("if oil rallies the portfolio could climb") is not mistaken for a claim
    # about the period that just ended.
    if gain < 0:
        for sentence in sentences:
            if _HYPOTHETICAL.search(sentence):
                continue
            if re.search(r"\bportfolio\b[^.!?]{0,80}\b"
                         r"(gain(?:ed)?|jumped|rose|climbed|up)\b", sentence, re.I):
                problems.append(
                    f"script describes the portfolio as up, but the period change was "
                    f"${gain:,.0f} ({gain_pct:+.1f}%) over {facts.get('span_days')} days")
                break

    seen, unique = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


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
    try:
        script, facts = generate_script(intel, snapshot, old_meta, groq_key, computed_holdings, registry)
    except Exception as exc:
        print(f"ERROR: Script generation failed: {exc}")
        return 1

    # Refuse to publish portfolio figures the context does not support. One
    # regeneration, then fail the run — a wrong number spoken with confidence is
    # worse than a missing episode, and this is the failure that shipped four
    # times before anyone noticed.
    problems = verify_script_figures(script, facts)
    if problems:
        print(f"  ⚠ {len(problems)} unsupported figure(s) — regenerating once:")
        for p in problems[:5]:
            print(f"      • {p}")
        try:
            script, facts = generate_script(intel, snapshot, old_meta, groq_key,
                                            computed_holdings, registry)
        except Exception as exc:
            print(f"ERROR: Regeneration failed: {exc}")
            return 1
        problems = verify_script_figures(script, facts)
        if problems:
            print("ERROR: Script still cites unsupported portfolio figures after "
                  "regeneration — refusing to publish.")
            for p in problems:
                print(f"      • {p}")
            return 1
        print("  ✓ Regenerated script passes figure verification")
    else:
        print("  ✓ Portfolio figures verified against context")

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
