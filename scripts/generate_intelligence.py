#!/usr/bin/env python3
"""
Portfolio Pulse — Daily Intelligence Generator
Runs via GitHub Actions at 6 AM EST weekdays.
Pipeline: Finnhub news → Groq (Llama 3.3 70B) → intelligence.json
Output is consumed by index.html to populate sections 05-10.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# Groq — free tier, OpenAI-compatible API
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"


# ============================================================
# PORTFOLIO CONTEXT  (update when positions change)
# ============================================================
PORTFOLIO_CONTEXT = """
INVESTOR: Christopher, 24M, Toronto. $90K salary + Oct bonus. HIGH risk tolerance.
GTA home purchase planned: FHSA ~$55K + RRSP HBP $35K = ~$90K down payment.
Non-resident 2026 (abroad) → returns Canada March 2027.
ACCOUNT RULE: All new BUY picks → RRSP or Investment ONLY. NEVER TFSA/FHSA (room suspended 2026).

ACCOUNTS (~$280K total, +83% ROI):
  TFSA $100K +121% | Investment $97K +45% | FHSA $55K +123% | RRSP $28K +72%

HOLDINGS: Leveraged ETFs 49% (FNGU/SPXL/UDOW), Tech 20% (NVDA+1776% NEVER SELL, TXF.TO, AVGO, TSM, MSFT, AAPL, QCOM, MSTR), CDN Fin 10% (CM.TO, RY.TO, BMO.TO), Other (ENB.TO, TSLA, IBKR, V, ET, LYV, GBTC, BYDDF).

SENSITIVITIES: 3x leverage amplifies S&P/NASDAQ/Dow both ways. 68% USD → $1,800/1¢ USD/CAD. FX book 1.3925. VIX>22 = decay risk.

PICKS RULE: Include stocks NOT currently held. RRSP or Investment account only. Use news to find fresh ideas from any global market.
"""


def _fetch_tax_context() -> str:
    """Fetch the user's live tax situation from KV settings (RRSP limit).
    Returns a formatted string to inject into the LLM tax prompt."""
    rrsp_limit = 0
    try:
        r = requests.get("https://portfolio-pulse-dun.vercel.app/api/settings", timeout=8)
        if r.ok:
            rrsp_limit = float(r.json().get("rrsp_limit") or 0)
    except Exception:
        pass

    rrsp_contributions = 16132  # Current contributions from _CONTRIB_BASE
    rrsp_room     = max(0, rrsp_limit - rrsp_contributions) if rrsp_limit > 0 else None
    tax_saving    = round(rrsp_room * 0.43) if rrsp_room else None  # ~43% marginal rate

    return f"""=== REAL TAX SITUATION — use this for the "tax" section. Be specific, not generic. ===
TFSA: MAXED. Non-resident 2026 = ZERO contributions. DO NOT suggest adding to TFSA.
      $7,000 new room opens Jan 1, 2027. Focus: manage existing positions, plan 2027 re-entry.
FHSA: NON-RESIDENT 2026 = zero contributions. $16,000 lifetime room remaining resumes March 2027.
      DO NOT suggest contributing this year. Focus: protect down payment capital, plan withdrawal.
RRSP: {'NOA deduction limit: $' + f'{rrsp_limit:,.0f}' + ' | Contributed: $' + f'{rrsp_contributions:,.0f}' + ' | Remaining room: $' + f'{rrsp_room:,.0f}' + '.' + (f' A full $' + f'{rrsp_room:,.0f}' + f' contribution saves ~${tax_saving:,.0f} in taxes at current income. October bonus = natural deployment window.' if rrsp_room else '') if rrsp_limit > 0 else f'User has not yet entered their NOA deduction limit. Contributed ${rrsp_contributions:,.0f}. Advise them to check their NOA for exact room.'}
      RRSP cash (~$7,685 USD) idle — priority deploy into BMO S&P 500 ETF for 0% US dividend withholding.
NON-REG: Unrealized gains on SPXL, FNGU (50% inclusion + ~43% rate = ~21.5¢ tax/$ at disposition).
          Harvest candidates: MicroStrategy (~-43%), Grayscale Bitcoin (-17%), BYD (-28%).
