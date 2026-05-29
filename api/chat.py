"""
Portfolio Pulse — AI Chat Endpoint
Architecture: Groq Llama 3.3 70B with tool calling + SSE streaming

Tools available to the model:
  search_web                — Tavily AI real-time search
  get_portfolio_data        — structured holdings retrieval
  calculate_capital_gains_tax — deterministic Canadian tax calculator
  get_dividend_forecast     — Finnhub + Tavily dividend lookup

SSE event format:
  data: {"status": "..."}   — tool-call progress (shown in typing indicator)
  data: {"content": "..."}  — response text chunks (streamed)
  data: [DONE]              — end of stream
"""
import json
import os
import re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler

# ── Config ────────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY  = os.environ.get("TAVILY_API_KEY", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
GROQ_MODEL      = "llama-3.3-70b-versatile"
GROQ_URL        = "https://api.groq.com/openai/v1/chat/completions"
MAX_TOOL_ROUNDS = 5  # max back-and-forth tool-call rounds per request

# ── Tool status labels (shown in UI while tools run) ──────────────────────────
_TOOL_STATUS = {
    "search_web":                  "🔍 Searching the web...",
    "get_portfolio_data":          "📊 Retrieving portfolio data...",
    "calculate_capital_gains_tax": "🧮 Calculating capital gains tax...",
    "get_dividend_forecast":       "💰 Looking up dividend data...",
}

# ── Tool JSON schemas ─────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the internet for real-time financial data. Use this for: "
                "earnings dates, analyst price targets, upgrades/downgrades, stock news, "
                "dividend announcements, economic releases (CPI, GDP, Fed), market events, "
                "Canadian tax rules, sector trends, company filings. "
                "NEVER fabricate financial data — always call this when you need current info. "
                "You can call this multiple times with different queries for different tickers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Specific search query. Include ticker, company name, and what you need. "
                            "E.g. 'NVDA Nvidia next earnings date Q2 2026' or "
                            "'Canadian capital gains tax rate 2025 Ontario non-registered account'"
                        )
                    },
                    "depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "description": (
                            "'advanced' for precise financial data (earnings dates, dividend amounts, "
                            "analyst targets, tax rates). 'basic' for news and general context."
                        )
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_data",
            "description": (
                "Retrieve detailed portfolio holdings data. Returns positions with cost basis, "
                "unrealized/realized gains, live prices, account breakdown, and dividends received. "
                "Call this before any calculation that requires specific cost basis, position size, "
                "or detailed P&L. The system prompt has a compact overview; call this for full detail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific tickers (e.g. ['NVDA', 'FNGU']). Empty = all positions."
                    },
                    "account": {
                        "type": "string",
                        "description": "Filter by account: 'TFSA', 'RRSP', 'FHSA', 'Investment'. Empty = all."
                    },
                    "include_closed": {
                        "type": "boolean",
                        "description": "Include closed/sold positions. Default false."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_capital_gains_tax",
            "description": (
                "Deterministic Canadian capital gains tax calculator. "
                "Handles account-specific treatment: TFSA/FHSA = completely tax-free; "
                "RRSP = full withdrawal amount taxed as income at marginal rate; "
                "non-registered (Investment) = 50% capital gains inclusion rate. "
                "If marginal_tax_rate is omitted, returns estimates for all Ontario tax brackets. "
                "ALWAYS call this for any tax estimation question — never calculate manually."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tickers to include. Empty = all open positions."
                    },
                    "marginal_tax_rate": {
                        "type": "number",
                        "description": (
                            "Marginal tax rate as decimal (e.g. 0.43 for 43%). "
                            "Omit to get estimates across all income brackets."
                        )
                    },
                    "province": {
                        "type": "string",
                        "description": "Province for rates. Default: Ontario."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_dividend_forecast",
            "description": (
                "Fetch upcoming dividend data for portfolio holdings from Finnhub and Tavily. "
                "Returns dividend dates, amounts per share, and calculates total income based on shares held. "
                "Use for any question about upcoming dividends, expected dividend income, or yield."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": "Months to look ahead. Default: 3."
                    },
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific tickers. Empty = all holdings that have historically paid dividends."
                    }
                },
                "required": []
            }
        }
    }
]


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_search_web(query: str, depth: str = "basic") -> dict:
    """Tavily AI search — works from cloud/AWS IPs."""
    if not TAVILY_API_KEY:
        return {"error": "TAVILY_API_KEY not configured", "hint": "Add TAVILY_API_KEY to Vercel env vars"}
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TAVILY_API_KEY}",
            },
            json={
                "query": query,
                "search_depth": depth,
                "max_results": 8,
                "include_answer": True,
            },
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        if data.get("answer"):
            results.append({"type": "direct_answer", "content": data["answer"]})
        for r in data.get("results", []):
            results.append({
                "title":   r.get("title", ""),
                "content": (r.get("content") or "")[:600],
                "url":     r.get("url", ""),
                "date":    r.get("published_date", ""),
            })
        print(f"  [search_web] {len(results)} results: {query[:60]}")
        return {"query": query, "results": results}
    except Exception as exc:
        print(f"  [search_web] error: {exc}")
        return {"error": str(exc), "query": query}


