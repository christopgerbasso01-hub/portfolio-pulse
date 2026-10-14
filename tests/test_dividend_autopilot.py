import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api'))
os.environ.setdefault('KV_REST_API_URL',''); os.environ.setdefault('KV_REST_API_TOKEN','')
from notify import shares_at_ex_date, resolve_rate, score_confidence, booked_dividend_for, learned_pay_lag, purge_booked
import notify

fails = []
def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:52} got={got} want={want}")
    if not ok: fails.append(name)

print("── shares_at_ex_date (current=100, ex=2026-07-15) ──")
T=[
 ("no txs in window",            [], (100.0,False)),
 ("BUY 10 AFTER ex -> excluded", [{"ticker":"X","account":"TFSA","date":"2026-07-20","type":"BUY","shares":10}], (90.0,False)),
 ("BUY 10 ON ex-date -> excluded",[{"ticker":"X","account":"TFSA","date":"2026-07-15","type":"BUY","shares":10}], (90.0,False)),
 ("BUY 10 BEFORE ex -> counted", [{"ticker":"X","account":"TFSA","date":"2026-07-14","type":"BUY","shares":10}], (100.0,False)),
 ("SELL 10 after ex -> added bk",[{"ticker":"X","account":"TFSA","date":"2026-07-20","type":"SELL","shares":10}], (110.0,False)),
 ("other account ignored",       [{"ticker":"X","account":"RRSP","date":"2026-07-20","type":"BUY","shares":10}], (100.0,False)),
 ("other ticker ignored",        [{"ticker":"Y","account":"TFSA","date":"2026-07-20","type":"BUY","shares":10}], (100.0,False)),
 ("SPLIT after ex -> flagged",   [{"ticker":"X","account":"TFSA","date":"2026-07-20","type":"SPLIT"}], (100.0,True)),
 ("never negative",              [{"ticker":"X","account":"TFSA","date":"2026-07-20","type":"BUY","shares":500}], (0.0,False)),
]
for n,txs,want in T:
    ck(n, shares_at_ex_date("X","TFSA","2026-07-15",100.0,txs), want)

print("\n── resolve_rate (statutory, no learned data) ──")
L={}
ck("CM.TO in TFSA -> 0%",        resolve_rate("CM.TO","TFSA",L),  (0.0,"statutory"))
ck("AAPL in RRSP -> 0% (treaty)",resolve_rate("AAPL","RRSP",L),   (0.0,"statutory"))
ck("AAPL in TFSA -> 15%",        resolve_rate("AAPL","TFSA",L),   (0.15,"statutory"))
ck("AAPL in FHSA -> 15%",        resolve_rate("AAPL","FHSA",L),   (0.15,"statutory"))
ck("TSM  -> 21% Taiwan",         resolve_rate("TSM","TFSA",L),    (0.21,"statutory"))
ck("SHEL -> 0% UK",              resolve_rate("SHEL","TFSA",L),   (0.0,"statutory"))
ck("ET   -> 37% MLP",            resolve_rate("ET","TFSA",L),     (0.37,"statutory"))
ck("TSM in RRSP still 21%",      resolve_rate("TSM","RRSP",L),    (0.21,"statutory"))

print("\n── learned rate overrides statutory ──")
ck("1 sample: not enough",  resolve_rate("ET","TFSA",{"ET|TFSA":{"effective_net":0.63,"samples":1}}), (0.37,"statutory"))
r,b = resolve_rate("ET","TFSA",{"ET|TFSA":{"effective_net":0.63,"samples":4}})
ck("4 samples: learned wins", (round(r,4),b), (0.37,"learned"))

print("\n── confidence ──")
ck("CA ticker -> HIGH",      score_confidence("CM.TO","statutory",False,False), "HIGH")
ck("learned -> HIGH",        score_confidence("AAPL","learned",False,False),    "HIGH")
ck("US statutory -> MEDIUM", score_confidence("AAPL","statutory",False,False),  "MEDIUM")
ck("ET statutory -> LOW",    score_confidence("ET","statutory",False,False),    "LOW")
ck("split -> LOW",           score_confidence("CM.TO","learned",True,False),    "LOW")
ck("estimated pay -> LOW",   score_confidence("AAPL","statutory",False,True),   "LOW")