PLANNING: FHSA ($55K) + RRSP HBP ($35K) = ~$90K down payment for GTA. Use FHSA first (no repayment).
          January 2027: $7K TFSA room opens — prioritize Broadcom, AMD, or highest-conviction growth."""


# US tickers to pull company-specific news for (Finnhub free tier, no .TO support)
COMPANY_NEWS_TICKERS = [
    "NVDA", "TSLA", "AVGO", "COST", "MSFT", "AAPL", "QCOM",
    "TSM", "IBKR", "V", "LYV", "MSTR", "SHEL", "SPXL", "FNGU", "UDOW",
]

# ============================================================
# OUTPUT SCHEMA — must match renderAISection() in index.html
# ============================================================
OUTPUT_SCHEMA = """
Return ONE valid JSON object only. No markdown fences, no explanatory text before or after.

{
  "macro": [
    {
      "title": "Theme title under 80 chars",
      "impact": "HIGH|MED|LOW",
      "confidence": <integer 0–100, your conviction this theme plays out>,
      "body": "2–4 sentences analysing this macro theme for this specific portfolio",
      "bull": "1–2 sentence bull case for portfolio",
      "bull_estimate": "Quantified CAD portfolio impact e.g. '+$35,000–$50,000 via FNGU/SPXL 3x leverage'",
      "bull_probability": <integer 0–100>,
      "base": "1–2 sentence base case for portfolio",
      "base_estimate": "Quantified CAD portfolio impact e.g. '±$5,000 — markets grind higher'",
      "base_probability": <integer 0–100>,
      "bear": "1–2 sentence bear case for portfolio",
      "bear_estimate": "Quantified CAD portfolio impact e.g. '-$45,000–$65,000 via 3x ETF amplification'",
      "bear_probability": <integer 0–100, all 3 probabilities must sum to 100>
    }
  ],
  "risks": [
    {
      "title": "Risk factor name",
      "level": "HIGH|MED|LOW",
      "context": "Brief metric e.g. '49% of portfolio ~ $135K CAD'",
      "body": "2–3 sentences explaining the risk specific to this portfolio"
    }
  ],
  "news": [
    {
      "headline": "Portfolio-relevant headline under 90 chars",
      "impact": "HIGH|MED|LOW",
      "confidence": <integer 0–100, how confident you are in this assessment>,
      "category": "Sector & Stock|Canadian Markets",
      "body": "2–3 sentences: what happened and EXACTLY which holdings are affected and why",
      "exposure": "Name the specific tickers and approximate CAD value affected e.g. 'FNGU ~$87K CAD (3x leverage), SPXL ~$22K CAD'",
      "outcomes": [
        {
          "label": "Bull",
          "probability": <integer, must sum to 100 across all 3>,
          "scenario": "1–2 sentences: what happens to THIS portfolio if bull plays out",
          "estimate": "Quantified CAD impact e.g. '+$12,000–$18,000 on leveraged positions'"
        },
        {
          "label": "Base",
          "probability": <integer>,
          "scenario": "1–2 sentences: most likely path for this portfolio",
          "estimate": "Quantified CAD impact e.g. '±$3,000 — minimal net change'"
        },
        {
          "label": "Bear",
          "probability": <integer>,
          "scenario": "1–2 sentences: downside path for this portfolio",
          "estimate": "Quantified CAD impact e.g. '-$20,000–$30,000 via 3x leverage amplification'"
        }
      ]
    }
  ],
  "picks": [
    {
      "ticker": "TICKER — can be ANY stock from ANY market. Include stocks NOT in the portfolio.
                 Use current news to find the most relevant opportunities right now.
                 At least 1 pick should be a stock NOT currently held.",
      "action": "ADD (add to existing position)|WATCH (monitor for entry)|NEW (no current holding)|SPECULATIVE",
      "account": "RRSP or Investment ONLY. NEVER TFSA or FHSA (non-resident year 2026, room suspended).",
      "thesis": "2–3 sentence investment thesis tied to current news/market conditions and this portfolio",
      "entry": "Specific entry price range, catalyst to watch, or timing note"
    }
  ],
  "strengths": [
    {"ticker": "TICKER or null", "text": "Portfolio strength description"}
  ],
  "concerns": [
    {"ticker": "TICKER or null", "text": "Portfolio concern description"}
  ],
  "strategy_short": [
    {"num": "01", "text": "0–6 month tactical awareness or action"}
  ],
  "strategy_mid": [
    {"num": "01", "text": "6–24 month strategic positioning note"}
  ],
  "strategy_long": [
    {"num": "01", "text": "2–10+ year long-horizon note"}
  ],
  "tax": {
    "tfsa":       [{"icon": "⚠️|✅|💡", "text": "Specific note based on REAL TAX SITUATION above. NO generic advice. Must address actual 2026 non-resident status. Max 2 items."}],
    "fhsa":       [{"icon": "⚠️|✅|💡", "text": "Specific FHSA note: non-resident 2026, home purchase timeline, actual account value context. Max 2 items."}],
    "rrsp":       [{"icon": "⚠️|✅|💡|💰", "text": "Specific RRSP note: use actual deduction limit and room from TAX SITUATION. If room > 0: quantify tax savings. If cash idle: name the ETF. Max 2 items."}],
    "investment": [{"icon": "⚠️|✅|💡", "text": "Specific non-reg note: reference actual harvest candidates by name, estimate deferred tax, migration plan. Max 2 items."}]
  },
  "daily_outlook": "2–3 sentences on today's overall portfolio outlook",
  "market_mood": "risk-on|risk-off|neutral|mixed"
}

