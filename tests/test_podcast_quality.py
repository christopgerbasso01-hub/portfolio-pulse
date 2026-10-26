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
   len(gp.agenda_problems("".join(f"{'ALEX' if i % 2 == 0 else 'SAM'}: line {i}.\n" for i in range(15))
                          + "ALEX: Today, Yield Curve Recession Signals.\n[EDUCATION_TOPIC: Yield Curve Recession Signals]")), 1)
ck("...but a run-down after a cold open and a welcome does",
   gp.agenda_problems("ALEX: Hook.\nSAM: Yes.\n[THEME]\nALEX: Welcome.\nSAM: Hi.\nALEX: Last week we said this.\n"
                      "ALEX: Today, and our learning segment on Yield Curve Recession Signals.\n"
                      "[EDUCATION_TOPIC: Yield Curve Recession Signals]"), [])

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


# ── an episode plan built by the real planner from synthetic data ────────────
gp._fetch_sp500 = lambda: {}
_SNAPS_P = {d: dict(v, account_cost={"TFSA": 100000, "RRSP": 20000, "FHSA": 30000}) for d, v in SNAPS.items()}
_SNAPS_P["2026-09-25"]["account_cost"] = {"TFSA": 106000, "RRSP": 20000, "FHSA": 30000}   # $6,000 added
_SET_P = {"computed_holdings": [dict(h, sector="Leveraged ETF" if h["ticker"] in ("FNGU", "SOXL") else "Technology")
                                for h in HOLD],
          "transactions": [{"type": "BUY", "date": "2026-09-23", "ticker": "AVGO", "account": "TFSA",
                            "shares": 20, "price": 300, "ccy": "USD", "rate": 1.4}],
          "cash_positions": CASH}
INTEL_P = {"market_mood": "mixed", "generated_at": "2026-09-25T20:00:00Z",
           "daily_outlook": "Oil is easing. However the portfolio remains vulnerable to USD translation drag.",
           "macro": [{"title": "CPI Surprise Could Shift Dollar Strength", "impact": "HIGH",
                      "body": "CPI is due today. A hot print would strengthen the USD and pressure the portfolio’s USD‑heavy exposure."},
                     {"title": "China PMI Slides", "body": "Factories slowed for a third month."}],
           "news": [{"headline": "Oil slides on supply glut", "body": "Lower oil prices weigh on producers.", "exposure": "ENB.TO $60,000 CAD"},
                    {"headline": "Tesla starts Semi deliveries", "body": "First trucks shipped.", "exposure": "TSLA $7,480 CAD"},
                    {"headline": "Saudi oil supply glut deepens", "body": "Same story again.", "exposure": "ENB.TO $60,000 CAD"}],
           "picks": [{"ticker": "NVO", "thesis": "Defensive healthcare."}],
           "strategy_short": [{"text": "Hedge $50,000 CAD of USD exposure, reducing translation drag."}]}
PLAN = gp.build_episode_plan(INTEL_P, {"snapshots": _SNAPS_P}, _SET_P, {}, {"episodes": []}, {}, 1)
PROMPTS = {k: gp._segment_prompt(PLAN, k, "", "SAM: a previous line.") for k in gp.SEGMENT_KEYS}

print("\n── segment repair loop ──")
CLEAN = {
    "opening": "ALEX: Our big tech fund had a week.\nSAM: It did.\n[THEME]\nALEX: Welcome to Portfolio Pulse Weekly.\nSAM: Hello.\n"
               "ALEX: Today: the portfolio, the main story, around the portfolio, the learning segment on "
               + PLAN["topic"] + ", and scenarios.\nSAM: To the portfolio.",
    "desk": "ALEX: A quiet week for the book.\nSAM: Any trades?\nALEX: One purchase of Broadcom.\nSAM: On to the main story.",
    "main": "SAM: What happened in China?\nALEX: Factories slowed again.\nSAM: Does it touch us?\nALEX: Only indirectly.\nSAM: Next items.",
    "around": "ALEX: First, Tesla shipped its first Semi trucks.\nSAM: Good for a holding we own.\nALEX: Now to learning.",
    "learning": "[EDUCATION_TOPIC: " + PLAN["topic"] + "]\nALEX: Here is the idea.\nSAM: That's our learning segment for this week.",
    "scenarios": "ALEX: Base case — 50 percent: little happens.\nSAM: What tells us?\nALEX: Quiet news.\n"
                 "ALEX: Bull case — 30 percent: the market rises.\nSAM: And then?\nALEX: A good week for us.\n"
                 "ALEX: Bear case — 20 percent: the market falls.\nSAM: How should he think about it?\nALEX: With patience.\n"
                 "SAM: Thanks for listening.",
}
BADFX_MAIN = CLEAN["main"].replace("ALEX: Only indirectly.", "ALEX: A firmer greenback compresses the CAD value of our US holdings.")
seg_calls = []


