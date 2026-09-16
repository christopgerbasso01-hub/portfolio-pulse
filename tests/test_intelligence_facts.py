"""
Portfolio figures in the daily briefing — scripts/generate_intelligence.py

The intelligence generator never fetched holdings. Its only portfolio data was a
hand-maintained constant behind a comment asking a human to update it, and the
output schema asked the model for "approximate CAD value affected" with worked
examples of the right shape. So the model invented the numbers:

    intel said            actual (2026-09-14)
    MSFT   ~$18,000  ->   $1,405      12.8x
    QCOM   ~$10,000  ->   $1,252       8.0x
    NVDA   ~$30,000  ->   $11,733      2.6x
    ENB.TO ~$12,000  ->   $5,496       2.2x

Those strings are rendered verbatim on the dashboard (index.html renders
`n.exposure` for every news card) and were read aloud in podcast episode 16.

_live_portfolio_block() replaces the guesswork with a computed table. These
tests pin the three things that matter: the numbers are real, a price-less
snapshot is never used, and when the data cannot be fetched the model is told
it does not know rather than left free to invent.

Run: python3 tests/test_intelligence_facts.py
"""
import importlib.util
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   '..', 'scripts', 'generate_intelligence.py')
spec = importlib.util.spec_from_file_location("gi_facts", SRC)
gi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gi)

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


HOLDINGS = [
    {"ticker": "FNGU",   "shares": 100, "ccy": "USD", "account": "TFSA",       "name": "FANG+ 3x ETF"},
    {"ticker": "NVDA",   "shares": 40,  "ccy": "USD", "account": "TFSA",       "name": "NVIDIA"},
    {"ticker": "ENB.TO", "shares": 82,  "ccy": "CAD", "account": "FHSA",       "name": "Enbridge"},
    {"ticker": "CASHUSD", "shares": 7685, "ccy": "USD", "account": "RRSP",     "name": "Cash"},
]
FAT = {
    "total_value": 20000, "roi_pct": 68.3, "usdcad": 1.40,
    "accounts": {"TFSA": 14280, "FHSA": 5494},
    "holdings_prices": {"FNGU": {"price": 32.0}, "NVDA": {"price": 175.0},
                        "ENB.TO": {"price": 67.0}},
}
THIN = {"total_value": 20500, "accounts": {"TFSA": 14000}}   # no holdings_prices


def stub(settings=None, snapshots=None, boom=False):
    def fake_get(url, **kw):
        if boom:
            raise RuntimeError("network down")
        if "settings" in url:
            return FakeResp({"computed_holdings": settings if settings is not None else HOLDINGS})
        return FakeResp({"snapshots": snapshots if snapshots is not None else {"2026-09-15": FAT}})
    gi.requests.get = fake_get


_real_get = gi.requests.get

# ── the figures are real ─────────────────────────────────────────────────────
print("── computed position table ──")
stub()
block = gi._live_portfolio_block()

# 100 x 32.00 USD x 1.40 = 4,480 | 40 x 175.00 x 1.40 = 9,800 | 82 x 67.00 CAD = 5,494
ck("FNGU priced from shares x price x fx", "$    4,480 CAD" in block, True)
ck("NVDA priced from shares x price x fx", "$    9,800 CAD" in block, True)
ck("CAD-denominated holding is not FX-converted", "$    5,494 CAD" in block, True)
ck("cash rows are excluded from the table", "CASHUSD" in block, False)
ck("accounts are listed per holding", "[TFSA]" in block and "[FHSA]" in block, True)
ck("leveraged sleeve totalled", "3x leveraged: $4,480" in block, True)

# USD book 4,480 + 9,800 = 14,280 CAD -> notional 14,280 / 1.40 = 10,200 USD
ck("USD exposure stated in CAD", "$14,280 CAD" in block, True)
ck("USD notional published so FX cannot be re-derived wrongly",
   "$10,200 USD notional" in block, True)
# The bug this prevents: 14,280 x 0.01 = $143, not 10,200 x 0.01 = $102.
ck("FX sensitivity uses the notional, not the CAD value", "$102 CAD" in block, True)
ck("...and not the CAD value", "$143 CAD" in block, False)
ck("the FX rule is stated explicitly",
   "Never multiply the CAD value by the cent move" in block, True)

# ── a price-less snapshot is never the source ────────────────────────────────
print("\n── price-less (intraday) snapshots ──")
stub(snapshots={"2026-09-15": FAT, "2026-09-16": THIN})
block = gi._live_portfolio_block()
ck("the thin snapshot is skipped for the priced one",
   "LIVE POSITIONS as of 2026-09-15" in block, True)
ck("the thin snapshot's total is not used", "20,500" in block, False)
ck("real figures still present", "$    4,480 CAD" in block, True)

stub(snapshots={"2026-09-16": THIN})
ck("all-thin history withholds figures entirely",
   gi._live_portfolio_block(), gi._NO_FIGURES_BLOCK)

# ── missing data must forbid figures, never invite them ──────────────────────
print("\n── degraded paths ──")
stub(boom=True)
ck("a failed fetch withholds figures", gi._live_portfolio_block(), gi._NO_FIGURES_BLOCK)
stub(settings=[])
ck("no holdings withholds figures", gi._live_portfolio_block(), gi._NO_FIGURES_BLOCK)
stub(snapshots={})
ck("no snapshots withholds figures", gi._live_portfolio_block(), gi._NO_FIGURES_BLOCK)
# Normalised so the assertion tests the instruction, not where the line wraps.
_no_figs = " ".join(gi._NO_FIGURES_BLOCK.split())
ck("the withholding block forbids estimating rather than staying silent",
   "Do not state, estimate or illustrate one" in _no_figs, True)

# ── the prompt actually carries the block ────────────────────────────────────
print("\n── prompt wiring ──")
gi.requests.get = _real_get
prompt = gi.build_prompt([], {}, portfolio_block="SENTINEL-POSITIONS-TABLE")
ck("build_prompt injects the computed block", "SENTINEL-POSITIONS-TABLE" in prompt, True)
prompt_bare = gi.build_prompt([], {})
ck("a caller that passes nothing still forbids invented figures",
   "position size unavailable" in prompt_bare, True)
ck("the stale hand-maintained totals are gone from the constant",
   any(s in gi.PORTFOLIO_CONTEXT for s in ("$280K", "+83%", "$100K", "$1,800")), False)
ck("the schema no longer shows a worked CAD example to copy",
   "FNGU ~$87K CAD" in gi.OUTPUT_SCHEMA, False)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
