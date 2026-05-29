"""
Portfolio Pulse — AI Chat Endpoint
Powered by Groq (Llama 3.3 70B, free tier).
POST /api/chat  →  { reply: string }

Request body:
  {
    "message":         string,
    "portfolio":       object   (live portfolio totals from /api/market),
    "intelligence":    object   (today's intelligence.json — full object),
    "history":         array    (last N chat turns: [{role, content}]),
    "holdings_prices": object   ({ticker: {price, change_pct, ...}}),
    "holdings_list":   array    ([{ticker, name, account, shares, ccy, cost_total, ...}])
  }
"""
import datetime
import json
import os
import re
import requests
from http.server import BaseHTTPRequestHandler

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# ── Search trigger words ─────────────────────────────────────────────────────

_TRIGGERS_FINANCIAL = [
    "earnings", "when are", "when is", "when does", "when will",
    "ex-div", "ex div", "ex-dividend", "dividend date", "pay date", "record date",
    "split", "stock split", "ipo", "merger", "acquisition",
    "guidance", "eps", "revenue estimate",
    "price target", "analyst", "upgrade", "downgrade", "rating",
    "quarter", "report date", "next report", "fiscal",
]

_TRIGGERS_NEWS = [
    "news", "today", "latest", "why", "what happened", "what's happening",
    "whats happening", "inflation", "fed ", "rate ", "rates",
    "market", "nasdaq", "s&p", "tsx", "crypto", "bitcoin", "etf", "sector",
    "tariff", "trade", "geopolit", "oil", "gold", "bond", "yield",
    "moved", "moving", "dropped", "fell", "surged", "rallied", "crashed",
    "dipped", "spiked", "gdp", "jobs", "cpi", "pce", "recession",
    "interest rate", "bank of canada", "federal reserve",
]

# Words that look like tickers but aren't
_STOP_WORDS = {
    "I", "A", "AN", "THE", "IN", "ON", "AT", "TO", "OF", "AND", "OR",
    "BUT", "IF", "MY", "BE", "IT", "DO", "GO", "NO", "UP", "SO", "BY",
    "AS", "US", "AM", "IS", "AI", "CAD", "USD", "ETF", "ADD", "MED",
    "HIGH", "LOW", "BUY", "SELL", "ASK", "GET", "CAN", "NOW", "HOW",
    "WHY", "WHO", "FOR", "ALL", "NEW", "NOT", "HAS", "HAD", "WAS", "ARE",
    "ITS", "WHEN", "NEXT", "WILL", "THAT", "THIS", "WITH", "FROM", "HAVE",
    "WHAT", "DOES", "WOULD", "COULD", "SHOULD", "THEIR", "ABOUT", "INTO",
    "ALSO", "JUST", "OVER", "SHOW", "TELL", "GIVE", "EACH", "BOTH",
}


# ── Ticker extraction ────────────────────────────────────────────────────────

def extract_tickers(message: str, holdings_list: list) -> list:
    """Pull ticker symbols from the message; known portfolio holdings first."""
    candidates = re.findall(r'\b([A-Z]{2,5}(?:\.[A-Z]{1,3})?)\b', message.upper())
    known = {h.get("ticker", "").upper() for h in holdings_list if h.get("ticker")}

    seen, result = set(), []
    for c in candidates:
        if c not in _STOP_WORDS and c not in seen:
            seen.add(c)
            result.append(c)

    # Sort: known portfolio holdings first, then others alphabetically
    result.sort(key=lambda x: (0 if x in known else 1, x))
    return result[:4]


# ── Yahoo Finance lookup (earnings, analyst targets, dividends) ──────────────

