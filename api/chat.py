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
import json
import os
import requests
from http.server import BaseHTTPRequestHandler

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Keywords that suggest the user wants external market/news information
_SEARCH_TRIGGERS = [
    "news", "today", "latest", "why", "what happened", "what's happening",
    "whats happening", "earnings", "inflation", "fed ", "rate ", "rates",
    "market", "nasdaq", "s&p", "tsx", "crypto", "bitcoin", "etf", "sector",
    "analyst", "upgrade", "downgrade", "report", "data", "gdp", "jobs",
    "tariff", "trade", "geopolit", "oil", "gold", "bond", "yield",
]

SYSTEM_INSTRUCTION = """You are "Pulse", an AI portfolio analyst embedded in a personal Canadian investment dashboard.

You have access to the user's live portfolio data — including individual holding prices and today's performance — along with today's intelligence briefing and, when relevant, live web search results.

YOUR ROLE:
  ✓ Answer questions about this specific portfolio — holdings, P&L, daily winners/losers, account balances
  ✓ Explain what's happening in the market and why it matters to these positions
  ✓ Summarise news, themes, and sector moves in portfolio context
  ✓ Help understand account structures (TFSA, FHSA, RRSP, non-reg) and Canadian tax rules
  ✓ Use web search results when provided to answer questions about external market events

STRICT LIMITS:
  ✗ Never predict specific future prices or guaranteed returns
  ✗ Never recommend selling core positions (especially NVDA, FNGU, SPXL)
  ✗ Never claim data you don't have — say so if you're unsure
  ✗ Only mention consulting a financial professional if the user is explicitly asking for personalised investment advice on a major decision

STYLE:
  - Concise: 2–4 paragraphs unless a detailed breakdown is requested
  - Canadian context: CAD amounts, CRA rules, registered account nuances
  - Reference specific tickers and dollar amounts from the provided context
  - Be direct and analytical, not vague and hedged
  - Never add a regulatory disclaimer to routine portfolio or market questions"""


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via DuckDuckGo for live market/news context."""
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            # Try news search first (most timely)
            for r in ddgs.news(query, max_results=max_results, timelimit="w"):
                date = (r.get("date") or "")[:10]
                title = r.get("title") or ""
                body  = (r.get("body") or "")[:220]
                results.append(f"[{date}] {title}: {body}")
            # Fall back to text search if no news results
            if not results:
                for r in ddgs.text(query, max_results=3):
                    results.append(f"{r.get('title','')}: {(r.get('body') or '')[:220]}")
        return "\n".join(results)
    except Exception as exc:
        print(f"  web_search error: {exc}")
        return ""


def should_search(message: str) -> bool:
    msg = message.lower()
    return any(kw in msg for kw in _SEARCH_TRIGGERS)


def build_holdings_context(holdings_list: list, holdings_prices: dict) -> str:
    """Build a per-holding performance table sorted by today's change."""
    if not holdings_list:
        return ""

    rows = []
    for h in holdings_list:
        ticker = h.get("ticker", "")
        if ticker.startswith("_CASH"):
            continue
        p = holdings_prices.get(ticker) or {}
        price    = p.get("price")
        day_pct  = p.get("change_pct")
        day_chg  = p.get("change")
        shares   = h.get("shares", 0)
        ccy      = h.get("ccy", "USD")
        if price is not None:
            rows.append({
                "ticker":  ticker,
                "name":    h.get("name", ""),
                "account": h.get("account", ""),
                "shares":  shares,
                "ccy":     ccy,
                "price":   price,
                "day_pct": day_pct if day_pct is not None else 0.0,
                "day_chg": day_chg if day_chg is not None else 0.0,
            })

    if not rows:
        return ""

    rows.sort(key=lambda x: x["day_pct"], reverse=True)

    lines = ["", "HOLDINGS — TODAY'S PERFORMANCE (sorted best → worst):"]
    lines.append(f"  {'TICKER':<8}  {'ACCOUNT':<12}  {'SHARES':>6}  {'PRICE':>9}  {'DAY %':>7}")
    lines.append("  " + "-" * 52)
    for r in rows:
        sign = "+" if r["day_pct"] >= 0 else ""
        lines.append(
            f"  {r['ticker']:<8}  {r['account']:<12}  {r['shares']:>6}  "
            f"${r['price']:>8.2f} {r['ccy']}  {sign}{r['day_pct']:.2f}%"
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
            # ── 1. Build portfolio context ────────────────────────────
            ctx_lines = [f"[Session context — {intelligence.get('generated_date', 'today')}]"]

            if portfolio:
                pf = portfolio
                ctx_lines += [
                    "",
                    "LIVE PORTFOLIO SNAPSHOT:",
                    f"  Total Value:  CAD ${pf.get('total_value', 0):,.0f}",
                    f"  Total P&L:    CAD ${pf.get('total_pnl', 0):,.0f}  ({pf.get('roi_pct', 0):.2f}% ROI)",
                    f"  Daily Δ:      CAD ${pf.get('daily_change', 0):,.0f}  ({pf.get('daily_change_pct', 0):.2f}%)",
                    f"  Unrealized:   CAD ${pf.get('unrealized_gain', 0):,.0f}",
                    f"  Realized:     CAD ${pf.get('realized_gain', 0):,.0f}",
                    f"  FX Impact:    CAD ${pf.get('fx_impact', 0):,.0f}",
                    f"  Accounts:     {json.dumps(pf.get('accounts', {}))}",
                ]

            # Per-holding performance table
            holdings_ctx = build_holdings_context(holdings_list, holdings_prices)
            if holdings_ctx:
                ctx_lines.append(holdings_ctx)

            if intelligence:
                mood     = intelligence.get("market_mood", "—")
                themes   = [t.get("title", "") for t in intelligence.get("macro", [])[:3]]
                outlook  = intelligence.get("daily_outlook", "")
                strengths = "; ".join(s.get("text", "") for s in intelligence.get("strengths", [])[:3])
                concerns  = "; ".join(c.get("text", "") for c in intelligence.get("concerns", [])[:3])
                ctx_lines += [
                    "",
                    "TODAY'S INTELLIGENCE BRIEFING:",
                    f"  Market Mood:  {mood}",
                    f"  Top Themes:   {' | '.join(themes)}",
                    f"  Outlook:      {outlook}",
                    f"  Strengths:    {strengths}",
                    f"  Concerns:     {concerns}",
                ]

            # ── 2. Web search if question needs external data ─────────
            if should_search(message):
                search_query = f"{message} stock market finance"
                search_results = web_search(search_query)
                if search_results:
                    ctx_lines += [
                        "",
                        "WEB SEARCH RESULTS (use to answer questions about current events):",
                        search_results,
                    ]

            ctx_text = "\n".join(ctx_lines)

            # ── 3. Build message array ────────────────────────────────
            messages = [
                {"role": "system",    "content": SYSTEM_INSTRUCTION},
                {"role": "user",      "content": ctx_text},
                {"role": "assistant", "content": "Understood — I have the live portfolio data, individual holding performance, and today's briefing. Ready to help."},
            ]
            for turn in history:
                role = "user" if turn.get("role") == "user" else "assistant"
                messages.append({"role": role, "content": turn.get("content", "")})
            messages.append({"role": "user", "content": message})

            # ── 4. Call Groq ──────────────────────────────────────────
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
