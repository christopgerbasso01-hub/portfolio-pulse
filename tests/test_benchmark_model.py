"""
Contribution events behind XIRR and the contribution-weighted benchmark — api/market.py

The first version kept only the months where an account's contribution total
went UP. The non-registered account had two withdrawals ($9,026 in Aug 2024,
moved to the RRSP; $8,000 in Sep 2025), so they vanished and the following
deposits were understated by the same amounts. Totals still balanced, which is
why it went unnoticed, but money was treated as invested for months it wasn't.

Run: python3 tests/test_benchmark_model.py
"""
import importlib.util
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api', 'market.py')
spec = importlib.util.spec_from_file_location('market', SRC)
market = importlib.util.module_from_spec(spec)
spec.loader.exec_module(market)

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ('' if ok else f"\n        got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


print('\nPre-log contribution events')
ev = market.contribution_events([])
net = {}
for e in ev:
    net[e['account']] = net.get(e['account'], 0) + e['amount']
ck('net per account matches the dashboard base (May 2026)',
   {k: round(v) for k, v in net.items()}, {'TFSA': 44500, 'Investment': 65000, 'FHSA': 24000, 'RRSP': 16132})
ck('Aug 2024 non-reg withdrawal is kept',
   [e['amount'] for e in ev if e['date'] == '2024-08-15' and e['account'] == 'Investment'], [-9026.0])
ck('Sep 2025 non-reg withdrawal is kept',
   [e['amount'] for e in ev if e['date'] == '2025-09-15' and e['account'] == 'Investment'], [-8000.0])
ck('May 2025 deposit is the full $17,394',
   [e['amount'] for e in ev if e['date'] == '2025-05-15' and e['account'] == 'Investment'], [17394.0])
ck('events are in date order', [e['date'] for e in ev], sorted(e['date'] for e in ev))

logged = market.contribution_events([{'type': 'CONTRIBUTION', 'date': '2026-06-04', 'account': 'Investment', 'amount': 13000},
                                     {'type': 'BUY', 'date': '2026-06-04', 'account': 'Investment', 'amount': 99}])
ck('logged CONTRIBUTION added, other types ignored', len(logged), len(ev) + 1)

print('\nBenchmark units')
hist = {'sp500': [('2024-01-02', 100.0), ('2024-06-03', 200.0)]}
u = market.benchmark_units([{'date': '2024-01-01', 'account': 'A', 'amount': 1000},
                            {'date': '2024-06-01', 'account': 'A', 'amount': -1000}], hist)
ck('deposit buys 10 units at 100; withdrawal sells 5 at 200 → 5 left', round(u['A']['sp500'], 9), 5.0)

print(f"\n{len(fails)} FAILED" if fails else '\nALL PASS')
sys.exit(1 if fails else 0)
