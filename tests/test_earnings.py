"""
Earnings on held stocks — scripts/earnings.py, and how the podcast uses it.

Fixtures follow the real Yahoo quoteSummary shapes seen for TSLA and RY.TO in
October 2026 (RY.TO's earningsDate was still the August report while
earningsCallDate already held December's).

Run: python3 tests/test_earnings.py
"""
import importlib.util
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import earnings as E  # noqa: E402

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


def ts(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


NOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)          # Thu 2pm New York
TSLA = {
    "quoteType": {"quoteType": "EQUITY"}, "price": {"shortName": "Tesla, Inc."},
    "calendarEvents": {"earnings": {
        "earningsDate": [{"raw": ts("2026-10-21T20:00:00")}], "earningsCallDate": [{"raw": ts("2026-10-21T20:00:00")}],
        "isEarningsDateEstimate": False, "earningsAverage": {"raw": 0.50}, "revenueAverage": {"raw": 29.3e9}}},
    "earnings": {
        "earningsChart": {"quarterly": [
            {"date": "2Q2026", "actual": {"raw": 0.40}, "estimate": {"raw": 0.41}, "reportedDate": {"raw": ts("2026-07-22T20:05:00")}},
            {"date": "3Q2026", "actual": {"raw": 0.48}, "estimate": {"raw": 0.45114}, "reportedDate": {"raw": ts("2026-10-21T20:01:42")}}]},
        "financialsChart": {"quarterly": [{"date": "2Q2026", "revenue": {"raw": 28236000000}},
                                          {"date": "3Q2026", "revenue": {"raw": 28046000000}}]}},
}
RY = {
    "quoteType": {"quoteType": "EQUITY"}, "price": {"shortName": "Royal Bank of Canada"},
    "calendarEvents": {"earnings": {
        "earningsDate": [{"raw": ts("2026-08-27T10:30:00")}],               # stale: the last report
        "earningsCallDate": [{"raw": ts("2026-12-03T13:00:00")}],           # the real next one
        "isEarningsDateEstimate": False, "earningsAverage": {"raw": 4.14}}},
    "earnings": {"earningsChart": {"quarterly": [
        {"date": "2Q2026", "actual": {"raw": 4.28}, "estimate": {"raw": 4.07478}, "reportedDate": {"raw": ts("2026-08-27T10:00:00")}}]},
        "financialsChart": {"quarterly": []}},
}

print("── parsing a report ──")
t = E.parse_summary("TSLA", TSLA, NOW)
L = t["last"]
ck("the latest REPORTED quarter is picked", L["quarter"], "3Q2026")
ck("report date is New York's date", L["reported_at"], "2026-10-21")
ck("4:01pm New York is after the close", L["timing"], "after the close")
ck("6.4% above the estimate is a beat", (L["verdict"], L["surprise_pct"]), ("beat", 6.4))
ck("revenue against the previous quarter", L["revenue_qoq_pct"], -0.7)
ck("a report already out is not 'upcoming'", t["next"], None)
r = E.parse_summary("RY.TO", RY, NOW)
ck("Yahoo's stale earningsDate is skipped for the real next date", r["next"]["date"], "2026-12-03")
ck("a Canadian bank's EPS is read too", r["last"]["verdict"], "beat")
ck("within 2% is in line", E.parse_summary("X", {**TSLA, "earnings": {"earningsChart": {"quarterly": [
    {"date": "Q", "actual": {"raw": 1.01}, "estimate": {"raw": 1.0}, "reportedDate": {"raw": ts("2026-10-20T12:00:00")}}]}}},
    NOW)["last"]["verdict"], "in line")
ck("a midday stamp is a placeholder, not a time", E.timing_of(ts("2026-11-02T17:00:00")), "time not confirmed")
ck("8am New York is before the open", E.timing_of(ts("2026-11-03T12:00:00")), "before the open")

print("\n── the market's verdict ──")
closes = {"2026-10-20": 100.0, "2026-10-21": 102.0, "2026-10-22": 98.94, "2026-10-23": 99.0}
ck("after the close: the next session against the report day", E.reaction_pct(closes, "2026-10-21", "after the close"), -3.0)
ck("before the open: the report day against the day before", E.reaction_pct(closes, "2026-10-21", "before the open"), 2.0)
ck("no next session yet: no reaction rather than a guess", E.reaction_pct(closes, "2026-10-23", "after the close"), None)

print("\n── the summary may not add numbers ──")
card = {"eps_actual": 0.48, "eps_estimate": 0.45, "surprise_pct": 6.4, "revenue_qoq_pct": -0.7, "reaction_pct": -3.08}
src = "Tesla beat estimates with EPS of $0.48; deliveries rose 7% and margins hit 18.1%."
kept = E.check_summary(["Earnings beat at $0.48 a share.", "Deliveries rose 7%.",
                        "Margins jumped to 25%.", "Guidance was unchanged."], src, card)
ck("bullets whose numbers are in the coverage stay", kept[:2], ["Earnings beat at $0.48 a share.", "Deliveries rose 7%."])
ck("an invented 25% is dropped", any("25%" in b for b in kept), False)
ck("at most three", len(kept) <= 3, True)

