#!/usr/bin/env python3
"""
Portfolio Pulse — Weekly Podcast Generator
==========================================
Runs via cron-job.org every Monday at 6:00 AM UTC.

Pipeline:
  1. Load data/intelligence.json + KV snapshot data
  2. Groq (Llama 3.3 70B) → full podcast script (3,000–3,800 words)
     Split into 2 Groq calls to stay within token limits
  3. Kokoro TTS (kokoro-onnx) → WAV segments per speaker turn
  4. ffmpeg → concatenate WAVs to MP3
  5. Save episode + metadata

Voices: am_michael (Alex — warm male analyst), af_heart (Sam — curious female)
Style: NotebookLM-inspired — deep dives, mechanisms, genuine push-back
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
GROQ_MODEL    = "llama-3.3-70b-versatile"
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


def _build_portfolio_context(holdings: list[dict], snapshots: dict) -> str:
    """Build a fully dynamic portfolio context string.
    Combines live share counts (from KV) with live prices (from snapshots)
    to produce exact values, weekly movers, and math anchors.
    Falls back to static context if either source is missing.
    """
    if not holdings or not snapshots:
        return _PORTFOLIO_CONTEXT_FALLBACK

    sorted_dates = sorted(snapshots.keys())
    latest       = snapshots[sorted_dates[-1]]
    prev         = snapshots[sorted_dates[0]] if len(sorted_dates) > 1 else latest

    prices      = latest.get("holdings_prices", {})
    prev_prices = prev.get("holdings_prices",   {})
    usdcad      = float(latest.get("usdcad")    or 1.38)
    accounts    = latest.get("accounts",        {})
    prev_accts  = prev.get("accounts",          {})
    acct_cost   = latest.get("account_cost",    {})

    leverage_cad = 0.0
    usd_exp_cad  = 0.0
    movers       = []

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

    movers.sort(key=lambda x: abs(x["wk_cad"]), reverse=True)
    top_movers = movers[:6]

    # Portfolio-level totals come from the snapshot (calculated by the same
    # market API the dashboard uses — guaranteed to match what the user sees)
    total_val = float(latest.get("total_value") or 0)
    roi_pct   = float(latest.get("roi_pct")     or 0)
    wk_start  = float(prev.get("total_value")   or total_val)
    wk_gain   = total_val - wk_start
    wk_pct    = (wk_gain / wk_start * 100) if wk_start > 0 else 0.0

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
    per_1cent_fx = usd_exp_cad  * 0.01   # per 1¢ USD/CAD shift
    lev_pct      = leverage_cad / total_val * 100 if total_val else 0
    usd_pct      = usd_exp_cad  / total_val * 100 if total_val else 0

    movers_str = "\n".join(
        f"  {m['name']:<26} ({m['acct']})  "
        f"{m['wk_pct']:>+6.1f}%  →  {m['wk_cad']:>+9,.0f} CAD"
        for m in top_movers
    ) or "  (snapshot prices unavailable for this week)"

    period = (f"{sorted_dates[0]} → {sorted_dates[-1]}"
              if len(sorted_dates) > 1 else sorted_dates[0])

    return f"""INVESTOR: Christopher, 24M, Toronto. $90K salary. HIGH risk tolerance.
GTA home purchase: FHSA + RRSP HBP = ~$90K down payment. Returns Canada March 2027.
TFSA/FHSA: no new contributions in 2026. RRSP: eligible for new buys only.

━━━ LIVE PORTFOLIO [{sorted_dates[-1]}] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total value:   ${total_val:>10,.0f} CAD
Weekly change: {wk_gain:>+10,.0f} CAD ({wk_pct:>+5.1f}%)   [{period}]
All-time ROI:  {roi_pct:>10.1f}%

ACCOUNTS (week-over-week):
{chr(10).join(acct_lines)}

USD/CAD: {usdcad:.4f}

━━━ MATH ANCHORS — every dollar estimate in this episode MUST derive from these ━━━
3× Leveraged exposure: ${leverage_cad:>9,.0f} CAD ({lev_pct:.0f}% of portfolio)
USD exposure:          ${usd_exp_cad:>9,.0f} CAD ({usd_pct:.0f}% of portfolio)
Per 1% S&P 500 move  → ±${per_1pct_sp:>7,.0f} CAD on leveraged positions alone
Per 1¢ USD/CAD move  → ±${per_1cent_fx:>7,.0f} CAD on USD holdings
Portfolio implied β  ≈ 1.8× market (leverage concentration)
RULE: Bear case estimates must be at least as large as bull case estimates in absolute terms.
RULE: Never invent a dollar figure — use the anchors above and show your reasoning.