def yf_lookup(tickers: list, message: str) -> str:
    """Structured financial data from Yahoo Finance — no API key, completely free."""
    if not tickers:
        return ""
    try:
        import yfinance as yf
    except ImportError:
        return ""

    msg = message.lower()
    want_earnings = any(k in msg for k in ["earnings", "when", "report", "quarter", "eps", "fiscal"])
    want_analyst  = any(k in msg for k in ["target", "analyst", "rating", "upgrade", "downgrade"])
    want_dividend = any(k in msg for k in ["dividend", "ex-div", "pay date", "record date"])
    show_all = not (want_earnings or want_analyst or want_dividend)

    sections = []
    for ts in tickers[:3]:
        try:
            t    = yf.Ticker(ts)
            info = t.info or {}
            name = info.get("longName") or info.get("shortName") or ts
            lines = [f"  {ts} — {name}:"]

            # Earnings calendar
            if want_earnings or show_all:
                try:
                    cal = t.calendar
                    if isinstance(cal, dict):
                        dates = cal.get("Earnings Date") or []
                        if not isinstance(dates, list):
                            dates = [dates]
                        if dates:
                            date_str = " to ".join(str(d)[:10] for d in dates[:2])
                            lines.append(f"    Next Earnings Date: {date_str}")
                        if cal.get("Earnings Average"):
                            lo = cal.get("Earnings Low", 0)
                            hi = cal.get("Earnings High", 0)
                            lines.append(f"    EPS Estimate: ${cal['Earnings Average']:.2f} avg  (${lo:.2f}–${hi:.2f} range)")
                        if cal.get("Revenue Average"):
                            lines.append(f"    Revenue Estimate: ${cal['Revenue Average'] / 1e9:.1f}B")
                except Exception as e:
                    print(f"    yf calendar {ts}: {e}")

            # Analyst price targets
            if want_analyst or show_all:
                if info.get("targetMeanPrice"):
                    lo = info.get("targetLowPrice", 0)
                    hi = info.get("targetHighPrice", 0)
                    n  = info.get("numberOfAnalystOpinions", "?")
                    lines.append(f"    Analyst Price Target: ${info['targetMeanPrice']:.2f} mean  (${lo:.2f}–${hi:.2f})  [{n} analysts]")
                if info.get("recommendationKey"):
                    lines.append(f"    Consensus Rating: {info['recommendationKey'].upper()}")

            # Dividend info
            if want_dividend or show_all:
                if info.get("exDividendDate"):
                    ex = datetime.datetime.fromtimestamp(info["exDividendDate"]).strftime("%Y-%m-%d")
                    lines.append(f"    Ex-Dividend Date: {ex}")
                if info.get("dividendRate"):
                    lines.append(f"    Annual Dividend: ${info['dividendRate']:.2f}  ({info.get('dividendYield', 0) * 100:.2f}% yield)")

            if len(lines) > 1:
                sections.extend(lines)
                sections.append("")
        except Exception as e:
            print(f"  yf_lookup {ts}: {e}")

    if not sections:
        return ""
    return "YAHOO FINANCE DATA:\n" + "\n".join(sections)


# ── Tavily AI Search (primary — works from cloud IPs, free 1k/month) ─────────

def tavily_search(query: str, search_depth: str = "basic", max_results: int = 7) -> str:
    """Search via Tavily AI — reliable from Vercel/AWS, built for AI agents."""
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        print("  tavily: no API key set")
        return ""
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json={
                "query":          query,
                "search_depth":   search_depth,
                "max_results":    max_results,
                "include_answer": True,
            },
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        if data.get("answer"):
            results.append(f"Direct answer: {data['answer']}")
        for r in data.get("results", []):
            title   = r.get("title", "")
            content = (r.get("content") or "")[:400]
            results.append(f"{title}: {content}")

        print(f"  tavily: returned {len(results)} results for: {query[:60]}")
        return "\n\n".join(results)
    except Exception as exc:
        print(f"  tavily_search error: {exc}")
        return ""


# ── Google News RSS (fallback if no Tavily key) ───────────────────────────────

def google_news_search(query: str, max_results: int = 6) -> str:
    """Google News RSS — fallback when Tavily is unavailable."""
    import urllib.parse
    import xml.etree.ElementTree as ET

    encoded = urllib.parse.quote(query[:200])
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    try:
        resp = requests.get(
            url, timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PortfolioPulse/1.0)"},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        results = []
        for item in root.findall(".//item")[:max_results]:
            title     = item.findtext("title") or ""
            desc      = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()[:300]
            pub_date  = (item.findtext("pubDate") or "")[:16]
            results.append(f"[{pub_date}] {title}: {desc}")
        return "\n\n".join(results)
    except Exception as exc:
        print(f"  google_news error: {exc}")
        return ""


def get_external_context(message: str, holdings_list: list) -> list:
    """Fetch live financial data and news for the question."""
    ctx     = []
    msg     = message.lower()
    tickers = extract_tickers(message, holdings_list)

    needs_financial = any(kw in msg for kw in _TRIGGERS_FINANCIAL)
    needs_news      = any(kw in msg for kw in _TRIGGERS_NEWS)

    has_tavily = bool(os.environ.get("TAVILY_API_KEY", ""))

    if needs_financial or needs_news:
        # Build a focused query
        if tickers:
            base_query = " ".join(tickers[:2]) + " " + message
        else:
            base_query = message + " stock market"

        # Tavily: handles both financial lookups AND news in one call
        if has_tavily:
            depth = "advanced" if needs_financial else "basic"
            results = tavily_search(base_query[:300], search_depth=depth)
            if results:
                ctx += ["", "LIVE WEB SEARCH RESULTS:", results]
        else:
            # Fallback chain: yfinance for financial data, Google News for news
            if needs_financial:
                yf_data = yf_lookup(tickers, message)
                if yf_data:
                    ctx += ["", yf_data]
            news_query = base_query + (" earnings date" if needs_financial else "")
            news = google_news_search(news_query)
            if news:
                ctx += ["", "WEB NEWS:", news]

    return ctx


