"""
Portfolio Pulse — AI Chat Endpoint
Powered by Groq (Llama 3.3 70B, free tier).
POST /api/chat  →  { reply: string }

Request body:
  {
    "message":      string,
    "portfolio":    object (live portfolio from /api/market),
    "intelligence": object (today's intelligence.json),
    "history":      array  (last N chat turns: [{role, content}])
  }
"""
import json
import os
import requests
from http.server import BaseHTTPRequestHandler

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"


SYSTEM_INSTRUCTION = """You are "Pulse", an AI portfolio analyst embedded in a personal Canadian investment dashboard.

You have access to the user's live portfolio data and today's intelligence briefing (provided in conversation context).

YOUR ROLE:
  ✓ Explain what's happening in the market and why it matters to this portfolio
  ✓ Summarise news, themes, and sector moves in portfolio context
  ✓ Help understand account structures (TFSA, FHSA, RRSP, non-reg)
  ✓ Analyse exposure, concentration, and FX impact when asked
  ✓ Reference specific tickers and amounts from the provided context

STRICT LIMITS:
  ✗ Never predict future prices or returns
  ✗ Never give personalised financial advice
  ✗ Never recommend selling the NVDA position or other core holds
  ✗ Never claim data you don't have (e.g. real-time prices unless in context)
  ✗ If asked for advice, say "consider discussing with a registered CFP/CFA"

STYLE:
  - Concise: 2–4 paragraphs unless a detailed breakdown is requested
  - Canadian context: CAD amounts, CRA rules, registered account nuances
  - Reference specific tickers when relevant
  - Be direct and factual, not hedged and vague"""


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        # Parse body
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            return self._error(400, "Invalid JSON body")

        message = (body.get("message") or "").strip()
        if not message:
            return self._error(400, "message is required")

        portfolio = body.get("portfolio") or {}
        intelligence = body.get("intelligence") or {}
        history = (body.get("history") or [])[-12:]   # last 12 turns max

        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            return self._error(503, "AI service unavailable — GROQ_API_KEY not configured on Vercel")

        try:
            # Build context block
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

            if intelligence:
                mood = intelligence.get("market_mood", "—")
                themes = [t.get("title", "") for t in intelligence.get("macro", [])[:3]]
                outlook = intelligence.get("daily_outlook", "")
                strengths = "; ".join(s.get("text", "") for s in intelligence.get("strengths", [])[:3])
                concerns = "; ".join(c.get("text", "") for c in intelligence.get("concerns", [])[:3])
                ctx_lines += [
                    "",
                    "TODAY'S INTELLIGENCE BRIEFING:",
                    f"  Market Mood:  {mood}",
                    f"  Top Themes:   {' | '.join(themes)}",
                    f"  Outlook:      {outlook}",
                    f"  Strengths:    {strengths}",
                    f"  Concerns:     {concerns}",
                ]

            ctx_text = "\n".join(ctx_lines)

            # Build OpenAI-compatible messages array
            messages = [
                {"role": "system",    "content": SYSTEM_INSTRUCTION},
                {"role": "user",      "content": ctx_text},
                {"role": "assistant", "content": "Understood — I have the portfolio context and today's briefing. Ready to help."},
            ]
            for turn in history:
                role = "user" if turn.get("role") == "user" else "assistant"
                messages.append({"role": role, "content": turn.get("content", "")})
            messages.append({"role": "user", "content": message})

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
            print(f"Gemini error: {exc}")
            reply = "Sorry, I'm having trouble right now. Please try again in a moment."

        self._json(200, {"reply": reply})

    # ── helpers ──────────────────────────────────────────────────────────────

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