def tool_get_portfolio_data(
    holdings_list: list,
    holdings_prices: dict,
    closed_positions: list,
    cash_positions: list,
    tickers=None,
    account=None,
    include_closed: bool = False,
) -> dict:
    """Return filtered, enriched holdings data."""
    filtered = holdings_list
    if tickers:
        up = {t.upper() for t in tickers}
        filtered = [h for h in filtered if h.get("ticker", "").upper() in up]
    if account:
        filtered = [h for h in filtered if h.get("account", "").upper() == account.upper()]

    out = []
    total_value = total_cost = total_pnl = 0.0

    for h in filtered:
        ticker = h.get("ticker", "")
        pd = holdings_prices.get(ticker) or {}
        price = pd.get("price")
        shares = h.get("shares", 0)
        cost = h.get("cost_total", 0)
        unreal = h.get("unrealized", 0)
        mkt = (price * shares) if (price and shares) else (cost + unreal)
        total_value += mkt
        total_cost += cost
        total_pnl += h.get("total_pnl", 0)
        out.append({
            "ticker":           ticker,
            "name":             h.get("name", ""),
            "account":          h.get("account", ""),
            "sector":           h.get("sector", ""),
            "shares":           shares,
            "currency":         h.get("ccy", "USD"),
            "live_price":       round(price, 4) if price else None,
            "day_change_pct":   pd.get("change_pct"),
            "market_value":     round(mkt, 2),
            "cost_basis":       round(cost, 2),
            "unrealized_gain":  round(unreal, 2),
            "realized_gain":    round(h.get("realized", 0), 2),
            "dividends_received": round(h.get("dividends", 0), 2),
            "total_pnl":        round(h.get("total_pnl", 0), 2),
            "return_pct":       round(h.get("pct_return", 0), 2),
            "weight_pct":       round(h.get("weight", 0), 2),
        })

    result = {
        "holdings": out,
        "summary": {
            "count":             len(out),
            "total_market_value": round(total_value, 2),
            "total_cost_basis":  round(total_cost, 2),
            "total_pnl":         round(total_pnl, 2),
        },
    }
    if include_closed and closed_positions:
        result["closed_positions"] = closed_positions
    if not tickers and not account:
        result["cash_positions"] = cash_positions

    print(f"  [get_portfolio_data] {len(out)} positions returned")
    return result


