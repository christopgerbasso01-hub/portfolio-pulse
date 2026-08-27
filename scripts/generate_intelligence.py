#!/usr/bin/env python3
"""
Portfolio Pulse — Daily Intelligence Generator
Runs via GitHub Actions at 6 AM EST weekdays.
Pipeline: Finnhub news → Groq (openai/gpt-oss-120b, 2 passes) → intelligence.json
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
# Groq shut down llama-3.3-70b-versatile and llama-3.1-8b-instant on 2026-08-16.
# openai/gpt-oss-120b is Groq's named replacement.
#
# WHY main() MAKES TWO CALLS INSTEAD OF ONE
# The free tier allows 8K tokens/minute and max_tokens counts toward it, so the
# ceiling per request is prompt + max_tokens <= 8000. The prompt is ~3.4K and a
# whole-schema response needs ~5.5-6.3K, which Groq rejects outright
# ("Limit 8000, Requested 11683"). Lowering max_tokens enough to be accepted
# instead truncates the JSON — measured, not guessed: at max_tokens=3000 the
# model generated and then died on "Unterminated string".
# Neither alternative model helps: qwen/qwen3.6-27b has the same 8K ceiling,
# and groq/compound, despite 70K TPM, rejects this request shape with a bare
# request_too_large at every max_tokens tried (8192, 8000, 3000).
# So the schema is requested in two halves, ~7.4K per request. Nothing is
# dropped from the output; see the note in main().
GROQ_MODEL = "openai/gpt-oss-120b"


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


SETTINGS_API = "https://portfolio-pulse-dun.vercel.app/api/settings"
PICKS_HISTORY_WEEKS = 3   # avoid picks suggested in last 3 weeks


def _load_picks_history() -> list[dict]:
    """Fetch previous pick tickers from KV settings (last N weeks)."""
    try:
        r = requests.get(SETTINGS_API, timeout=8)
        if r.ok:
            return r.json().get("picks_history", [])
    except Exception:
        pass
    return []


def _save_picks_history(new_tickers: list[str], existing: list[dict]) -> None:
    """Prepend today's picks and keep only the last N week entries."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Remove any existing entry for today
    trimmed = [e for e in existing if e.get("date") != today]
    updated = [{"date": today, "tickers": new_tickers}] + trimmed
    updated = updated[:PICKS_HISTORY_WEEKS * 2]   # keep extra buffer
    try:
        requests.post(
            SETTINGS_API,
            headers={"Content-Type": "application/json"},
            json={"picks_history": updated},
            timeout=10,
        )
    except Exception as exc:
        print(f"  ⚠ Picks history save failed (non-fatal): {exc}")


def fetch_fmp_discovery_news(fmp_key: str) -> list[dict]:
    """Fetch general stock news from FMP — broader than Finnhub company-specific news.
    Returns list of {symbol, headline} covering stocks we don't currently hold."""
    try:
        r = requests.get(
            "https://financialmodelingprep.com/api/v3/stock_news",
            params={"limit": 40, "apikey": fmp_key},
            timeout=10,
        )
        if not r.ok:
            return []
        return [
            {"symbol": a.get("symbol", "?"), "headline": a.get("title", "")[:120]}
            for a in (r.json() if isinstance(r.json(), list) else [])[:40]
            if a.get("title") and a.get("symbol")
        ]
    except Exception:
        return []


def fetch_fmp_market_movers(fmp_key: str) -> dict:
    """Fetch today's top gainers and losers — momentum context for picks."""
    result = {"gainers": [], "losers": []}
    for endpoint, key in [("gainers", "gainers"), ("losers", "losers")]:
        try:
            r = requests.get(
                f"https://financialmodelingprep.com/api/v3/{endpoint}",
                params={"apikey": fmp_key},
                timeout=8,
            )
            if r.ok and isinstance(r.json(), list):
                result[key] = [
                    {"symbol": s.get("ticker",""), "pct": s.get("changesPercentage",""),
                     "name": s.get("companyName","")}
                    for s in r.json()[:8] if s.get("ticker")
                ]
        except Exception:
            pass
    return result


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

    rrsp_str = (f"${rrsp_room:,.0f} room left, saves ~${tax_saving:,.0f} in tax. Oct bonus = deploy window."
                if rrsp_limit > 0 and rrsp_room else
                "Enter NOA limit to see exact room." if rrsp_limit == 0 else "Room fully used.")
    return (f"TAX: TFSA/FHSA maxed+non-resident 2026 (no new contributions). "
            f"RRSP: {rrsp_str} Cash ~$7,685 USD idle—deploy to BMO S&P500 ETF. "
            f"Non-Reg: harvest MSTR/GBTC/BYD losses before Dec 31. "
            f"FHSA($55K)+HBP($35K)=~$90K down payment. Jan 2027: $7K TFSA room opens.")