Quantity limits — STRICTLY ENFORCE:
  macro: EXACTLY 3 (no more, no fewer)
  risks: EXACTLY 3 (no more, no fewer)
  news: EXACTLY 3 (no more, no fewer)
  picks: 2–3 | strengths: 4–5 | concerns: 4–5 | strategy items: 3–4 each | tax items: 2–3 each

CRITICAL RULE — news vs macro separation:
  "macro" array = broad macro/geopolitical themes (US-Iran, Fed policy, tariffs, inflation, FX).
  "news" array = ONLY individual tickers, specific industries, earnings, company catalysts.
  NEVER put geopolitical or macro-economic themes into "news". If a topic (e.g. US-Iran tensions,
  Fed rates, tariffs) belongs in macro, put it ONLY there, not duplicated in news.
  news items MUST reference specific tickers from the portfolio by name.
"""


# ============================================================
# NEWS FETCHING
# ============================================================

def fetch_general_news(api_key: str) -> list[dict]:
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={api_key}"
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        articles = resp.json()[:30]
        return [
            {"headline": a.get("headline", ""), "summary": (a.get("summary") or "")[:300]}
            for a in articles
            if a.get("headline")
        ]
    except Exception as exc:
        print(f"  ⚠ General news failed: {exc}")
        return []


def fetch_company_news(api_key: str, ticker: str) -> list[dict]:
    try:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=4)
        url = (
            f"https://finnhub.io/api/v1/company-news"
            f"?symbol={ticker}"
            f"&from={start_dt.strftime('%Y-%m-%d')}"
            f"&to={end_dt.strftime('%Y-%m-%d')}"
            f"&token={api_key}"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            articles = resp.json()[:5]
            return [{"headline": a.get("headline", "")} for a in articles if a.get("headline")]
    except Exception as exc:
        print(f"  ⚠ {ticker} news failed: {exc}")
    return []


# ============================================================
# PROMPT ASSEMBLY
# ============================================================

def _load_previous_intelligence(path: str = "data/intelligence.json") -> str:
    """Load the previous intelligence summary to avoid repeating it."""
    try:
        import pathlib
        p = pathlib.Path(path)
        if not p.exists():
            return ""
        old = json.loads(p.read_text())
        # Extract key themes so the LLM knows what was already covered
        prev_date  = old.get("generated_date", "")
        prev_mood  = old.get("market_mood", "")
        prev_macro = " | ".join(m.get("title","") for m in (old.get("macro") or [])[:3])
        prev_news  = " | ".join(n.get("headline","") for n in (old.get("news") or [])[:4])
        if not prev_macro and not prev_news:
            return ""
        return (
            f"PREVIOUS BRIEFING ({prev_date}, mood: {prev_mood}):\n"
            f"  Macro themes already covered: {prev_macro or 'none'}\n"
            f"  News already covered: {prev_news or 'none'}\n"
            f"  → TODAY'S BRIEFING MUST DIFFER. Focus on what has CHANGED or is NEW since then."
        )
    except Exception:
        return ""


def build_prompt(general_news: list[dict], company_news: dict[str, list[dict]]) -> str:
    general_block = (
        "\n".join(f"• {a['headline']}" for a in general_news[:22])
        or "(no general news fetched)"
    )

    company_block = ""
    for ticker, articles in company_news.items():
        if articles:
            company_block += f"\n{ticker}:\n"
            for a in articles[:3]:
                company_block += f"  • {a['headline']}\n"
    if not company_block:
        company_block = "(no company-specific news fetched)"

    today    = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    prev_ctx = _load_previous_intelligence()

    prev_section = f"\n{prev_ctx}\n" if prev_ctx else ""
    tax_context  = _fetch_tax_context()

    return f"""You are a portfolio intelligence analyst generating a daily briefing for a personal Canadian investment portfolio.

