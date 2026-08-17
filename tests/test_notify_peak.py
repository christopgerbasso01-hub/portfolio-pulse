"""
All-time-high / drawdown peak handling — api/notify.py

KEY_PEAK is a permanent high-water mark that only ratchets upward, so one
corrupt reading raises it forever and the drawdown alert then fires against a
level the portfolio never reached. That is what produced a "10% below your
$335,852 peak" notification when the highest value ever recorded was $306,459.

Run: python3 tests/test_notify_peak.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api'))
os.environ.setdefault('KV_REST_API_URL', '')
os.environ.setdefault('KV_REST_API_TOKEN', '')

import notify

fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name:54} got={got} want={want}")
    if not ok:
        fails.append(name)


# Stub the KV writes and the snapshot history lookup.
written = {}
notify.kv_set = lambda k, v, *a, **kw: written.__setitem__(k, v)


def with_history(observed_max):
    # None models "history could not be read", which must not trigger a
    # downward correction — distinct from a genuinely low observed max.
    notify._observed_max = lambda default=None: observed_max


REAL_MAX = 306_459     # highest snapshot ever recorded
BOGUS = 335_852        # what the notification claimed
TODAY = "2026-08-17"

print("── _validated_peak ──")

with_history(REAL_MAX)
ck("corrupt peak corrected down to the real max",
   notify._validated_peak({"value": BOGUS}, 302_542, TODAY), REAL_MAX)
ck("  and the correction is persisted",
   written.get("notify:peak", {}).get("value"), REAL_MAX)
ck("  original value kept for audit",
   written.get("notify:peak", {}).get("corrected_from"), BOGUS)

written.clear()
ck("legitimate peak is left alone",
   notify._validated_peak({"value": REAL_MAX}, 302_542, TODAY), REAL_MAX)
ck("  and nothing is rewritten", written.get("notify:peak"), None)

# Snapshots can lag a true intraday high slightly; small excess is allowed.
ck("peak 1% above observed is trusted",
   notify._validated_peak({"value": int(REAL_MAX * 1.01)}, 302_542, TODAY),
   int(REAL_MAX * 1.01))

ck("today's value counts as observed",
   notify._validated_peak({"value": 310_000}, 310_000, TODAY), 310_000)

with_history(None)
ck("history unreadable -> stored peak retained",
   notify._validated_peak({"value": BOGUS}, 302_542, TODAY), BOGUS)

ck("no stored peak -> seed with today",
   notify._validated_peak(None, 302_542, TODAY), 302_542)

print("\n── drawdown consequence ──")
dd_bogus = (302_542 - BOGUS) / BOGUS * 100
dd_real = (302_542 - REAL_MAX) / REAL_MAX * 100
print(f"  vs corrupt peak: {dd_bogus:+.1f}%  (alerts at {-notify.DRAWDOWN_PCT}%)")
print(f"  vs real peak   : {dd_real:+.1f}%")
ck("real peak does not trigger a drawdown alert", dd_real <= -notify.DRAWDOWN_PCT, False)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