def tool_calculate_capital_gains_tax(
    holdings_list: list,
    holdings_prices: dict,
    tickers=None,
    marginal_tax_rate=None,
    province: str = "Ontario",
) -> dict:
    """
    Deterministic Canadian capital gains tax estimate.

    Account treatment:
      TFSA  → completely tax-free
      FHSA  → tax-free (qualifying home purchase assumed)
      RRSP  → full withdrawal taxed as income at marginal rate
      Investment (non-registered) → 50% capital gains inclusion
    """
    positions = holdings_list
    if tickers:
        up = {t.upper() for t in tickers}
        positions = [h for h in positions if h.get("ticker", "").upper() in up]

    accts: dict = {}
    for h in positions:
        ticker = h.get("ticker", "")
        acct = h.get("account", "Investment")
        shares = h.get("shares", 0)
        cost = h.get("cost_total", 0)
        unreal = h.get("unrealized", 0)
        pd = holdings_prices.get(ticker) or {}
        price = pd.get("price")
        mkt = (price * shares) if (price and shares) else (cost + unreal)
        gain = mkt - cost

        if acct not in accts:
            accts[acct] = {"positions": [], "proceeds": 0.0, "cost": 0.0, "gain": 0.0}
        accts[acct]["positions"].append({
            "ticker": ticker,
            "name": h.get("name", ""),
            "shares": shares,
            "cost_basis":    round(cost, 2),
            "market_value":  round(mkt, 2),
            "capital_gain":  round(gain, 2),
        })
        accts[acct]["proceeds"] += mkt
        accts[acct]["cost"]     += cost
        accts[acct]["gain"]     += gain

    for a in accts.values():
        a["proceeds"] = round(a["proceeds"], 2)
        a["cost"]     = round(a["cost"], 2)
        a["gain"]     = round(a["gain"], 2)

    # Ontario combined marginal rates (fed + prov, 2025 approximate)
    brackets = [
        ("Up to ~$57K",     0.2965),
        ("~$57K–$100K",     0.4316),
        ("~$100K–$155K",    0.4797),
        ("~$155K–$221K",    0.5197),
        ("Over $221K",      0.5353),
    ]

    treatment = {
        "TFSA":       ("tax_free",      "Completely tax-free — no tax on any gains"),
        "FHSA":       ("tax_free",      "Tax-free for qualifying first home purchase"),
        "RRSP":       ("income",        "Full withdrawal amount taxed as ordinary income"),
        "Investment": ("cap_gains_50",  "50% capital gains inclusion rate"),
    }

    def tax_at_rate(rate: float):
        total = 0.0
        detail = {}
        for acct, d in accts.items():
            kind, note = treatment.get(acct, ("cap_gains_50", "50% capital gains"))
            if kind == "tax_free":
                detail[acct] = {"tax": 0, "note": note}
            elif kind == "income":
                t = d["proceeds"] * rate
                total += t
                detail[acct] = {
                    "taxable_amount": round(d["proceeds"], 2),
                    "tax": round(t, 2),
                    "note": note,
                }
            else:  # cap_gains_50
                taxable = max(d["gain"], 0) * 0.5
                t = taxable * rate
                total += t
                detail[acct] = {
                    "capital_gain":      round(d["gain"], 2),
                    "taxable_inclusion": round(taxable, 2),
                    "tax":               round(t, 2),
                    "note":              note,
                }
        return round(total, 2), detail

    result = {
        "account_summary": {
            acct: {**d, "tax_treatment": treatment.get(acct, ("cap_gains_50", ""))[1]}
            for acct, d in accts.items()
        }
    }

    if marginal_tax_rate is not None:
        total, detail = tax_at_rate(float(marginal_tax_rate))
        result["calculation"] = {
            "province":             province,
            "marginal_rate":        marginal_tax_rate,
            "per_account":          detail,
            "total_estimated_tax":  total,
        }
    else:
        estimates = []
        for label, rate in brackets:
            total, _ = tax_at_rate(rate)
            estimates.append({
                "income_range":   label,
                "marginal_rate":  rate,
                "estimated_tax":  total,
            })
        result["estimates_by_bracket"] = estimates
        result["follow_up"] = (
            "What is your approximate annual income or marginal tax rate? "
            "I can then give you a precise number. Also, what province are you in?"
        )

    result["important_notes"] = [
        "TFSA/FHSA gains are 100% tax-free — no capital gains tax",
        "RRSP: the FULL withdrawal (not just gains) is taxed as income — this is significant",
        "Non-registered: only 50% of capital gains are included in taxable income",
        "USD positions: FX gains/losses on purchase vs. sale rate are also taxable in non-reg",
        f"Rates shown are combined federal + {province} provincial (approximate)",
        "These are estimates — consult a CPA for exact figures and tax planning",
    ]

    print(f"  [calculate_capital_gains_tax] {len(positions)} positions, rate={marginal_tax_rate}")
    return result