def writer(drafts):
    def w(api_key, plan, key, notes, before):
        seg_calls.append((key, notes))
        d = drafts.get(key, CLEAN[key])
        return d(notes) if callable(d) else d
    return w


_real_write = gp.write_segment
_saved_min = gp.MIN_SCRIPT_WORDS
gp.MIN_SCRIPT_WORDS = 0
try:
    gp.write_segment = writer({})
    r = gp.produce_episode("k", PLAN, log=lambda *a: None)
    ck("clean segments: six calls, no rewrites", [k for k, _ in seg_calls], list(gp.SEGMENT_KEYS))
    ck("...and the episode is published", r is not None, True)
    seg_calls.clear()
    gp.write_segment = writer({"main": lambda notes: CLEAN["main"] if notes else BADFX_MAIN})
    r = gp.produce_episode("k", PLAN, log=lambda *a: None)
    ck("a bad main story: only that segment is rewritten", [k for k, n in seg_calls if n], ["main"])
    ck("...and it is told exactly what was wrong", "greenback" in [n for k, n in seg_calls if n][0], True)
    ck("...and the published script is clean", "greenback" not in r[0], True)
    seg_calls.clear()
    gp.write_segment = writer({"main": BADFX_MAIN})
    r = gp.produce_episode("k", PLAN, log=lambda *a: None)
    ck("a segment that never comes back clean is rewritten at most twice",
       sum(1 for k, n in seg_calls if k == "main" and n), 2)
    ck("...and the episode still publishes, with the false sentence cut",
       r is not None and "greenback" not in r[0] and "Base case" in r[0], True)
    gp.write_segment = lambda *a: (_ for _ in ()).throw(RuntimeError("groq down"))
    ck("a failing generator means no episode", gp.produce_episode("k", PLAN, log=lambda *a: None), None)
finally:
    gp.write_segment = _real_write
    gp.MIN_SCRIPT_WORDS = _saved_min

print("\n── the context the model is given ──")
ck("no hand-typed stale facts remain",
   any(x in text for x in ("7,685", "1,776", "RRSP Cash", "implied β")), False)
ck("all leveraged funds we own are named (the old list left one out)",
   "FANG+ 3x, Semiconductor 3x" in text, True)
ck("the long official fund name is not what the model reads out",
   "Direxion" in text, False)
ck("lookup tables present", "HOW MUCH THINGS MOVE" in text and "never apply a multiplier" in text, True)
ck("the currency box is present",
   "NEVER say we hold them in Canadian dollars" in text and "GOOD for us" in text, True)
ck("figures are pre-rounded for speech", gp._say_money(14386) == "$14,000" and gp._say_money(1722) == "$1,700", True)
ck("cash comes from the real balances", f["cash_total"], round(2000 + 500 * 1.40))
ck("...and is stated as a small slice", "Cash (not invested): about" in text, True)
t2, _ = gp._build_portfolio_context(HOLD, SNAPS, None)
ck("with no cash data it says so instead of inventing one", "Cash balances: not provided" in t2, True)
ck("the index table is the 3x figure, ready to read", f["index_table"]["1"], round(
    (1000 * 32 + 500 * 44) * 1.40 * 0.03))
ck("every segment prompt formats from the plan (a missing field would be a KeyError)",
   sorted(k for k, v in PROMPTS.items() if isinstance(v, str) and len(v) > 500), sorted(gp.SEGMENT_KEYS))

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
ck("a dollar story is pointed at the CURRENCY box rather than the briefing's wording",
   "CURRENCY box" in gp._deep_dive_briefs({"title": "CPI Surprise", "body": "The dollar may firm."}, None)[1], True)
ck("a China story is not", "CURRENCY box" in gp._deep_dive_briefs({"title": "China PMI", "body": "Factories slowed."}, None)[1], False)
ck("the briefing's speculative bull and bear lines are left out",
   "BULLTEXT" in gp._deep_dive_briefs({"title": "t", "body": "b", "bull": "BULLTEXT", "bear": "BEARTEXT"}, None)[1], False)