# ── Intelligence context (full — all sections) ───────────────────────────────

def build_intelligence_context(intelligence: dict) -> list:
    """Serialize the full intelligence.json into context lines for the model."""
    if not intelligence:
        return []

    lines = [
        "",
        f"TODAY'S INTELLIGENCE BRIEFING  [{intelligence.get('generated_date', '—')}]",
        "(This is supplementary market analysis — the HOLDINGS LIST above is the authoritative source for portfolio positions.)",
        f"  Market Mood: {intelligence.get('market_mood', '—')}",
        f"  Outlook: {intelligence.get('daily_outlook', '—')}",
    ]

    # Macro themes — full body + bull/bear scenarios
    for t in intelligence.get("macro", []):
        lines.append(
            f"\n  MACRO — {t.get('title', '')}  "
            f"[Impact: {t.get('impact', '')} | Confidence: {t.get('confidence', '')}%]"
        )
        lines.append(f"    {t.get('body', '')}")
        if t.get("bull"):  lines.append(f"    Bull: {t['bull']}")
        if t.get("base"):  lines.append(f"    Base: {t['base']}")
        if t.get("bear"):  lines.append(f"    Bear: {t['bear']}")

    # Portfolio risks
    for r in intelligence.get("risks", []):
        lines.append(f"\n  RISK — {r.get('title', '')}  [{r.get('level', '')}]  {r.get('context', '')}")
        lines.append(f"    {r.get('body', '')}")

    # Key news events with portfolio exposure + outcome scenarios
    for n in intelligence.get("news", []):
        lines.append(
            f"\n  KEY EVENT — {n.get('headline', '')}  "
            f"[{n.get('impact', '')} impact | {n.get('category', '')}]"
        )
        lines.append(f"    {n.get('body', '')}")
        if n.get("exposure"):
            lines.append(f"    Portfolio exposure: {n['exposure']}")
        for o in n.get("outcomes", []):
            lines.append(
                f"    {o.get('label', '')} ({o.get('probability', '')}%): "
                f"{o.get('scenario', '')} → {o.get('estimate', '')}"
            )

    # Watchlist / picks
    for p in intelligence.get("picks", []):
        lines.append(
            f"\n  PICK — {p.get('ticker', '')}  [{p.get('action', '')} in {p.get('account', '')}]: "
            f"{p.get('thesis', '')}  |  Entry: {p.get('entry', '')}"
        )

    # Strengths and concerns — all of them
    lines.append("\n  PORTFOLIO STRENGTHS:")
    for s in intelligence.get("strengths", []):
        lines.append(f"    [{s.get('ticker', '')}] {s.get('text', '')}")

    lines.append("\n  PORTFOLIO CONCERNS:")
    for c in intelligence.get("concerns", []):
        lines.append(f"    [{c.get('ticker', '')}] {c.get('text', '')}")

    # Strategy recommendations
    shorts = intelligence.get("strategy_short", [])
    mids   = intelligence.get("strategy_mid", [])
    longs  = intelligence.get("strategy_long", [])
    if shorts or mids or longs:
        lines.append("\n  STRATEGY:")
        for s in shorts:
            lines.append(f"    Short-term: {s.get('text', '')}")
        for m in mids:
            lines.append(f"    Mid-term: {m.get('text', '')}")
        for lo in longs[:2]:
            lines.append(f"    Long-term: {lo.get('text', '')}")

    # Tax notes
    tax = intelligence.get("tax", {})
    if tax:
        lines.append("\n  TAX NOTES:")
        for acct, notes in tax.items():
            if isinstance(notes, list):
                for note in notes[:2]:
                    lines.append(f"    {acct.upper()}: {note.get('text', '')}")

    return lines


# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_INSTRUCTION = """You are "Pulse", an AI portfolio analyst embedded in a personal Canadian investment dashboard.

