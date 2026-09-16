"""
Podcast figure integrity — scripts/generate_podcast.py

Two defects let four consecutive episodes state portfolio figures that were
wrong, twice describing a losing week as a strong gain:

  A. _build_portfolio_context used sorted_dates[0] — the OLDEST retained
     snapshot, up to 90 days back — as the "weekly" baseline. On 2026-09-14 it
     compared against 2026-06-19, an 87-day span.
  B. The model cited numbers absent from its context entirely. ep016 opened
     with "+$23,927, an 8.6% weekly gain"; neither figure appeared in the
     context, and the real week was -$7,366.

This exercises the real shipped functions, and its regression cases are the
four actual published transcripts rather than invented ones.

Run: python3 tests/test_podcast_figures.py
"""
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
SRC = os.path.join(ROOT, 'scripts', 'generate_podcast.py')

_src = open(SRC).read()
_tree = ast.parse(_src)
_lines = _src.splitlines()

# Import the real implementations without importing the module (its import-time
# work needs network and credentials).
NS = {
    "re": re, "datetime": dt.datetime, "timedelta": dt.timedelta,
    "_LEVERAGE_3X": {"FNGU", "SPXL", "UDOW", "TQQQ", "SOXL"},
    "_PORTFOLIO_CONTEXT_FALLBACK": "(fallback)",
}
_WANT_FN = ("_week_baseline_date", "_build_portfolio_context",
            "_claim_value", "_close", "verify_script_figures", "_stitch_parts",
            "_choose_education_topic", "_fix_weekday_claims", "_clean_transcript",
            "_speaker_run_warnings")
_WANT_CONST = ("WEEK_BASELINE_DAYS", "_CLAIM_MONEY", "_CLAIM_PCT",
               "_PORTFOLIO_CLAIM", "_POSITION_SCOPED", "_HYPOTHETICAL",
               "_MONEY_RANGE", "_SIZE_PHRASE", "_PART1_SIGNOFF", "_PART2_REOPEN",
               "_SECTORS", "EDUCATION_TOPICS", "_MONTHS", "_WEEKDAY_CLAIM",
               "SCRIPT_PROMPT_PART1", "SCRIPT_PROMPT_PART2",
               "MONEY_TOLERANCE_PCT", "MONEY_TOLERANCE_ABS", "PCT_TOLERANCE")

for _node in _tree.body:
    if isinstance(_node, ast.Assign):
        for _t in _node.targets:
            if isinstance(_t, ast.Name) and _t.id in _WANT_CONST:
                exec("\n".join(_lines[_node.lineno - 1:_node.end_lineno]), NS)
    elif isinstance(_node, ast.FunctionDef) and _node.name in _WANT_FN:
        exec("\n".join(_lines[_node.lineno - 1:_node.end_lineno]), NS)

missing = [n for n in _WANT_FN + _WANT_CONST if n not in NS]
if missing:
    print(f"FAIL  could not extract from source: {missing}")
    sys.exit(1)

week_baseline = NS["_week_baseline_date"]
build_ctx = NS["_build_portfolio_context"]
verify = NS["verify_script_figures"]
week_fix = NS["_fix_weekday_claims"]
clean_txt = NS["_clean_transcript"]
choose_topic = NS["_choose_education_topic"]
speaker_runs = NS["_speaker_run_warnings"]

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


# ── Defect A: baseline selection ─────────────────────────────────────────────
print("── _week_baseline_date ──")

daily = [(dt.date(2026, 9, 14) - dt.timedelta(days=i)).isoformat() for i in range(90)][::-1]
ck("picks exactly 7 days back when present", week_baseline(daily), "2026-09-07")

# Weekday-only snapshots: Sep 5/6 are a weekend, so the nearest is Sep 4 or 7.
weekdays = [d for d in daily
            if dt.date.fromisoformat(d).weekday() < 5]
ck("weekday-only history still lands within a day of target",
   abs((dt.date.fromisoformat(week_baseline(weekdays)) - dt.date(2026, 9, 7)).days) <= 1, True)

ck("never returns the latest snapshot as its own baseline",
   week_baseline(["2026-09-13", "2026-09-14"]) != "2026-09-14", True)
ck("single snapshot degrades safely", week_baseline(["2026-09-14"]), "2026-09-14")
ck("does NOT use the oldest snapshot (the original bug)",
   week_baseline(daily) != daily[0], True)

# ── Defect B: figure verification against the real transcripts ───────────────
print("\n── verify_script_figures against published episodes ──")


def transcript(ep):
    r = subprocess.run(["git", "show", f"origin/main:data/podcast_ep{ep}.txt"],
                       cwd=ROOT, capture_output=True, text=True)
    return r.stdout


