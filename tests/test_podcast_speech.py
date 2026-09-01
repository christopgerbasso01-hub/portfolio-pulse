"""
Speech normalisation for the podcast TTS — scripts/generate_podcast.py

The script was passed to edge-tts verbatim, so "**$17,427 CAD**" was spoken as
"dollar sign four, two hundred twenty" style gibberish: the engine read the
symbol and the comma literally and the markdown asterisks aloud.

Run: python3 tests/test_podcast_speech.py
"""
import importlib.util
import os
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   '..', 'scripts', 'generate_podcast.py')

# Import only the pure helpers — the module's import-time work needs network/env.
_src = open(SRC).read()
_ns = {"re": re}
for _fn in ("_int_words", "_num_words", "_money_words", "normalize_for_speech"):
    m = re.search(rf"^def {_fn}\(.*?(?=^\S|\Z)", _src, re.S | re.M)
    exec(m.group(0), _ns)
for _const in ("_ONES", "_TENS", "_SCALES", "_MAGNITUDE", "_CURRENCY"):
    m = re.search(rf"^{_const} = .*?(?=^\S|\Z)", _src, re.S | re.M)
    exec(m.group(0), _ns)

norm = _ns["normalize_for_speech"]
int_words = _ns["_int_words"]

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got : {got!r}\n        want: {want!r}")
        fails.append(name)


print("── integers ──")
for n, want in [(0, "zero"), (7, "seven"), (19, "nineteen"), (20, "twenty"),
                (42, "forty-two"), (100, "one hundred"), (605, "six hundred five"),
                (4678, "four thousand six hundred seventy-eight"),
                (12000, "twelve thousand"),
                (233605, "two hundred thirty-three thousand six hundred five")]:
    ck(f"{n}", int_words(n), want)

print("\n── the reported bug ──")
ck("$4,220 reads as an amount",
   norm("It added $4,220 to the account."),
   "It added four thousand two hundred twenty dollars to the account.")

print("\n── real phrases from episode 14 ──")
ck("bold + CAD suffix",
   norm("The portfolio jumped **$17,427 CAD**, which is a **6.2 %** move."),
   "The portfolio jumped seventeen thousand four hundred twenty-seven Canadian "
   "dollars, which is a six point two percent move.")
ck("leveraged exposure",
   norm("via our $155,927 CAD leveraged exposure"),
   "via our one hundred fifty-five thousand nine hundred twenty-seven Canadian "
   "dollars leveraged exposure")
ck("percent without space",
   norm("the chipmaker fell 23 % in the TFSA"),
   "the chipmaker fell twenty-three percent in the TFSA")

print("\n── magnitudes, currency, edge cases ──")
ck("$1.2M", norm("worth $1.2M today"), "worth one point two million dollars today")
ck("$140K", norm("about $140K"), "about one hundred forty thousand dollars")
ck("USD suffix", norm("$500 USD"), "five hundred US dollars")
ck("singular dollar", norm("just $1 left"), "just one dollar left")
ck("3x leverage", norm("our 3x leveraged ETFs"), "our three times leveraged ETFs")
ck("bare grouped number", norm("we hold 12,000 shares"), "we hold twelve thousand shares")
ck("year is left alone", norm("returning in March 2027"), "returning in March 2027")
ck("plain ticker untouched", norm("NVDA and SPXL"), "NVDA and SPXL")

print("\n── no leftovers on the real script ──")
ep = "/tmp/ep14.txt"
if os.path.exists(ep):
    lines = [l.split(":", 1)[1] for l in open(ep) if l.startswith(("ALEX:", "SAM:"))]
    out = " ".join(norm(l) for l in lines)
    for label, pattern in [("dollar signs", r"\$"), ("percent signs", r"%"),
                           ("markdown asterisks", r"\*"),
                           ("comma-grouped digits", r"\d,\d")]:
        hits = re.findall(pattern, out)
        ck(f"no {label} remain ({len(lines)} turns)", len(hits), 0)
else:
    print("  (skipped — /tmp/ep14.txt not present)")

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