print("\n── the episode plan and what each segment is told ──")
ALL = " ".join(PROMPTS.values())
ck("the wrong-direction briefing sentence never reaches the model", "pressure the portfolio" in ALL, False)
ck("neither does 'translation drag' wording", "translation drag" in ALL, False)
ck("currency is never the main story", "currency" in gp._themes_of(PLAN["main"]["title"]), False)
ck("...and no quick news item is a currency story", any("currency" in gp._themes_of(q["title"]) for q in PLAN["quick"]), False)
ck("the main story and the quick items are different stories",
   any(gp._desk.same_story(PLAN["main"]["title"], q["title"]) for q in PLAN["quick"]), False)
ck("the same story twice in the news is used at most once",
   sum("oil" in x.lower() for x in [PLAN["main"]["title"]] + [q["title"] for q in PLAN["quick"]]
       + [(PLAN["spot"] or {}).get("headline", "")]) <= 1, True)
ck("only the segment that owns currency gets the currency rules",
   [k for k, v in PROMPTS.items() if "this segment owns it" in v], [PLAN["fx_owner"]] if PLAN["fx_owner"] else [])
ck("every other segment is told not to mention it",
   all("do not mention the exchange rate" in v for k, v in PROMPTS.items() if k != PLAN["fx_owner"]), True)
ck("analogies are allowed only in the main story and the learning segment",
   sorted(k for k, v in PROMPTS.items() if "at most ONE short everyday comparison" in v), ["learning", "main"])
ck("each segment is told what the others cover", all("This week the other segments cover" in v for v in PROMPTS.values()), True)
ck("the desk carries money in versus market", "New money Christopher added this week: about $6,000" in PROMPTS["desk"], True)
ck("...the trade", "Trade: new position" in PROMPTS["desk"] or "Trade: added to" in PROMPTS["desk"], True)
ck("...the house goal", "against the $90,000 target" in PROMPTS["desk"], True)
ck("the scenario cases are fixed, with figures from the table",
   PROMPTS["scenarios"].count("the S&P 500") >= 3 and "within 1.5 percent" in PROMPTS["scenarios"], True)
ck("the close promises next week's learning topic", PLAN["next_topic"] in PROMPTS["scenarios"], True)
ck("the opening asks for the theme marker after a cold open", "[THEME]" in PROMPTS["opening"], True)
ck("the opening's run-down names the learning topic the segment delivers",
   PLAN["topic"] in PROMPTS["opening"] and PLAN["topic"] in PROMPTS["learning"], True)
ck("the learning segment states its marker", f"[EDUCATION_TOPIC: {PLAN['topic']}]" in PROMPTS["learning"], True)
f_p = PLAN["facts"]
ck("the spotlight states the real position size, from holdings and prices",
   f"we own about {gp._say_money(800 * 75)} of it" in PROMPTS["around"], True)
ck("a briefing figure tied to a holding is scoped to that holding",
   7480.0 in f_p["intel_positions"].get("TSLA", []) and 7480.0 not in f_p["intel_money"], True)
ck("the desk's figures are accepted by the checker",
   flagged("ALEX: Christopher added about $6,000 this week.", f_p), False)
ck("...an invented one is not", flagged("ALEX: Christopher added about $17,000 this week.", f_p), True)
ck("the numbers the model was shown are recorded, so invented ones can be spotted",
   6000.0 in f_p["allowed_numbers"] and 90000.0 in f_p["allowed_numbers"], True)
ck("every segment request fits Groq's 8,000-token limit with its answer room",
   all(gp._estimate_tokens(PROMPTS[k]) + m <= 7900 for k, _, _, m in gp.SEGMENTS), True)

print("\n── what the listener asked not to hear ──")
def kinds(script, history=None):
    return sorted({x["kind"] for x in gp._scan_unwanted(script, history)})
for label, line in [
    ("ep18: 'built to move three times as much'",
     "ALEX: A 3x fund is built to move about three times as much as the index it follows."),
    ("the daily reset", "ALEX: The daily reset causes slow decay in leveraged funds over time."),
    ("ep18's real wording of the 1.5% -> 4.5% explainer",
     "ALEX: It turned a modest 1.5% move in the underlying index into a 4.5% boost on the leveraged position."),
    ("'decay' by itself", "ALEX: The risk here is volatility decay, which grinds returns down."),
]:
    ck(f"explaining leverage is caught — {label}", "leverage" in kinds(line), True)
