"""
The week's biggest stories — ranked across EVERY daily briefing of the past week.

The podcast used to read only Monday morning's briefing (about six items) and then
steer away from any theme it had covered recently. So in November 2026 it skipped
the US midterms (3-4 Nov) and Jensen Huang leaving Nvidia (13-19 Nov) — both in the
briefings for days — and led with German gas storage and an Iraqi gas field.

Here every macro and news item from the week's briefings is grouped into stories,
and a story ranks by how big it actually was:
  - persistence: on how many days, in how many briefings, it appeared;
  - the briefing's own impact rating;
  - how much of THIS portfolio it touches (holdings named, weighted by size, plus the
    3x funds that carry them — Nvidia news moves FNGU and SOXL too);
  - whether it is a market-wide event (an election, the Fed, a CEO leaving, ...).

Pure and stdlib-only; the generator supplies the briefings.
"""
from __future__ import annotations

import re
from datetime import datetime

_STOP = frozenset("""
the a an and or but of to in on for with at by from as is are was were be been it its this that these those
into about over under amid after ahead before while than more less new says said say could may might will would
can just still also again its their our your his her they them what which who how why when where
stocks stock market markets shares share investors investor trading traders wall street global world week
weekly today daily portfolio holdings holding impact potential signals signal rises rise rose falls fall fell
gains gain drop drops jumps jump slips slip surges surge eases ease lifts lift boosts boost hits hit sees
outlook update report reports reported data news amid mixed key big top higher lower record keeps keep
""".split())

# Words too common across unrelated stories to join two of them on their own.
_WEAK = frozenset("us u.s. trump china canada canadian american europe asia oil rates rate tech ai energy".split())

# Market-wide events: worth leading the episode even with little direct exposure.
_MAJOR = re.compile(
    r"\b(?:election|midterm|vote|congress|senate|white house|fed\b|federal reserve|powell|fomc|rate (?:hike|cut)|"
    r"interest rates?|inflation|cpi|jobs report|payrolls|recession|gdp|tariff|trade war|shutdown|"
    r"ceo|chief executive|steps? down|resign\w*|exit|succession|ipo|merger|acquisition|bankrupt\w*|"
    r"war|invasion|sanction\w*|bank of canada|opec)\b", re.I)

_IMPACT = {"HIGH": 3.0, "MED": 2.0, "MEDIUM": 2.0, "LOW": 1.0}

# Which 3x funds a story also moves, by what it is about.
_FUND_LINKS = {
    "FNGU": re.compile(r"\b(?:nvidia|apple|microsoft|amazon|alphabet|google|meta|netflix|tesla|broadcom|"
                       r"big tech|megacap|magnificent)\b", re.I),
    "SOXL": re.compile(r"\b(?:semiconductor\w*|chips?|chipmaker\w*|nvidia|broadcom|tsmc|taiwan semi\w*|"
                       r"qualcomm|amd|micron|intel)\b", re.I),
    "SPXL": re.compile(r"\b(?:s&p|fed\b|federal reserve|rates?|inflation|election|midterm|recession|tariff|"
                       r"wall street|stocks)\b", re.I),
    "UDOW": re.compile(r"\b(?:dow\b|fed\b|federal reserve|rates?|inflation|election|midterm|recession|tariff)\b", re.I),
}


# Subjects that make two headlines the same story even when the wording differs
# ("Fed officials signal half-point hike" / "US Fed rate path tightening"; "Democrats
# win Congress" / "US midterm outcomes reshape fiscal outlook").
ANCHORS = {
    "fed": r"\b(?:fed|federal reserve|powell|fomc)\b",
    "election": r"\b(?:election|midterms?|congress|senate|democrats?|republicans?|post-election)\b",
    "boc": r"\b(?:bank of canada|boc|macklem)\b",
    "tariff": r"\b(?:tariffs?|trade war|trade deal)\b",
    "hormuz": r"\b(?:hormuz|tankers?)\b",
    "debt-ceiling": r"\b(?:debt[- ]ceiling|shutdown)\b",
    "opec": r"\bopec\b",
}
_ANCHOR_RX = {k: re.compile(v, re.I) for k, v in ANCHORS.items()}


def anchors(title: str, names: dict = None) -> set:
    t = (title or "").replace("’", "'")
    out = {k for k, rx in _ANCHOR_RX.items() if rx.search(t)}
    # A held company is its own story — but only when the headline is about ONE company;
    # "Anthropic IPO lifts Nvidia and Broadcom" must not fuse the Nvidia and Broadcom stories.
    cos = {"co:" + tkr for tkr, aliases in (names or {}).items()
           if any(a and len(a) >= 4 and re.search(r"\b" + re.escape(a) + r"\b", t, re.I) for a in aliases)}
    return out | (cos if len(cos) == 1 else set())


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9&.'-]+", (text or "").lower().replace("’", "'"))
            if len(w) > 2 and w not in _STOP}


def _entities(title: str) -> set:
    """Capitalised words in a headline (the who/what), lower-cased."""
    words = re.findall(r"\b([A-Z][A-Za-z0-9&.-]+)", title or "")
    return {w.lower() for w in words[1:] if w.lower() not in _STOP} | \
           ({words[0].lower()} if words and words[0].lower() not in _STOP and len(words[0]) > 3 else set())