def _finnhub_dividends(symbol: str) -> list:
    """Try Finnhub for structured dividend history. Returns [] on failure."""
    if not FINNHUB_API_KEY:
        return []
    # Finnhub uses bare symbol for TSX stocks (no .TO suffix)
    for sym in [symbol, symbol.replace(".TO", "")]:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/dividend2",
                params={"symbol": sym, "token": FINNHUB_API_KEY},
                timeout=8,
            )
            if r.ok:
                data = r.json().get("data") or []
                if data:
                    return data
        except Exception:
            pass
    return []


def tool_get_dividend_forecast(
    holdings_list: list,
    holdings_prices: dict,
    months: int = 3,
    tickers=None,
) -> dict:
    """Fetch dividend data via Finnhub + Tavily for held positions."""
    if tickers:
        up = {t.upper() for t in tickers}
        candidates = [h for h in holdings_list if h.get("ticker", "").upper() in up]
    else:
        candidates = [h for h in holdings_list if (h.get("dividends") or 0) > 0]

    if not candidates:
        return {
            "message": "No dividend-paying holdings found in the specified criteria.",
            "results": [],
        }

    def process(h):
        ticker = h.get("ticker", "")
        shares = h.get("shares", 0)
        ccy    = h.get("ccy", "USD")
        entry = {
            "ticker":       ticker,
            "name":         h.get("name", ""),
            "account":      h.get("account", ""),
            "shares":       shares,
            "currency":     ccy,
            "total_divs_received_historically": round(h.get("dividends", 0), 2),
            "source": None,
            "dividend_records": None,
        }

        # Try Finnhub first
        records = _finnhub_dividends(ticker)
        if records:
            recent = sorted(records, key=lambda x: x.get("date", ""), reverse=True)[:8]
            processed = []
            for d in recent:
                amt = d.get("amount") or 0
                if amt > 0:
                    processed.append({
                        "ex_date":           d.get("date", ""),
                        "pay_date":          d.get("payDate", ""),
                        "declaration_date":  d.get("declarationDate", ""),
                        "amount_per_share":  amt,
                        "estimated_total":   round(amt * shares, 2),
                        "currency":          d.get("currency", ccy),
                    })
            if processed:
                entry["dividend_records"] = processed
                entry["source"] = "Finnhub (structured data)"
                # Estimate frequency and upcoming amount
                if len(processed) >= 2:
                    avg_amount = sum(r["amount_per_share"] for r in processed[:4]) / min(4, len(processed))
                    entry["avg_dividend_per_share"] = round(avg_amount, 4)
                    entry["forecast_note"] = (
                        f"Based on recent history, estimated {months}-month income: "
                        f"~${round(avg_amount * shares * (months / 3), 2)} {ccy} "
                        f"(assuming quarterly payments)"
                    )
                return entry

        # Tavily fallback
        if TAVILY_API_KEY:
            try:
                q = f"{ticker} next dividend ex-date payment amount per share 2025 2026"
                r = requests.post(
                    "https://api.tavily.com/search",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {TAVILY_API_KEY}",
                    },
                    json={
                        "query":        q,
                        "search_depth": "advanced",
                        "max_results":  5,
                        "include_answer": True,
                    },
                    timeout=10,
                )
                if r.ok:
                    d = r.json()
                    raw = d.get("answer", "")
                    for rec in d.get("results", [])[:3]:
                        raw += f"\n\n{rec.get('title','')}: {(rec.get('content') or '')[:400]}"
                    entry["dividend_records"] = raw
                    entry["source"] = "Tavily web search (interpret dates and amounts from text)"
            except Exception:
                entry["source"] = "search unavailable"

        return entry

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(process, h) for h in candidates]
        results = [f.result() for f in as_completed(futures)]

    results.sort(key=lambda x: x.get("ticker", ""))
    print(f"  [get_dividend_forecast] {len(results)} holdings processed")
    return {
        "forecast_months":         months,
        "forecast_end":            (datetime.now() + timedelta(days=months * 31)).strftime("%Y-%m-%d"),
        "dividend_holding_results": results,
        "instructions": (
            "For Finnhub results: sum 'estimated_total' values for upcoming ex-dates. "
            "For Tavily results: interpret dates, amounts, and shares from the raw text. "
            "Present a clear table to the user showing ticker, ex-date, $/share, total, account."
        ),
    }