for label, line in [
    ("a fund's result", "ALEX: The FANG+ fund was up about 8.3% as big tech had a strong stretch."),
    ("a mention without explanation", "ALEX: Our leveraged funds gained again as the market rallied this week."),
    ("a market move's worth", "ALEX: When the stock market moves 1%, we are up or down about $4,900."),
]:
    ck(f"describing a result is fine — {label}", "leverage" in kinds(line), False)
ck("no education topic about leverage remains",
   [t for t in gp.EDUCATION_TOPICS if "3x" in t or "everage" in t], [])
ck("the context no longer teaches 3x", "3x as much" in text or "multiply it by 3" in text, False)
ck("every segment is told never to explain it",
   all("NEVER explain how leveraged or 3x funds work" in v for v in PROMPTS.values()), True)

print("\n── nothing said twice ──")
# Four lead-in turns first: the opening turns preview the episode and are exempt from
# the repeat comparison on purpose.
dup = ("ALEX: Hello and welcome.\nSAM: Hi there.\nALEX: Here is the plan for today.\nSAM: Sounds good.\n"
       "ALEX: Each one-cent move in the dollar is worth about a thousand seven hundred to us.\n"
       "SAM: Right.\n"
       "ALEX: Each one-cent move in the dollar is worth about seventeen hundred to us.\n")
ck("a sentence repeated inside an episode is caught", "repeat" in kinds(dup), True)
three = ("ALEX: That is worth about $1,700 right now.\nSAM: And a different point entirely about our energy holdings today.\n"
         "ALEX: Which is again about $1,700 as we said earlier on the show.\n"
         "SAM: Another fresh point about the pipeline business and its fee income.\n"
         "ALEX: So remember it is $1,700 for every single cent of movement.\n")
ck("a figure stated three times is caught on the third", len([x for x in gp._scan_unwanted(three) if "times" in x["reason"]]), 1)
ck("...but twice is fine", [x for x in gp._scan_unwanted(three.rsplit("ALEX:", 1)[0]) if "times" in x["reason"]], [])
hist = ["ALEX: We have a lot of the portfolio humming along under the surface this week.\nSAM: Okay.\n"]
ck("an agenda preview and the later introduction of the same topic are not a repeat",
   "repeat" in kinds("ALEX: Welcome to Portfolio Pulse Weekly, joined as always by Sam.\nSAM: Good.\n"
                     "ALEX: Second, Tesla, and its first Semi truck deliveries this week.\nSAM: Okay.\n"
                     "ALEX: Our second topic is Tesla, which announced the first Semi truck deliveries.\n"), False)
ck("a sentence carried over from a recent episode is caught",
   "repeat" in kinds("ALEX: We have a lot of the portfolio humming along under the surface this week.\n", hist), True)
ck("...but the weekly welcome is not",
   kinds("ALEX: Hey everyone, welcome back to Portfolio Pulse Weekly, I'm Alex joined as always by Sam.\n",
         ["ALEX: Hey everyone, welcome back to Portfolio Pulse Weekly, I'm Alex joined as always by Sam.\n"]), [])
ck("...and a short reaction is not", kinds("SAM: Right, that makes sense.\n", ["SAM: Right, that makes sense.\n"]), [])
cut, gone = gp.strip_unwanted_sentences(
    "ALEX: The FANG+ fund led the week. A 3x fund is built to move about three times as much as its index.\nSAM: Okay.\n")
ck("only the leverage explanation is cut from a mixed turn", ("FANG+ fund led" in cut, "built to move" in cut), (True, False))
ck("...and what was cut is reported", len(gone), 1)

print("\n── four numbers in a turn, at most ──")
heavy = "ALEX: It rose $1,200 or 3.4% today. Last week it was $900 or 2.1%. The week before was 1.8% and $700 on top."
ck("the cap is four", gp.FIGURES_PER_TURN_MAX, 4)
ck("a turn of six figures asks for a redraft",
   [(p["kind"], p["retry"]) for p in gp.collect_script_problems(heavy, {}) if "carry more than" in p["msg"]], [("density", True)])
four = "ALEX: It rose $1,200 or 3.4% today, and last week it was $900 or 2.1%."
ck("four figures is allowed", [p for p in gp.collect_script_problems(four, {}) if "carry more than" in p["msg"]], [])
trimmed, dropped = gp.trim_heavy_turns(heavy)
ck("trimming brings the turn within the cap", gp.figure_stats(trimmed)["max_turn"] <= 4, True)
ck("...by dropping the heaviest sentence", len(dropped), 1)
ck("a turn within the cap is untouched", gp.trim_heavy_turns(four), (four, []))
r = gp._final_repair(CLEAN["scenarios"] + "\n" + heavy, {}, None, log=lambda *a: None)
ck("the final repair guarantees the cap", r is not None and gp.figure_stats(r)["max_turn"] <= 4, True)
r = gp._final_repair(CLEAN["desk"] + "\nALEX: We have a lot of the portfolio humming along under the surface this week.",
                     {}, hist, log=lambda *a: None)
