"""
A cut-off or malformed answer from the model must not lose the daily briefing.

Six of twelve runs between 26 and 29 October 2026 died on json.loads(): the model's
JSON ran past max_tokens mid-string or came back malformed. salvage_json() keeps every
complete section, and call_llm() retries when nothing usable survives.

Run: python3 tests/test_intelligence_json.py
"""
import importlib.util
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
spec = importlib.util.spec_from_file_location("gi_json", os.path.join(ROOT, "scripts", "generate_intelligence.py"))
gi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gi)
fails = []


def ck(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got={got!r} want={want!r}")
        fails.append(name)


cut = '{"macro":[{"title":"A","body":"x"},{"title":"B","body":"y"}],"news":[{"headline":"N1","body":"b1"},{"headline":"N2","body":"b2 cut'
ck("a cut-off answer keeps its complete sections", gi.salvage_json(cut),
   {"macro": [{"title": "A", "body": "x"}, {"title": "B", "body": "y"}], "news": [{"headline": "N1", "body": "b1"}]})
ck("...and drops only the broken item", len(gi.salvage_json(cut)["news"]), 1)
ck("quotes and braces inside strings are handled",
   gi.salvage_json('{"macro":[{"title":"Has \\"q\\" and } brace","body":"ok"}],"news":[{"he'),
   {"macro": [{"title": 'Has "q" and } brace', "body": "ok"}]})
ck("a complete answer is returned as is", gi.salvage_json('{"daily_outlook":"fine"}'), {"daily_outlook": "fine"})
ck("nothing usable means None (so the call is retried)", gi.salvage_json('{"macro": [ {"title": "A", "bo'), None)
ck("not JSON at all is None", gi.salvage_json("sorry, I cannot"), None)


class Resp:
    def __init__(self, text, code=200):
        self.status_code, self._t, self.headers, self.text = code, text, {}, text

    def json(self):
        return {"choices": [{"message": {"content": self._t}}]}

    def raise_for_status(self):
        pass


answers = iter(["{\"macro\": [ {\"title\": \"A\", \"bo", '{"daily_outlook":"ok"}'])
gi.requests.post = lambda *a, **k: Resp(next(answers))
gi.time.sleep = lambda s: None
ck("an unusable answer is retried, not fatal", gi.call_llm("k", "prompt"), {"daily_outlook": "ok"})
gi.requests.post = lambda *a, **k: Resp(cut)
ck("a truncated answer is used, not fatal", list(gi.call_llm("k", "prompt")), ["macro", "news"])

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
sys.exit(1 if fails else 0)