TODAY'S DATE: {today}
{prev_section}
PORTFOLIO CONTEXT:
{PORTFOLIO_CONTEXT}

{tax_context}

TODAY'S GENERAL MARKET NEWS (latest ~22 headlines):
{general_block}

HOLDINGS-SPECIFIC NEWS (last 4 days):
{company_block}

INSTRUCTIONS:
1. Analyse news in context of this specific portfolio — reference actual tickers and amounts
2. Do NOT give financial advice or predict specific prices
3. Do NOT recommend selling core positions (especially NVDA, FNGU, SPXL)
4. If you don't have data about something, say so — don't fabricate
5. Be concrete and useful: explain WHY news matters to THIS portfolio
6. Canadian context: reference TFSA/FHSA/RRSP rules, CAD amounts, and CRA where relevant
7. For PICKS: suggest additions that complement existing holdings or use idle RRSP cash

SECTION DISTINCTIVENESS — each section must cover UNIQUE ground, zero repetition across sections:
- macro:     Broad economic/geopolitical forces ONLY (Fed, inflation, GDP, trade, sector rotations).
             Do NOT mention specific portfolio tickers. Do NOT repeat events covered in 'news'.
- news:      Specific headlines from TODAY'S feed ONLY. Each item = one distinct event.
             Do NOT restate macro themes — only the event and its direct holding impact.
- risks:     Portfolio-specific TECHNICAL risks ONLY (3x leverage amplification, FX book rate drag,
             account concentration). Do NOT rehash macro themes already in 'macro'.
- picks:     Genuinely NEW tickers not already held, OR a specific sizing action on an existing position.
             Do NOT suggest anything already covered as a strength.
- strengths: Structural/positional advantages ONLY (low cost basis, tax shelter, realised gains locked in).
             Do NOT list macroeconomic tailwinds — those belong in 'macro'.
- concerns:  Structural/positional WEAKNESSES only (uninvested RRSP cash drag, ETF overlap, illiquidity).
             Do NOT repeat items already in 'risks'.
- strategy_short: Actions for 0–6 months ONLY. No overlap with mid or long.
- strategy_mid:   Actions for 6–24 months ONLY. Must be different themes from short.
- strategy_long:  Actions for 2–10+ years ONLY. Horizon-appropriate, not a repeat of short/mid.
- tax:        CRA/account-mechanics notes ONLY. Not strategy already in strategy sections.

{OUTPUT_SCHEMA}"""


# ============================================================
# GEMINI CALL
# ============================================================

def call_llm(api_key: str, prompt: str) -> dict:
    """
    Call Groq's free-tier Llama API and return parsed JSON.
    Retries up to 4 times on 429 rate-limit errors with exponential backoff.
    Falls back to the 8B model if the 70B model keeps hitting limits.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    models  = [GROQ_MODEL, "llama-3.1-8b-instant"]   # 70B → 8B fallback

    for model in models:
        for attempt in range(4):
            payload = {
                "model":       model,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.35,
                "max_tokens":  8192,
            }
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)

            if resp.status_code == 429:
                # Read retry-after header; default to exponential backoff
                try:
                    wait = min(int(resp.headers.get("retry-after", 2 ** (attempt + 1))), 60)
                except (ValueError, TypeError):
                    wait = 2 ** (attempt + 1)
                print(f"  ⏳ Rate limited on {model} attempt {attempt+1}/4 — waiting {wait}s")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()

            # Strip markdown code fences if the model adds them
            if text.startswith("```"):
                lines = text.split("\n")
                start = 1
                end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
                text = "\n".join(lines[start:end])

            print(f"  ✓ Generated with {model} (attempt {attempt+1})")
            return json.loads(text)

        print(f"  ⚠ All retries exhausted for {model}, trying next model...")

    raise RuntimeError("All Groq models exhausted after retries — check rate limits")


# ============================================================
# SAVE
# ============================================================

def save(data: dict, path: str = "data/intelligence.json") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print(f"  ✓ Saved → {path}")


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    groq_key    = os.environ.get("GROQ_API_KEY", "")
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")

    if not groq_key:
        print("ERROR: GROQ_API_KEY environment variable not set")
        return 1
    if not finnhub_key:
        print("ERROR: FINNHUB_API_KEY environment variable not set")
        return 1

    now_utc = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"  Portfolio Pulse — Daily Intelligence")
    print(f"  {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    # 1. Fetch general market news
    print("1/3  Fetching general market news from Finnhub...")
    general_news = fetch_general_news(finnhub_key)
    print(f"     → {len(general_news)} headlines")

    # 2. Fetch company-specific news (rate-limited)
    print("2/3  Fetching holdings news...")
    company_news: dict[str, list[dict]] = {}
    for ticker in COMPANY_NEWS_TICKERS:
        company_news[ticker] = fetch_company_news(finnhub_key, ticker)
        time.sleep(0.2)  # stay within 60 calls/min free-tier limit
    total_articles = sum(len(v) for v in company_news.values())
    print(f"     → {total_articles} articles across {len(COMPANY_NEWS_TICKERS)} tickers")

    # 3. Generate with Groq (Llama 3.3 70B)
    print(f"3/3  Generating intelligence with Groq ({GROQ_MODEL})...")
    prompt = build_prompt(general_news, company_news)
    intelligence = call_llm(groq_key, prompt)

    # Add metadata
    intelligence["generated_at"] = now_utc.isoformat()
    intelligence["generated_date"] = now_utc.strftime("%B %d, %Y")
    intelligence["next_update"] = "6:00 AM EST next trading day"

    # Validate required keys
    required = [
        "macro", "risks", "news", "picks",
        "strengths", "concerns",
        "strategy_short", "strategy_mid", "strategy_long",
        "tax", "daily_outlook", "market_mood",
    ]
    missing = [k for k in required if k not in intelligence]
    if missing:
        print(f"  ⚠ Missing keys in Gemini response: {missing}")

    # Fetch upcoming earnings for held US tickers and add to intelligence
    fmp_key = os.environ.get("FMP_API_KEY", "")
    if fmp_key:
        try:
            from datetime import timedelta
            end_dt = now_utc + timedelta(days=14)
            er = requests.get(
                "https://financialmodelingprep.com/api/v3/earning_calendar",
                params={"from": now_utc.strftime("%Y-%m-%d"), "to": end_dt.strftime("%Y-%m-%d"), "apikey": fmp_key},
                timeout=10,
            )
            if er.ok:
                held_us = {"NVDA","AVGO","TSLA","TSM","MSFT","AAPL","QCOM","IBKR","V","LYV","GBTC","MSTR","SHEL","ET"}
                earnings_events = [
                    {"symbol": e["symbol"], "date": e["date"], "eps_estimate": e.get("epsEstimated")}
                    for e in (er.json() if isinstance(er.json(), list) else [])
                    if e.get("symbol") in held_us
                ]
                intelligence["upcoming_earnings"] = sorted(earnings_events, key=lambda x: x["date"])[:8]
                print(f"  ✓ {len(intelligence['upcoming_earnings'])} earnings events fetched")
        except Exception as exc:
            print(f"  ⚠ Earnings fetch failed (non-fatal): {exc}")
            intelligence["upcoming_earnings"] = []
    else:
        intelligence["upcoming_earnings"] = []

    save(intelligence)

    # Also push to Vercel KV via /api/intelligence so the dashboard
    # can detect fresh data immediately (bypasses CDN cache on static files)
    vercel_url = "https://portfolio-pulse-dun.vercel.app/api/intelligence"
    cron_secret = os.environ.get("CRON_SECRET", "")
    try:
        resp = requests.post(
            vercel_url,
            headers={"Authorization": f"Bearer {cron_secret}", "Content-Type": "application/json"},
            json=intelligence,
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"  ✓ Intelligence pushed to KV (generated_at: {intelligence['generated_at']})")
        else:
            print(f"  ⚠ KV push returned {resp.status_code}: {resp.text[:100]}")
    except Exception as exc:
        print(f"  ⚠ KV push failed (non-fatal): {exc}")

    print(f"\n  Summary:")
    print(f"    {len(intelligence.get('macro', []))} macro themes")
    print(f"    {len(intelligence.get('news',  []))} news items")
    print(f"    {len(intelligence.get('picks', []))} stocks to watch")
    print(f"    Market mood: {intelligence.get('market_mood', '—')}")
    print(f"\n  Done ✓\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
