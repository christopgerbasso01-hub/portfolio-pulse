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
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
TAVILY_API_KEY   = os.environ.get("TAVILY_API_KEY", "")
FINNHUB_API_KEY  = os.environ.get("FINNHUB_API_KEY", "")
LLM_MODEL        = "llama-3.3-70b"   # high quality responses (60k TPM free tier)
LLM_MODEL_FAST   = "llama3.1-8b"     # tool-calling rounds (60k TPM free tier)
LLM_URL          = "https://api.cerebras.ai/v1/chat/completions"
MAX_TOOL_ROUNDS  = 5  # max back-and-forth tool-call rounds per request

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
            "description": "Search internet for live financial data: earnings dates, analyst targets, news, dividends, economic data, Canadian tax rules. Call immediately — don't narrate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query. Include ticker and specific data needed."},
                    "depth": {"type": "string", "enum": ["basic", "advanced"], "description": "advanced=financial precision, basic=news"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_data",
            "description": "Get detailed holdings: cost basis, unrealized/realized gains, live prices. Call for exact P&L calculations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tickers": {"type": "array", "items": {"type": "string"}, "description": "Specific tickers or empty for all"},
                    "account": {"type": "string", "description": "TFSA/RRSP/FHSA/Investment or empty for all"},
                    "include_closed": {"type": "boolean", "description": "Include closed positions"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_capital_gains_tax",
            "description": "Canadian tax calculator: TFSA/FHSA=tax-free, RRSP=income, Investment=50% inclusion. Always call for tax questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tickers": {"type": "array", "items": {"type": "string"}, "description": "Tickers or empty for all"},
                    "marginal_tax_rate": {"type": "number", "description": "Rate as decimal (0.43=43%). Omit for bracket estimates."},
                    "province": {"type": "string", "description": "Province (default Ontario)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_dividend_forecast",
            "description": "Fetch upcoming dividend dates and amounts from Finnhub/Tavily for held positions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "months": {"type": "integer", "description": "Months ahead (default 3)"},
                    "tickers": {"type": "array", "items": {"type": "string"}, "description": "Specific tickers or empty for all dividend payers"}
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
        return {"error": "TAVILY_API_KEY not configured"}
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
                "max_results": 5,       # reduced from 8 to save tokens
                "include_answer": True,
            },
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        if data.get("answer"):
            results.append({"answer": data["answer"]})  # compact key
        for r in data.get("results", []):
            results.append({
                "t": r.get("title", ""),
                "c": (r.get("content") or "")[:300],   # reduced from 600
                "d": r.get("published_date", ""),
            })
        print(f"  [search_web] {len(results)} results: {query[:60]}")
        return {"q": query, "results": results}
    except Exception as exc:
        print(f"  [search_web] error: {exc}")
        return {"error": str(exc)}


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


_YF_DIV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://finance.yahoo.com",
}


def _yahoo_dividends(ticker: str) -> list:
    """Fetch dividend history from Yahoo Finance chart API. Returns [{date, amount}] sorted newest first."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?events=dividends&range=2y&interval=3mo"
    try:
        r = requests.get(url, headers=_YF_DIV_HEADERS, timeout=8)
        if not r.ok:
            return []
        data = r.json()
        result = data["chart"]["result"][0]
        divs = result.get("events", {}).get("dividends", {})
        if not divs:
            return []
        records = []
        for ts, d in divs.items():
            amt = float(d.get("amount") or 0)
            if amt > 0:
                records.append({
                    "date":   datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d"),
                    "amount": round(amt, 4),
                })
        return sorted(records, key=lambda x: x["date"], reverse=True)[:12]
    except Exception:
        return []


def _finnhub_dividends(symbol: str) -> list:
    """Try Finnhub for structured dividend history. Returns [{date, amount}] or []."""
    if not FINNHUB_API_KEY:
        return []
    for sym in [symbol, symbol.replace(".TO", "")]:
        try:
            r = requests.get(
                "https://finnhub.io/api/v1/stock/dividend2",
                params={"symbol": sym, "token": FINNHUB_API_KEY},
                timeout=8,
            )
            if r.ok:
                data = r.json().get("data") or []
                records = [
                    {"date": d.get("date", ""), "amount": round(float(d.get("amount") or 0), 4)}
                    for d in data if (d.get("amount") or 0) > 0
                ]
                if records:
                    return sorted(records, key=lambda x: x["date"], reverse=True)[:12]
        except Exception:
            pass
    return []


def tool_get_dividend_forecast(
    holdings_list: list,
    holdings_prices: dict,
    months: int = 3,
    tickers=None,
) -> dict:
    """Fetch dividend forecasts using Yahoo Finance historical data — all math done in Python."""
    today      = date.today()
    end_date   = today + timedelta(days=months * 31)

    if tickers:
        up = {t.upper() for t in tickers}
        candidates = [h for h in holdings_list if h.get("ticker", "").upper() in up]
    else:
        candidates = [h for h in holdings_list if (h.get("dividends") or 0) > 0]

    if not candidates:
        return {"message": "No dividend-paying holdings found.", "results": []}

    # Deduplicate tickers across accounts so we only fetch each ticker once
    ticker_data: dict = {}

    def fetch_ticker(ticker):
        records = _yahoo_dividends(ticker) or _finnhub_dividends(ticker)
        ticker_data[ticker] = records

    unique_tickers = list({h["ticker"] for h in candidates})
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(as_completed([ex.submit(fetch_ticker, t) for t in unique_tickers]))

    results = []
    for h in candidates:
        ticker  = h.get("ticker", "")
        shares  = h.get("shares", 0)
        account = h.get("account", "")
        ccy     = h.get("ccy", "USD")
        records = ticker_data.get(ticker, [])

        if not records:
            results.append({
                "ticker": ticker, "account": account, "shares": shares,
                "no_data": True,
                "note": f"No dividend history found — use search_web('{ticker} next dividend ex-date 2025 2026') for current data",
            })
            continue

        # ── Detect payment frequency from inter-payment intervals ──────────
        if len(records) >= 2:
            dates = sorted(
                [datetime.strptime(r["date"], "%Y-%m-%d") for r in records],
                reverse=True,
            )
            intervals = [(dates[i] - dates[i + 1]).days for i in range(min(5, len(dates) - 1))]
            avg_interval = sum(intervals) / len(intervals)
        else:
            avg_interval = 91

        if avg_interval < 40:
            freq_label, freq_days = "monthly",    30
        elif avg_interval < 75:
            freq_label, freq_days = "bi-monthly", 60
        elif avg_interval < 135:
            freq_label, freq_days = "quarterly",  91
        elif avg_interval < 270:
            freq_label, freq_days = "semi-annual", 183
        else:
            freq_label, freq_days = "annual",     365

        # Use average of last 4 payments as forecast amount
        avg_amount = sum(r["amount"] for r in records[:4]) / min(4, len(records))
        last_paid  = datetime.strptime(records[0]["date"], "%Y-%m-%d")

        # ── Project upcoming payments inside the forecast window ──────────
        upcoming = []
        next_dt = last_paid + timedelta(days=freq_days)
        while next_dt.date() <= end_date:
            if next_dt.date() >= today:
                upcoming.append({
                    "estimated_ex_date":  next_dt.strftime("%Y-%m-%d"),
                    "month":              next_dt.strftime("%B %Y"),
                    "amount_per_share":   round(avg_amount, 4),
                    "shares":             shares,
                    "estimated_total":    round(avg_amount * shares, 2),
                    "currency":           ccy,
                    "account":            account,
                })
            next_dt += timedelta(days=freq_days)

        results.append({
            "ticker":                  ticker,
            "account":                 account,
            "shares":                  shares,
            "currency":                ccy,
            "frequency":               freq_label,
            "last_payment_date":       records[0]["date"],
            "last_amount_per_share":   records[0]["amount"],
            "avg_amount_per_share":    round(avg_amount, 4),
            "upcoming_payments":       upcoming,
            "subtotal_forecast":       round(sum(p["estimated_total"] for p in upcoming), 2),
        })

    results.sort(key=lambda x: x.get("ticker", ""))
    grand_total_usd = round(sum(
        r["subtotal_forecast"] for r in results
        if not r.get("no_data") and r.get("currency") == "USD"
    ), 2)
    grand_total_cad = round(sum(
        r["subtotal_forecast"] for r in results
        if not r.get("no_data") and r.get("currency") == "CAD"
    ), 2)

    print(f"  [get_dividend_forecast] {len(results)} holdings | USD ${grand_total_usd} + CAD ${grand_total_cad}")
    return {
        "forecast_period":  f"{today} to {end_date}",
        "grand_total_usd":  grand_total_usd,
        "grand_total_cad":  grand_total_cad,
        "results":          results,
        "note": "All amounts are in native currency. Convert USD totals at current USD/CAD rate for combined CAD figure.",
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

    acct_lines = " | ".join(f"{a}:${v:,.0f}" for a, v in sorted(account_totals.items()))

    # USD/CAD rate for conversions
    try:
        usdcad = float((holdings_prices.get("USDCAD=X") or {}).get("price") or 1.37)
    except (TypeError, ValueError):
        usdcad = 1.37

    def _cad(val, ccy, rate=None):
        """Convert native-currency value to CAD."""
        return val * (rate or usdcad) if ccy == "USD" else val

    # Full positions table — every column the holdings table shows, in CAD
    sorted_holdings = sorted(holdings_list, key=lambda x: -(x.get("weight") or 0))
    pos_lines = []
    for h in sorted_holdings:
        ticker    = h.get("ticker", "")
        pd_       = holdings_prices.get(ticker) or {}
        price     = pd_.get("price")
        shares    = h.get("shares", 0)
        ccy       = h.get("ccy", "USD")
        book_rate = h.get("book_rate") or (usdcad if ccy == "USD" else 1.0)
        cost      = h.get("cost_total", 0)
        unreal    = h.get("unrealized", 0)
        realized  = h.get("realized", 0)
        divs      = h.get("dividends", 0)
        total_pnl = h.get("total_pnl", 0)
        pct_ret   = h.get("pct_return", 0)

        # Market value in CAD
        mkt_cad   = _cad(price * shares, ccy) if (price and shares) else _cad(cost + unreal, ccy)
        # Cost (book value) in CAD — use purchase FX rate
        cost_cad  = cost * book_rate if ccy == "USD" else cost
        # Unrealized gain in CAD
        unreal_cad = _cad(unreal, ccy)
        # Today's change in CAD
        chg       = pd_.get("change")
        chg_pct   = pd_.get("change_pct")
        daily_cad = _cad(chg * shares, ccy) if chg is not None else None
        daily_str = (f"{daily_cad:+,.0f} ({chg_pct:+.1f}%)" if daily_cad is not None else "—")
        # Total P&L in CAD (unrealized + realized gains, no dividends)
        total_cad = _cad(total_pnl, ccy)

        price_str = f"@${price:.2f}{ccy}" if price else "—"
        pos_lines.append(
            f"  {ticker:<8} {h.get('account',''):<11} {shares:>5}sh {price_str:<14} | "
            f"cost ${cost_cad:>8,.0f} | mkt ${mkt_cad:>8,.0f} | "
            f"unreal {unreal_cad:>+8,.0f} | today {daily_str} | "
            f"total P&L {total_cad:>+8,.0f} ({pct_ret:+.0f}%)"
        )
    positions_block = "\n".join(pos_lines)

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
    intel_block = ("\nMARKET CONTEXT: " + " | ".join(intel_lines)) if intel_lines else ""

    prompt = f"""You are Pulse, an AI portfolio analyst for a Canadian investor. Today: {today}.

RULES: Answer directly from the positions table below when data is there. Call get_portfolio_data for cost basis, unrealized gains, or detailed P&L. Call search_web for live market data, news, earnings, dividends, analyst targets. Call calculate_capital_gains_tax for tax questions. Call get_dividend_forecast for dividend income. Never invent numbers — if unsure, call the tool.

PORTFOLIO: ${total_value:,.0f} total | P&L ${total_pnl:+,.0f} ({overall_return:.1f}%) | Cash ${cash_total:,.0f}
Accounts: {acct_lines}
Dividend payers: {', '.join(dividend_payers[:10]) if dividend_payers else 'none'}

POSITIONS (all values in CAD | cols: ticker · account · shares · cost · mkt value · unrealized gain · today's $ change · total P&L):
{positions_block}{intel_block}

TAX: TFSA/FHSA=tax-free | RRSP=full withdrawal taxed as income | Investment=50% cap gains inclusion
Use tables/bullets. Disclaimer for tax advice."""

    return prompt


# ── Request handler ───────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    @staticmethod
    def _retry_wait(resp) -> int:
        """Return seconds to wait based on Groq's retry-after header, capped at 20s."""
        try:
            return min(int(resp.headers.get("retry-after", 8)), 20)
        except (ValueError, TypeError):
            return 8

    def do_POST(self):
        # ── Parse request ──────────────────────────────────────────────────────
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            return self._error(400, "Invalid JSON body")

        message          = (body.get("message") or "").strip()
        history          = (body.get("history") or [])[-10:]
        holdings_list    = body.get("holdings_list")    or []
        holdings_prices  = body.get("holdings_prices")  or {}
        closed_positions = body.get("closed_positions") or []
        cash_positions   = body.get("cash_positions")   or []
        intelligence     = body.get("intelligence")     or {}

        if not message:
            return self._error(400, "message is required")
        if not CEREBRAS_API_KEY:
            return self._error(503, "CEREBRAS_API_KEY not configured")

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
                    LLM_URL,
                    headers={
                        "Authorization": f"Bearer {CEREBRAS_API_KEY}",
                        "Content-Type":  "application/json",
                    },
                    json={
                        "model":       LLM_MODEL_FAST,  # smaller/faster model for tool decisions
                        "messages":    messages,
                        "tools":       TOOLS,
                        "tool_choice": tool_choice,
                        "temperature": 0.2,
                        "max_tokens":  512,
                        "stream":      False,
                    },
                    timeout=25,
                )
                if not resp.ok:
                    print(f"  Groq round-{round_num} error {resp.status_code}: {resp.text[:300]}")
                    if resp.status_code == 429:
                        import time
                        wait = self._retry_wait(resp)
                        send_event({"status": f"⏳ Rate limited — retrying in {wait}s..."})
                        time.sleep(wait)
                        resp = requests.post(
                            LLM_URL,
                            headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"},
                            json={"model": LLM_MODEL_FAST, "messages": messages, "tools": TOOLS,
                                  "tool_choice": tool_choice, "temperature": 0.2,
                                  "max_tokens": 512, "stream": False},
                            timeout=25,
                        )
                        if not resp.ok and resp.status_code == 429:
                            # Still limited — wait again and try 8B
                            wait2 = self._retry_wait(resp)
                            send_event({"status": f"⏳ Still limited — waiting {wait2}s more..."})
                            time.sleep(wait2)
                            resp = requests.post(
                                LLM_URL,
                                headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"},
                                json={"model": LLM_MODEL_FAST, "messages": messages, "tools": TOOLS,
                                      "tool_choice": tool_choice, "temperature": 0.2,
                                      "max_tokens": 512, "stream": False},
                                timeout=25,
                            )
                        if not resp.ok:
                            break
                    elif resp.status_code == 413:
                        # Payload too large — trim tool results and retry
                        for m in messages:
                            if m.get("role") == "tool" and len(m.get("content", "")) > 400:
                                try:
                                    obj = json.loads(m["content"])
                                    if isinstance(obj, dict) and "results" in obj:
                                        obj["results"] = obj["results"][:2]
                                        m["content"] = json.dumps(obj)
                                except Exception:
                                    m["content"] = m["content"][:400]
                        resp = requests.post(
                            LLM_URL,
                            headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"},
                            json={"model": LLM_MODEL_FAST, "messages": messages, "tools": TOOLS,
                                  "tool_choice": tool_choice, "temperature": 0.2,
                                  "max_tokens": 512, "stream": False},
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
                "model":       LLM_MODEL,
                "messages":    messages,
                "temperature": 0.3,
                "max_tokens":  2048,
                "stream":      True,
            }
            if has_tool_context:
                stream_payload["tools"]       = TOOLS
                stream_payload["tool_choice"] = "none"

            stream_resp = requests.post(
                LLM_URL,
                headers={
                    "Authorization": f"Bearer {CEREBRAS_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json=stream_payload,
                timeout=35,
                stream=True,
            )

            if not stream_resp.ok and stream_resp.status_code == 429:
                import time
                # 70B rate-limited → wait (header-guided) then fall back to 8B
                wait = self._retry_wait(stream_resp)
                send_event({"status": f"⏳ Rate limited — switching models, retrying in {wait}s..."})
                time.sleep(wait)
                stream_payload["model"] = LLM_MODEL_FAST
                stream_resp = requests.post(
                    LLM_URL,
                    headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"},
                    json=stream_payload, timeout=35, stream=True,
                )
                if not stream_resp.ok and stream_resp.status_code == 429:
                    # 8B also limited — wait once more
                    wait2 = self._retry_wait(stream_resp)
                    send_event({"status": f"⏳ Still rate limited — final retry in {wait2}s..."})
                    time.sleep(wait2)
                    stream_resp = requests.post(
                        LLM_URL,
                        headers={"Authorization": f"Bearer {CEREBRAS_API_KEY}", "Content-Type": "application/json"},
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
