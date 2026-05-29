#!/usr/bin/env python3
"""
Portfolio Pulse — Weekly Podcast Generator
Runs via GitHub Actions every Monday at 6:00 AM UTC (8:00 AM Geneva).

Pipeline:
  1. Load data/intelligence.json
  2. Groq (Llama 3.3 70B) → two-host podcast script (~1,800 words)
  3. Groq → structured text summary JSON
  4. edge-tts → MP3 segments per speaker turn (two distinct voices)
  5. Pure-Python bytes concat → data/podcast_ep{N:03d}.mp3
  6. Save episode metadata to data/podcast_meta.json

No API keys required beyond GROQ_API_KEY (already configured).
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

# Two distinct Microsoft neural voices — genuinely different character
VOICE_ALEX = "en-US-AndrewMultilingualNeural"   # Male — the analyst, cites numbers
VOICE_SAM  = "en-US-AvaMultilingualNeural"      # Female — the questioner, listener proxy

SPEECH_RATE = "+8%"   # Slightly faster = more energetic podcast feel

DATA_DIR     = Path("data")
PODCAST_META = DATA_DIR / "podcast_meta.json"
INTEL_FILE   = DATA_DIR / "intelligence.json"
MAX_EPISODES = 4   # Keep current + this many in archive


def podcast_file(ep: int) -> Path:
    """Return the Path for a numbered episode MP3."""
    return DATA_DIR / f"podcast_ep{ep:03d}.mp3"

# ── Portfolio context (kept in sync with market.py) ───────────────────────────
PORTFOLIO_CONTEXT = """
PORTFOLIO SNAPSHOT (approximate, May 2026):
  TFSA:        ~$98K  | Contributed $44,500  | ROI ~+121%  | Tax-free growth
  Investment:  ~$94K  | Contributed $65,000  | ROI ~+45%   | 50% cap-gains inclusion
  FHSA:        ~$54K  | Contributed $24,000  | ROI ~+123%  | Double tax win (buy home)
  RRSP:        ~$28K  | Contributed $16,132  | ROI ~+72%   | Deferred tax growth
  TOTAL:       ~$274K | Total P&L ~+$124K    | Overall ROI ~+83%

KEY HOLDINGS:
  Leveraged ETFs ~49% of portfolio:
    FNGU  — Direxion 3x FANG+ (1,373 shares across TFSA/Investment/FHSA/RRSP)
    SPXL  — Direxion 3x S&P500 (151 shares across TFSA/Investment/FHSA)
    UDOW  — ProShares 3x Dow (206 shares across TFSA/FHSA/RRSP)

  Technology ~20%:
    NVDA  — 40sh TFSA, cost ~$16/sh split-adj → unrealized +1,776% (never sell)
    TXF.TO — CI Tech Giants Covered Call ETF (1,259 shares total)
    AVGO, MSFT, AAPL, QCOM, TSM, MSTR

  Canadian Financials ~10%:
    CM.TO (CIBC) — 95 shares total | RY.TO — 41 shares | BMO.TO — 15 shares

  Other ~21%:
    ENB.TO (Enbridge), TSLA, IBKR, V (Visa), ET (Energy Transfer),
    LYV (Live Nation), GBTC (Bitcoin proxy), BYDDF (BYD)

  Cash: ~$8,130 USD uninvested (RRSP $7,685 + TFSA $344 + Investment $50)

KEY SENSITIVITIES:
  - 49% leveraged ETFs → 3x amplification of S&P/NASDAQ/Dow moves
  - 68% USD exposure → each 1¢ CAD/USD move = ~$1,800 portfolio impact
  - NVDA is the best trade ever made — never a reason to sell
  - RRSP cash drag is the biggest opportunity cost right now
"""


# ── Groq helper ──────────────────────────────────────────────────────────────
def call_groq(prompt: str, max_tokens: int = 4096, temperature: float = 0.55) -> str:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set")
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ── Script generation ─────────────────────────────────────────────────────────
SCRIPT_PROMPT_TEMPLATE = """You are writing a podcast script for "Portfolio Pulse Weekly" — a sharp, personal finance podcast for a Canadian investor.

TODAY: {today}
MARKET MOOD THIS WEEK: {mood}

PORTFOLIO:
{portfolio}

THIS WEEK'S INTELLIGENCE:
Daily Outlook: {outlook}

Macro Themes:
{macro}

Market News:
{news}

Stocks to Watch:
{picks}

Portfolio Strengths:
{strengths}

Portfolio Concerns:
{concerns}

Short-Term Strategy (0–6 months):
{strategy}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCRIPT RULES — follow every single one:

1. TWO HOSTS ONLY:
   ALEX — the sharp portfolio analyst. Cites exact numbers. Gets excited about moves.
   SAM  — the smart investor asking exactly what the listener is wondering. Challenges Alex.

2. EVERY line must start with "ALEX:" or "SAM:" — zero exceptions. No stage directions.

3. NATURAL CONVERSATION — not bullet reading:
   Use: "Right, but here's the thing...", "Wait, hold on...", "That's what I was going to say...",
   "And the crazy part is...", "So what you're saying is...", "Exactly, and on top of that..."
   Interrupt each other. Build on each other's points. React to surprises.

4. TARGET: 1,800–2,100 words total. That's 12–14 minutes spoken.

5. DO NOT explain how TFSA/FHSA/RRSP accounts work — assume listener knows.
   DO NOT dwell on past performance beyond a quick context line.
   DO NOT repeat the same point twice.
   BE specific: say "FNGU is up 6.2% this week" not "FNGU performed well".

6. FORWARD-LOOKING: what matters NOW and what to watch NEXT WEEK.

STYLE EXAMPLE (format only — don't copy this content):
ALEX: So the headline number this week — FNGU is up 6.2%, which means the 3x FANG+ index had a monster run.
SAM: And FNGU is the biggest position, right? Like across all the accounts combined.
ALEX: Biggest single holding by a mile. About 1,373 shares total, split across TFSA, the investment account, FHSA and RRSP.
SAM: So when it moves 6%, that's not a small number in dollar terms.
ALEX: Not even close. We're talking roughly thirty-something thousand dollars of paper gain just this week.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EPISODE STRUCTURE:

[COLD OPEN — 30 seconds]
Hook with the single most interesting/surprising thing from this week. No intro, just dive in.

[PORTFOLIO SNAPSHOT — 2 minutes]
Quick performance check: total portfolio, biggest movers, daily changes this week.
What went up, what went down, any surprises.

[MARKET DEEP DIVE — 3 minutes]
The macro themes and news that directly hit these specific holdings.
Alex explains why it matters, Sam pushes for the "so what does that mean for me" angle.

[POSITION SPOTLIGHT — 3 minutes]
Pick 1–2 holdings worth discussing in depth this week based on what happened.
Could be a big winner, a concern, a new catalyst, or a strategic question.

[WATCH LIST — 2 minutes]
Specific upcoming catalysts, dates, signals to monitor.
Concrete: "watch for X on Y date because it directly affects Z holding".

[ACTION ITEMS — 2 minutes]
Concrete things to consider. Not vague hedges — real, specific actions.
Could be: deploy RRSP cash, trim a position, add to something, watch a level.

[SIGN OFF — 30 seconds]
Quick forward look for next week. Warm close.

Write the full script now:"""


def build_script_prompt(intel: dict) -> str:
    today   = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    mood    = intel.get("market_mood", "neutral").upper()
    outlook = intel.get("daily_outlook", "No outlook available.")
    macro   = "\n".join(
        f"• {m['title']} [{m.get('impact','?')}]: {m.get('body','')[:250]}"
        for m in intel.get("macro", [])[:4]
    )
    news    = "\n".join(
        f"• {n['headline']}: {n.get('body','')[:200]} | Exposure: {n.get('exposure','')[:100]}"
        for n in intel.get("news", [])[:5]
    )
    picks   = "\n".join(
        f"• {p['ticker']} — {p['action']} in {p.get('account','?')}: {p.get('thesis','')[:180]}"
        for p in intel.get("picks", [])[:4]
    )
    strengths = "\n".join(f"• {s['text'][:180]}" for s in intel.get("strengths", [])[:4])
    concerns  = "\n".join(f"• {c['text'][:180]}" for c in intel.get("concerns", [])[:4])
    strategy  = "\n".join(f"• {s['text'][:180]}" for s in intel.get("strategy_short", [])[:4])

    return SCRIPT_PROMPT_TEMPLATE.format(
        today=today, mood=mood, portfolio=PORTFOLIO_CONTEXT,
        outlook=outlook, macro=macro, news=news, picks=picks,
        strengths=strengths, concerns=concerns, strategy=strategy,
    )


def generate_script(intel: dict) -> str:
    print("1/3  Generating podcast script with Groq...")
    script = call_groq(build_script_prompt(intel), max_tokens=4096, temperature=0.6)
    # Validate it has enough turns
    turns = [l for l in script.split('\n') if l.strip().startswith(('ALEX:', 'SAM:'))]
    print(f"     → {len(turns)} speaker turns, ~{len(script.split()):,} words")
    if len(turns) < 15:
        print("     ⚠ Script seems sparse — retrying with higher temperature")
        script = call_groq(build_script_prompt(intel), max_tokens=4096, temperature=0.75)
    return script


# ── Summary generation ────────────────────────────────────────────────────────
def generate_summary(intel: dict) -> dict:
    print("2/3  Generating text summary...")
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")

    intel_compact = {
        k: intel[k]
        for k in ("market_mood","daily_outlook","macro","news","picks","strengths","concerns","strategy_short")
        if k in intel
    }

    prompt = f"""Based on this weekly portfolio intelligence briefing (date: {today}), write a structured summary for display on a dashboard.

INTELLIGENCE DATA:
{json.dumps(intel_compact, indent=2)[:3500]}

PORTFOLIO CONTEXT (abbreviated):
{PORTFOLIO_CONTEXT[:800]}

Return ONLY a valid JSON object with these exact keys — no markdown, no fences:
{{
  "episode_title": "Punchy 8-10 word title summarising the most important thing this week",
  "mood_summary": "One sharp sentence on market mood and what it means for this portfolio",
  "portfolio_snapshot": [
    "3-4 specific bullet points about portfolio performance — use dollar amounts and % where possible"
  ],
  "market_context": [
    "3-4 bullet points on market themes directly affecting these holdings — be specific about which tickers"
  ],
  "position_spotlight": [
    "2-3 bullet points on 1-2 specific holdings worth highlighting this week"
  ],
  "watch_list": [
    "3-4 items with specific tickers, upcoming catalysts or levels to watch"
  ],
  "action_items": [
    "3-4 concrete, specific actions — not vague. E.g. 'Deploy RRSP cash into ZSP.TO before next earnings'"
  ],
  "key_news": [
    "3-4 relevant news items with direct impact on specific holdings"
  ]
}}"""

    raw = call_groq(prompt, max_tokens=1200, temperature=0.4)
    if raw.startswith("```"):
        lines = raw.split("\n")
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        raw = "\n".join(lines[1:end])
    return json.loads(raw)


# ── Audio generation ──────────────────────────────────────────────────────────
def parse_script(script: str) -> list[tuple[str, str]]:
    """Return list of (speaker, text) tuples."""
    turns = []
    for line in script.strip().split("\n"):
        line = line.strip()
        if line.startswith("ALEX:"):
            text = line[5:].strip()
            if text:
                turns.append(("ALEX", text))
        elif line.startswith("SAM:"):
            text = line[4:].strip()
            if text:
                turns.append(("SAM", text))
    return turns


def split_long_text(text: str, max_chars: int = 480) -> list[str]:
    """Split a long turn at sentence boundaries to avoid TTS timeouts."""
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


async def synthesize_one(text: str, voice: str, path: str, retries: int = 3) -> None:
    import edge_tts
    for attempt in range(retries):
        try:
            comm = edge_tts.Communicate(text, voice, rate=SPEECH_RATE)
            await comm.save(path)
            return
        except Exception as exc:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))


async def generate_all_audio(turns: list[tuple[str, str]], tmp_dir: Path) -> list[str]:
    """Synthesize all turns, splitting long ones; return ordered list of mp3 paths."""
    paths: list[str] = []
    tasks: list = []

    for i, (speaker, text) in enumerate(turns):
        voice = VOICE_ALEX if speaker == "ALEX" else VOICE_SAM
        chunks = split_long_text(text)
        for j, chunk in enumerate(chunks):
            path = str(tmp_dir / f"seg_{i:04d}_{j:02d}.mp3")
            paths.append(path)
            tasks.append(synthesize_one(chunk, voice, path))

    # Process in batches of 8 to be polite to the free service
    batch = 8
    for start in range(0, len(tasks), batch):
        await asyncio.gather(*tasks[start:start + batch])
        if start + batch < len(tasks):
            await asyncio.sleep(0.3)

    return paths


def merge_audio(segment_paths: list[str], output: Path, _tmp_dir: Path) -> None:
    """Concatenate MP3 segments using pure Python — no ffmpeg needed.

    Browsers handle concatenated MP3 streams perfectly. edge-tts already
    adds natural trailing silence to each segment, so no explicit pause needed.
    """
    with open(output, "wb") as out:
        for path in segment_paths:
            with open(path, "rb") as seg:
                out.write(seg.read())


def audio_duration(path: Path) -> float:
    """Return audio duration in seconds using mutagen (pure Python)."""
    try:
        from mutagen.mp3 import MP3
        return MP3(str(path)).info.length
    except Exception:
        # Fallback: rough estimate from file size assuming ~24kbps
        try:
            return path.stat().st_size / (24_000 / 8)
        except Exception:
            return 0.0


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"  Portfolio Pulse — Weekly Podcast")
    print(f"  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    # Load intelligence
    if not INTEL_FILE.exists():
        print("ERROR: data/intelligence.json not found")
        return 1

    intel = json.loads(INTEL_FILE.read_text())
    print(f"  Intelligence from: {intel.get('generated_date','?')}\n")

    # ── Determine episode number and build archive from existing meta ─────────
    episode_num = 1
    archive: list[dict] = []
    if PODCAST_META.exists():
        try:
            old = json.loads(PODCAST_META.read_text())
            episode_num = old.get("episode", 0) + 1
            # If there was a real previous episode, push it onto the archive
            if old.get("episode", 0) > 0 and old.get("file"):
                prev_entry = {
                    "episode":      old["episode"],
                    "title":        old.get("title", ""),
                    "date":         old.get("date", ""),
                    "display_date": old.get("display_date", ""),
                    "duration":     old.get("duration", ""),
                    "mood":         old.get("mood", "neutral"),
                    "mood_summary": old.get("mood_summary", ""),
                    "file":         old["file"],
                    "summary":      old.get("summary", {}),
                }
                archive = [prev_entry] + old.get("archive", [])
        except Exception:
            pass

    # Keep only the last (MAX_EPISODES - 1) archive entries so total = MAX_EPISODES
    archive = archive[: MAX_EPISODES - 1]
    new_filename = f"podcast_ep{episode_num:03d}.mp3"
    output_mp3   = DATA_DIR / new_filename

    # ── Step 1: Script ───────────────────────────────────────────────────────
    script = generate_script(intel)
    turns  = parse_script(script)
    if not turns:
        print("ERROR: Could not parse any speaker turns from script")
        return 1

    # ── Step 2: Summary ──────────────────────────────────────────────────────
    try:
        summary = generate_summary(intel)
    except Exception as exc:
        print(f"  ⚠ Summary failed ({exc}) — using empty summary")
        summary = {}

    # ── Step 3: Audio ────────────────────────────────────────────────────────
    print(f"3/3  Synthesising audio ({len(turns)} turns)...")
    DATA_DIR.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        seg_paths = asyncio.run(generate_all_audio(turns, tmp_path))
        print(f"     → {len(seg_paths)} audio segments generated")
        merge_audio(seg_paths, output_mp3, tmp_path)

    secs    = audio_duration(output_mp3)
    dur_str = f"{int(secs//60)}:{int(secs%60):02d}"
    size_kb = output_mp3.stat().st_size // 1024
    print(f"     → {dur_str}  ({size_kb} KB)  saved to {output_mp3}")

    # ── Step 4: Prune old episode files ─────────────────────────────────────
    keep = {new_filename} | {a["file"] for a in archive if a.get("file")}
    for old_mp3 in DATA_DIR.glob("podcast_ep*.mp3"):
        if old_mp3.name not in keep:
            old_mp3.unlink()
            print(f"     → Deleted old episode: {old_mp3.name}")

    # ── Step 5: Metadata ─────────────────────────────────────────────────────
    meta = {
        "episode":          episode_num,
        "file":             new_filename,
        "date":             now.strftime("%Y-%m-%d"),
        "display_date":     now.strftime("%B %d, %Y"),
        "title":            summary.get("episode_title", f"Week of {now.strftime('%B %d')}"),
        "mood":             intel.get("market_mood", "neutral"),
        "mood_summary":     summary.get("mood_summary", ""),
        "duration":         dur_str,
        "duration_seconds": int(secs),
        "generated_at":     now.isoformat(),
        "archive":          archive,
        "summary":          summary,
    }
    PODCAST_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(f"\n  ✓ Episode #{episode_num}: \"{meta['title']}\" ({dur_str})")
    print(f"  ✓ Archive: {len(archive)} previous episode(s) kept\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