print("\n── already-booked dividends are never queued again (2026-10-14) ──")
LEDGER = [
 {"type":"DIVIDEND","ticker":"UDOW","account":"RRSP","date":"2026-09-29","amount":5.27},
 {"type":"DIVIDEND","ticker":"UDOW","account":"TFSA","date":"2026-09-29","amount":10.47},
 {"type":"DIVIDEND","ticker":"UDOW","account":"FHSA","date":"2026-10-14","amount":10.71},
 {"type":"DIVIDEND","ticker":"TXF.TO","account":"FHSA","date":"2026-08-31","amount":206.19},
 {"type":"DIVIDEND","ticker":"TXF.TO","account":"FHSA","date":"2026-09-30","amount":124.04,"source":"autopilot"},
 {"type":"DIVIDEND","ticker":"TXF.TO","account":"FHSA","date":"2026-08-17","amount":185.88,"source":"autopilot"},
]
ck("UDOW RRSP ex 9/23 matches the 9/29 booking",
   (booked_dividend_for("UDOW","RRSP","2026-09-23","2026-12-23",LEDGER) or {}).get("amount"), 5.27)
ck("UDOW FHSA booked 10/14 still matches",
   (booked_dividend_for("UDOW","FHSA","2026-09-23","2026-12-23",LEDGER) or {}).get("amount"), 10.71)
ck("TXF ex 9/23 matches 9/30, not the August payment",
   (booked_dividend_for("TXF.TO","FHSA","2026-09-23","2026-10-23",LEDGER) or {}).get("amount"), 124.04)
ck("another account's booking does not count",
   booked_dividend_for("UDOW","Investment","2026-09-23","2026-12-23",LEDGER), None)
ck("a payout with nothing booked is still queued",
   booked_dividend_for("UDOW","RRSP","2026-12-24","",LEDGER), None)

PEND = [{"id":f"{t}|{a}|2026-09-23","ticker":t,"account":a,"ex_date":"2026-09-23"}
        for t,a in [("UDOW","RRSP"),("UDOW","TFSA"),("UDOW","FHSA"),("TXF.TO","FHSA"),("TXF.TO","Investment")]]
st = {"pending": [dict(p) for p in PEND]}
dropped = purge_booked(st, LEDGER)
ck("the duplicates waiting in the queue are cleared", sorted(dropped),
   sorted(["UDOW|RRSP|2026-09-23","UDOW|TFSA|2026-09-23","UDOW|FHSA|2026-09-23","TXF.TO|FHSA|2026-09-23"]))
ck("...an entry with no booking stays", [p["id"] for p in st["pending"]], ["TXF.TO|Investment|2026-09-23"])
ck("...and cleared ones are remembered so they never return", st["seen"]["UDOW|RRSP|2026-09-23"]["action"], "already_booked")
st2 = {"pending":[{"id":"TXF.TO|FHSA|2026-09-23","ticker":"TXF.TO","account":"FHSA","ex_date":"2026-09-23"}]}
ck("a monthly payer's NEXT payment does not clear this one",
   purge_booked(st2, [{"type":"DIVIDEND","ticker":"TXF.TO","account":"FHSA","date":"2026-10-30"}]), [])

print("\n── payment-date estimate learned from the ledger ──")
TXF_EX = ["2026-07-27","2026-08-25","2026-09-23"]
ck("TXF: real bookings 6-7 days after ex; the old 21-day echo is ignored",
   learned_pay_lag("TXF.TO", TXF_EX, LEDGER), 7)
ck("UDOW learns from the hand bookings", learned_pay_lag("UDOW", ["2026-06-24","2026-09-23"], LEDGER[:2]), 6)
ck("no history -> None (caller uses the default)", learned_pay_lag("SPXL", ["2026-09-23"], LEDGER), None)
ck("default lag is about a week, not three", notify.ASSUMED_PAY_LAG_DAYS, 7)

print("\n── detect() end to end with today's real case ──")
notify.fetch_dividend_events = lambda t: [{"ex_date":"2026-09-23","amount":0.146,"ccy":"USD"}] if t=="UDOW" else []
notify.fetch_payment_dates   = lambda t: {}
SET = {"computed_holdings":[{"ticker":"UDOW","account":"RRSP","shares":36},{"ticker":"UDOW","account":"Investment","shares":20}],
       "transactions": LEDGER}
found, diag = notify.detect(SET, {"start_date":"2026-09-01"})
ck("booked UDOW RRSP is skipped; unbooked Investment is queued", [f["account"] for f in found], ["Investment"])
ck("...dated about a week after the ex-date, not three weeks", found[0]["pay_date"] if found else None, "2026-09-29")
ck("...and diagnostics count the skip", diag.get("already_booked"), 1)

print(f"\n{'ALL PASS' if not fails else 'FAILURES: '+', '.join(fails)}")
sys.exit(1 if fails else 0)