# US tickers to pull company-specific news for (Finnhub free tier, no .TO support)
COMPANY_NEWS_TICKERS = [
    "NVDA", "TSLA", "AVGO", "COST", "MSFT", "AAPL", "QCOM",
    "TSM", "IBKR", "V", "LYV", "MSTR", "SHEL", "SPXL", "FNGU", "UDOW",
]

# ============================================================
# OUTPUT SCHEMA — must match renderAISection() in index.html
# ============================================================
THEME_CATEGORIES = [
    "geopolitical", "monetary-policy", "inflation", "fx",
    "commodities", "credit", "equity-sectors", "corporate",
    "macro-economic", "regulatory",
]

# Keywords used to filter general news headlines when a category is in the ban window.
# Intentionally specific to avoid over-filtering unrelated articles.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "geopolitical":    ["iran", "israel", "hamas", "hezbollah", "gaza", "ukraine", "russia",
                        "nato", "ceasefire", "military strike", "missile", "sanction",
                        "middle east conflict", "escalat", "geopolit"],
    "monetary-policy": ["federal reserve", "fomc", "jerome powell", "rate hike", "rate cut",
                        "interest rate decision", "basis point", "central bank rate",
                        "monetary policy", "fed funds"],
    "inflation":       ["consumer price index", "cpi data", "pce inflation", "producer price",
                        "inflation rate", "inflation report", "deflation", "stagflation"],
    "fx":              ["dollar index", "dxy", "usd/cad", "cad/usd", "currency war",
                        "forex market", "exchange rate volatility"],
    "commodities":     ["crude oil price", "wti crude", "brent crude", "opec", "natural gas price",
                        "gold price", "silver price", "commodity rout", "oil rally"],
    "credit":          ["treasury yield", "bond yield", "credit spread", "junk bond",
                        "high-yield bond", "debt ceiling", "sovereign debt"],
    "equity-sectors":  ["sector rotation", "rotation out of", "tech sector selloff",
                        "financials outperform", "sector etf"],
    "macro-economic":  ["gdp growth", "gdp report", "recession risk", "nonfarm payroll",
                        "jobs report", "unemployment rate", "economic contraction"],
    "regulatory":      ["antitrust ruling", "sec enforcement", "regulation passed",
                        "legislation signed", "compliance deadline"],
    "corporate":       [],
}


def _get_banned_categories(theme_history: list[dict], days: int = 3) -> set[str]:
    """Return category names covered in the last N days."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    banned: set[str] = set()
    for entry in theme_history:
        if entry.get("date", "") >= cutoff:
            for t in entry.get("themes", []):
                if t.get("category"):
                    banned.add(t["category"])
    return banned


def _filter_banned_headlines(news: list[dict], banned: set[str]) -> tuple[list[dict], int]:
    """Remove general news headlines that match banned category keywords.
    Returns (kept_headlines, count_removed).
    """
    if not banned:
        return news, 0
    keywords = []
    for cat in banned:
        keywords.extend(CATEGORY_KEYWORDS.get(cat, []))
    if not keywords:
        return news, 0
    kept, removed = [], 0
    for item in news:
        text = (item.get("headline", "") + " " + item.get("summary", "")).lower()
        if any(kw in text for kw in keywords):
            removed += 1
        else:
            kept.append(item)
    return kept, removed

OUTPUT_SCHEMA = """
Return ONE valid JSON object only. No markdown fences, no explanatory text before or after.