━━━ THIS WEEK'S TOP MOVERS (your exact shares × price change) ━━━━━━━━━━━━━━━━
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
{prev_ep_context}

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

OPENING STRUCTURE:
[WELCOME BACK — 60 seconds]
Alex welcomes listeners back warmly.
"Hey everyone, welcome back to Portfolio Pulse Weekly. I'm Alex, joined as always by Sam..."
Give a SHORT agenda teaser: "This week we're covering [Topic 1], [Topic 2], and wrapping up with what we're watching going into next week."
ONE sentence hook about the most interesting or paradoxical thing this week.
(The hook should create a "wait, what?" reaction — like "The thing that stopped me this week was...")

[PORTFOLIO RECAP — 2.5 minutes]
Do NOT recite a scoreboard. Tell the STORY of what drove the portfolio's moves.
Structure:
- How did the overall portfolio do vs last week? (one honest sentence)
- What drove the biggest moves? (mechanism first, number second)
  Bad: "FANG+ 3x ETF was up 6.2%"
  Good: "The FANG+ 3x ETF ripped this week because Meta guided their AI capital spending way higher than expected —
         and when three of the five biggest names in that basket all move together, the 3x leverage turns that
         into something that really shows up in the numbers."
- One honest acknowledgment if something underperformed or surprised us
- Reference the PREVIOUS episode callback naturally here (one sentence only)

[DEEP DIVE 1 — 5 to 6 minutes]
Pick the single most important macro force impacting the portfolio this week.
Structure:
- SAM opens with the paradox/tension hook for this segment
- ALEX explains using ONE central metaphor (introduce it early, return to it)
- SAM pushes back TWICE with real challenges ("But wait, I need to challenge that...")
- ALEX re-explains more clearly each time
- Connect explicitly to named portfolio holdings: "Which means for us, Nvidia and Broadcom in particular..."
- End with "So what does this mean right now?" — concrete implication

DIALOGUE RULES (non-negotiable):
- 90% company NAMES, 10% tickers. Say "Nvidia", not "NVDA". Say "Broadcom", not "AVGO".
- Every % explained as a mechanism and dollar impact on the portfolio
- Short reaction turns mixed with longer explanations (min 25% of turns under 20 words)
- Natural filler: "Right.", "Yeah.", "Exactly.", "Hmm.", "Okay but...", "Ah — I see."
- Sam says "But hang on" or "I need to push back" at least twice in Deep Dive 1
- NO phrases: "it's worth noting", "going forward", "as mentioned", "at the end of the day"
- Every turn starts differently — never two consecutive turns with the same opening word

WORD COUNT: MINIMUM 1,900 words, TARGET 2,200 words for this half.
You must reach 1,900+ words. If you are running short, extend the deep dive — go deeper into the mechanism, add more push-back turns, add more portfolio implications.
FORMAT: Every line starts with "ALEX:" or "SAM:" — no exceptions, no stage directions.

Write PART 1 now (Welcome Back + Portfolio Recap + Deep Dive 1):"""


# ============================================================
# SCRIPT PROMPT — PART 2: Deep Dive 2 + Scenarios + Close
# ============================================================
SCRIPT_PROMPT_PART2 = """You are writing the SECOND HALF of "Portfolio Pulse Weekly" for {today}.

RECAP OF PART 1 ALREADY WRITTEN (continue naturally from here):
Deep Dive 1 covered: {dive1_summary}

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

[DEEP DIVE 2 — 5 to 6 minutes]
Pick a specific position, sector rotation, or portfolio opportunity this week.
Could be: why one holding matters more than usual, a holding that's at a decision point,
or a new idea tied to this week's news that fits the portfolio thesis.

Structure (same style as Deep Dive 1):
- ALEX introduces the specific story with a hook
- SAM asks the obvious "but why does this matter for us specifically?" question
- ALEX explains the mechanism with a fresh metaphor (different from Part 1)
- At least ONE genuine push-back from SAM
- Explicit connection to the portfolio: "$X of our portfolio is directly exposed..."
- End with a concrete "here's what we're watching"

[SCENARIO FRAMEWORK — 2 minutes]
Three scenarios for the NEXT 2–4 WEEKS with approximate probability.
Each scenario must state the specific portfolio implication in dollar terms.

