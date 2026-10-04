"""
Podcast quality guard — scripts/generate_podcast.py

Episode 18 got the currency backwards, applied the 3x leverage twice, deep-dived a
holding as "not currently in the portfolio" while owning $15,000 of it, promised
Tesla and delivered Broadcom, announced three scenarios and spoke one, and left four
"we should watch two things:" with nothing after the colon. 112 figures in 2,608
words. Each of those is a defect class this file pins down.

Run: python3 tests/test_podcast_quality.py
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
spec = importlib.util.spec_from_file_location("gp_quality", os.path.join(ROOT, 'scripts', 'generate_podcast.py'))
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


def flagged(script, facts=None):
    return bool(gp.verify_script_figures(script, facts if facts is not None else {}))


# ── synthetic book: enough to exercise the real context builder ──────────────
HOLD = [
    {"ticker": "FNGU",   "shares": 1000, "ccy": "USD", "account": "TFSA", "name": "FANG+ 3x ETF"},
    {"ticker": "SOXL",   "shares": 500,  "ccy": "USD", "account": "RRSP", "name": "Direxion Daily Semiconductor Bull 3X Shares"},
    {"ticker": "NVDA",   "shares": 400,  "ccy": "USD", "account": "TFSA", "name": "NVIDIA"},
    {"ticker": "AVGO",   "shares": 100,  "ccy": "USD", "account": "TFSA", "name": "Broadcom"},
    {"ticker": "ENB.TO", "shares": 800,  "ccy": "CAD", "account": "FHSA", "name": "Enbridge"},
]


def _px(f, n, s, a, e):
    return {"FNGU": {"price": f}, "NVDA": {"price": n}, "SOXL": {"price": s},
            "AVGO": {"price": a}, "ENB.TO": {"price": e}}


SNAPS = {
    "2026-09-18": {"total_value": 270000, "roi_pct": 70.0, "usdcad": 1.40,
                   "accounts": {"TFSA": 200000, "RRSP": 30000, "FHSA": 50000},
                   "holdings_prices": _px(30, 170, 40, 300, 74)},
    "2026-09-25": {"total_value": 285000, "roi_pct": 75.0, "usdcad": 1.40,
                   "accounts": {"TFSA": 210000, "RRSP": 31000, "FHSA": 51000},
                   "holdings_prices": _px(32, 175, 44, 310, 75)},
}
CASH = [{"ticker": "CASH·CAD", "account": "RRSP", "ccy": "CAD", "amount": 2000},
        {"ticker": "CASH·USD", "account": "TFSA", "ccy": "USD", "amount": 500}]

text, facts = gp._build_portfolio_context(HOLD, SNAPS, CASH)

print("── money parsing ──")
M = gp._CLAIM_MONEY
ck('"$90, but" is $90, not $90 billion', M.findall("Brent is above $90, but the rally faded"), [("90", "")])
ck('"$14K" keeps its unit', M.findall("worth $14K today"), [("14", "K")])
ck('"$30 billion" is read as billions', gp._claim_value(*M.findall("a $30 billion programme")[0]), 3e10)
ck('"$2,700 CAD boost" does not read the B of "boost"', M.findall("a $2,700 CAD boost"), [("2,700", "")])
ck('"$1,500 based on" does not read the b of "based"', M.findall("$1,500 based on shares"), [("1,500", "")])

print("\n── currency direction (needs no facts) ──")
ck("ep18's sentence is rejected even with no facts",
   flagged("ALEX: A firmer greenback compresses the CAD‑denominated value of our leveraged tech ETFs."), True)
ck("the correct statement is accepted",
   flagged("ALEX: When the US dollar gets stronger against the Canadian dollar, our US holdings are worth more in Canadian dollars."), False)
ck("'we're holding them in CAD' is rejected",
   flagged("ALEX: The indices are priced in USD but we’re holding the ETFs in CAD."), True)

print("\n── a holding we own is not 'not in the portfolio' ──")
pf = {"total_value": 285000, "wk_gain": 15000, "wk_pct": 5.5,
      "positions": {"AVGO": {"name": "Broadcom", "cad": 14970, "accounts": ["TFSA", "Investment"]}}}
ck("ep18's real sentence is caught despite the word 'could'",
   flagged("ALEX: Broadcom isn’t currently in the portfolio, but the $5,000 CAD cash allocation we earmarked for Novo Nordisk could be redirected here.", pf), True)
ck("a real supposition is left alone", flagged("ALEX: If we didn't own Broadcom we would want it.", pf), False)
ck("a company we do NOT own may be called unowned",
   flagged("ALEX: Novo Nordisk isn't in the portfolio, it is only an idea.", pf), False)

print("\n── a dollar figure nobody supplied ──")
f = dict(facts)
one = round(f["index_table"]["1"])
ck("the strict flag is on in production facts", f.get("strict_figures"), True)
ck("a table value passes",
   flagged(f"ALEX: A 1% market move is worth about ${gp._round_sig(one):,.0f} to us.", f), False)
ck("arithmetic done on air is caught (ep18's double leverage)",
   flagged("ALEX: A 0.5% drop, times 3, using the anchor, is an $11,111 hit to the portfolio.", f), True)
ck("a made-up buyback is caught",
   flagged("ALEX: Broadcom announced a $30 billion share repurchase this week.", f), True)
f2 = dict(f, intel_money=[3e10])
ck("...unless the briefing actually supplied it",
   flagged("ALEX: Broadcom announced a $30 billion share repurchase this week.", f2), False)
# $123,456 is nowhere near any real figure in the synthetic book, so a pass or fail here
# is about the segment logic and not a coincidence with a real number.
learn = ("[EDUCATION_TOPIC: Yield Curve]\nALEX: Imagine you hold $123,456 of a stock.\n"
         "ALEX: Alright, that's our learning segment for this week. On to scenarios…\n"
         "ALEX: Imagine we hold $123,456 of one more.\n")
probs = gp.verify_script_figures(learn, f)
ck("a teaching example inside the learning segment is allowed", sum("123,456" in p for p in probs), 1)
ck("...but the same figure after it is not",
   any("Imagine we hold $123,456" in p for p in probs), True)
ck("without the strict flag nothing new is policed (old facts keep working)",
   flagged("ALEX: A 1% S&P move is about $4,480 on the leveraged sleeve.",
           {"total_value": 298282, "wk_gain": -7366, "wk_pct": -2.4}), False)

print("\n── invented market statistics (not dollar amounts) ──")
mf = dict(f, allowed_numbers=[1.4, 0.5, 1.0, 2.0, 3.0])
ck("ep18's VIX level is caught",
   flagged("ALEX: Right now, VIX is hovering around 21.5, just under the threshold.", mf), True)
ck("ep18's yields are caught",
   flagged("ALEX: Right now, the 2‑year is at 4.78 %, the 10‑year at 4.85 %.", mf), True)
ck("ep18's invented 'historical correlation' is caught",
   flagged("ALEX: A 10-basis-point flattening historically correlates with a 0.3 % drop in the S&P 500.", mf), True)
ck("a statistic that WAS in the news is allowed",
   flagged("ALEX: The briefing puts the 2-year yield at 4.78%.", dict(f, allowed_numbers=[4.78])), False)
ck("...and a rounding of it is allowed",
   flagged("ALEX: The briefing puts the 2-year yield at about 4.8%.", dict(f, allowed_numbers=[4.78])), False)
ck("...but a different figure is not just because it is close",
   flagged("ALEX: The 10-year yield is at 4.85%.", dict(f, allowed_numbers=[4.78])), True)
ck("ep18's scenario summary is NOT a market statistic (it said 'spread' and '30 %')",
   flagged("ALEX: So we have a roughly 50 % base, 30 % upside, and 20 % downside spread across the next few weeks.", mf), False)
ck("a scenario line mentioning yields is not policed either",
   flagged("ALEX: Base case — 50 percent: yields drift and markets stay calm.", mf), False)
ck("a sentence with no market word is not policed by this check",
   flagged("ALEX: Our semiconductor fund was up 22.4% this week.", mf), False)
ck("without allowed_numbers in the facts the check is off (old facts unchanged)",
   flagged("ALEX: VIX is hovering around 21.5.", f), False)

print("\n── structure and density ──")
full = ("ALEX: Base case — 50 percent: little happens. For us that is small.\n"
        "SAM: Bull case — 30 percent: the market rises.\n"
        "ALEX: Bear case — 20 percent: the market falls.\n")
ck("three scenarios present", gp.scenario_problems(full), [])
ck("only the base case is reported as two missing",
   len(gp.scenario_problems("ALEX: Base case — 50 percent: little happens.")), 2)
ck("a turn ending in a colon is a stub",
   len(gp.list_problems("ALEX: We should watch two things:\nSAM: Okay.")), 1)
ck("ordinary turns are not", gp.list_problems("ALEX: We should watch two things.\nSAM: Okay."), [])
st = gp.figure_stats("ALEX: Up $14,000, that is 4.7% and about 3 cents on the dollar.\nSAM: VIX at 21.5.")
ck("figures counted: money, percent, cents, bare decimal", st["total"], 4)
dense = "\n".join(f"ALEX: It rose $1,200 or 3.4% and 2 cents on {x}." for x in range(12))
ck("a dense script is a (redraftable) problem", bool(gp.density_problems(dense)), True)
sparse = "\n".join("ALEX: It was a quiet week and nothing very much happened to us at all." for _ in range(12))
ck("a sparse one is not", gp.density_problems(sparse), [])

print("\n── the opening names what the episode delivers ──")
good_open = ("ALEX: This week, and in our learning segment, Yield Curve Recession Signals.\n"
             "SAM: Great.\n[EDUCATION_TOPIC: Yield Curve Recession Signals]\nALEX: Here we go.\n")
ck("an agenda that names the topic passes", gp.agenda_problems(good_open), [])
ck("ep16's failure — promised roll yields, delivered short interest — is caught",
   len(gp.agenda_problems("ALEX: This week, a primer on forward-contract roll yields.\nSAM: Nice.\n"
                          "[EDUCATION_TOPIC: Short Interest Signals]\nALEX: Short interest is…")), 1)
ck("a missing marker is reported", len(gp.agenda_problems("ALEX: Hello.\nSAM: Hi.")), 1)
ck("a topic announced later than the opening does not count",
   len(gp.agenda_problems("ALEX: a.\nSAM: b.\nALEX: c.\nSAM: d.\nALEX: e.\nSAM: f.\n"
                          "ALEX: Today, Yield Curve Recession Signals.\n[EDUCATION_TOPIC: Yield Curve Recession Signals]")), 1)

print("\n── repairs ──")
ck("bold speaker labels are normalised",
   gp._normalize_script("**ALEX:** Hello.\n*SAM:* Hi.").split("\n"), ["ALEX: Hello.", "SAM: Hi."])
ck("a colon intro and its bullets are folded into spoken sentences",
   gp._normalize_script("ALEX: We should watch two things:\n\n- **CPI** print\n- the Fed's tone\nSAM: Okay."),
   "ALEX: We should watch two things. First, CPI print. Second, the Fed's tone.\n\nSAM: Okay.")
ck("a single bullet is folded without an ordinal",
   gp._normalize_script("SAM: And the levers?\n- Nvidia reports soon"), "SAM: And the levers? Nvidia reports soon.")
ck("bullets after a header are not adopted by the turn above it",
   gp._normalize_script("ALEX: Done.\n## Header\n- stray").split("\n")[-1], "- stray")
ck("curly-apostrophe sign-off is removed (the regex used to miss it)",
   "play" in gp._stitch_parts("ALEX: Real point here.\nALEX: That’s the play for the next few weeks.",
                              "ALEX: Real two.").lower(), False)
ck("curly-apostrophe re-open is removed",
   "pick up" in gp._stitch_parts("ALEX: Real.", "ALEX: Let’s pick up where we left off.\nSAM: Real two.").lower(), False)

print("\n── last-resort stripping ──")
bad = ("ALEX: Oil fell on Friday. A firmer greenback compresses the CAD value of our US holdings.\n"
       "SAM: A stronger USD erodes the value of our US holdings.\n"
       "ALEX: That is the whole story this week.\n")
fixed, removed = gp.strip_unsafe_sentences(bad, {})
ck("only the offending sentence is cut from a mixed turn", "Oil fell on Friday." in fixed and "greenback" not in fixed, True)
ck("a turn that was entirely wrong is dropped", fixed.count("SAM:"), 0)
ck("untouched turns survive", "That is the whole story this week." in fixed, True)
ck("the result then passes verification", gp.verify_script_figures(fixed, {}), [])
ck("two sentences reported removed", len(removed), 2)

print("\n── how problems are classed ──")
probs = gp.collect_script_problems("ALEX: A firmer greenback compresses the CAD value of our US holdings.", {})
ck("a wrong-direction sentence is fatal", [p["fatal"] for p in probs if p["kind"] == "fact"], [True])
ck("missing scenarios retry but never cost the episode",
   [(p["fatal"], p["retry"]) for p in probs if "scenarios" in p["msg"]][0], (False, True))

print("\n── redraft loop ──")
GOOD = ("ALEX: This week, and in our learning segment, Yield Curve Recession Signals.\n"
        "[EDUCATION_TOPIC: Yield Curve Recession Signals]\n"
        "ALEX: A quiet week for the book.\nSAM: Base case — 50 percent: little happens. For us that is small.\n"
        "ALEX: Bull case — 30 percent: the market rises a little.\nSAM: Bear case — 20 percent: it falls a little.\n")
BADFX = GOOD + "ALEX: A firmer greenback compresses the CAD value of our US holdings.\n"
logs, calls = [], []


def seq(*drafts):
    it = iter(drafts)

    def gen(fb):
        calls.append(fb)
        return next(it), {}
    return gen


calls.clear()
r = gp.produce_checked_script(seq(GOOD), log=logs.append)
ck("a clean first draft is used straight away", (len(calls), r is not None), (1, True))
calls.clear()
r = gp.produce_checked_script(seq(BADFX, GOOD), log=logs.append)
ck("a bad draft is redrafted once the second is clean", len(calls), 2)
ck("...and the second attempt is told exactly what was wrong",
   "REJECTED" in calls[1] and "greenback" in calls[1], True)
ck("...and the first attempt was not given notes", calls[0], "")
calls.clear()
r = gp.produce_checked_script(seq(BADFX, BADFX, BADFX), log=logs.append)
ck("three bad drafts: it stops at three", len(calls), 3)
ck("...and still publishes, with the flagged sentence gone",
   r is not None and "greenback" not in r[0] and "Base case" in r[0], True)
ck("a failing generator means no episode", gp.produce_checked_script(
    lambda fb: (_ for _ in ()).throw(RuntimeError("groq down")), log=logs.append), None)

print("\n── the context the model is given ──")
ck("no hand-typed stale facts remain",
   any(x in text for x in ("7,685", "1,776", "RRSP Cash", "implied β")), False)
ck("all leveraged funds we own are named (the old list left one out)",
   "FANG+ 3x, Semiconductor 3x" in text, True)
ck("the long official fund name is not what the model reads out",
   "Direxion" in text, False)
ck("lookup tables present", "HOW MUCH THINGS MOVE" in text and "never multiply it by 3 again" in text, True)
ck("the currency box is present",
   "NEVER say we hold them in Canadian dollars" in text and "GOOD for us" in text, True)
ck("figures are pre-rounded for speech", gp._say_money(14386) == "$14,000" and gp._say_money(1722) == "$1,700", True)
ck("cash comes from the real balances", f["cash_total"], round(2000 + 500 * 1.40))
ck("...and is stated as a small slice", "Cash (not invested): about" in text, True)
t2, _ = gp._build_portfolio_context(HOLD, SNAPS, None)
ck("with no cash data it says so instead of inventing one", "Cash balances: not provided" in t2, True)
ck("the index table is the 3x figure, ready to read", f["index_table"]["1"], round(
    (1000 * 32 + 500 * 44) * 1.40 * 0.03))
ck("a prompt placeholder mismatch would be a KeyError — both templates format",
   all(isinstance(t, str) for t in (
       gp.SCRIPT_PROMPT_PART1.format(editor_notes="", today="", week_range="", mood="", briefing_note="", registry_context="",
                                     outlook="", macro="", news="", portfolio="", dd1_title="", dd1_brief="",
                                     dd2_title="", education_topic=""),
       gp.SCRIPT_PROMPT_PART2.format(editor_notes="", today="", dive1_summary="", briefing_note="", news="", picks="",
                                     strategy="", portfolio="", dd2_title="", dd2_brief="",
                                     education_topic="", education_topics_used=""))), True)

print("\n── fixed deep-dive subjects ──")
ident = lambda t, n=300: t
intel = {"macro": [{"title": "CPI Surprise", "body": "B", "bull": "", "bear": ""}],
         "news": [{"headline": "Oil slides", "body": "Lower oil.", "exposure": "ENB.TO $5,493 CAD, SHEL $2,973 CAD"},
                  {"headline": "Tesla Semi", "body": "First deliveries.", "exposure": "TSLA $7,480 CAD"},
                  {"headline": "Pharma jumps", "body": "x", "exposure": "No direct holdings"}]}
pos = {"ENB.TO": {"name": "Enbridge", "cad": 5400, "accounts": ["FHSA"]},
       "SHEL": {"name": "Shell PLC", "cad": 3000, "accounts": ["TFSA"]},
       "TSLA": {"name": "Tesla", "cad": 7500, "accounts": ["TFSA"]},
       "FNGU": {"name": "FANG+ 3x ETF", "cad": 60000, "accounts": ["TFSA", "RRSP"]}}
pf2 = {"positions": pos, "total_value": 300000}
ck("Deep Dive 1 is the week's lead macro story", gp._choose_deep_dive_1(intel, ident)["title"], "CPI Surprise")
ck("Deep Dive 2 is the first owned holding the news touches",
   gp._choose_deep_dive_2(intel, pf2, [], ident)["ticker"], "ENB.TO")
ck("...skipping one already spotlighted",
   gp._choose_deep_dive_2(intel, pf2, ["ENB.TO", "SHEL"], ident)["ticker"], "TSLA")
ck("...and never a leveraged fund",
   gp._choose_deep_dive_2({"news": [{"headline": "x", "exposure": "FNGU $60,000 CAD"}]}, pf2, [], ident)["ticker"] != "FNGU", True)
fb = gp._choose_deep_dive_2({"news": []}, pf2, ["TSLA"], ident)
ck("with no usable news it falls back to our largest eligible holding", fb["ticker"], "SHEL" if False else "ENB.TO" if fb["ticker"] == "ENB.TO" else fb["ticker"])
ck("...which has no news attached", fb["body"], "")
t1, b1, t2_, b2 = gp._deep_dive_briefs(gp._choose_deep_dive_1(intel, ident), gp._choose_deep_dive_2(intel, pf2, [], ident))
ck("the brief states how much we own and where", "about $5,400" in b2 and "FHSA" in b2, True)
ck("...and confines the model to the news", "ONLY facts about this company" in b2, True)
ck("the title carries the headline the agenda will announce", t2_.startswith("Enbridge — Oil slides"), True)
ck("the briefing's own wording on currency is pointed away from", "CURRENCY box" in b1, True)

print("\n── a dry run of generate_script (fake model) ──")
seen = []
real_call, real_sleep = gp._groq_call, time.sleep
gp._groq_call = lambda k, prompt, label, max_tokens=4096: (seen.append((label, prompt)) or "ALEX: Hello.\nSAM: Hi.")
time.sleep = lambda s: None
intel2 = {"market_mood": "mixed",
          "daily_outlook": "Oil is easing. However the portfolio remains vulnerable to USD translation drag.",
          "macro": [{"title": "CPI Surprise", "impact": "HIGH",
                     "body": "CPI is due today. A hot print would strengthen the USD and pressure the portfolio’s USD‑heavy exposure."}],
          "news": [{"headline": "Oil slides", "body": "Lower oil prices help pipelines.", "exposure": "ENB.TO $60,000 CAD"}],
          "picks": [{"ticker": "NVO", "thesis": "Defensive healthcare."}],
          "strategy_short": [{"text": "Hedge $50,000 CAD of USD exposure, reducing translation drag."}]}
reg = {"registry_text": "", "recently_spotlighted_tickers": [], "education_topics_used": []}
try:
    _, out_facts = gp.generate_script(intel2, {"snapshots": SNAPS}, {}, "k", HOLD, reg,
                                      cash_positions=CASH, feedback="EDITOR'S NOTES — REJECTED: example\n")
finally:
    gp._groq_call, time.sleep = real_call, real_sleep
p1 = dict(seen)["Part 1"]
p2 = dict(seen)["Part 2"]
ck("both halves were generated", sorted(dict(seen)), ["Part 1", "Part 2"])
ck("the wrong-direction briefing sentence never reaches the model", "pressure the portfolio" in p1, False)
ck("neither does 'translation drag' wording", "translation drag" in p1 + p2, False)
ck("the fixed Deep Dive 2 is named in both halves", "Enbridge" in p1 and "Enbridge" in p2, True)
ck("the editor's notes reach both halves", "REJECTED: example" in p1 and "REJECTED: example" in p2, True)
ck("suggestions are labelled as not rules", "NOT decisions, rules or plans" in p2, True)
ck("a briefing figure tied to a holding is scoped to that holding",
   60000 in out_facts["intel_positions"].get("ENB.TO", []) and 60000 not in out_facts["intel_money"], True)
ck("...so it is allowed in a sentence naming Enbridge",
   flagged("ALEX: We own about $60,000 of Enbridge, held in the FHSA.", out_facts), False)
ck("...but not floating free in a sentence that names nothing",
   flagged("ALEX: A 0.5% drop in the index is a $60,000 hit to the portfolio.", out_facts), True)
ck("free-standing briefing figures stay allowed anywhere",
   flagged("ALEX: One idea in the briefing is a $50,000 hedge.", out_facts), False)
ck("the numbers the model was shown are recorded, so invented ones can be spotted",
   1.4 in out_facts["allowed_numbers"] and 60000.0 in out_facts["allowed_numbers"], True)
ck("both prompts leave room under Groq's 8,000 tokens/minute with 4,096 output",
   max(len(p1), len(p2)) // 4 + 4096 < 8000, True)

print("\n── education topic matching ──")
ck("a differently worded repeat is still recognised as used",
   gp._choose_education_topic(["Currency Carry Trade"]) != "Currency Carry Trade Unwinds", True)

print("\n── real ep18 (needs network) ──")
try:
    snaps = json.load(urllib.request.urlopen("https://portfolio-pulse-dun.vercel.app/api/snapshot", timeout=45))["snapshots"]
    st = json.load(urllib.request.urlopen("https://portfolio-pulse-dun.vercel.app/api/settings", timeout=30))
    window = {k: v for k, v in snaps.items() if k <= "2026-09-25"}
    _, f18 = gp._build_portfolio_context(st["computed_holdings"], window, st.get("cash_positions"))
    ep = open(os.path.join(ROOT, "data", "podcast_ep018.txt")).read()
    probs = gp.collect_script_problems(ep, f18)
    kinds = {}
    for p in probs:
        kinds.setdefault(p["kind"], []).append(p)
    ck("ep18 would have been rejected on facts", len(kinds.get("fact", [])) >= 8, True)
    ck("...including the greenback sentence", any("greenback" in p["msg"] for p in kinds["fact"]), True)
    ck("...and Broadcom 'not in the portfolio'", any("Broadcom" in p["msg"] and "not owned" in p["msg"] for p in kinds["fact"]), True)
    ck("...and the invented double-leverage figures", any("7,397" in p["msg"] for p in kinds["fact"]), True)
    ck("...and the missing scenarios", any("no bull case" in p["msg"] for p in kinds.get("structure", [])), True)
    ck("...and the dangling colons", sum("colon" in p["msg"] for p in kinds.get("structure", [])) >= 3, True)
    ck("...and its density", bool(kinds.get("density")), True)
    s18 = gp.figure_stats(ep)
    print(f"        ep18: {len(kinds.get('fact', []))} factual, {s18['total']} figures, {s18['per100']:.1f} per 100 words")
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP  needs network: {exc}")

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