{
  "macro": [
    {
      "title": "Theme title under 80 chars",
      "category": "geopolitical|monetary-policy|inflation|fx|commodities|credit|equity-sectors|corporate|macro-economic|regulatory",
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
      "title": "Specific risk name — vary this daily. Draw from: leverage amplification, FX exposure, earnings concentration, cash drag, liquidity risk, single-stock risk, tax timing risk, sector overlap, account rule constraints, correlation risk, volatility decay, or any other portfolio-specific risk relevant TODAY.",
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
  "daily_outlook": "2–3 sentences on today's overall portfolio outlook",
  "market_mood": "risk-on|risk-off|neutral|mixed"
}

Quantity limits — STRICTLY ENFORCE:
  macro: EXACTLY 3 (no more, no fewer)
  risks: EXACTLY 3 (no more, no fewer)
  news: EXACTLY 3 (no more, no fewer)
  picks: 2–3 | strengths: 4–5 | concerns: 4–5 | strategy items: 3–4 each

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

def _load_theme_history(path: str = "data/intelligence.json") -> list[dict]:
    """Load the rolling 7-day macro theme history stored in intelligence.json."""
    try:
        import pathlib
        p = pathlib.Path(path)
        if not p.exists():
            return []
        return json.loads(p.read_text()).get("theme_history", [])
    except Exception:
        return []


def _load_risk_history(path: str = "data/intelligence.json") -> list[str]:
    """Load the rolling 5-day risk title history stored in intelligence.json."""
    try:
        import pathlib
        p = pathlib.Path(path)
        if not p.exists():
            return []
        return json.loads(p.read_text()).get("risk_history", [])
    except Exception:
        return []


def _load_macro_history(path: str = "data/intelligence.json") -> list[dict]:
    """Load the rolling 15-article full macro history stored in intelligence.json."""
    try:
        import pathlib
        p = pathlib.Path(path)
        if not p.exists():
            return []
        return json.loads(p.read_text()).get("macro_history", [])
    except Exception:
        return []


def _build_theme_context(theme_history: list[dict], risk_history: list[str],
                         macro_history: list[dict] = None) -> str:
    """Build the deduplication block for macro themes, full article history, and risk factors."""
    lines = []

    if macro_history:
        lines.append("MACRO ARTICLES ALREADY PUBLISHED (last 15) — read every article before writing.")
        lines.append("Do NOT repeat the topic, angle, conclusion, or specific data point from any article below.")
        lines.append("A different title is NOT enough — the gist must cover genuinely new ground.")
        lines.append("")
        # Inject last 9 articles (3 runs) into prompt — title + 100-char body snippet only
        # Full articles are stored in macro_history for the file; we trim here to stay under prompt size limits
        for m in macro_history[:9]:
            date  = m.get("date", "?")
            cat   = m.get("category", "?")
            title = m.get("title", "")
            body  = m.get("body", "")
            snippet = body[:100].rstrip() + ("…" if len(body) > 100 else "")
            lines.append(f"[{date} | {cat}] {title} — {snippet}")
        lines.append("")
    elif theme_history:
        # Fallback to title-only history if full articles not yet populated
        lines.append("MACRO THEME HISTORY — last 5 days (most recent first):")
        for entry in theme_history[:5]:
            date   = entry.get("date", "?")
            themes = entry.get("themes", [])
            for t in themes:
                lines.append(f"  [{date}] {t.get('category','?')}: {t.get('title','')}")

    if macro_history or theme_history:
        lines += [
            "MACRO DEDUPLICATION RULES (apply to all 3 macro slots):",
            "  • Do NOT use a category or topic covered in the articles above UNLESS there is a",
            "    MATERIAL NEW EVENT — a ceasefire, rate decision, fresh data release, military",
            "    strike, or new legislation. Ongoing commentary does NOT qualify.",
            "  • If you revisit a recent category, your body MUST address the specific new event",
            "    and explain how it differs from what was already published above.",
            f"  • Valid categories: {', '.join(THEME_CATEGORIES)}",
            "",
        ]

    if risk_history:
        lines += [
            f"RISK TITLES USED RECENTLY (last 5 days) — do NOT reuse these exact titles:",
            *[f"  • {r}" for r in risk_history],
            "  → Pick 3 DIFFERENT risk angles relevant to today's conditions.",
        ]

    return "\n".join(lines) if lines else ""


def build_prompt(general_news: list[dict], company_news: dict[str, list[dict]],
                 picks_history: list[dict] = None,
                 discovery_news: list[dict] = None,
                 movers: dict = None,
                 only_sections: list[str] = None,
                 already_covered: str = "") -> str:
    general_block = (
        "\n".join(f"• {a['headline']}" for a in general_news[:15])
        or "(no general news fetched)"
    )

    company_block = ""
    for ticker, articles in company_news.items():
        if articles:
            company_block += f"\n{ticker}:\n"
            for a in articles[:2]:
                company_block += f"  • {a['headline']}\n"
    if not company_block:
        company_block = "(no company-specific news fetched)"

    # Previous picks to avoid (last 3 weeks)
    avoid_tickers = []
    for entry in (picks_history or [])[:PICKS_HISTORY_WEEKS]:
        avoid_tickers.extend(entry.get("tickers", []))
    avoid_block = (
        f"RECENTLY SUGGESTED (DO NOT repeat these for at least 3 weeks): {', '.join(set(avoid_tickers))}"
        if avoid_tickers else ""
    )

    # FMP broad market discovery news (non-held stocks)
    held_set = set(COMPANY_NEWS_TICKERS)
    fresh_news = [n for n in (discovery_news or []) if n.get("symbol") not in held_set]
    discovery_block = ""
    if fresh_news:
        discovery_block = "\nMARKET DISCOVERY NEWS (use these to find picks BEYOND held positions):\n"
        discovery_block += "\n".join(f"  • {n['symbol']}: {n['headline']}" for n in fresh_news[:10])

    # Market movers
    movers_block = ""
    if movers:
        if movers.get("gainers"):
            movers_block += "\nTODAY'S TOP GAINERS (potential momentum plays):\n"
            movers_block += "\n".join(f"  • {m['symbol']} ({m['name']}) {m['pct']}" for m in movers["gainers"][:4])
        if movers.get("losers"):
            movers_block += "\nTODAY'S TOP LOSERS (potential value/harvest candidates):\n"
            movers_block += "\n".join(f"  • {m['symbol']} ({m['name']}) {m['pct']}" for m in movers["losers"][:3])

    today        = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    theme_history = _load_theme_history()
    risk_history  = _load_risk_history()
    macro_history = _load_macro_history()
    theme_ctx     = _build_theme_context(theme_history, risk_history, macro_history)
    prev_section  = f"\n{theme_ctx}\n" if theme_ctx else ""

    # Filter general news headlines that match banned categories
    banned_cats = _get_banned_categories(theme_history, days=3)
    if banned_cats:
        general_news, n_removed = _filter_banned_headlines(general_news, banned_cats)
        if n_removed:
            print(f"  ✓ Filtered {n_removed} headlines matching banned categories: {banned_cats}")

    # Restrict schema category enum to non-banned categories so model can't pick them
    # Restrict the response to a subset of top-level keys. The free tier allows
    # 8K tokens/minute including max_tokens, and a whole-schema response needs
    # more output than fits alongside the prompt, so main() asks for the schema
    # in two halves. The schema and the distinctiveness rules above stay intact
    # for both calls — only the set of keys returned changes.
    _restrict = ""
    if only_sections:
        _restrict = (
            "\nRESPONSE SCOPE — CRITICAL:\n"
            f"Return ONLY these top-level keys: {', '.join(only_sections)}.\n"
            "Omit every other key entirely. Do not include them as empty values.\n"
            "The rules above still apply to the keys you do return.\n"
        )

    _schema = OUTPUT_SCHEMA
    if banned_cats:
        allowed_cats = [c for c in THEME_CATEGORIES if c not in banned_cats]
        _schema = _schema.replace(
            "geopolitical|monetary-policy|inflation|fx|commodities|credit|equity-sectors|corporate|macro-economic|regulatory",
            "|".join(allowed_cats)
        )
        print(f"  ✓ Schema restricted to categories: {allowed_cats}")

    return f"""You are a portfolio intelligence analyst generating a daily briefing for a personal Canadian investment portfolio.

TODAY'S DATE: {today}
{prev_section}
PORTFOLIO CONTEXT:
{PORTFOLIO_CONTEXT}

{avoid_block}

TODAY'S GENERAL MARKET NEWS (latest ~22 headlines):
{general_block}

HOLDINGS-SPECIFIC NEWS (last 4 days):
{company_block}
{discovery_block}
{movers_block}

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
- risks:     3 DISTINCT portfolio-specific risks relevant to TODAY's market conditions.
             Rotate focus daily — do not reuse the same risk titles as recent briefings.
             Pool to draw from: leverage decay, FX drag, earnings concentration, cash drag,
             liquidity gaps, single-stock tail risk, tax timing, sector overlap, correlation
             risk, volatility regime shift, account rule constraints, or any fresh risk.
             Do NOT rehash macro themes already in 'macro'.
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

{_restrict}{already_covered}
{_schema}"""


# ============================================================
# GEMINI CALL
# ============================================================

TPM_LIMIT       = 8000   # free-tier tokens/minute; max_tokens counts toward it
TPM_SAFETY      = 350    # cushion for tokenizer estimate error and overhead
MAX_TOKENS_WANT = 4000   # enough for either half of the schema
MAX_TOKENS_MIN  = 2600   # below this the JSON truncates; better to fail loudly


def _fit_max_tokens(prompt: str) -> int:
    """
    Largest output budget that still fits under the per-minute ceiling.

    Groq counts max_tokens toward the 8K/minute limit, so prompt + max_tokens
    must stay under it. Hard-coding 4000 worked when this was written and then
    broke in ordinary use: the second pass carries the titles the first pass
    produced, and on a busy news day that pushed the request to 8,075 — 75
    tokens over. It failed on most days and squeaked through on quiet ones.

    Sizing from the actual prompt removes the guesswork, so daily variation in
    news volume changes the output budget instead of breaking the run.
    """
    est_prompt = len(prompt) // 4 + 200          # ~4 chars/token + envelope
    budget     = TPM_LIMIT - est_prompt - TPM_SAFETY
    return max(MAX_TOKENS_MIN, min(MAX_TOKENS_WANT, budget))


def call_llm(api_key: str, prompt: str) -> dict:
    """
    Call Groq's free-tier Llama API and return parsed JSON.

    Retries on 429 (rate limit) and 5xx (transient server error) with
    exponential backoff, and falls through to the next model on 404 or 413.

    The fallback list previously only covered 429 and 413: every other status
    hit raise_for_status() and killed the run outright. A 404 from Groq —
    which is what it returns when a model is unavailable, and exactly the case
    the fallback exists for — therefore aborted the job with a working
    fallback model sitting unused. That took out the 2026-08-17 11:31 run and
    both runs on 2026-08-10.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    models  = [GROQ_MODEL, "openai/gpt-oss-20b"]     # 120B → 20B fallback

    for model in models:
        for attempt in range(4):
            payload = {
                "model":       model,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0.35,
                # gpt-oss are reasoning models: their chain of thought is
                # generated before the answer and spends the same token budget.
                # Left at the default, pass 1 truncated after ~6,100 characters
                # of JSON despite max_tokens=4000 — the reasoning had consumed
                # most of it. This job wants structured JSON, not deliberation.
                "reasoning_effort":  "low",
                "include_reasoning": False,
                # Largest value that fits under the 8K TPM ceiling alongside
                # a ~3,700-token prompt. Not enough for a full-size response —
                # see the KNOWN LIMITATION note at GROQ_MODEL.
                "max_tokens":  _fit_max_tokens(prompt),
            }
            try:
                resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)
            except requests.RequestException as exc:
                # Connection reset / read timeout — worth another attempt.
                wait = 2 ** (attempt + 1)
                print(f"  ⏳ Network error on {model} attempt {attempt+1}/4 "
                      f"({type(exc).__name__}) — waiting {wait}s")
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                # Read retry-after header; default to exponential backoff
                try:
                    wait = min(int(resp.headers.get("retry-after", 2 ** (attempt + 1))), 60)
                except (ValueError, TypeError):
                    wait = 2 ** (attempt + 1)
                print(f"  ⏳ Rate limited on {model} attempt {attempt+1}/4 — waiting {wait}s")
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                # Model unavailable or retired. Retrying the same name cannot
                # help, so go straight to the next model.
                print(f"  ⚠ 404 Not Found for model {model} — unavailable or retired. "
                      f"Skipping to next model.")
                break

            if resp.status_code == 413:
                # Groq's body names the exact limit and what was requested,
                # which is the difference between "shrink the prompt" and
                # "this model's TPM cannot fit this job at all".
                print(f"  ⚠ 413 Payload Too Large on {model} — prompt is {len(prompt):,} chars, "
                      f"max_tokens={payload['max_tokens']}. Groq said: {resp.text[:300]}")
                break   # try smaller model

            if resp.status_code >= 500:
                # Transient upstream fault — back off and retry the same model.
                wait = 2 ** (attempt + 1)
                print(f"  ⏳ Groq {resp.status_code} on {model} attempt {attempt+1}/4 "
                      f"— waiting {wait}s")
                time.sleep(wait)
                continue

            # 400/401/403 and friends: a bad key or malformed request. Retrying
            # or downgrading the model will not fix it, so fail loudly.
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

    raise RuntimeError(
        f"All Groq models exhausted after retries: {', '.join(models)}. "
        f"Check rate limits, and confirm these model names are still current — "
        f"a 404 on every model means they have been retired."
    )


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

    fmp_key = os.environ.get("FMP_API_KEY", "")

    # 1. Fetch news + picks history + FMP discovery data
    print("1/3  Fetching news and pick context...")
    general_news   = fetch_general_news(finnhub_key)
    picks_history  = _load_picks_history()
    discovery_news = fetch_fmp_discovery_news(fmp_key) if fmp_key else []
    movers         = fetch_fmp_market_movers(fmp_key)  if fmp_key else {}
    print(f"     → {len(general_news)} Finnhub headlines | {len(discovery_news)} FMP discovery | {len(picks_history)} weeks history")

    # 2. Fetch company-specific news (rate-limited)
    print("2/3  Fetching holdings news...")
    company_news: dict[str, list[dict]] = {}
    for ticker in COMPANY_NEWS_TICKERS:
        company_news[ticker] = fetch_company_news(finnhub_key, ticker)
        time.sleep(0.2)  # stay within 60 calls/min free-tier limit
    total_articles = sum(len(v) for v in company_news.values())
    print(f"     → {total_articles} articles across {len(COMPANY_NEWS_TICKERS)} tickers")

    # 3. Generate with Groq, in two halves.
    #
    # The whole schema in one response does not fit the free tier: 8K
    # tokens/minute, max_tokens counts toward it, and prompt(~3.7K) + a
    # full-size response(~5.5-6.3K) is ~9-10K. Groq rejects that outright, and
    # trimming max_tokens enough to be accepted truncates the JSON mid-string.
    #
    # Splitting keeps every section and every rule — each call sees the same
    # prompt and schema and simply returns fewer keys, so each request lands
    # around 7.5K and fits. World-facing sections come first; the
    # portfolio-facing half is then told what the first half already covered,
    # which preserves the "no repetition across sections" rules that a single
    # call enforced implicitly.
    print(f"3/3  Generating intelligence with Groq ({GROQ_MODEL}), 2 passes...")

    pass_a = ["macro", "news", "daily_outlook", "market_mood"]
    pass_b = ["risks", "picks", "strengths", "concerns",
              "strategy_short", "strategy_mid", "strategy_long", "tax"]

    print(f"     pass 1/2 → {', '.join(pass_a)}")
    intelligence = call_llm(groq_key, build_prompt(
        general_news, company_news, picks_history, discovery_news, movers,
        only_sections=pass_a,
    ))

    # Tell the second pass what the first already said, so it does not repeat
    # it. Titles are truncated and the list capped: the model only needs enough
    # to recognise a topic it must avoid, and this block is what pushed the
    # second request past the per-minute ceiling when it carried 20 full titles.
    covered = []
    for m in intelligence.get("macro", []):
        if m.get("title"):
            covered.append(f"  • [macro] {m['title'][:70]}")
    for n in intelligence.get("news", []):
        t = n.get("title") or n.get("headline")
        if t:
            covered.append(f"  • [news] {t[:70]}")
    covered_block = (
        "\nALREADY COVERED in this same briefing — do NOT restate or rehash these:\n"
        + "\n".join(covered[:12]) + "\n"
    ) if covered else ""

    # Groq's 8K limit is per minute, so back-to-back calls would have the second
    # rejected even though each fits on its own. call_llm retries on 429, but
    # waiting out the window is deterministic and costs a minute in a job that
    # runs twice a day.
    print("     waiting 65s for the per-minute token window to reset...")
    time.sleep(65)

    print(f"     pass 2/2 → {', '.join(pass_b)}")
    part_b = call_llm(groq_key, build_prompt(
        general_news, company_news, picks_history, discovery_news, movers,
        only_sections=pass_b, already_covered=covered_block,
    ))
    intelligence.update(part_b)

    # Add metadata
    intelligence["generated_at"] = now_utc.isoformat()
    intelligence["generated_date"] = now_utc.strftime("%B %d, %Y")
    intelligence["next_update"] = "6:00 AM EST next trading day"

    # Validate required keys
    required = [
        "macro", "risks", "news", "picks",
        "strengths", "concerns",
        "strategy_short", "strategy_mid", "strategy_long",
        "daily_outlook", "market_mood",
    ]
    missing = [k for k in required if k not in intelligence]
    if missing:
        print(f"  ⚠ Missing keys in Gemini response: {missing}")

    # Fetch upcoming earnings for held US tickers and add to intelligence
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

    # Save picks history to KV so tomorrow's run avoids repeating them
    new_pick_tickers = [p.get("ticker","") for p in intelligence.get("picks", []) if p.get("ticker")]
    if new_pick_tickers:
        _save_picks_history(new_pick_tickers, picks_history)
        print(f"  ✓ Saved pick history: {new_pick_tickers}")

    # Build rolling 7-day theme history (title+category, used for schema banning)
    today_str    = now_utc.strftime("%Y-%m-%d")
    today_themes = [
        {"category": m.get("category", "macro-economic"), "title": m.get("title", "")}
        for m in intelligence.get("macro", [])
    ]
    history = [e for e in _load_theme_history() if e.get("date") != today_str]
    history = [{"date": today_str, "themes": today_themes}] + history
    intelligence["theme_history"] = history[:7]
    print(f"  ✓ Theme history updated ({len(intelligence['theme_history'])} days)")

    # Build rolling 15-article full macro history (full body text for content-level dedup)
    new_macro_articles = [
        {
            "date":     today_str,
            "category": m.get("category", "macro-economic"),
            "title":    m.get("title", ""),
            "body":     m.get("body", ""),
            "bull":     m.get("bull", ""),
            "base":     m.get("base", ""),
            "bear":     m.get("bear", ""),
        }
        for m in intelligence.get("macro", [])
    ]
    existing_macro_hist = _load_macro_history()
    # Deduplicate by title only — drop any stored article whose title matches a new one
    # (handles exact re-runs), but keep all other same-day articles so multiple daily
    # runs accumulate rather than overwrite each other
    new_titles = {a.get("title", "") for a in new_macro_articles}
    existing_macro_hist = [a for a in existing_macro_hist if a.get("title", "") not in new_titles]
    intelligence["macro_history"] = (new_macro_articles + existing_macro_hist)[:15]
    print(f"  ✓ Macro history updated ({len(intelligence['macro_history'])} articles)")

    # Build rolling 5-day risk title history and attach before saving
    today_risk_titles = [r.get("title", "") for r in intelligence.get("risks", []) if r.get("title")]
    existing_risk_history = _load_risk_history()
    # Remove today's if re-running, then prepend
    risk_hist = [t for t in existing_risk_history if t not in today_risk_titles]
    intelligence["risk_history"] = (today_risk_titles + risk_hist)[:15]  # ~5 days × 3
    print(f"  ✓ Risk history updated ({len(intelligence['risk_history'])} titles)")

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