def facts_for(run_date):
    """Rebuild the context facts as they stood on a past run date."""
    snaps = json.load(urllib.request.urlopen(
        "https://portfolio-pulse-dun.vercel.app/api/snapshot", timeout=45))["snapshots"]
    hold = json.load(urllib.request.urlopen(
        "https://portfolio-pulse-dun.vercel.app/api/settings", timeout=30))["computed_holdings"]
    cut = (dt.date.fromisoformat(run_date) - dt.timedelta(days=90)).isoformat()
    window = {k: v for k, v in snaps.items() if cut <= k <= run_date}
    _, f = build_ctx(hold, window)
    return f


try:
    # As-of dates are the FRIDAY before each Monday episode. The workflow fires
    # Monday 06:2x UTC, before that Monday's snapshot is written, so Friday is
    # the latest data the context ever saw. Dating these to the Monday made
    # ep014 look sound; against the context it really had, it claimed +$17,427
    # on an actual +$8,591. All four episodes overstate — none is a clean
    # control, so the sound-script cases below are synthetic by necessity.
    CASES = [("013", "2026-08-21", True), ("014", "2026-08-28", True),
             ("015", "2026-09-04", True), ("016", "2026-09-11", True)]
    for ep, run, should_flag in CASES:
        txt = transcript(ep)
        if not txt:
            print(f"  SKIP  ep{ep} transcript unavailable")
            continue
        f = facts_for(run)
        problems = verify(txt, f)
        label = "flags bad figures" if should_flag else "accepts a sound script"
        ck(f"ep{ep} ({run}) {label}", bool(problems), should_flag)
        # On an unexpected result show everything: one truncated line once made
        # three false positives look like a single tolerance miss.
        for p in (problems if bool(problems) != should_flag else problems[:1]):
            print(f"        -> {p[:150]}")
        print(f"        context now reports {f['wk_gain']:+,} CAD over "
              f"{f['span_days']} days ({f['baseline_date']} → {f['latest_date']})")
except urllib.error.URLError as exc:
    print(f"  SKIP  regression cases need network: {exc}")

# ── The guard must not fire on a clean, context-faithful script ──────────────
print("\n── no false positives ──")
clean_facts = {
    "total_value": 298282, "wk_gain": -7366, "wk_pct": -2.4, "roi_pct": 68.3,
    "span_days": 7, "baseline_date": "2026-09-07", "latest_date": "2026-09-14",
    "leverage_cad": 149326, "usd_exposure": 225533,
    "accounts": {"TFSA": 97617, "Investment": 118454, "FHSA": 53642, "RRSP": 28568},
    "account_change": {"TFSA": -1665, "Investment": -2743, "FHSA": -1322, "RRSP": -1637},
    "movers": [{"ticker": "FNGU", "pct": -1.6, "cad": -2903}],
}
ck("a script quoting the context exactly is accepted",
   verify("ALEX: The portfolio fell $7,366 this week, down 2.4%.", clean_facts), [])
ck("derived per-holding maths is not policed",
   verify("ALEX: A 1% S&P move is about $4,480 on the leveraged sleeve.", clean_facts), [])
# The three sentences that wrongly blocked episode 14 and friends.
ck("a figure scoped to one holding is not policed",
   verify("ALEX: Our Enbridge position, which sits at roughly $12,000 CAD, "
          "lifted the portfolio.", clean_facts), [])
ck("an FX sensitivity is not policed",
   verify("ALEX: For every 0.01 CAD move in USD/CAD the portfolio swings $2,325 CAD.",
          clean_facts), [])
ck("a projected band is not policed",
   verify("ALEX: In that environment the portfolio drifts up $7,000 - $9,000 CAD.",
          clean_facts), [])
ck("a forecast does not trip the direction check in a down week",
   verify("ALEX: If oil rallies from here, the portfolio could climb next week.",
          clean_facts), [])
ck("a fabricated portfolio gain is caught",
   bool(verify("ALEX: The portfolio jumped $23,927 this week, an 8.6% gain.", clean_facts)),
   True)
ck("calling a losing week a gain is caught",
   bool(verify("ALEX: The portfolio gained ground this week.", clean_facts)), True)

# ── position sizes and the account a holding sits in ─────────────────────────
print("\n── position-level claims ──")
pos_facts = dict(clean_facts)
pos_facts["positions"] = {
    "ET":     {"name": "Energy Transfer", "cad": 1791,  "accounts": ["TFSA"]},
    "NVDA":   {"name": "NVIDIA",          "cad": 11733, "accounts": ["TFSA"]},
    "ENB.TO": {"name": "Enbridge",        "cad": 5496,  "accounts": ["FHSA"]},
    "SHEL":   {"name": "Shell PLC",       "cad": 2951,  "accounts": ["TFSA"]},
}
ck("an overstated position is caught (the real ep016 claim)",
   bool(verify("ALEX: Right now we hold about $14,000 CAD of Energy Transfer.", pos_facts)),
   True)
