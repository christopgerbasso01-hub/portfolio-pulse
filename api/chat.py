"""
Portfolio Pulse — AI Chat Endpoint
Powered by Groq (Llama 3.3 70B, free tier).
POST /api/chat  →  { reply: string }

Request body:
  {
    "message":        string,
    "portfolio":      object   (live portfolio totals from /api/market),
    "intelligence":   object   (today's intelligence.json),
    "history":        array    (last N chat turns: [{role, content}]),
    "holdings_prices": object  ({ticker: {price, change_pct, ...}}),
    "holdings_list":  array    ([{ticker, name, account, shares, ccy}])
  }
"""
import datetime
import json
import os
import requests
from http.server import BaseHTTPRequestHandler

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ── Search trigger categories ────────────────────────────────────────────────

# Factual / calendar lookups — no time cap needed; use text search
_TRIGGERS_LOOKUP = [
    "earnings", "when are", "when is", "when does", "when will",
    "ex-div", "ex div", "ex-dividend", "dividend date", "pay date", "record date",
    "split", "stock split", "ipo", "merger", "acquisition", "takeover",
    "guidance", "forecast", "outlook", "eps", "revenue estimate",
    "price target", "analyst", "upgrade", "downgrade", "rating",
    "quarter", "report date", "next report", "fiscal",
]

# Timely / news queries — use news search (1-month window)
_TRIGGERS_NEWS = [
    "news", "today", "latest", "why", "what happened", "what's happening",
    "whats happening", "inflation", "fed ", "rate ", "rates",
    "market", "nasdaq", "s&p", "tsx", "crypto", "bitcoin", "etf", "sector",
    "tariff", "trade", "geopolit", "oil", "gold", "bond", "yield",
    "moved", "moving", "dropped", "fell", "surged", "rallied", "crashed",
    "pumped", "dipped", "spiked", "gdp", "jobs", "cpi", "pce",
    "recession", "interest rate", "bank of canada", "federal reserve",
]


def classify_search(message: str) -> str | None:
    """Return 'lookup', 'news', or None based on what the message needs."""
    msg = message.lower()
    if any(kw in msg for kw in _TRIGGERS_LOOKUP):
        return "lookup"
    if any(kw in msg for kw in _TRIGGERS_NEWS):
        return "news"
    return None


def build_search_query(message: str, mode: str) -> str:
    """Build an optimised DuckDuckGo query for the given search mode."""
    year = datetime.date.today().year
    if mode == "lookup":
        # Append current year so facts are fresh without needing a time cap
        base = message.strip()
        if str(year) not in base:
            base = f"{base} {year}"
        return base
    # News mode — ground it in finance/stocks
    return f"{message} stock market"


def web_search(query: str, mode: str = "news", max_results: int = 6) -> str:
    """Search via DuckDuckGo. mode='news' for recent events, 'lookup' for facts/dates."""
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            if mode == "news":
                # 1-month window (was 1 week) — catches recent but not ancient news
                for r in ddgs.news(query, max_results=max_results, timelimit="m"):
                    date  = (r.get("date") or "")[:10]
                    title = r.get("title") or ""
                    body  = (r.get("body") or "")[:300]
                    results.append(f"[{date}] {title}: {body}")
                # Fall back to text search if no news results
                if not results:
                    for r in ddgs.text(query, max_results=3):
                        results.append(f"{r.get('title','')}: {(r.get('body') or '')[:300]}")

            else:  # lookup — text search has no time cap, great for dates/facts
                for r in ddgs.text(query, max_results=max_results):
                    title = r.get("title", "")
                    body  = (r.get("body") or "")[:350]
                    results.append(f"{title}: {body}")
                # Supplement with any recent news on the same topic
                for r in ddgs.news(query, max_results=3, timelimit="m"):
                    date  = (r.get("date") or "")[:10]
                    title = r.get("title") or ""
                    body  = (r.get("body") or "")[:220]
                    results.append(f"[{date}] {title}: {body}")

        # Deduplicate by leading content
        seen, deduped = set(), []
        for r in results:
            key = r[:80].lower()
            if key not in seen:
                seen.add(key)
                deduped.append(r)

        return "\n\n".join(deduped[:8])
    except Exception as exc:
        print(f"  web_search error: {exc}")
        return ""