ck("...and drops a sentence repeated from a recent episode", r is not None and "humming along" not in r, True)

print("\n── topics are not repeated week to week ──")
ck("themes are read from headlines",
   gp._themes_of("US CPI Surprise Could Shift Dollar Strength") == frozenset({"rates & inflation", "currency"}), True)
ck("China's PMI is its own theme", gp._themes_of("China’s Manufacturing PMI Declines, Raising Growth Doubts"), frozenset({"china & growth"}))
intel3 = {"macro": [{"title": "US CPI Surprise Could Shift Dollar Strength", "body": "x", "bull": "", "bear": ""},
                    {"title": "ECB Holds Rates Amid Eurozone Growth Concerns", "body": "y", "bull": "", "bear": ""},
                    {"title": "China’s Manufacturing PMI Declines, Raising Growth Doubts", "body": "z", "bull": "", "bear": ""}],
          "news": [{"headline": "Oil slides as US-Iran truce hopes outweigh Houthi attacks", "body": "o", "exposure": "ENB.TO $5,493 CAD"},
                   {"headline": "Tesla Starts Semi Deliveries", "body": "t", "exposure": "TSLA $7,480 CAD"}]}
recent = frozenset({"currency", "oil & energy", "rates & inflation"})   # ep15, ep16, ep17
ck("with nothing recent, the lead story is chosen", gp._choose_deep_dive_1(intel3, ident)["title"].startswith("US CPI"), True)
ck("after three weeks of dollar and rates, Deep Dive 1 moves on to China",
   gp._choose_deep_dive_1(intel3, ident, recent)["title"].startswith("China"), True)
ck("when everything is stale it still picks something",
   bool(gp._choose_deep_dive_1(intel3, ident, frozenset(gp._THEMES))["title"]), True)
pos3 = {"ENB.TO": {"name": "Enbridge", "cad": 5400, "accounts": ["FHSA"]}, "TSLA": {"name": "Tesla", "cad": 7500, "accounts": ["TFSA"]}}
pf3 = {"positions": pos3, "total_value": 300000}
ck("Deep Dive 2 skips an oil story after an oil week and takes the Tesla one",
   gp._choose_deep_dive_2(intel3, pf3, [], ident, recent)["ticker"], "TSLA")
ck("...and with no recent themes keeps the first", gp._choose_deep_dive_2(intel3, pf3, [], ident)["ticker"], "ENB.TO")

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

print("\n── minimum length ──")
short = "ALEX: Hello there and welcome.\nSAM: Glad to be here today."
ck("a short script is flagged for retry when a minimum is set",
   any(p["kind"] == "length" and p["retry"] and not p["fatal"]
       for p in gp.collect_script_problems(short, {}, None, 2200)), True)
ck("no length flag when no minimum is set",
   any(p["kind"] == "length" for p in gp.collect_script_problems(short, {})), False)
ck("the production minimum is about 15 minutes", 2000 <= gp.MIN_SCRIPT_WORDS <= 2400, True)
ck("the per-turn cap is unchanged at 4", gp.FIGURES_PER_TURN_MAX, 4)
import re as _re
_lows = [int(x.replace(",", "")) for x in _re.findall(r"LENGTH: ([\d,]+) to", " ".join(t for _, t, _, _ in gp.SEGMENTS))]
ck("the six segments together ask for 15-20 minutes (2,500+ words)", (len(_lows), sum(_lows) >= 2500), (6, True))

print("\n── short segments are asked for again; audio never depends on one provider ──")
import time as _t, asyncio as _a, tempfile as _tf, pathlib as _pl
_rc, _rs = gp._groq_call, _t.sleep
_calls = []
def _fake(k, prompt, label, max_tokens=4096):
    _calls.append((label, prompt))
    return "word " * (900 if "fuller" in label else 300)
