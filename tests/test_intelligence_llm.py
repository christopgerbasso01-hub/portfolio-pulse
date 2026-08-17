"""
call_llm() status-code handling — scripts/generate_intelligence.py

Guards the regression that killed the 2026-08-17 11:31 run and both runs on
2026-08-10: Groq returned 404 for the 70B model, and because only 429 and 413
were handled, every other status fell through to raise_for_status() and aborted
the job — with a perfectly good fallback model configured and never tried.

Run: python3 tests/test_intelligence_llm.py
"""
import importlib.util
import json
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   '..', 'scripts', 'generate_intelligence.py')

spec = importlib.util.spec_from_file_location("gi", SRC)
gi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gi)

M70 = gi.GROQ_MODEL          # primary
M8 = "openai/gpt-oss-120b"   # fallback — must match call_llm's models list
PAYLOAD = {"macro": [], "risks": [], "news": []}

fails = []


class FakeResp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        # Real requests.Response always exposes .text; the error branches log it.
        self.text = text or f'{{"error":{{"code":{status}}}}}'

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gi.requests.HTTPError(f"{self.status_code} Error")


def ok_body():
    return {"choices": [{"message": {"content": json.dumps(PAYLOAD)}}]}


def ck(name, script, want_models, want_raise=None):
    """script(model, nth_call_for_that_model) -> FakeResp"""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        model = json["model"]
        calls.append(model)
        return script(model, len([c for c in calls if c == model]))

    gi.requests.post = fake_post
    gi.time.sleep = lambda s: None          # skip real backoff

    try:
        out = gi.call_llm("key", "prompt")
        raised = None
    except Exception as exc:
        out = None
        raised = type(exc).__name__

    ok = calls == want_models and raised == want_raise
    if want_raise is None:
        ok = ok and out == PAYLOAD
    print(f"  {'PASS' if ok else 'FAIL'}  {name:48} calls={len(calls)} "
          f"result={raised or 'json'}")
    if not ok:
        fails.append(name)


print("── call_llm status handling ──")

# The exact 2026-08-17 failure: 404 must fall through, not abort.
ck("404 on 70B falls through to 8B",
   lambda m, n: FakeResp(404) if m == M70 else FakeResp(200, ok_body()),
   [M70, M8])

ck("5xx retries same model then succeeds",
   lambda m, n: FakeResp(200, ok_body()) if n >= 3 else FakeResp(503),
   [M70, M70, M70])

ck("429 retries with backoff",
   lambda m, n: FakeResp(200, ok_body()) if n >= 2
   else FakeResp(429, headers={"retry-after": "1"}),
   [M70, M70])

ck("413 skips to the smaller model",
   lambda m, n: FakeResp(413) if m == M70 else FakeResp(200, ok_body()),
   [M70, M8])

# A bad key is not fixed by retrying or downgrading — fail immediately.
ck("401 fails fast, no pointless fallback",
   lambda m, n: FakeResp(401),
   [M70], want_raise="HTTPError")

ck("404 on every model raises RuntimeError",
   lambda m, n: FakeResp(404),
   [M70, M8], want_raise="RuntimeError")

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