SYSTEM_INSTRUCTION = """You are "Pulse", an AI portfolio analyst embedded in a personal Canadian investment dashboard.

CRITICAL — DATA AUTHORITY:
  The FULL HOLDINGS LIST provided in your context is the definitive, complete list of every position in this portfolio.
  NEVER say a ticker is not held unless it is genuinely absent from that list.
  If a holding shows "n/a" for live price it means the market is closed or data is delayed — the position still exists.
  The list includes cost basis, unrealized/realized P&L, dividends, and weight for every position.

YOUR ROLE:
  ✓ Answer questions about any holding in the full list — price, daily gain/loss, cost basis, total P&L, account
  ✓ Calculate daily dollar gains: shares × live price × day% (or use the live price data directly)
  ✓ Explain what's happening in the market and why it matters to these specific positions
  ✓ Summarise news, themes, and sector moves in portfolio context
  ✓ Help understand account structures (TFSA, FHSA, RRSP, non-reg) and Canadian tax rules
  ✓ Use web search results when provided — for earnings dates, analyst targets, news events, and any factual lookup

STRICT LIMITS:
  ✗ Never predict specific future prices or guaranteed returns
  ✗ Never recommend selling core positions (especially NVDA, FNGU, SPXL)
  ✗ Only mention consulting a financial professional if the user is explicitly asking for personalised advice on a major decision

STYLE:
  - Concise: 2–4 paragraphs unless a detailed breakdown is requested
  - Canadian context: CAD amounts, CRA rules, registered account nuances
  - Reference specific tickers and dollar amounts from the provided data
  - Be direct and analytical, not vague and hedged
  - When web search results are provided, cite the specific date or source so the user knows how fresh the information is
  - Never add a regulatory disclaimer to routine portfolio or market questions"""


