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

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