ck("Nvidia overstated at $30,000 is caught",
   bool(verify("ALEX: Nvidia sits in the TFSA at about $30,000 CAD.", pos_facts)), True)
ck("the wrong account is caught",
   bool(verify("ALEX: We hold $1,800 CAD of Energy Transfer in the Investment account.",
               pos_facts)), True)
ck("a correct position size passes",
   verify("ALEX: Nvidia sits in the TFSA at about $11,700 CAD.", pos_facts), [])
ck("a combined figure is not attributed to one holding",
   verify("ALEX: Energy Transfer and Shell together represent roughly $4,700 CAD.",
          pos_facts), [])
ck("a hypothetical position figure is not policed",
   verify("ALEX: If we doubled it, Energy Transfer would be worth $3,600 CAD.", pos_facts), [])
# A holding, a size word and a figure can co-occur without a size being claimed.
# Flagging this would block a sound episode, which is how a guard gets disabled.
ck("an incidental figure near a size word is not policed",
   verify("ALEX: Nvidia's position in the AI market is worth watching, and a 1% move "
          "is about $1,200 CAD.", pos_facts), [])
ck("a size claim still caught when the figure follows the phrase closely",
   bool(verify("ALEX: Enbridge sits at roughly $12,000 CAD today.", pos_facts)), True)

# ── the Part 1 / Part 2 seam ─────────────────────────────────────────────────
# Reproduces episode 16 lines 55-59 exactly: Part 1 signed off, a separator was
# emitted, and Part 2 re-opened claiming the hosts had "just walked through" a
# topic that was one bullet in a list of upcoming dates.
print("\n── the Part 1 / Part 2 seam ──")
stitch = NS["_stitch_parts"]
_p1 = ("ALEX: Nvidia's move came from the AI capex guide.\n"
       "SAM: That's the mechanism.\n"
       "ALEX: That's the play for the next few weeks. Stay tuned, and we'll reconvene next Monday.")
_p2 = ("---\n"
       "ALEX: All right, let's pick up where we left off. We just walked through the "
       "Bank of Canada's upcoming rate call.\n"
       "SAM: So what's the next piece?\n"
       "ALEX: Energy Transfer is the one to watch.")
_out = stitch(_p1, _p2)
ck("Part 1's sign-off is removed", "Stay tuned" in _out, False)
ck("Part 2's false recap is removed", "walked through" in _out, False)
ck("the stray separator is removed", "---" in _out, False)
ck("real content on both sides survives",
   ("AI capex guide" in _out and "Energy Transfer is the one to watch" in _out), True)
ck("a clean join is left untouched",
   stitch("ALEX: One.\nSAM: Two.", "ALEX: Three.\nSAM: Four."),
   "ALEX: One.\nSAM: Two.\n\nALEX: Three.\nSAM: Four.")
ck("trimming is bounded — it cannot eat a whole half",
   len(stitch("ALEX: Stay tuned.\nSAM: Stay tuned.\nALEX: Stay tuned.\nSAM: Real point here.",
              "ALEX: Opening line.").split("\n")) >= 3, True)

# ── the degraded path must honour the (text, facts) contract ─────────────────
# This branch only runs when KV holdings or snapshots are unavailable, so
# nothing exercised it: returning a bare string here crashed the whole run on
# exactly the week the data was already degraded.
print("\n── price-less (intraday) snapshots ──")
# The snapshot written before the close job carries account totals but no
# prices. Used as `latest` it zeroes leverage, USD exposure, movers and
# positions while total_value still reads correctly — episode 16 told the
# listener three times that the book held no leveraged exposure at all.
_thin_hist = {
    "2026-09-11": {"total_value": 5000, "roi_pct": 70.0, "usdcad": 1.39,
                   "accounts": {"TFSA": 5000},
                   "holdings_prices": {"FNGU": {"price": 32.0}}},
    "2026-09-16": {"total_value": 5200, "accounts": {"TFSA": 5200}},  # no prices
}
_h = [{"ticker": "FNGU", "shares": 100, "ccy": "USD",
       "account": "TFSA", "name": "FANG+ 3x ETF"}]
_txt, _f = build_ctx(_h, _thin_hist)
ck("a price-less snapshot is not chosen as the latest",
   _f.get("latest_date"), "2026-09-11")
ck("leverage is not zeroed by the price-less snapshot",
   _f.get("leverage_cad", 0) > 0, True)
ck("all-thin history degrades to the fallback contract",
   isinstance(build_ctx(_h, {"2026-09-16": _thin_hist["2026-09-16"]}), tuple), True)