def build_holdings_context(holdings_list: list, holdings_prices: dict) -> str:
    """Build complete per-holding table with live prices + static P&L data.
    ALL positions are always included — live price shown where available."""
    if not holdings_list:
        return ""

    live_rows, no_price_rows = [], []

    for h in holdings_list:
        ticker = h.get("ticker", "")
        if ticker.startswith("_CASH"):
            continue
        p         = holdings_prices.get(ticker) or {}
        price     = p.get("price")
        day_pct   = p.get("change_pct")
        day_chg   = p.get("change")
        shares    = h.get("shares", 0)
        ccy       = h.get("ccy", "USD")
        row = {
            "ticker":     ticker,
            "name":       h.get("name", ""),
            "account":    h.get("account", ""),
            "shares":     shares,
            "ccy":        ccy,
            "price":      price,
            "day_pct":    day_pct,
            "day_chg":    day_chg,
            "cost_total": h.get("cost_total", 0),
            "unrealized": h.get("unrealized", 0),
            "realized":   h.get("realized", 0),
            "dividends":  h.get("dividends", 0),
            "pct_return": h.get("pct_return", 0),
            "weight":     h.get("weight", 0),
        }
        if price is not None:
            live_rows.append(row)
        else:
            no_price_rows.append(row)

    live_rows.sort(key=lambda x: x["day_pct"] if x["day_pct"] is not None else 0.0, reverse=True)
    no_price_rows.sort(key=lambda x: x["ticker"])
    all_rows = live_rows + no_price_rows

    lines = ["", "FULL HOLDINGS LIST — ALL POSITIONS (live price where available):"]
    lines.append(
        f"  {'TICKER':<8}  {'NAME':<20}  {'ACCT':<10}  {'SHS':>5}  "
        f"{'PRICE':>10}  {'DAY%':>6}  {'DAY $':>7}  {'COST':>8}  {'UNREAL':>8}  {'RTN%':>6}"
    )
    lines.append("  " + "-" * 110)

    for r in all_rows:
        if r["price"] is not None:
            price_s = f"${r['price']:.2f}{r['ccy']}"
            pct_s   = (("+" if r["day_pct"] >= 0 else "") + f"{r['day_pct']:.2f}%") if r["day_pct"] is not None else "n/a"
            chg_s   = (("+" if (r["day_chg"] or 0) >= 0 else "") + f"${abs(r['day_chg'] or 0):.0f}") if r["day_chg"] is not None else "n/a"
        else:
            price_s = "n/a"
            pct_s   = "n/a"
            chg_s   = "n/a"

        lines.append(
            f"  {r['ticker']:<8}  {r['name'][:20]:<20}  {r['account']:<10}  {r['shares']:>5}  "
            f"  {price_s:>10}  {pct_s:>6}  {chg_s:>7}  "
            f"${r['cost_total']:>7,.0f}  ${r['unrealized']:>7,.0f}  {r['pct_return']:>5.1f}%"
        )

    return "\n".join(lines)


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            return self._error(400, "Invalid JSON body")

        message = (body.get("message") or "").strip()
        if not message:
            return self._error(400, "message is required")

        portfolio       = body.get("portfolio")      or {}
        intelligence    = body.get("intelligence")   or {}
        history         = (body.get("history") or [])[-12:]
        holdings_prices = body.get("holdings_prices") or {}
        holdings_list   = body.get("holdings_list")  or []

        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            return self._error(503, "AI service unavailable — GROQ_API_KEY not configured")

        try:
            # ── 1. Build context — holdings first so model sees full list ──
            ctx_lines = [f"[Dashboard context — {intelligence.get('generated_date', 'today')}]"]

            # Full holdings table is the FIRST thing the model sees
            holdings_ctx = build_holdings_context(holdings_list, holdings_prices)
            if holdings_ctx:
                ctx_lines.append(holdings_ctx)

            # Portfolio-level totals
            if portfolio:
                pf = portfolio
                ctx_lines += [
                    "",
                    "PORTFOLIO TOTALS (live):",
                    f"  Total Value:  CAD ${pf.get('total_value', 0):,.0f}",
                    f"  Total P&L:    CAD ${pf.get('total_pnl', 0):,.0f}  ({pf.get('roi_pct', 0):.2f}% ROI)",
                    f"  Daily Δ:      CAD ${pf.get('daily_change', 0):,.0f}  ({pf.get('daily_change_pct', 0):.2f}%)",
                    f"  Unrealized:   CAD ${pf.get('unrealized_gain', 0):,.0f}",
                    f"  Realized:     CAD ${pf.get('realized_gain', 0):,.0f}",
                    f"  FX Impact:    CAD ${pf.get('fx_impact', 0):,.0f}",
                    f"  Accounts:     {json.dumps(pf.get('accounts', {}))}",
                ]

            # Intelligence briefing (supplementary market context)
            if intelligence:
                mood      = intelligence.get("market_mood", "—")
                themes    = [t.get("title", "") for t in intelligence.get("macro", [])[:3]]
                outlook   = intelligence.get("daily_outlook", "")
                strengths = "; ".join(s.get("text", "") for s in intelligence.get("strengths", [])[:3])
                concerns  = "; ".join(c.get("text", "") for c in intelligence.get("concerns", [])[:3])
                ctx_lines += [
                    "",
                    "TODAY'S INTELLIGENCE BRIEFING (market context — not the source of holdings data):",
                    f"  Market Mood:  {mood}",
                    f"  Top Themes:   {' | '.join(themes)}",
                    f"  Outlook:      {outlook}",
                    f"  Strengths:    {strengths}",
                    f"  Concerns:     {concerns}",
                ]

            # ── 2. Web search if question needs external data ─────────────
            search_mode = classify_search(message)
            if search_mode:
                search_query   = build_search_query(message, search_mode)
                search_results = web_search(search_query, mode=search_mode)
                if search_results:
                    label = "LOOKUP" if search_mode == "lookup" else "NEWS"
                    ctx_lines += [
                        "",
                        f"WEB SEARCH RESULTS ({label}):",
                        search_results,
                    ]

            ctx_text = "\n".join(ctx_lines)

            # ── 3. Build message array ────────────────────────────────────
            messages = [
                {"role": "system",    "content": SYSTEM_INSTRUCTION},
                {"role": "user",      "content": ctx_text},
                {"role": "assistant", "content": "Understood — I have the complete holdings list with cost basis and live prices, portfolio totals, today's market briefing, and any web search results. Ready to help."},
            ]
            for turn in history:
                role = "user" if turn.get("role") == "user" else "assistant"
                messages.append({"role": role, "content": turn.get("content", "")})
            messages.append({"role": "user", "content": message})

            # ── 4. Call Groq ──────────────────────────────────────────────
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": 0.45,
                "max_tokens": 1024,
            }
            resp = requests.post(GROQ_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"]

        except Exception as exc:
            print(f"chat error: {exc}")
            reply = "Sorry, I'm having trouble right now. Please try again in a moment."

        self._json(200, {"reply": reply})

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass
