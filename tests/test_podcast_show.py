"""
What the November 2026 review found, pinned down:
  - the week's biggest stories were in the briefings but never aired (midterms, Nvidia's
    CEO leaving) — stories are now ranked across every briefing of the week;
  - learning topics came back after four weeks (yield curve ep18 -> ep22, VIX ep19 -> ep25)
    — every taught topic is now kept for good;
  - hosts sounded scripted ("Exactly." x13) — cue openers are trimmed.

Run: python3 tests/test_podcast_show.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import podcast_curriculum as C  # noqa: E402
import podcast_desk as D        # noqa: E402
import podcast_stories as S     # noqa: E402

spec = importlib.util.spec_from_file_location("gp_show", os.path.join(ROOT, "scripts", "generate_podcast.py"))
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)
fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


def brief(day, *items):
    return (f"2026-11-{day:02d}T20:00:00Z", {"macro": [], "news": [
        {"headline": h, "body": b, "impact": i, "exposure": e} for h, b, i, e in items]})


WEEK = [
    brief(13, ("Nvidia board still searching for permanent CEO successor", "After Jensen Huang's exit.", "HIGH", "NVDA $7,400 CAD"),
              ("German gas storage hits record low", "Reserves fall.", "MED", "")),
    brief(16, ("Nvidia stock falls ahead of earnings", "Investors await results.", "HIGH", "NVDA"),
              ("Iraq Khor Mor gas field halted by drone", "Output stops.", "MED", "")),
    brief(17, ("Fed officials signal half-point hike as inflation sticks", "Rates may rise.", "HIGH", ""),
              ("Canadian dollar weakens on oil", "The loonie slips.", "HIGH", "")),
    brief(18, ("Nvidia interim CEO Kress rallies staff after Jensen Huang exit", "Continuity signalled.", "HIGH", "NVDA"),
              ("US Fed rate path tightening ahead of year-end", "Markets price hikes.", "HIGH", "")),
    brief(19, ("Nvidia interim CEO Kress rallies staff at first all-hands meeting", "Calming concerns.", "HIGH", "NVDA"),),
]
W = {"NVDA": 3.0, "FNGU": 14.0, "SPXL": 19.0, "ENB.TO": 2.0}
N = {"NVDA": ["Nvidia"], "ENB.TO": ["Enbridge"]}

print("── the week's biggest stories ──")
st = S.rank(WEEK, W, N, same_story=D.same_story, exclude=lambda t: "currency" in gp._themes_of(t))
ck("the story in the news every day leads (Nvidia), not a one-day niche item", st[0]["anchor"], "co:NVDA")
ck("...its headlines across four days are one story", len(st[0]["items"]), 4)
ck("...and it reaches the FANG+ fund too", "FNGU" in st[0]["held"], True)
ck("differently worded Fed headlines are one story", sum(1 for x in st if x.get("anchor") == "fed"), 1)
ck("the daily currency item is left to the portfolio review", any("dollar" in x["title"].lower() for x in st), False)
ck("German gas ranks below the big stories", [x["title"] for x in st].index("German gas storage hits record low") >= 2, True)
ck("a headline naming two companies does not fuse their stories",
   S.anchors("Anthropic IPO lifts Nvidia and Broadcom", {"NVDA": ["Nvidia"], "AVGO": ["Broadcom"]}), set())
tl = S.timeline(st[0])
ck("the timeline reads as a development, one line a day", tl.count("\n") + 1, 4)
again = S.rank(WEEK, W, N, last_main="Nvidia interim CEO Kress rallies staff at first all-hands meeting",
               same_story=D.same_story, exclude=lambda t: "currency" in gp._themes_of(t))
ck("last week's main story is marked down", next(x for x in again if x.get("anchor") == "co:NVDA")["score"] < st[0]["score"], True)
ck("the story's name is its subject, not one headline", gp._story_label(st[0], N), "Nvidia")

print("\n── a curriculum that never repeats ──")
ck("VIX, taught in ep19 and ep25, is never chosen again",
   any("vix" in C.choose([]).lower() for _ in [0]), False)
ck("a reworded repeat still counts as taught", C.is_taught("Reading the VIX fear gauge", C.HISTORY), True)
taught, seen = [], set()
for _ in range(len(C.CURRICULUM)):
    t = C.choose(taught)
    seen.add(t)
    taught.append(t)
ck("a whole year of lessons without one repeat", len(seen), len(C.CURRICULUM))
ck("no lesson about leverage or decay", [t for t, _, _ in C.CURRICULUM if "decay" in t.lower() or "leverag" in t.lower()], [])
ck("a Fed week gets the rate lesson", C.choose([], C.themes_for("the Fed and interest rates", frozenset({"rates & inflation"}))),
   "How a Central Bank Rate Hike Reaches Stock Prices")
ck("a CEO-exit week gets the succession lesson", C.choose([], C.themes_for("Nvidia CEO steps down")),
   "What Happens to a Stock When a CEO Leaves")
ck("last week's promise is kept", C.choose([], frozenset({"politics"}), promised="Share Buybacks"), "Share Buybacks")
ck("...unless it was already taught", C.choose(["Share Buybacks"], frozenset(), promised="Share Buybacks") != "Share Buybacks", True)
ck("a lesson that wanders into an old one is caught",
   C.drifted_into("The VIX jumped. When the VIX rises...", "Share Buybacks", []), ["What VIX Actually Measures"])
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "m.json"
    m = {"episodes": []}
    for n in range(1, 20):
        m = D.save_memory(f, m, {"episode": n, "learning_topic": f"Topic {n}", "analogies": [f"comparison {n}"]})
    m = D.load_memory(f)
    ck("taught topics are kept for good, not just the last twelve episodes", len(m["taught"]), 19)
    ck("...and so are comparisons", "comparison 1" in m["analogies"], True)

print("\n── sounding natural ──")
seg = "SAM: Exactly. The Fed is the story.\nALEX: Got it.\nALEX: Right, and rates matter.\nSAM: Exactly.\nSAM: Precisely, yes."
out = gp._trim_fillers(seg)
ck("cue openers are removed, the substance kept", out.split("\n")[0], "SAM: The Fed is the story.")
ck("a turn that is only a cue is dropped after the first", out.count("Exactly"), 0)
ck("one host never ends up speaking twice in a row", all(a.split(":")[0] != b.split(":")[0]
                                                        for a, b in zip(out.split("\n"), out.split("\n")[1:])), True)
ck("natural scenario wording is read back",
   D.scenario_probabilities("ALEX: Our base case, which I'd put at about 50 percent, is calm. SAM: The bull case, "
                            "maybe 30 percent, is a rally. ALEX: And the bear case, 20 percent."),
   {"base": 50, "bull": 30, "bear": 20})
ck("the scenarios no longer demand the old 'Base case — NN percent:' opener",
   "starting exactly" in gp.SEG_SCENARIOS or 'beginning exactly "Base case' in gp.SEG_SCENARIOS, False)
ck("the hosts are told not to open with cue words", "Never begin a turn with \"Exactly\"" in gp.SHOW_BIBLE, True)
ck("comparisons only in the lesson", gp.ANALOGY_SEGMENTS, ("learning",))
ck("phrases the last episodes leaned on are listed to avoid",
   '"that sounds like"' in gp._stale_phrases({1: "SAM: That sounds like a plan.\n" * 3}), True)

print("\n── the run of show ──")
ck("look back, big story, around, lesson, week ahead", [gp.SEGMENT_TITLES[k] for k in gp.SEGMENT_KEYS],
   ["the opening", "the week that was", "the week's big story", "around the portfolio",
    "the learning segment", "the week ahead"])
ck("the look back explains why", "WHY" in gp.SEG_DESK and "{why}" in gp.SEG_DESK, True)
ck("the week ahead starts with what to watch", gp.SEG_SCENARIOS.index("{watch}") < gp.SEG_SCENARIOS.index("{frame}"), True)

print("\n── professional finish: errors episode 25 let through ──")
lev = lambda t: "leverage" in {u["kind"] for u in gp._scan_unwanted("ALEX: " + t)}
for line in ("The SPXL fund will capture the aggregate market move, but its leveraged nature means the percentage change is magnified relative to the underlying index.",
             "Leveraged exposure does amplify movements.",
             "If the market reacts positively, the 3x funds could outpace the underlying index by a sizable margin.",
             "The fund's performance mirrors the daily percentage change of its benchmark."):
    ck(f"leverage explainer caught: {line[:45]}...", lev(line), True)
for line in ("Our leveraged funds fell about 8% this week.", "The leveraged funds were the biggest drag on the portfolio."):
    ck(f"a plain result is not: {line[:40]}", lev(line), False)
P = {"fx_owner": None, "banned_analogies": [], "topic": "X", "facts": {}, "taught": [],
     "fields": {"around": {"items": ""}, "opening": {"hook": ""}}, "score": {"probability": 30}}
probs = lambda k, t: gp.segment_problems(k, t, P)
ck("'that's the house side' about the non-registered account is caught",
   any("down payment is only" in m for m in probs("desk", "SAM: The non-registered account moved most. That's the house side of things.")), True)
ck("...but saying it is NOT house money is fine",
   any("down payment is only" in m for m in probs("desk", "ALEX: The TFSA isn't part of the house money.")), False)
ck("'late yesterday' from an old briefing is caught",
   any("name the day" in m for m in probs("main", "ALEX: The bill cleared the Senate late yesterday.")), True)
ck("claiming a 30% call was right is caught",
   any("honestly" in m for m in probs("opening", "ALEX: The bull case played out. Looks like we were right on the money.\n[THEME]")), True)
q = "SAM: If that happens, how should Christopher think about it?\nALEX: Next week we'll break down earnings revisions."
ck("a question answered only by a sign-off is removed", gp.drop_orphan_questions(q)[1], ["If that happens, how should Christopher think about it?"])
ck("...a real answer keeps it", gp.drop_orphan_questions("SAM: Why?\nALEX: Because rates rose.")[1], [])

print("\n── professional finish: sound ──")
ck("one speed per host", (gp.VOICE_RATE["ALEX"], gp.VOICE_RATE["SAM"]), ("+3%", "+6%"))
gaps = [gp._pause_after(("ALEX", "A " * n + "?"), ("SAM", "B " * m)) for n, m in ((5, 3), (5, 20), (60, 20), (10, 20))]
ck("pauses vary: a quick reaction comes in fast, an answer after a beat", gaps[0] < gaps[3] < gaps[1] and gaps[2] > 0, True)
br = gp.assemble({"opening": "ALEX: Hi.\n[THEME]\nSAM: Hello.", "desk": "SAM: The week.\nALEX: Fell.", "main": "ALEX: Nvidia."}, breaks=True)
ck("segments are separated by a sting marker", br.count("[BREAK]"), 2)
ck("...which becomes a pause-and-sting in the audio",
   [t[0] for t in gp.parse_with_breaks(br.split("[THEME]")[1])], ["SAM", "BREAK", "SAM", "ALEX", "BREAK", "ALEX"])
ck("the transcript never shows the marker", "[BREAK]" in gp._clean_transcript(br), False)
ck("the sting asset exists", gp.THEME_STING.exists(), True)
ck("the episode is mastered to podcast loudness", (gp.TARGET_LUFS, gp.THEME_GAIN_DB < -6), (-16.0, True))

print("\n── professional finish: lesson fact-check and joins ──")
_rc, _rw = gp._groq_call, gp.write_segment
PL = dict(P, topic="What VIX Actually Measures", fields=dict(P["fields"]))
SEG = {"around": "ALEX: Tesla rose.\nSAM: Good for us.\nALEX: Now the lesson.",
       "learning": "[EDUCATION_TOPIC: What VIX Actually Measures]\nALEX: The VIX is delta-weighted.\nSAM: Hm?\n"
                   "ALEX: A $4 premium means 35.\nSAM: That's our learning segment for this week."}
calls = []
gp._groq_call = lambda k, prompt, label, max_tokens=0: (calls.append(label) or
                '{"problems": ["The VIX is not delta-weighted.", "The $4 example does not add up."]}')
gp.write_segment = lambda k, plan, key, notes, before: (calls.append("rewrite:" + key) or
                   "[EDUCATION_TOPIC: What VIX Actually Measures]\nALEX: The VIX comes from option prices.\n"
                   "SAM: That's our learning segment for this week.")
try:
    segs = dict(SEG)
    gp._check_lesson("k", PL, segs, None, log=lambda *a: None)
    ck("a lesson with factual errors is rewritten with the checker's notes", calls, ["Lesson fact-check", "rewrite:learning"])
    ck("...and the corrected lesson is used", "delta-weighted" in segs["learning"], False)
    calls.clear()
    gp._groq_call = lambda *a, **k: '{"problems": []}'
    segs = dict(SEG)
    gp._check_lesson("k", PL, segs, None, log=lambda *a: None)
    ck("an accurate lesson is left alone", segs["learning"], SEG["learning"])

    J = {"desk": "ALEX: The market fell.\nSAM: Why?\nALEX: Rates.\nSAM: Over to Nvidia then.",
         "main": "ALEX: The bill cleared late.\nSAM: And?\nALEX: Nvidia changed CEO.\nSAM: Wow."}
    good = "ALEX: Rates did most of it.\nSAM: Which brings us to Nvidia.\n--- sting ---\nALEX: Nvidia had the bigger week of all.\nSAM: And?\nALEX: It changed chief executive."
    gp._groq_call = lambda *a, **k: good
    segs = dict(J)
    gp._edit_seams("k", dict(P, facts={}), segs, None, log=lambda *a: None)
    ck("a smoother join is kept", segs["main"].split("\n")[0], "ALEX: Nvidia had the bigger week of all.")
    gp._groq_call = lambda *a, **k: good.replace("the bigger week of all", "a 12% drop and a $40,000 hit")
    segs = dict(J)
    gp._edit_seams("k", dict(P, facts={}), segs, None, log=lambda *a: None)
    ck("a join rewrite that adds numbers is rejected", segs, J)
finally:
    gp._groq_call, gp.write_segment = _rc, _rw

print("\n── what is read aloud, and who it belongs to ──")
gp.SPOKEN_NAMES["ZWB.TO"] = "BMO's Canadian bank covered-call fund"
ck("a ticker becomes a name", gp._speakable("ALEX: ZWB.TO just landed in the portfolio."),
   "ALEX: BMO's Canadian bank covered-call fund just landed in the portfolio.")
ck("a 3x ticker becomes 'the ... fund'", gp._speakable("ALEX: SPXL rose, the FNGU fund fell."),
   "ALEX: The S&P 500 3x fund rose, the FANG+ 3x fund fell.")
ck("short words are never taken for tickers", gp._speakable("SAM: The ET position and V are small; the CEO spoke."),
   "SAM: The ET position and V are small; the CEO spoke.")
ck("Christopher's trades are his", gp._speakable("ALEX: We sold Broadcom and we've added to Nvidia."),
   "ALEX: You sold Broadcom and you added to Nvidia.")
ck("billions are said as billions", gp._digitize("about three hundred billion dollars"), "about $300 billion")
ck("'6.2 %' is tidied", gp._speakable("ALEX: up 6.2 % on the week"), "ALEX: up 6.2% on the week")
ck("a leftover ticker is spelled, not read as a word", gp.normalize_for_speech("XYZ.TO moved"), "X Y Z moved")
ck("a lesson title is said the way a person would", C.spoken("Free Cash Flow, the Number Behind the Earnings"),
   "free cash flow, the number behind the earnings")
ck("...keeping acronyms and names", C.spoken(C.CURRICULUM[0][0]), "how the FHSA and the RRSP Home Buyers' Plan work together")
ck("the preview names subjects, not a menu of segments", "not segment names" in gp.SEG_OPENING, True)

print("\n── the two hosts share the talking ──")
lop = "\n".join(["ALEX: " + "word " * 40, "SAM: Why?"] * 4)
ck("a segment where SAM only asks is sent back", any("SAM says only" in m for m in gp.segment_problems("main", lop, P)), True)
q = "\n".join(["ALEX: " + "word " * 10, "SAM: But why " + "word " * 9 + "?"] * 4)
ck("...and so is one where SAM's turns are nearly all questions", any("interview" in m for m in gp.segment_problems("main", q, P)), True)
ok = "\n".join(["ALEX: " + "word " * 12, "SAM: I think " + "word " * 10 + "."] * 4)
ck("a balanced exchange passes", [m for m in gp.segment_problems("main", ok, P) if "SAM" in m], [])
ck("SAM leads the around-the-portfolio segment", "SAM leads this segment" in gp.SEG_AROUND, True)

print("\n── packaging ──")
plan = {"main": {"title": "Nvidia"}, "topic": "Share Buybacks"}
ch = gp._chapters(plan, [0.0, 33.2, 120.5, 300.1, 420.0, 560.9], 900)
ck("six chapters with their titles", [c["title"] for c in ch],
   ["Intro", "The week that was", "The big story: Nvidia", "Around the portfolio", "Lesson: share buybacks", "The week ahead"])
ck("...in whole seconds", ch[1]["t"], 33)
ck("no measured breaks, no chapters (never wrong ones)", gp._chapters(plan, [0.0], 900), [])
ck("the disclaimer is fixed wording", "not financial advice" in gp.DISCLAIMER.lower() or "nothing in it is financial advice" in gp.DISCLAIMER.lower(), True)
with tempfile.TemporaryDirectory() as d:
    real = gp.DATA_DIR
    gp.DATA_DIR = Path(d)
    try:
        gp.write_feed({"episode": 2, "file": "podcast_ep002.mp3", "date": "2026-11-23", "title": "T & <x>",
                       "duration_seconds": 1000, "chapters": [{"t": 0, "title": "Intro"}],
                       "show_notes": {"big_story": "Nvidia"},
                       "archive": [{"episode": 1, "file": "podcast_ep001.mp3", "date": "2026-11-16", "title": "Old"}]})
        import xml.dom.minidom
        feed = (Path(d) / "podcast_feed.xml").read_text()
        doc = xml.dom.minidom.parseString(feed)
        ck("the feed is valid XML with every kept episode", len(doc.getElementsByTagName("item")), 2)
        ck("...titles are escaped", "T &amp; &lt;x&gt;" in feed, True)
        ck("...and it is kept out of the public directories", "<itunes:block>Yes</itunes:block>" in feed, True)
    finally:
        gp.DATA_DIR = real

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
