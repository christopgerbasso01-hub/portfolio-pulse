"""
Currency-direction guard — scripts/fx_guard.py

The investor is CAD-based and holds US-listed assets in USD. USD/CAD rising
means a stronger US dollar and MORE CAD value. Both the weekly podcast and the
daily briefing kept saying the opposite: episode 18 said "a firmer greenback
compresses the CAD-denominated value of our leveraged tech ETFs ... we're
holding them in CAD", and 28 distinct sentences across two months of briefings
had a stronger USD "eroding" CAD value. The briefing fed the podcast, so the
podcast repeated it.

The guard is deliberately conservative — it must not flag sound scripts — so
half of this file is things it must LEAVE ALONE.

Run: python3 tests/test_fx_guard.py
"""
import glob
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import fx_guard as g   # noqa: E402

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


def flagged(text):
    return bool(g.fx_problems(text))


print("── wrong direction: must be flagged ──")
for label, text in [
    ("ep18: firmer greenback compresses value (the reported sentence)",
     "A firmer greenback compresses the CAD‑denominated value of our leveraged tech ETFs, "
     "because the underlying U.S. indices would be priced higher in USD but we’re holding them in CAD."),
    ("briefing: stronger USD pressuring USD-heavy exposure",
     "A larger‑than‑expected CPI would likely push the Fed toward another rate hike, strengthening "
     "the USD against the CAD and pressuring the portfolio’s USD‑heavy exposure."),
    ("briefing: lift the USD, increasing translation drag",
     "A CPI surprise to the upside could lift the USD, increasing the translation drag on the "
     "$170,721 USD notional."),
    ("briefing: weaker USD 'reducing translation drag'",
     "If CPI comes in below expectations, the USD may weaken, reducing translation drag."),
    ("briefing: stronger USD erodes CAD value",
     "Stronger USD erodes CAD value of $167,863 USD notional."),
    ("briefing: USD weakens, translation gain",
     "USD weakens 0.02 CAD, translation gain."),
    ("briefing: weaker USD boosts value of USD assets",
     "A weaker USD could boost the portfolio’s value, especially for USD-denominated assets like FNGU and SPXL."),
    ("briefing: CAD weakening erodes returns on USD positions",
     "Rapid CAD weakening erodes returns on USD-heavy positions."),
    ("ep16: CAD slips, eroding the USD exposure",
     "A surprise surge in crude inventories drives Brent down, and the CAD slips to 1.3650, eroding "
     "the USD exposure by roughly $0.015 CAD per dollar."),
    ("stronger Canadian dollar RAISES US holdings (backwards)",
     "A stronger Canadian dollar raises the value of our US holdings."),
]:
    ck(label, flagged(text), True)

print("\n── right direction: must pass ──")
for label, text in [
    ("plain correct statement",
     "When the US dollar gets stronger against the Canadian dollar, our US holdings are worth more "
     "in Canadian dollars."),
    ("ep18: stronger dollar means more CAD",
     "That would strengthen the U.S. dollar – and a stronger dollar means our $172,166 USD notional "
     "translates into more CAD."),
    ("a falling USD/CAD shaves value",
     "A 3‑cent drop in USD/CAD would shave $5,166 CAD off our USD holdings."),
    ("a weaker USD lowers CAD value",
     "A weaker US dollar lowers the CAD value of our USD holdings."),
    ("both directions in one sentence is ambiguous, so left alone",
     "A weakening CAD would increase the CAD value of USD assets, but a strengthening CAD erodes them."),
    ("a question is not a claim",
     "Hold on – if the CAD weakens versus the USD, wouldn’t that increase the CAD value of our USD "
     "assets, not decrease it?"),
    ("an amount with 'CAD gain' is not the Canadian dollar gaining",
     "That secondary boost could translate into an extra $2,500 CAD gain on a $50,000 position."),
    ("'dollar-strengthening pressure' is the cause, not an effect on value",
     "That dollar‑strengthening pressure pushes the USD/CAD higher – a cent‑move that translates "
     "directly into CAD value for our USD‑denominated holdings."),
    ("volatility hurting positions is not an FX claim",
     "A spike in volatility could hurt the FANG+ 3‑x and Dow 3‑x positions, especially if the market "
     "interprets a stronger dollar as a sign of tightening rates."),
    ("'reduces diversification' is not a value effect",
     "Limited Canadian equity exposure reduces diversification against a potential USD strength cycle."),
    ("'increases the risk of a decline' IS a decline",
     "The portfolio’s significant USD exposure increases the risk of a decline in the portfolio’s "
     "value if the CAD strengthens against the USD."),
    ("a firmer CAD helping Canadian banks is not about USD holdings",
     "A firmer CAD would benefit our Canadian bank names."),
    ("a per-cent sensitivity with no direction",
     "Each cent move in USD/CAD adds $1,722 CAD to our USD holdings."),
]:
    ck(label, flagged(text), False)

print("\n── held in CAD ──")
ck("'we're holding them in CAD' about US ETFs is flagged",
   flagged("The U.S. indices are priced in USD but we’re holding the ETFs in CAD."), True)
ck("holding a CAD amount of a stock is fine",
   flagged("We hold about $14,000 CAD of Energy Transfer."), False)
ck("CAD cash is fine",
   flagged("We hold about $5,000 in CAD cash in the RRSP."), False)

print("\n── scrubbing input ──")
text = ("Rates may rise. A stronger USD erodes CAD value of our USD holdings. "
        "Oil fell on Friday.")
clean, removed = g.scrub_fx_sentences(text)
ck("only the offending sentence is removed", removed, ["A stronger USD erodes CAD value of our USD holdings."])
ck("the other sentences survive",
   ("Rates may rise." in clean and "Oil fell on Friday." in clean and "erodes" not in clean), True)
ck("clean text passes through unchanged", g.scrub_fx_sentences("Oil fell on Friday.")[0], "Oil fell on Friday.")
ck("empty input is safe", g.scrub_fx_sentences(""), ("", []))
ck("None-safe", g.scrub_fx_sentences(None), (None, []))

print("\n── real published episodes ──")


def spoken(path):
    txt = open(path).read()
    return "\n".join(l.split(":", 1)[1] for l in txt.splitlines()
                     if l.strip().startswith(("ALEX:", "SAM:")))


eps = {os.path.basename(p)[10:13]: p for p in glob.glob(os.path.join(ROOT, 'data', 'podcast_ep*.txt'))}
if "018" in eps:
    hits = g.fx_problems(spoken(eps["018"]))
    ck("ep18 has exactly one wrong-direction sentence", len(hits), 1)
    ck("...and it is the greenback sentence", "greenback" in hits[0][0], True)
if "017" in eps:
    ck("ep17 (correct on FX) is not flagged", len(g.fx_problems(spoken(eps["017"]))), 0)
if "015" in eps:
    ck("ep15 (corrects itself mid-episode) is not flagged", len(g.fx_problems(spoken(eps["015"]))), 0)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