CRITICAL — DATA AUTHORITY:
  The OPEN POSITIONS table is the definitive, complete list of every current holding. It begins with a ticker
  roster line ("OPEN POSITIONS (N positions): AAPL, COST, FNGU ...") — if a ticker appears there, it IS held.
  NEVER say a ticker is not held unless it is genuinely absent from both the roster and the table.
  If a holding shows "n/a" for live price it means the market is closed or data is delayed — the position still exists.
  The table includes sector, cost basis, unrealized P&L, total P&L, dividends, return %, and portfolio weight.
  CLOSED POSITIONS shows fully exited trades (realized gains/losses). CASH POSITIONS shows uninvested cash per account.

EXTERNAL DATA:
  When YAHOO FINANCE DATA or LIVE WEB NEWS appears in your context, use it to give precise, up-to-date answers.
  Always cite the date from news results so the user knows how fresh the information is.
  For earnings dates and analyst targets, quote the exact numbers from Yahoo Finance data.

YOUR ROLE:
  ✓ Answer questions about any holding — price, daily gain/loss, cost basis, total P&L, account
  ✓ Calculate daily dollar gains: shares × live price × day% (or use the live price data directly)
  ✓ Explain what's happening in the market and why it matters to these specific positions
  ✓ Answer earnings dates, analyst targets, dividend dates using the Yahoo Finance data provided
  ✓ Summarise news, themes, and sector moves in portfolio context
  ✓ Help understand account structures (TFSA, FHSA, RRSP, non-reg) and Canadian tax rules

STRICT LIMITS:
  ✗ Never predict specific future prices or guaranteed returns
  ✗ Never recommend selling core positions (especially NVDA, FNGU, SPXL)
  ✗ Only mention consulting a financial professional if the user is explicitly asking for personalised advice on a major decision