def items_from(briefings: list) -> list:
    """Flatten [(generated_at, briefing)] into story items."""
    out = []
    for when, b in briefings or []:
        for kind, key in (("macro", "title"), ("news", "headline")):
            for it in (b or {}).get(kind) or []:
                title = str(it.get(key) or "").strip()
                if not title:
                    continue
                out.append({"when": str(when or ""), "day": str(when or "")[:10], "kind": kind,
                            "title": title, "body": str(it.get("body") or ""),
                            "impact": str(it.get("impact") or "").upper(),
                            "exposure": str(it.get("exposure") or "")})
    return out


def _linked(a: dict, b: dict) -> bool:
    if a["_anc"] & b["_anc"]:
        return True
    # Otherwise only near-identical headlines join: loose word overlap chains unrelated
    # items together (ConocoPhillips -> OPEC -> AI export controls -> tanker attacks).
    ta, tb = a["_tok"] - _WEAK, b["_tok"] - _WEAK
    shared = ta & tb
    return len(shared) >= 2 and len(shared) / max(1, min(len(ta), len(tb))) >= 0.5


def cluster(items: list, names: dict = None) -> list:
    """Group items about the same story (union-find on headline overlap and anchors)."""
    for it in items:
        it["_tok"] = _tokens(it["title"])
        it["_ent"] = _entities(it["title"])
        it["_anc"] = anchors(it["title"], names)
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if find(i) != find(j) and _linked(items[i], items[j]):
                parent[find(j)] = find(i)
    groups = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    return list(groups.values())


def holdings_named(text: str, names: dict) -> set:
    """Tickers whose ticker or company name appears in the text. names: {ticker: [names]}."""
    found = set()
    for tkr, aliases in names.items():
        base = tkr.split(".")[0]
        pats = [re.escape(a) for a in aliases if a and len(a) >= 3]
        if len(base) >= 3:
            pats.append(re.escape(base))
        if pats and re.search(r"\b(?:" + "|".join(pats) + r")\b", text, re.I):
            found.add(tkr)
    return found


def rank(briefings: list, weights: dict, names: dict, last_main: str = "", same_story=None,
         exclude=None) -> list:
    """Stories of the week, biggest first.

    weights: {ticker: % of portfolio}; names: {ticker: [company names]}.
    Each story: {title, items (oldest first), days, runs, impact, exposure_pct, held, major, score}."""
    stories = []
    items = [i for i in items_from(briefings) if not (exclude and exclude(i["title"]))]
    for grp in cluster(items, names):
        text = " ".join(i["title"] + " " + i["body"] + " " + i["exposure"] for i in grp)
        held = holdings_named(text, names)
        for fund, rx in _FUND_LINKS.items():
            if fund in weights and rx.search(text):
                held.add(fund)
        exposure = sum(weights.get(t, 0.0) for t in held)
        days = len({i["day"] for i in grp})
        runs = len({i["when"] for i in grp})
        impact = max((_IMPACT.get(i["impact"], 1.5) for i in grp), default=1.5)
        major = bool(_MAJOR.search(" ".join(i["title"] for i in grp)))
        score = days * 2.0 + runs * 0.5 + impact + min(exposure, 40.0) * 0.15 + (3.0 if major else 0.0)
        grp.sort(key=lambda i: i["when"])
        # Headline: the highest-impact, most recent item says best what the story is now.
        lead = max(grp, key=lambda i: (_IMPACT.get(i["impact"], 1.5), i["when"]))
        if last_main and same_story and any(same_story(i["title"], last_main) for i in grp):
            score *= 0.6                      # told last week: only lead again if still the biggest by far
        # What the story is about, when most of its headlines share one subject.
        counts = {}
        for i in grp:
            for k in i.get("_anc", ()):
                counts[k] = counts.get(k, 0) + 1
        anchor = max(counts, key=counts.get) if counts else None
        anchor = anchor if anchor and counts[anchor] * 2 >= len(grp) else None
        stories.append({"title": lead["title"], "anchor": anchor, "items": grp, "days": days, "runs": runs,
                        "impact": impact, "exposure_pct": round(exposure, 1), "held": sorted(held),
                        "major": major, "score": round(score, 2)})
    stories.sort(key=lambda s: (-s["score"], -s["days"]))
    return stories


def timeline(story: dict, clean=lambda t, n=260: t[:n], limit: int = 5) -> str:
    """'Thu Nov 13 — headline: body' lines, oldest first, one per distinct headline."""
    # One item per day where possible (the most important that day), so a week-long
    # story reads as a development — CEO leaves, interim named, earnings preview —
    # rather than five versions of its latest headline.
    by_day = {}
    for it in story["items"]:
        best = by_day.get(it["day"])
        if best is None or _IMPACT.get(it["impact"], 1.5) > _IMPACT.get(best["impact"], 1.5):
            by_day[it["day"]] = it
    picks = sorted(by_day.values(), key=lambda i: i["when"])
    if len(picks) < limit:
        picks = sorted(picks + [i for i in story["items"] if i not in picks][: limit - len(picks)],
                       key=lambda i: i["when"])
    seen, lines = [], []
    for it in picks:
        if any(it["title"].lower() == s for s in seen):
            continue
        seen.append(it["title"].lower())
        try:
            d = datetime.fromisoformat(it["when"].replace("Z", "+00:00")).strftime("%a %b %d")
        except ValueError:
            d = it["day"]
        body = clean(it["body"], 260)
        lines.append(f"- {d} — {it['title']}" + (f": {body}" if body else ""))
    return "\n".join(lines[-limit:])
