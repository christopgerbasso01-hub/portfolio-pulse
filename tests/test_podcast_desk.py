"""
The portfolio desk and the show's memory — scripts/podcast_desk.py

Episodes 21 and 22 never said what the listener did that week, how much of the
change was his own money, how the mix moved, or whether last week's scenarios came
true. Those facts are computed here now; these tests pin the arithmetic and the
continuity rules.

Run: python3 tests/test_podcast_desk.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import podcast_desk as pd  # noqa: E402

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


SNAPS = {
    "2026-10-16": {"total_value": 100_000, "usdcad": 1.40,
                   "accounts": {"TFSA": 60_000, "FHSA": 25_000, "RRSP": 15_000},
                   "account_cost": {"TFSA": 50_000, "FHSA": 20_000, "RRSP": 10_000},
                   "holdings_prices": {"AAA": {"price": 100}, "BBB": {"price": 50}, "OLD": {"price": 10}}},
    "2026-10-23": {"total_value": 104_000, "usdcad": 1.42,
                   "accounts": {"TFSA": 63_000, "FHSA": 26_000, "RRSP": 15_000},
                   "account_cost": {"TFSA": 56_000, "FHSA": 20_000, "RRSP": 10_000},
                   "holdings_prices": {"AAA": {"price": 95}, "BBB": {"price": 55}, "NEW": {"price": 20}}},
}
SETTINGS = {
    "computed_holdings": [
        {"ticker": "AAA", "account": "TFSA", "shares": 300, "ccy": "CAD", "sector": "Technology", "name": "Aaa"},
        {"ticker": "BBB", "account": "FHSA", "shares": 100, "ccy": "USD", "sector": "Energy", "name": "Bbb"},
        {"ticker": "NEW", "account": "TFSA", "shares": 50, "ccy": "CAD", "sector": "Financials", "name": "Newco"},
    ],
    "transactions": [
        {"type": "CONTRIBUTION", "date": "2026-10-20", "account": "TFSA", "amount": 6000},
        {"type": "BUY", "date": "2026-10-20", "ticker": "NEW", "account": "TFSA", "shares": 50, "price": 20, "ccy": "CAD"},
        {"type": "BUY", "date": "2026-10-21", "ticker": "AAA", "account": "TFSA", "shares": 50, "price": 98, "ccy": "CAD"},
        {"type": "SELL", "date": "2026-10-22", "ticker": "OLD", "account": "RRSP", "shares": 40, "price": 10, "ccy": "CAD"},
        {"type": "DIVIDEND", "date": "2026-10-22", "ticker": "BBB", "account": "FHSA", "amount": 10, "ccy": "USD", "rate": 1.42},
        {"type": "DIVIDEND", "date": "2026-10-10", "ticker": "AAA", "account": "TFSA", "amount": 99, "ccy": "CAD"},  # before the window
    ],
}

print("── the week, computed ──")
c = pd.portfolio_changes(SETTINGS, SNAPS, "2026-10-16", "2026-10-23")
ck("value change", c["change"], 4_000)
ck("new money is read from the contributions", c["contrib_added"], 6_000)
ck("...so the market's share is the rest", c["market_change"], -2_000)
ck("total profit is value minus everything contributed", c["pnl_total"], 104_000 - 86_000)
ck("a first purchase is a new position", [t["kind"] for t in c["trades"] if t["ticker"] == "NEW"], ["new position"])
ck("buying more of something held is an addition", [t["kind"] for t in c["trades"] if t["ticker"] == "AAA"], ["added to"])
ck("selling everything is an exit", [t["kind"] for t in c["trades"] if t["ticker"] == "OLD"], ["sold out of"])
ck("...and listed as having left", c["exited_tickers"], ["OLD"])
ck("trade values are in Canadian dollars", [t["cad"] for t in c["trades"] if t["ticker"] == "AAA"], [4_900])
ck("only dividends inside the week count", c["dividends"]["count"], 1)
ck("...converted at their own rate", c["dividends"]["total"], 14.2)
ck("sector weights add to about 100", round(sum(c["sector_weights"].values())), 100)
ck("a sector that appeared shows as a shift", any(s["sector"] == "Financials" and s["then"] == 0 for s in c["sector_shifts"]), True)
ck("accounts carry value and weekly change", c["accounts"]["TFSA"], {"value": 63_000, "change": 3_000, "contributed": 56_000})
ck("the house is the FHSA plus the RRSP", c["house"]["value"], 41_000)
ck("the currency move is in cents", c["fx_cents"], 2.0)
ck("...and two cents is worth mentioning", pd.fx_is_newsworthy(c), True)
ck("...but half a cent is not", pd.fx_is_newsworthy({"fx_cents": -0.5}), False)

print("\n── scenarios, and scoring last week ──")
frame = pd.scenario_frame({"1": 4600, "2": 9300, "3": 14000})
ck("three fixed cases", [f["case"] for f in frame], ["base", "bull", "bear"])
ck("bull and bear use the same size of move", frame[1]["effect"] == frame[2]["effect"], True)
prev = {"scenarios": {"sp500_close": 6000.0, "band_pct": 1.5, "probabilities": {"base": 50, "bull": 30, "bear": 20}}}
ck("a 0.8% week is the base case", pd.score_last_week(prev, 6048.0)["case"], "base")
ck("...with the odds we gave it", pd.score_last_week(prev, 6048.0)["probability"], 50)
ck("a 2% rise is the bull case", pd.score_last_week(prev, 6120.0)["case"], "bull")
ck("a 2% fall is the bear case", pd.score_last_week(prev, 5880.0)["case"], "bear")
ck("no stored close, no scorecard", pd.score_last_week({"scenarios": {}}, 6000.0), None)
ck("odds are read back from the spoken lines",
   pd.scenario_probabilities("ALEX: Base case — 50 percent: calm.\nALEX: Bull case — 30 percent: up.\nALEX: Bear case - 20%: down."),
   {"base": 50, "bull": 30, "bear": 20})

print("\n── repetition across episodes ──")
ep22 = ("ALEX: Think of it like a grocery store that keeps a backup freezer. "
        "SAM: So it's like a farmer who plants extra corn? ALEX: It's like having a spare tire in the trunk. "
        "ALEX: Think of the economy as a four-season year.")
an = pd.extract_analogies(ep22)
ck("ep22's comparisons are recognised", [a for a in ("grocery store", "farmer plants", "spare tire") if a in an],
   ["grocery store", "farmer plants", "spare tire"])
ck("...and counted", pd.analogy_count(ep22), 4)
ck("plain speech is not an analogy", pd.extract_analogies("ALEX: It feels like a big week for us. Picture why that matters."), [])
ck("ep22's two deep dives were one story",
   pd.same_story("Asia-Focused Oil Storage Alters Global Supply Balance",
                 "Saudi Arabia to store oil in Singapore to bolster Asia supply security"), True)
ck("different stories are not", pd.same_story("China PMI Slides", "Tesla starts Semi deliveries"), False)

print("\n── memory ──")
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "m.json"
    ck("a missing file is an empty memory", pd.load_memory(f), {"episodes": []})
    m = pd.load_memory(f)
    for n in range(1, 16):
        m = pd.save_memory(f, m, {"episode": n, "learning_topic": f"T{n}"})
    ck("only the last twelve are kept", [e["episode"] for e in pd.load_memory(f)["episodes"]][0], 4)
    m = pd.save_memory(f, m, {"episode": 15, "learning_topic": "redo"})
    ck("redoing an episode replaces it", [e["learning_topic"] for e in m["episodes"] if e["episode"] == 15], ["redo"])
    ck("the previous episode is the latest before this one", pd.last_episode(m, 15)["episode"], 14)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