Format example:
"Base case — 50% probability: [what happens] → for our portfolio, this means [specific impact]"
"Bull case — 30% probability: [what happens] → [specific portfolio impact in $]"
"Bear case — 20% probability: [what happens] → [specific portfolio downside]"

[CLOSING — 60 seconds]
- One open, unanswered question to leave the listener thinking
  (Not a data question — a deeper "what does this all mean" question)
- "What we're watching next week" — ONE specific thing, why it matters for the portfolio
- Warm sign-off from both hosts, tease next week very briefly

WORD COUNT: MINIMUM 1,400 words, TARGET 1,700 words for this half.
If you are running short, extend the scenario discussion or add more depth to the closing thought.
FORMAT: Every line starts with "ALEX:" or "SAM:" — no exceptions.
NAMES not tickers (90%). Short reactions mixed with explanations.
NO repeated phrases. Every turn opens differently.

Write PART 2 now (Deep Dive 2 + Scenarios + Closing):"""


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


def _build_live_portfolio(snapshot: dict) -> str:
    """Build a narrative-ready portfolio performance string from snapshot data."""
    snaps = snapshot.get("snapshots", {})
    if not snaps:
        return "Portfolio performance data unavailable this week."

    sorted_dates = sorted(snaps.keys())
    if len(sorted_dates) < 2:
        latest = snaps[sorted_dates[-1]]
        return (f"Current portfolio value: ${latest.get('total_value', 0):,.0f} CAD. "
                f"ROI: {latest.get('roi_pct', 0):.1f}%.")

    newest = snaps[sorted_dates[-1]]
    oldest = snaps[sorted_dates[0]]
    tv_new = newest.get("total_value", 0) or 0
    tv_old = oldest.get("total_value", 0) or tv_new
    weekly_chg = tv_new - tv_old
    weekly_pct = (weekly_chg / tv_old * 100) if tv_old else 0
    accts_new = newest.get("accounts", {})
    accts_old = oldest.get("accounts", {})

    lines = [
        f"TOTAL PORTFOLIO: ${tv_new:,.0f} CAD | Weekly change: {weekly_chg:+,.0f} CAD ({weekly_pct:+.1f}%)",
        f"ROI all-time: {newest.get('roi_pct', 0):.1f}%",
    ]
    for acct in ["TFSA", "Investment", "FHSA", "RRSP"]:
        v_new = accts_new.get(acct, 0) or 0
        v_old = accts_old.get(acct, 0) or v_new
        chg = v_new - v_old
        pct = (chg / v_old * 100) if v_old else 0
        lines.append(f"  {acct}: ${v_new:,.0f} | {chg:+,.0f} ({pct:+.1f}%)")

    return "\n".join(lines)


def _build_prev_ep_context(meta: dict) -> str:
    """Build rich context from the last 1-2 episodes so the LLM avoids repeating
    themes and can make one natural callback. Returns a formatted string."""
    # The current meta IS last week's episode (we load it before writing the new one)
    episodes = []
    if meta.get("title"):
        episodes.append({
            "label": "LAST WEEK'S EPISODE",
            "title": meta.get("title", ""),
            "summary": meta.get("summary", {}),
        })
    for i, ep in enumerate(meta.get("archive", [])[:1]):
        episodes.append({
            "label": "TWO WEEKS AGO",
            "title": ep.get("title", ""),
            "summary": ep.get("summary", {}),
        })

    if not episodes:
        return "No previous episodes — fresh start, no callbacks needed."

    lines = ["=== PREVIOUS EPISODES — avoid repeating these themes ==="]
    for ep in episodes:
        lines.append(f"\n{ep['label']}: \"{ep['title']}\"")
        s = ep["summary"]
        if s.get("market_context"):
            lines.append(f"  Market themes covered: {' | '.join(x[:120] for x in s['market_context'][:3])}")
        if s.get("position_spotlight"):
            lines.append(f"  Positions spotlighted: {' | '.join(x[:80] for x in s['position_spotlight'][:2])}")
        if s.get("watch_list"):
            lines.append(f"  Watch list items: {' | '.join(x[:80] for x in s['watch_list'][:2])}")

    lines.append("\nCALLBACK RULE: Make exactly ONE natural callback reference to the last episode.")
    lines.append("TOPIC RULE: Do NOT rehash the same macro themes or portfolio angles from either episode. Find genuinely new angles this week.")
    return "\n".join(lines)


# ============================================================
# GROQ SCRIPT GENERATION
# ============================================================
def _groq_call(api_key: str, prompt: str, label: str, max_tokens: int = 4096) -> str:
    models = [GROQ_MODEL, "llama-3.1-8b-instant"]
    for attempt, model in enumerate(models * 2):
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens, "temperature": 0.85},
                timeout=120,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                print(f"  ✓ {label} with {model} ({len(text.split()):,} words)")
                return text
        except Exception as exc:
            print(f"  ⚠ {label} attempt {attempt+1} failed: {exc}")
            if attempt < 3:
                import time; time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"All Groq attempts failed for {label}")


def generate_script(intel: dict, snapshot: dict, old_meta: dict, api_key: str,
                    computed_holdings: list[dict]) -> str:
    now     = datetime.now(timezone.utc)
    today   = now.strftime("%A, %B %d, %Y")
    week    = _week_trading_range(now)
    mood    = intel.get("market_mood", "neutral").upper()
    outlook = intel.get("daily_outlook", "")[:300]
    macro   = "\n".join(f"• {m['title']} [{m.get('impact','?')}]: {m.get('body','')[:300]}"
                        for m in intel.get("macro", [])[:3])
    news    = "\n".join(f"• {n['headline']}: {n.get('body','')[:250]} | Exposure: {n.get('exposure','')[:100]}"
                        for n in intel.get("news", [])[:4])
    picks   = "\n".join(f"• {p['ticker']} ({COMPANY_NAMES.get(p['ticker'], p['ticker'])}): {p.get('thesis','')[:200]}"
                        for p in intel.get("picks", [])[:3])
    strengths = "\n".join(f"• {s['text'][:200]}" for s in intel.get("strengths", [])[:3])
    concerns  = "\n".join(f"• {c['text'][:200]}" for c in intel.get("concerns", [])[:3])
    strategy  = "\n".join(f"• {s['text'][:200]}" for s in intel.get("strategy_short", [])[:3])
    prev_ep_context = _build_prev_ep_context(old_meta)

    # Build fully dynamic portfolio context — live holdings + snapshot prices
    snaps = snapshot.get("snapshots", {})
    portfolio_ctx = _build_portfolio_context(computed_holdings, snaps)
    # live_port is now embedded inside portfolio_ctx; pass a brief label for the prompt slot
    live_port = "(see LIVE PORTFOLIO section in portfolio context below)"

    print("2/3  Generating Part 1 (Welcome + Recap + Deep Dive 1)...")
    part1 = _groq_call(api_key, SCRIPT_PROMPT_PART1.format(
        today=today, week_range=week, mood=mood, prev_ep_context=prev_ep_context,
        live_portfolio=live_port, outlook=outlook, macro=macro, news=news,
        portfolio=portfolio_ctx,
    ), "Part 1", max_tokens=4000)

    # Extract a summary of Deep Dive 1 for Part 2 context
    dive1_lines = [l for l in part1.split('\n') if l.strip().startswith(('ALEX:', 'SAM:'))]
    dive1_last  = ' '.join(l[5:].strip() for l in dive1_lines[-6:])[:400]

    print("     Generating Part 2 (Deep Dive 2 + Scenarios + Close)...")
    part2 = _groq_call(api_key, SCRIPT_PROMPT_PART2.format(
        today=today, dive1_summary=dive1_last, picks=picks,
        strengths=strengths, concerns=concerns, strategy=strategy,
        news=news, portfolio=portfolio_ctx,
    ), "Part 2", max_tokens=3500)

    full = part1.rstrip() + "\n\n" + part2.lstrip()
    print(f"  ✓ Full script: {len(full.split()):,} words across both parts")
    return full


# ============================================================
# SCRIPT SUMMARY (for podcast metadata)
# ============================================================
def generate_summary(script: str, intel: dict, api_key: str) -> dict:
    turns  = [l for l in script.split('\n') if l.startswith(('ALEX:', 'SAM:'))]
    sample = '\n'.join(turns[:30])
    prompt = f"""Extract a structured JSON summary of this podcast episode.

