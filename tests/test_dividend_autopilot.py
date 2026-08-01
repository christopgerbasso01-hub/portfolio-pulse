import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api'))
os.environ.setdefault('KV_REST_API_URL',''); os.environ.setdefault('KV_REST_API_TOKEN','')
from dividends import shares_at_ex_date, resolve_rate, score_confidence

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

print(f"\n{'ALL PASS' if not fails else 'FAILURES: '+', '.join(fails)}")
sys.exit(1 if fails else 0)
