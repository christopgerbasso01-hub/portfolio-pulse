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
Give a SHORT agenda teaser: "This week we're covering [Topic 1], [Topic 2], and in our learning segment, [topic]."
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

Write PART 1 now (Welcome Back + Portfolio Recap + Deep Dive 1):"""


# ============================================================
# SCRIPT PROMPT — PART 2: Deep Dive 2 + Learning Segment + Scenarios + Close
# ============================================================
SCRIPT_PROMPT_PART2 = """You are writing the SECOND HALF of "Portfolio Pulse Weekly" for {today}.

RECAP OF PART 1 ALREADY WRITTEN (continue naturally from here):
Deep Dive 1 covered: {dive1_summary}

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

EDUCATION TOPICS ALREADY COVERED IN PAST EPISODES (NEVER repeat these):
{education_topics_used}

Choose ONE topic that:
- Is NOT on the list above — if all examples below are used, invent a new one
- Has never been covered in any previous episode
- Is timeless investing knowledge, NOT tied to this week's specific news
- Is directly relevant to a 24-year-old growth investor with 3x leverage exposure
- Examples of eligible topics (pick one not in the used list above):
    How 3x ETF daily reset causes return decay in sideways or choppy markets
    How the yield curve predicts recessions (and when it lies)
    What VIX actually measures and why it spikes — mechanics not just "fear index"
    How earnings revisions move stock prices before the report even drops
    How P/E ratios work in practice — when high is fine and when it's a warning
    Sector rotation — which sectors lead vs lag in different economic phases
    How short interest works and what unusually high short interest signals
    What insider buying/selling data actually tells you (vs what it doesn't)
    How options pricing works — why implied volatility matters even if you don't trade options
    What the Fed's balance sheet is and how QE/QT flows through to equity markets
    How currency carry trade works and why it can suddenly unwind
    Reading 13F filings — what institutional ownership changes signal
    What book value means and when it matters (and when market cap is what counts)
    How dividend investing works mathematically — yield, growth, compounding
    Understanding leverage ratio vs leverage risk — they're not the same

CRITICAL: Place the following marker on its own line immediately BEFORE Alex starts this segment
(no ALEX: or SAM: prefix — just the raw marker line, it won't be read aloud):
[EDUCATION_TOPIC: <3 to 5 word name of the topic you chose>]

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
    fallback     = "llama-3.1-8b-instant"
    max_attempts = 8
    last_err     = "unknown"

    for attempt in range(max_attempts):
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": primary, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": max_tokens, "temperature": 0.85},
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
                              "max_tokens": max_tokens, "temperature": 0.85},
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
                    computed_holdings: list, registry: dict) -> str:
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
    portfolio_ctx = _build_portfolio_context(computed_holdings, snaps)
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
        live_portfolio=live_port, outlook=outlook, macro=macro, news=news,
        portfolio=portfolio_ctx,
    ), "Part 1", max_tokens=4096)

    # Extract a summary of Deep Dive 1 for Part 2 context
    dive1_lines = [l for l in part1.split('\n') if l.strip().startswith(('ALEX:', 'SAM:'))]
    dive1_last  = ' '.join(l[5:].strip() for l in dive1_lines[-6:])[:400]

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
        education_topics_used=education_topics_str,
        picks=picks, strengths=strengths, concerns=concerns, strategy=strategy,
        news=news, portfolio=portfolio_ctx,
    ), "Part 2", max_tokens=4096)

    full = part1.rstrip() + "\n\n" + part2.lstrip()
    print(f"  ✓ Full script: {len(full.split()):,} words across both parts")
    return full


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
        script = generate_script(intel, snapshot, old_meta, groq_key, computed_holdings, registry)
    except Exception as exc:
        print(f"ERROR: Script generation failed: {exc}")
        return 1

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

    # Save full script text (used by future episodes for deep topic registry)
    try:
        txt_path.write_text(script, encoding="utf-8")
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