SCRIPT SAMPLE:
{sample}

Return ONLY valid JSON:
{{
  "episode_title": "< 10-word punchy title for this specific episode >",
  "mood_summary": "one sentence on the market mood and portfolio outlook",
  "portfolio_snapshot": ["3-4 bullet strings about portfolio performance"],
  "market_context": ["3-4 bullet strings about macro themes covered"],
  "watch_list": ["2-3 things to watch next week"],
  "action_items": ["2-3 specific portfolio actions discussed"]
}}"""
    try:
        raw = _groq_call(api_key, prompt, "Summary", max_tokens=800)
        start, end = raw.find('{'), raw.rfind('}') + 1
        return json.loads(raw[start:end]) if start >= 0 else {}
    except Exception:
        return {"episode_title": intel.get("daily_outlook", "Weekly Update")[:60]}


# ============================================================
# AUDIO GENERATION — Kokoro TTS
# ============================================================
def parse_script(script: str) -> list[tuple[str, str]]:
    turns = []
    for line in script.strip().split("\n"):
        line = line.strip()
        if line.startswith("ALEX:"):
            text = line[5:].strip()
            if text: turns.append(("ALEX", text))
        elif line.startswith("SAM:"):
            text = line[4:].strip()
            if text: turns.append(("SAM", text))
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

    # 1. Load all data sources in parallel (intelligence, snapshot, live holdings)
    print("1/4  Loading data...")
    intel             = _load_intel()
    snapshot          = _fetch_snapshot()
    old_meta          = load_meta()
    computed_holdings = _fetch_computed_holdings()   # live from KV — auto-synced by dashboard

    if not intel.get("generated_at"):
        print("  ⚠ No intelligence.json found — generating without weekly intel data")
    if not computed_holdings:
        print("  ⚠ No computed_holdings in KV — portfolio figures will use fallback context")

    # 2. Generate script (two Groq calls)
    print("2/4  Generating script...")
    try:
        script = generate_script(intel, snapshot, old_meta, groq_key, computed_holdings)
    except Exception as exc:
        print(f"ERROR: Script generation failed: {exc}")
        return 1

    turns = parse_script(script)
    if len(turns) < 20:
        print(f"ERROR: Only {len(turns)} speaker turns parsed — script too short")
        return 1
    print(f"  ✓ {len(turns)} speaker turns")

    # 3. Synthesize audio with Kokoro
    print("3/4  Synthesizing audio (edge-tts)...")
    ep_num   = (old_meta.get("episode", 0) or 0) + 1
    mp3_name = f"podcast_ep{ep_num:03d}.mp3"
    mp3_path = DATA_DIR / mp3_name

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

    # 4. Generate summary + save metadata
    print("4/4  Generating summary & saving metadata...")
    summary = generate_summary(script, intel, groq_key)
    now     = datetime.now(timezone.utc)

    # Build archive
    archive = []
    if old_meta.get("episode") and old_meta.get("file"):
        prev_entry = {
            "episode":      old_meta["episode"],
            "title":        old_meta.get("title", ""),
            "date":         old_meta.get("date", ""),
            "display_date": old_meta.get("display_date", ""),
            "duration":     old_meta.get("duration", ""),
            "mood":         old_meta.get("mood", ""),
            "mood_summary": old_meta.get("mood_summary", ""),
            "file":         old_meta.get("file", ""),
            "summary":      old_meta.get("summary", {}),
        }
        archive = [prev_entry] + (old_meta.get("archive", []))
    archive = archive[:MAX_EPISODES - 1]

    # Clean up old MP3s not in archive
    keep = {mp3_name} | {a["file"] for a in archive if a.get("file")}
    for f in DATA_DIR.glob("podcast_ep*.mp3"):
        if f.name not in keep:
            f.unlink()

    mood_val = intel.get("market_mood", "neutral")
    mood_labels = {
        "risk-on": "Markets favouring growth — leveraged positions in tailwind",
        "risk-off": "Defensive positioning — reduce leverage exposure",
        "neutral":  "Mixed signals — stay disciplined",
        "mixed":    "Conflicting signals — watch volatility closely",
    }

    meta = {
        "episode":      ep_num,
        "file":         mp3_name,
        "date":         now.strftime("%Y-%m-%d"),
        "display_date": now.strftime("%B %d, %Y"),
        "title":        summary.get("episode_title", f"Portfolio Pulse Ep {ep_num}"),
        "mood":         mood_val,
        "mood_summary": mood_labels.get(mood_val, mood_val),
        "duration":     duration_str,
        "duration_seconds": duration_secs,
        "generated_at": now.isoformat(),
        "archive":      archive,
        "summary":      summary,
    }
    PODCAST_META.write_text(json.dumps(meta, indent=2))

    print(f"\n  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"  ✓ Episode {ep_num}: {meta['title']}")
    print(f"  ✓ Duration: {duration_str} | Archive: {len(archive)} previous")
    return 0


if __name__ == "__main__":
    sys.exit(main())