# ── Execute a single tool call ────────────────────────────────────────────────

def execute_tool(
    name: str,
    args: dict,
    holdings_list: list,
    holdings_prices: dict,
    closed_positions: list,
    cash_positions: list,
) -> str:
    """Dispatch a tool call and return JSON result string."""
    try:
        if name == "search_web":
            result = tool_search_web(args.get("query", ""), args.get("depth", "basic"))

        elif name == "get_portfolio_data":
            result = tool_get_portfolio_data(
                holdings_list, holdings_prices, closed_positions, cash_positions,
                tickers=args.get("tickers"),
                account=args.get("account"),
                include_closed=args.get("include_closed", False),
            )

        elif name == "calculate_capital_gains_tax":
            result = tool_calculate_capital_gains_tax(
                holdings_list, holdings_prices,
                tickers=args.get("tickers"),
                marginal_tax_rate=args.get("marginal_tax_rate"),
                province=args.get("province", "Ontario"),
            )

        elif name == "get_dividend_forecast":
            result = tool_get_dividend_forecast(
                holdings_list, holdings_prices,
                months=args.get("months", 3),
                tickers=args.get("tickers"),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}

        return json.dumps(result)
    except Exception as exc:
        print(f"  [execute_tool] {name} error: {exc}")
        return json.dumps({"error": str(exc)})


# ── System prompt builder ─────────────────────────────────────────────────────

