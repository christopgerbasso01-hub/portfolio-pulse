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
            "_claim_value", "_close", "verify_script_figures")
_WANT_CONST = ("WEEK_BASELINE_DAYS", "_CLAIM_MONEY", "_CLAIM_PCT",
               "_PORTFOLIO_CLAIM", "_POSITION_SCOPED", "_HYPOTHETICAL",
               "_MONEY_RANGE",
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
    CASES = [("013", "2026-08-24", True), ("014", "2026-08-31", False),
             ("015", "2026-09-07", True), ("016", "2026-09-14", True)]
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

# ── the degraded path must honour the (text, facts) contract ─────────────────
# This branch only runs when KV holdings or snapshots are unavailable, so
# nothing exercised it: returning a bare string here crashed the whole run on
# exactly the week the data was already degraded.
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

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