print("\n── building the block ──")
HOLD = [{"ticker": "TSLA", "account": "TFSA", "shares": 10, "ccy": "USD"},
        {"ticker": "TSLA", "account": "Investment", "shares": 5, "ccy": "USD"},
        {"ticker": "RY.TO", "account": "Investment", "shares": 20, "ccy": "CAD"},
        {"ticker": "FNGU", "account": "TFSA", "shares": 100, "ccy": "USD"},
        {"ticker": "BAD", "account": "TFSA", "shares": 1, "ccy": "USD"}]
calls = []


def fs(session, crumb, tkr):
    calls.append(tkr)
    if tkr == "BAD":
        raise RuntimeError("yahoo said no")
    return {"TSLA": TSLA, "RY.TO": RY}[tkr]


def fc(session, tkr):
    return {"TSLA": closes, "RY.TO": {"2026-10-28": 180.0}}[tkr]


summ = []
blk = E.build(HOLD, NOW, usdcad=1.40, total_value=100_000, fetch_summary_fn=fs, fetch_closes_fn=fc,
              news_fn=lambda t, a, b: [{"headline": "Tesla beats", "summary": "EPS $0.48"}, {"headline": "Shares fall 3%"}],
              summary_fn=lambda p: (summ.append(p) or ["EPS came in at $0.48.", "Shares fell 3%."]), log=lambda *a: None)
ck("funds are never asked for earnings", "FNGU" in calls, False)
ck("one ticker failing does not stop the rest", (blk["errors"], [c["ticker"] for c in blk["recent"]]), (1, ["TSLA"]))
c = blk["recent"][0]
ck("shares are summed across accounts", c["position_cad"], round(15 * 99.0 * 1.40))
ck("weight is against the whole portfolio", c["weight_pct"], round(15 * 99.0 * 1.40 / 100_000 * 100, 1))
ck("the move is turned into dollars for us", c["impact_cad"] < 0, True)
ck("Tesla is flagged as inside FNGU", c["in_fngu"], True)
ck("a summary is written from the coverage", c["summary"], ["EPS came in at $0.48.", "Shares fell 3%."])
ck("upcoming: the Canadian bank, outside 14 days, is left out", [u["ticker"] for u in blk["upcoming"]], [])
blk2 = E.build(HOLD, NOW, usdcad=1.40, total_value=100_000, fetch_summary_fn=fs, fetch_closes_fn=fc, previous=blk,
               news_fn=lambda *a: [], summary_fn=lambda p: (summ.append(p) or []), log=lambda *a: None)
ck("an earlier run's summary is reused, not rewritten", (blk2["recent"][0]["summary"], len(summ)),
   (["EPS came in at $0.48.", "Shares fell 3%."], 1))
ck("no total, no weight (never a wrong one)",
   E.build(HOLD[:1], NOW, fetch_summary_fn=fs, fetch_closes_fn=fc, log=lambda *a: None)["recent"][0]["weight_pct"], None)
ck("the old glance-card shape is still produced", E.legacy_upcoming({"upcoming": [{"ticker": "AAPL", "date": "2026-11-02", "eps_estimate": 1.98}]}),
   [{"symbol": "AAPL", "date": "2026-11-02", "eps_estimate": 1.98}])

print("\n── the podcast puts earnings first ──")
spec = importlib.util.spec_from_file_location("gp_e", os.path.join(ROOT, "scripts", "generate_podcast.py"))
gp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gp)
facts = {"positions": {"TSLA": {}, "SHEL": {}, "NVDA": {}}}
rec = [dict(c, ticker="TSLA", reported_at="2026-10-21", weight_pct=2.5, reaction_pct=-3.1),
       dict(c, ticker="SHEL", name="Shell PLC", reported_at="2026-10-29", weight_pct=6.0, reaction_pct=-7.0, in_fngu=False),
       dict(c, ticker="MSFT", reported_at="2026-10-28", weight_pct=0.5)]         # not held in `facts`
intel = {"earnings": {"recent": rec, "upcoming": [
    {"ticker": "NVDA", "date": "2026-11-04", "timing": "after the close", "name": "NVIDIA"},
    {"ticker": "NVDA", "date": "2026-12-20", "timing": "after the close", "name": "NVIDIA"}]}}
rep, big = gp._earnings_this_week(intel, facts, NOW)
ck("only holdings that reported in the last 8 days, biggest first", [x["ticker"] for x in rep], ["SHEL", "TSLA"])
ck("a 6% holding that moved 7% leads the episode", [x["ticker"] for x in big], ["SHEL"])
ck("...as the main story", gp._earnings_story(big[0])["title"], "Shell's earnings")
ck("the coming week's reports are named for the close",
   gp._earnings_ahead(intel, facts, NOW), "Nvidia on Wednesday, November 4 (after the close)")
brief = gp._earnings_brief(rep[1])
ck("the brief gives the scorecard, the move and our position",
   all(x in brief for x in ("$0.48 against $0.45 expected", "a beat", "fell about 3.1%", "We own about")), True)
ck("...and the FNGU line, without explaining the fund", "without explaining how the fund works" in brief, True)
ck("a quiet report does not take over the episode",
   gp._earnings_this_week({"earnings": {"recent": rec[:1]}}, facts, NOW)[1], [])
ck("no earnings data at all changes nothing", gp._earnings_this_week({}, facts, NOW), ([], []))

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