def build_system_prompt(
    holdings_list: list,
    holdings_prices: dict,
    closed_positions: list,
    cash_positions: list,
    intelligence: dict,
) -> str:
    today = date.today().isoformat()

    # Portfolio totals
    total_value = total_cost = total_pnl = 0.0
    account_totals: dict = {}
    dividend_payers = []

    for h in holdings_list:
        ticker = h.get("ticker", "")
        pd = holdings_prices.get(ticker) or {}
        price = pd.get("price")
        shares = h.get("shares", 0)
        cost = h.get("cost_total", 0)
        unreal = h.get("unrealized", 0)
        mkt = (price * shares) if (price and shares) else (cost + unreal)
        acct = h.get("account", "Other")
        total_value += mkt
        total_cost  += cost
        total_pnl   += h.get("total_pnl", 0)
        account_totals[acct] = account_totals.get(acct, 0.0) + mkt
        if (h.get("dividends") or 0) > 0:
            dividend_payers.append(ticker)

    cash_total = sum(c.get("amount", 0) for c in cash_positions)
    overall_return = (total_pnl / total_cost * 100) if total_cost else 0

    # Compact positions list (top 20 by weight to keep token count manageable)
    pos_lines = []
    sorted_holdings = sorted(holdings_list, key=lambda x: -(x.get("weight") or 0))
    for h in sorted_holdings[:20]:
        ticker = h.get("ticker", "")
        pd = holdings_prices.get(ticker) or {}
        price = pd.get("price")
        shares = h.get("shares", 0)
        cost = h.get("cost_total", 0)
        unreal = h.get("unrealized", 0)
        mkt = (price * shares) if (price and shares) else (cost + unreal)
        pos_lines.append(
            f"  {ticker:<9} {h.get('account',''):<11} {h.get('name','')[:22]:<22} "
            f"${mkt:>9,.0f}  {h.get('pct_return',0):>+6.1f}%"
        )
    if len(sorted_holdings) > 20:
        pos_lines.append(f"  ... and {len(sorted_holdings)-20} more — call get_portfolio_data for full list")

    acct_lines = "\n".join(
        f"  {a}: ${v:,.0f}" for a, v in sorted(account_totals.items())
    )

    # Intelligence context
    intel_lines = []
    if intelligence:
        if intelligence.get("market_mood"):
            intel_lines.append(f"Market mood: {intelligence['market_mood']}")
        if intelligence.get("daily_outlook"):
            intel_lines.append(f"Today's outlook: {intelligence['daily_outlook']}")
        for t in (intelligence.get("macro") or [])[:3]:
            if isinstance(t, dict):
                intel_lines.append(f"Macro — {t.get('title','')}: {t.get('body','')[:200]}")
        for r in (intelligence.get("risks") or [])[:2]:
            if isinstance(r, dict):
                intel_lines.append(f"Risk — {r.get('title','')}: {r.get('body','')[:150]}")

    intel_block = ("\n## TODAY'S MARKET CONTEXT\n" + "\n".join(intel_lines)) if intel_lines else ""

    positions_block = "\n".join(pos_lines)

    prompt = f"""You are Pulse — an elite AI financial analyst and portfolio advisor for a Canadian investor.
Today's date: {today}

## TOOL CALLING RULES (critical)
You have 4 tools. You MUST call them — do not narrate or plan to call them, just call them:
- Earnings dates, analyst targets, news → call search_web immediately
- Tax questions → call calculate_capital_gains_tax immediately
- Dividend income questions → call get_dividend_forecast immediately
- Questions needing exact cost basis, P&L detail → call get_portfolio_data
You may call multiple tools in one response. Ask follow-up questions when you need the user's
marginal tax rate, province, or time horizon before proceeding.

## RESPONSE STYLE
- Comprehensive answers with tables and bullet points
- Show exact dollar amounts and dates from tool results
- Cite the source/date of web search results
- Brief disclaimer for tax/financial advice

## PORTFOLIO OVERVIEW  (as of {today})
Total market value:  ${total_value:,.0f} (CAD equiv.)
Total P&L:           ${total_pnl:+,.0f}  |  Cost basis: ${total_cost:,.0f}  |  Return: {overall_return:.1f}%
Cash (uninvested):   ${cash_total:,.0f}

Accounts:
{acct_lines}

Dividend-paying holdings: {', '.join(dividend_payers) if dividend_payers else 'None identified'}

## ALL OPEN POSITIONS  (sorted by portfolio weight)
  {"TICKER":<10} {"ACCOUNT":<12} {"NAME":<28} {"MKT VALUE":>12}   {"RETURN":>8}   SHARES
  {"-" * 84}
{positions_block}

## CANADIAN TAX RULES (key facts)
- TFSA:       All growth and withdrawals 100% tax-free. No contribution room impact on gains.
- FHSA:       Tax-deductible contributions; withdrawals tax-free for qualifying home purchase.
- RRSP:       Contributions deductible; withdrawals fully taxed as income at marginal rate.
- Investment: Capital gains → 50% inclusion rate. Canadian dividends → dividend tax credit.
              USD position gains include FX component (also taxable in non-registered).
- Leveraged ETFs (FNGU, SPXL, UDOW): treated as capital gains, not income.{intel_block}

## STRICT RULES
- Never predict specific future prices or guaranteed returns
- For tax/legal questions: provide estimates from tools, then note to consult a CPA
- Reference specific dollar amounts and percentages from data — be precise"""

    return prompt