STYLE:
  - Concise: 2–4 paragraphs unless a detailed breakdown is requested
  - Canadian context: CAD amounts, CRA rules, registered account nuances
  - Reference specific tickers and dollar amounts from the provided data
  - Be direct and analytical — cite specific dates, prices, and percentages from the data
  - Never add a regulatory disclaimer to routine portfolio or market questions"""


# ── Holdings table ────────────────────────────────────────────────────────────

def build_holdings_context(
    holdings_list: list,
    holdings_prices: dict,
    closed_positions: list,
    cash_positions: list,
) -> str:
    """Complete holdings context: open positions, closed positions, and cash."""
    if not holdings_list:
        return ""

    # ── Open positions ────────────────────────────────────────────────────────
    live_rows, no_price_rows = [], []

    for h in holdings_list:
        ticker = h.get("ticker", "")
        if ticker.startswith("_CASH"):
            continue
        p       = holdings_prices.get(ticker) or {}
        price   = p.get("price")
        day_pct = p.get("change_pct")
        day_chg = p.get("change")
        row = {
            "ticker":     ticker,
            "name":       h.get("name", ""),
            "account":    h.get("account", ""),
            "sector":     h.get("sector", ""),
            "shares":     h.get("shares", 0),
            "ccy":        h.get("ccy", "USD"),
            "price":      price,
            "day_pct":    day_pct,
            "day_chg":    day_chg,
            "cost_total": h.get("cost_total", 0),
            "unrealized": h.get("unrealized", 0),
            "realized":   h.get("realized", 0),
            "dividends":  h.get("dividends", 0),
            "total_pnl":  h.get("total_pnl", 0),
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

    # Ticker roster — quick reference so model never denies a position
    all_tickers = sorted({r["ticker"] for r in all_rows})
    lines = [
        "",
        f"OPEN POSITIONS ({len(all_rows)} positions): {', '.join(all_tickers)}",
        "",
        "FULL HOLDINGS TABLE (live price where available):",
        f"  {'TICKER':<8}  {'NAME':<20}  {'ACCT':<10}  {'SECTOR':<18}  {'SHS':>5}  "
        f"{'PRICE':>10}  {'DAY%':>6}  {'DAY $':>7}  {'COST':>8}  {'UNREAL':>8}  {'TOT P&L':>9}  {'RTN%':>6}",
        "  " + "-" * 130,
    ]

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
            f"  {r['ticker']:<8}  {r['name'][:20]:<20}  {r['account']:<10}  {r['sector'][:18]:<18}  {r['shares']:>5}  "
            f"  {price_s:>10}  {pct_s:>6}  {chg_s:>7}  "
            f"${r['cost_total']:>7,.0f}  ${r['unrealized']:>7,.0f}  ${r['total_pnl']:>8,.0f}  {r['pct_return']:>5.1f}%"
        )

    # ── Closed / exited positions ─────────────────────────────────────────────
    if closed_positions:
        lines += ["", f"CLOSED / EXITED POSITIONS ({len(closed_positions)} positions):"]
        lines.append(
            f"  {'TICKER':<8}  {'NAME':<22}  {'ACCT':<10}  {'CCY':<4}  {'COST':>8}  {'TOT P&L':>9}  {'DIVS':>7}"
        )
        lines.append("  " + "-" * 80)
        for c in sorted(closed_positions, key=lambda x: x.get("account", "")):
            lines.append(
                f"  {c.get('ticker',''):<8}  {c.get('name','')[:22]:<22}  {c.get('account',''):<10}  "
                f"{c.get('ccy',''):<4}  ${c.get('cost_total',0):>7,.0f}  "
                f"${c.get('total_pnl',0):>8,.0f}  ${c.get('dividends',0):>6,.0f}"
            )

    # ── Cash positions ────────────────────────────────────────────────────────
    if cash_positions:
        lines += ["", "CASH POSITIONS:"]
        for c in cash_positions:
            lines.append(f"  {c.get('account',''):<12}  {c.get('ccy','')}  ${c.get('amount',0):,.2f}")

    return "\n".join(lines)


# ── Request handler ───────────────────────────────────────────────────────────

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

        portfolio         = body.get("portfolio")         or {}
        intelligence      = body.get("intelligence")      or {}
        history           = (body.get("history") or [])[-12:]
        holdings_prices   = body.get("holdings_prices")   or {}
        holdings_list     = body.get("holdings_list")     or []
        closed_positions  = body.get("closed_positions")  or []
        cash_positions    = body.get("cash_positions")    or []

        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            return self._error(503, "AI service unavailable — GROQ_API_KEY not configured")

        try:
            # ── 1. Holdings table — FIRST, definitive source ──────────────
            ctx_lines = [f"[Dashboard context — {intelligence.get('generated_date', 'today')}]"]

            holdings_ctx = build_holdings_context(
                holdings_list, holdings_prices, closed_positions, cash_positions
            )
            if holdings_ctx:
                ctx_lines.append(holdings_ctx)

            # ── 2. Portfolio-level totals ─────────────────────────────────
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

            # ── 3. Intelligence briefing (key sections only — keeps context lean) ──
            ctx_lines += build_intelligence_context(intelligence)

            print(f"  context lines: {len(ctx_lines)} | holdings: {len(holdings_list)} | tavily: {bool(os.environ.get('TAVILY_API_KEY'))}")

            # ── 4. External data — Yahoo Finance + Google News ────────────
            ctx_lines += get_external_context(message, holdings_list)

            ctx_text = "\n".join(ctx_lines)

            # ── 5. Build messages array ───────────────────────────────────
            messages = [
                {"role": "system",    "content": SYSTEM_INSTRUCTION},
                {"role": "user",      "content": ctx_text},
                {"role": "assistant", "content": (
                    "Understood — I have the complete holdings list with cost basis and live prices, "
                    "portfolio totals, the full intelligence briefing (macro themes, risks, news events, "
                    "picks, strategy, tax notes), and any live Yahoo Finance or web news data. Ready to help."
                )},
            ]
            for turn in history:
                role = "user" if turn.get("role") == "user" else "assistant"
                messages.append({"role": role, "content": turn.get("content", "")})

            # ── Inject relevant holding data directly beside the question ──
            # This prevents "lost in the middle" — model reads this right before answering
            tickers_asked = extract_tickers(message, holdings_list)
            matching = [h for h in holdings_list if h.get("ticker") in set(tickers_asked)]
            if matching:
                qr = "HOLDINGS DATA FOR THIS QUESTION:\n"
                for h in matching:
                    p     = holdings_prices.get(h.get("ticker", "")) or {}
                    price = p.get("price")
                    pstr  = f"${price:.2f} {h.get('ccy','')}" if price else f"market closed ({h.get('ccy','')})"
                    qr += (
                        f"• {h['ticker']} ({h.get('name','')}) — {h.get('account','')} | "
                        f"{h.get('shares',0)} shares | live: {pstr} | "
                        f"cost basis: ${h.get('cost_total',0):,.0f} | "
                        f"unrealized: ${h.get('unrealized',0):,.0f} | "
                        f"total P&L: ${h.get('total_pnl',0):,.0f} ({h.get('pct_return',0):+.1f}%)\n"
                    )
                final_message = qr + "\n" + message
            else:
                final_message = message

            messages.append({"role": "user", "content": final_message})

            # ── 6. Call Groq ──────────────────────────────────────────────
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model":       GROQ_MODEL,
                    "messages":    messages,
                    "temperature": 0.4,
                    "max_tokens":  1500,
                },
                timeout=35,
            )
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