print("\n── missing-data fallback ──")
for label, h, s in [("no holdings", [], {"2026-09-14": {}}),
                    ("no snapshots", [{"ticker": "FNGU"}], {}),
                    ("neither", [], {})]:
    out = build_ctx(h, s)
    ck(f"{label}: returns a 2-tuple, not a bare string",
       isinstance(out, tuple) and len(out) == 2, True)
    if isinstance(out, tuple) and len(out) == 2:
        ck(f"{label}: guard no-ops rather than validating against nothing",
           verify("ALEX: The portfolio jumped $23,927 this week.", out[1]), [])

# ── the prompt templates must accept exactly what generate_script passes ─────
# A placeholder the call does not supply, or a kwarg the template dropped, is a
# KeyError at generation time — an entire week with no episode, discovered only
# on the Monday. Nothing else in the suite would catch it.
print("\n── prompt format contract ──")
_K1 = dict(today="T", week_range="W", mood="M", registry_context="R",
           ticker_rotation="TR", education_topic="ET", live_portfolio="LP",
           outlook="O", macro="MA", news="N", portfolio="P")
_K2 = dict(today="T", dive1_summary="D", ticker_rotation="TR", education_topic="ET",
           education_topics_used="EU", picks="PK", strengths="S", concerns="C",
           strategy="ST", news="N", portfolio="P")
for _label, _tpl, _kw in (("Part 1", NS["SCRIPT_PROMPT_PART1"], _K1),
                          ("Part 2", NS["SCRIPT_PROMPT_PART2"], _K2)):
    _ph = set(re.findall(r"\{(\w+)\}", _tpl))
    ck(f"{_label}: every placeholder is supplied", sorted(_ph - set(_kw)), [])
    ck(f"{_label}: every argument is used", sorted(set(_kw) - _ph), [])
    try:
        _tpl.format(**_kw)
        _res = "ok"
    except Exception as exc:
        _res = f"{type(exc).__name__}: {exc}"
    ck(f"{_label}: formats without error", _res, "ok")
ck("both halves are told the same learning topic",
   "{education_topic}" in NS["SCRIPT_PROMPT_PART1"]
   and "{education_topic}" in NS["SCRIPT_PROMPT_PART2"], True)

# ── weekday claims are arithmetic, so they are corrected ─────────────────────
print("\n── weekday repair ──")
_ref = dt.datetime(2026, 9, 14)          # a Monday; the 18th is a Friday
_s, _f = week_fix("ALEX: the EIA report due Wednesday, September 18 matters.", _ref)
ck("a wrong weekday is corrected", "Friday, September 18" in _s, True)
ck("the correction is reported", len(_f), 1)
ck("a correct weekday is left alone",
   week_fix("ALEX: the report on Friday, September 18.", _ref)[1], [])
ck("prose with no date is untouched",
   week_fix("ALEX: nothing dated here.", _ref)[1], [])
ck("an impossible date is not mangled",
   week_fix("ALEX: due Monday, February 30.", _ref)[1], [])

# ── the saved transcript carries only what was spoken ───────────────────────
print("\n── transcript cleaning ──")
_raw = ("**PORTFOLIO RECAP**\n\n---\nALEX: Real line.\n"
        "**Metaphor** - a seesaw.\n[EDUCATION_TOPIC: Short Interest Signals]\n"
        "SAM: Another line.\n")
_clean = clean_txt(_raw)
ck("section headers are dropped", "PORTFOLIO RECAP" in _clean, False)
ck("horizontal rules are dropped", "---" in _clean, False)
ck("the stray metaphor label is dropped", "Metaphor" in _clean, False)
ck("the education marker survives", "[EDUCATION_TOPIC:" in _clean, True)
ck("spoken turns survive",
   "ALEX: Real line." in _clean and "SAM: Another line." in _clean, True)

# ── the learning topic is fixed before either half is written ───────────────
print("\n── education topic selection ──")
ck("a fresh run takes the first topic", choose_topic([]), NS["EDUCATION_TOPICS"][0])
ck("a used topic is skipped",
   choose_topic([NS["EDUCATION_TOPICS"][0]]) != NS["EDUCATION_TOPICS"][0], True)
ck("matching is case-insensitive",
   choose_topic([NS["EDUCATION_TOPICS"][0].upper()]) != NS["EDUCATION_TOPICS"][0], True)
ck("an exhausted list still returns a topic",
   choose_topic(NS["EDUCATION_TOPICS"]) in NS["EDUCATION_TOPICS"], True)

# ── speaker runs are reported, never rewritten ──────────────────────────────
print("\n── speaker runs ──")
ck("three turns in a row is flagged",
   len(speaker_runs("ALEX: a\nALEX: b\nALEX: c\nSAM: d")), 1)
ck("alternating dialogue is silent", speaker_runs("ALEX: a\nSAM: b\nALEX: c"), [])

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