# ── Request handler ───────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        # ── Parse request ──────────────────────────────────────────────────────
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            return self._error(400, "Invalid JSON body")

        message          = (body.get("message") or "").strip()
        history          = (body.get("history") or [])[-14:]
        holdings_list    = body.get("holdings_list")    or []
        holdings_prices  = body.get("holdings_prices")  or {}
        closed_positions = body.get("closed_positions") or []
        cash_positions   = body.get("cash_positions")   or []
        intelligence     = body.get("intelligence")     or {}

        if not message:
            return self._error(400, "message is required")
        if not GROQ_API_KEY:
            return self._error(503, "GROQ_API_KEY not configured")

        # ── Build system prompt + message history ──────────────────────────────
        system_prompt = build_system_prompt(
            holdings_list, holdings_prices, closed_positions, cash_positions, intelligence
        )

        messages = [{"role": "system", "content": system_prompt}]
        for turn in history:
            role = turn.get("role", "")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": turn.get("content", "")})
        messages.append({"role": "user", "content": message})

        # ── Start SSE response immediately ─────────────────────────────────────
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        def send_event(data: dict):
            try:
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ── Tool-calling loop (non-streaming) ──────────────────────────────────
        print(f"  chat: message='{message[:60]}' | holdings={len(holdings_list)}")

        try:
            for round_num in range(MAX_TOOL_ROUNDS):
                tool_choice = "auto"
                resp = requests.post(
                    GROQ_URL,
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type":  "application/json",
                    },
                    json={
                        "model":       GROQ_MODEL,
                        "messages":    messages,
                        "tools":       TOOLS,
                        "tool_choice": tool_choice,
                        "temperature": 0.3,
                        "max_tokens":  1024,
                        "stream":      False,
                    },
                    timeout=25,
                )
                if not resp.ok:
                    print(f"  Groq round-{round_num} error {resp.status_code}: {resp.text[:300]}")
                    if resp.status_code == 429:
                        # Rate limit — wait and retry once
                        import time; time.sleep(8)
                        resp = requests.post(
                            GROQ_URL,
                            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                            json={"model": GROQ_MODEL, "messages": messages, "tools": TOOLS,
                                  "tool_choice": tool_choice, "temperature": 0.3,
                                  "max_tokens": 1024, "stream": False},
                            timeout=25,
                        )
                        if not resp.ok:
                            break
                    else:
                        break

                choice = resp.json()["choices"][0]
                msg = choice["message"]
                tool_calls = msg.get("tool_calls") or []

                if not tool_calls:
                    # Model is satisfied — proceed to streaming final response
                    break

                # ── Add assistant's tool-call turn ─────────────────────────────
                messages.append({
                    "role":       "assistant",
                    "content":    msg.get("content"),
                    "tool_calls": tool_calls,
                })

                # ── Send status event(s) ───────────────────────────────────────
                unique_tools = list(dict.fromkeys(tc["function"]["name"] for tc in tool_calls))
                status_parts = [_TOOL_STATUS.get(n, f"🔧 {n}...") for n in unique_tools]
                send_event({"status": "  |  ".join(status_parts)})

                # ── Execute tools in parallel ──────────────────────────────────
                def run_tc(tc):
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        args = {}
                    result_json = execute_tool(
                        name, args,
                        holdings_list, holdings_prices,
                        closed_positions, cash_positions,
                    )
                    return tc["id"], result_json

                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(run_tc, tc) for tc in tool_calls]
                    for future in as_completed(futures):
                        tc_id, result_json = future.result()
                        messages.append({
                            "role":         "tool",
                            "tool_call_id": tc_id,
                            "content":      result_json,
                        })

            # ── Stream final response ──────────────────────────────────────────
            # Include tools + tool_choice:"none" when the conversation has tool
            # messages — Groq requires the tools schema to be present in that case.
            has_tool_context = any(m.get("role") == "tool" for m in messages)
            stream_payload = {
                "model":       GROQ_MODEL,
                "messages":    messages,
                "temperature": 0.3,
                "max_tokens":  2048,
                "stream":      True,
            }
            if has_tool_context:
                stream_payload["tools"]       = TOOLS
                stream_payload["tool_choice"] = "none"

            stream_resp = requests.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=stream_payload,
                timeout=35,
                stream=True,
            )

            if not stream_resp.ok and stream_resp.status_code == 429:
                import time; time.sleep(8)
                stream_resp = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                    json=stream_payload, timeout=35, stream=True,
                )

            if not stream_resp.ok:
                err = stream_resp.text[:400]
                print(f"  Groq stream error {stream_resp.status_code}: {err}")
                send_event({"content": f"AI error ({stream_resp.status_code}). Please try again."})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            for raw_line in stream_resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                if not line.startswith("data: "):
                    continue
                chunk = line[6:]
                if chunk == "[DONE]":
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    break
                try:
                    chunk_data = json.loads(chunk)
                    delta = chunk_data["choices"][0]["delta"]
                    text = delta.get("content") or ""
                    if text:
                        send_event({"content": text})
                except Exception:
                    pass

        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"  chat error: {exc}")
            send_event({"content": f"Sorry, something went wrong: {exc}"})
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _error(self, code: int, msg: str):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass
