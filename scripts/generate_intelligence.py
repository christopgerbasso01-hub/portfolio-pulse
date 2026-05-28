#!/usr/bin/env python3
"""
Portfolio Pulse — Daily Intelligence Generator
Runs via GitHub Actions at 6 AM EST weekdays.
Pipeline: Finnhub news → Gemini 2.0 Flash → intelligence.json
Output is consumed by index.html to populate sections 05-10.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False


# ============================================================
# PORTFOLIO CONTEXT  (update when positions change)
# ============================================================
PORTFOLIO_CONTEXT = """
ACCOUNTS & APPROXIMATE VALUES (as of May 2026):
  TFSA:        CAD ~$98K  | Contributions $44,500 | ROI ~+121%  | 100% tax-free
  Investment:  CAD ~$94K  | Contributions $65,000 | ROI ~+45%   | 50% cap-gains inclusion
  FHSA:        CAD ~$54K  | Contributions $24,000 | ROI ~+123%  | Double tax win
  RRSP:        CAD ~$28K  | Contributions $16,132 | ROI ~+72%   | Deferred
  Total:       CAD ~$274K | Total P&L ~+$124K     | Overall ROI ~+83%

CURRENT HOLDINGS (approximate weights):
  Leveraged ETFs ~49%:
    FNGU  — FANG+ 3x (TFSA 495sh + Investment 665sh + FHSA 157sh + RRSP 56sh)
    SPXL  — S&P500 3x (TFSA 23sh + Investment 75sh + FHSA 53sh)
    UDOW  — Dow 3x (TFSA 84sh + FHSA 86sh + RRSP 36sh)

  Technology ~20%:
    NVDA   — TFSA 40sh @ $16/sh split-adj cost (+1,776% ROI — core forever holding)
    TXF.TO — CI Tech Giants Covered Call ETF (TFSA 320sh+Inv 221sh+FHSA 434sh+RRSP 284sh)
    AVGO   — TFSA 8sh (acquired Apr 2026)
    MSFT   — TFSA 2sh | AAPL — TFSA 4sh | QCOM — TFSA 5sh
    TSM    — Investment 9sh + RRSP 6sh | MSTR — Investment 4sh

  Canadian Financials ~10%:
    CM.TO — CIBC (TFSA 45sh + Investment 50sh)
    RY.TO — Royal Bank (TFSA 22sh + Investment 19sh)
    BMO.TO — TFSA 15sh

  Other ~21%:
    ENB.TO — Enbridge FHSA 82sh | TSLA — TFSA 14sh | IBKR — Investment 40sh
    V — Investment 4sh (Visa) | ET — TFSA 60sh (Energy Transfer)
    LYV — Investment 11sh (Live Nation) | GBTC — Investment 25sh (Bitcoin proxy)
    BYDDF — Investment 3sh (BYD) | RRSP Cash — ~$10,531 uninvested

KEY SENSITIVITIES:
  - ~49% leveraged ETFs → amplifies S&P 500 / NASDAQ / Dow by 3x
  - ~68% USD-denominated → every 1¢ USD/CAD move ≈ $1,800 portfolio impact
  - FX book rate 1.3925 (drag when CAD < 1.3925, tailwind when CAD > 1.3925)
  - RRSP cash $10,531 uninvested = opportunity cost (ZSP.TO preferred for US treaty)
  - NVDA is best single trade; never sell (TFSA, tax-free, +1,776%)
"""

# US tickers to pull company-specific news for (Finnhub free tier, no .TO support)
COMPANY_NEWS_TICKERS = [
    "NVDA", "TSLA", "AVGO", "COST", "MSFT", "AAPL", "QCOM",
    "TSM", "IBKR", "V", "LYV", "MSTR", "SPXL", "FNGU", "UDOW",
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
      "confidence": <integer 0–100>,
      "body": "2–4 sentences analysing this macro theme for this specific portfolio",
      "bull": "1–2 sentence bull case outcome for portfolio",
      "base": "1–2 sentence base case outcome for portfolio",
      "bear": "1–2 sentence bear case outcome for portfolio"
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
      "headline": "Portfolio-relevant headline",
      "impact": "HIGH|MED|LOW",
      "category": "Macro & Rates|Sector & Stock|Canadian Markets",
      "body": "2–3 sentences: what happened and how it affects this portfolio",
      "exposure": "Specific holding exposure in this portfolio",
      "scenarios": "🟢 Bull: ... 🔴 Bear: ..."
    }
  ],
  "picks": [
    {
      "ticker": "TICKER",
      "action": "ADD|WATCH|NEW|SPECULATIVE",
      "account": "TFSA|FHSA|RRSP|Investment|Any",
      "thesis": "2–3 sentence investment thesis specific to this portfolio",
      "entry": "Entry / timing note"
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
    "tfsa":       [{"icon": "✦", "text": "TFSA tax-free optimisation note"}],
    "fhsa":       [{"icon": "✦", "text": "FHSA double tax advantage note"}],
    "rrsp":       [{"icon": "⚡", "text": "RRSP deferred growth note"}],
    "investment": [{"icon": "⚠",  "text": "Non-reg tax consideration"}]
  },
  "daily_outlook": "2–3 sentences on today's overall portfolio outlook",
  "market_mood": "risk-on|risk-off|neutral|mixed"
}

Quantity limits:
  macro: 3–4 | risks: 3 | news: 4–6 | picks: 3–4
  strengths: 4–5 | concerns: 4–5 | strategy items: 3–4 each | tax items: 2–3 each
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

    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    return f"""You are a portfolio intelligence analyst generating a daily briefing for a personal Canadian investment portfolio.

TODAY'S DATE: {today}

PORTFOLIO CONTEXT:
{PORTFOLIO_CONTEXT}

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

{OUTPUT_SCHEMA}"""


# ============================================================
# GEMINI CALL
# ============================================================

def call_gemini(api_key: str, prompt: str) -> dict:
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            temperature=0.35,
            max_output_tokens=8192,
        ),
    )
    text = response.text.strip()

    # Strip markdown code fences if Gemini adds them
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines)
        if lines[0].startswith("```"):
            start = 1
        if lines[-1].strip() == "```":
            end = len(lines) - 1
        text = "\n".join(lines[start:end])

    return json.loads(text)


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
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    finnhub_key = os.environ.get("FINNHUB_API_KEY", "")

    if not gemini_key:
        print("ERROR: GEMINI_API_KEY environment variable not set")
        return 1
    if not finnhub_key:
        print("ERROR: FINNHUB_API_KEY environment variable not set")
        return 1
    if not HAS_GENAI:
        print("ERROR: google-genai not installed — run: pip install google-genai")
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

    # 3. Generate with Gemini
    print("3/3  Generating intelligence with Gemini 1.5 Flash...")
    prompt = build_prompt(general_news, company_news)
    intelligence = call_gemini(gemini_key, prompt)

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

    save(intelligence)

    print(f"\n  Summary:")
    print(f"    {len(intelligence.get('macro', []))} macro themes")
    print(f"    {len(intelligence.get('news',  []))} news items")
    print(f"    {len(intelligence.get('picks', []))} stocks to watch")
    print(f"    Market mood: {intelligence.get('market_mood', '—')}")
    print(f"\n  Done ✓\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