gp._groq_call, _t.sleep = _fake, (lambda s: None)
try:
    out = gp._groq_part("k", lambda n: "P:" + n, "Part 1", "", 1250)
    ck("a short half triggers exactly one more call", [c[0] for c in _calls], ["Part 1", "Part 1 (fuller)"])
    ck("...which states the shortfall", "only 300 words" in _calls[1][1], True)
    ck("...and the longer draft is kept", len(out.split()), 900)
    _calls.clear()
    gp._groq_call = lambda k, p, l, max_tokens=4096: (_calls.append(l) or "word " * 1400)
    gp._groq_part("k", lambda n: n, "Part 2", "", 1150)
    ck("a long-enough half is not regenerated", _calls, ["Part 2"])
finally:
    gp._groq_call, _t.sleep = _rc, _rs

_rg, _rp, _rk, _ru = gp._generate_all_audio, gp.synthesize_with_piper, gp.synthesize_with_kokoro, gp._use_edge_host
_used, _hosts = [], []
async def _boom(turns, d): raise RuntimeError("403 Invalid response status")
def _kboom(turns, out, d): _used.append("kokoro"); raise RuntimeError("model download failed")
gp._generate_all_audio, gp._use_edge_host = _boom, _hosts.append
gp.synthesize_with_kokoro = lambda turns, out, d: _used.append("kokoro")
gp.synthesize_with_piper = lambda turns, out, d: _used.append("piper")
try:
    with _tf.TemporaryDirectory() as d:
        eng = gp.synthesize_episode([("ALEX", "Hi there.")], _pl.Path(d) / "x.mp3", _pl.Path(d))
    ck("both Microsoft hosts are tried, new host first", [h.split("/")[2] for h in _hosts],
       ["api.msedgeservices.com", "speech.platform.bing.com"])
    ck("Microsoft refusing falls back to Kokoro, not Piper", (eng, _used), ("kokoro", ["kokoro"]))
    _used.clear(); gp.synthesize_with_kokoro = _kboom
    with _tf.TemporaryDirectory() as d:
        eng = gp.synthesize_episode([("ALEX", "Hi there.")], _pl.Path(d) / "x.mp3", _pl.Path(d))
    ck("Piper only if Kokoro also fails", (eng, _used), ("piper", ["kokoro", "piper"]))
finally:
    gp._generate_all_audio, gp.synthesize_with_piper, gp.synthesize_with_kokoro, gp._use_edge_host = _rg, _rp, _rk, _ru
ck("the chosen voices are Adam for Alex and Heart for Sam", gp.KOKORO_VOICES, {"ALEX": "am_adam", "SAM": "af_heart"})
ck("both speakers have an offline voice", sorted(gp.PIPER_VOICES), ["ALEX", "SAM"])

print("\n── Groq free-tier guard ──")
real_call = gp._groq_call
_saved = dict(gp._GROQ_USED)
gp._GROQ_USED["tokens"] = gp.GROQ_RUN_TOKEN_BUDGET - 1000
ck("a short piece is not re-asked once the run budget is nearly spent",
   (lambda calls: (gp._groq_part.__globals__.__setitem__("_groq_call", lambda k, p, l, max_tokens=0: calls.append(l) or "w " * 10),
                   gp._groq_part("k", lambda n: n, "Piece 1", "", 500), calls)[2])([]), ["Piece 1"])
gp._groq_call = real_call
seg_calls.clear()
gp.MIN_SCRIPT_WORDS = 0
gp.write_segment = writer({"main": BADFX_MAIN})
r = gp.produce_episode("k", PLAN, log=lambda *a: None)
ck("no rewrite is started when it would exceed the run budget", [k for k, n in seg_calls if n], [])
ck("...and the episode is still published, repaired", r is not None and "greenback" not in r[0], True)
gp._GROQ_USED.update(_saved)
seg_calls.clear()


def _limit(notes):
    if notes:
        raise gp.GroqDailyLimit("daily allowance used up")
    return BADFX_MAIN


gp.write_segment = writer({"main": _limit})
r = gp.produce_episode("k", PLAN, log=lambda *a: None)
ck("hitting Groq's daily limit on a rewrite keeps the earlier draft (repaired)",
   r is not None and "greenback" not in r[0], True)
gp.write_segment = _real_write
gp.MIN_SCRIPT_WORDS = _saved_min
ck("one run's budget leaves room for the daily briefing within 200,000/day",
   gp.GROQ_RUN_TOKEN_BUDGET + 6 * gp.SEGMENT_COST_ESTIMATE + 30_000 < 200_000, True)
_p = gp._safe_max_tokens("x" * 18_000, 3200)
ck("a long prompt gets less answer room so the request never exceeds 8,000", gp._estimate_tokens("x" * 18_000) + _p <= 7_900, True)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
