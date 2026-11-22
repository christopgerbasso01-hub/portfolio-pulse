"""
The learning segment's curriculum — a year of distinct lessons, never repeated.

Until November 2026 the topic was "the first of 18 not used in the last few
episodes", and "the last few" was only the four episodes the show could still see.
So topics came straight back: the yield curve in episodes 18 and 22, VIX in 19 and
25. Now every topic ever taught is kept for good (data/podcast_memory.json, "taught",
seeded with the topics aired before the memory existed) and matched by its key words,
so a reworded repeat is still a repeat.

Order runs from the foundations to more advanced ideas, and leans on what this
listener holds and is saving for (TFSA, FHSA, RRSP, a US-heavy book, covered-call
and leveraged ETFs, a house in a few years). Leveraged-fund mechanics and decay are
deliberately absent: the listener knows them and asked not to hear them again.
"""
from __future__ import annotations

import re

# (title, key words that identify the idea, themes it pairs with in the news)
CURRICULUM = [
    ("How the FHSA and the RRSP Home Buyers' Plan Work Together", ["fhsa", "home buyers", "hbp"], {"housing"}),
    ("What Actually Moves a Stock Price", ["moves a stock price", "supply and demand for shares"], set()),
    ("Free Cash Flow, the Number Behind the Earnings", ["free cash flow"], {"earnings"}),
    ("How Company Guidance Moves a Stock More Than Results", ["guidance"], {"earnings"}),
    ("Profit Margins: Gross, Operating and Net", ["margin"], {"earnings"}),
    ("How a Central Bank Rate Hike Reaches Stock Prices", ["rate hike reaches", "transmission"], {"rates & inflation"}),
    ("What the CPI Report Measures and Why Markets Care", ["cpi", "consumer price index"], {"rates & inflation"}),
    ("Real Versus Nominal Returns", ["real versus nominal", "real return"], {"rates & inflation"}),
    ("How Elections Move Markets — and How Little They Usually Matter", ["election"], {"politics"}),
    ("What Happens to a Stock When a CEO Leaves", ["ceo", "succession"], {"leadership"}),
    ("How an IPO Works", ["ipo", "initial public offering"], {"ipo"}),
    ("Market-Cap Weighting: Why a Few Giants Drive the Index", ["market-cap weight", "cap-weighted"], {"ai & semiconductors"}),
    ("How Covered-Call ETFs Earn Their Income", ["covered call", "option premium"], set()),
    ("Calls and Puts in Plain Language", ["call option", "put option", "calls and puts"], set()),
    ("Dividend Yield Versus Dividend Growth", ["dividend yield", "dividend growth"], set()),
    ("Payout Ratios: Can the Dividend Last?", ["payout ratio"], set()),
    ("US Dividend Withholding Tax in a TFSA Versus an RRSP", ["withholding"], set()),
    ("Adjusted Cost Base and Capital Gains in a Non-Registered Account", ["adjusted cost base", "acb", "capital gain"], set()),
    ("The Superficial Loss Rule", ["superficial loss"], set()),
    ("Tax-Loss Harvesting Basics", ["tax-loss", "tax loss"], set()),
    ("Drawdowns and the Maths of Recovery", ["drawdown", "recovery math"], set()),
    ("Sequence Risk: Why Timing Matters Near a Goal", ["sequence risk", "sequence of returns"], {"housing"}),
    ("Diversification and Correlation", ["correlation", "diversification"], set()),
    ("Position Sizing: How Big Is Too Big?", ["position siz", "concentration"], set()),
    ("How Portfolio Rebalancing Works", ["rebalanc"], set()),
    ("Dollar-Cost Averaging Versus Lump Sums", ["dollar-cost", "lump sum"], set()),
    ("Index Funds Versus Picking Stocks", ["index fund", "active management"], set()),
    ("ETF Fees and What an MER Costs You", ["mer", "management expense", "expense ratio"], set()),
    ("Currency Hedging: Hedged Versus Unhedged ETFs", ["currency hedg", "hedged etf"], {"currency"}),
    ("Price-to-Sales and Valuing Fast-Growing Companies", ["price-to-sales", "price to sales"], {"ai & semiconductors"}),
    ("Enterprise Value and EV to EBITDA", ["ev/ebitda", "ebitda", "enterprise value"], set()),
    ("Return on Equity and What Makes a Quality Business", ["return on equity", "roe"], set()),
    ("Economic Moats", ["moat"], set()),
    ("The Semiconductor Supply Chain, From Design to Factory", ["supply chain", "foundry"], {"ai & semiconductors"}),
    ("The AI Capital Spending Cycle", ["capex", "capital spending"], {"ai & semiconductors"}),
    ("How Banks Make Money", ["net interest margin", "how banks make money"], {"banks & financials"}),
    ("How Pipelines Earn Fees", ["pipeline", "toll"], {"oil & energy"}),
    ("Oil Supply, OPEC and Why Prices Swing", ["opec", "oil supply"], {"oil & energy"}),
    ("Bond Basics: Price, Yield and Maturity", ["bond price", "bond basics"], {"rates & inflation"}),
    ("Credit Spreads as a Warning Light", ["credit spread"], {"banks & financials"}),
    ("The Jobs Report and the Unemployment Rate", ["jobs report", "payroll", "unemployment rate"], {"rates & inflation"}),
    ("GDP and What a Recession Really Is", ["gdp", "what a recession"], {"china & growth"}),
    ("How Tariffs Reach Companies' Profits", ["tariff"], {"regulation & trade"}),
    ("Share Buybacks", ["buyback"], {"earnings"}),
    ("Stock Splits and Why They Change Nothing", ["stock split"], set()),
    ("What an Earnings Call Reveals", ["earnings call"], {"earnings"}),
    ("Reading an Income Statement in Five Minutes", ["income statement"], {"earnings"}),
    ("The Balance Sheet: Debt and Cash", ["balance sheet"], set()),
    ("Recency Bias and Chasing Winners", ["recency bias", "chasing"], set()),
    ("Loss Aversion and Selling Too Early", ["loss aversion"], set()),
    ("Survivorship Bias in Investing Stories", ["survivorship"], set()),
    ("Bitcoin and Crypto-Linked Stocks", ["bitcoin", "crypto"], {"crypto"}),
    ("Short Selling and Short Squeezes", ["short squeeze"], set()),
]

# Aired before the memory file kept every topic (episodes 17-25 and earlier lists).
HISTORY = [
    "3x ETF Daily Reset Decay", "Leverage Ratio Versus Leverage Risk", "Yield Curve Recession Signals",
    "What VIX Actually Measures", "Earnings Revisions Move Prices", "P/E Ratios In Practice",
    "Sector Rotation Through Cycles", "Short Interest Signals",
]

# Ideas the listener has already been taught, by the words that give them away.
TAUGHT_KEYWORDS = {
    "3x ETF Daily Reset Decay": ["decay", "daily reset"],
    "Leverage Ratio Versus Leverage Risk": ["leverage ratio"],
    "Yield Curve Recession Signals": ["yield curve", "inverted", "inversion"],
    "What VIX Actually Measures": ["vix", "fear gauge", "fear index"],
    "Earnings Revisions Move Prices": ["earnings revision"],
    "P/E Ratios In Practice": ["p/e", "price-to-earnings", "price to earnings"],
    "Sector Rotation Through Cycles": ["sector rotation"],
    "Short Interest Signals": ["short interest"],
}


def _words(t: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (t or "").lower())
            if w not in {"what", "your", "with", "from", "that", "their", "work", "works", "versus", "plain",
                         "language", "basics", "really", "actually", "matter", "when", "they", "most", "more"}}


def keywords_for(title: str) -> list:
    for t, kws, _ in CURRICULUM:
        if t == title:
            return kws
    return TAUGHT_KEYWORDS.get(title, [])


def is_taught(title: str, taught: list) -> bool:
    """A topic counts as taught if any taught title shares its key words, or most of its words."""
    kws = [k.lower() for k in keywords_for(title)]
    tw = _words(title)
    for old in taught or []:
        o = str(old).lower()
        if o == title.lower():
            return True
        if kws and any(k in o for k in kws):
            return True
        if any(k in title.lower() for k in TAUGHT_KEYWORDS.get(str(old), [])):
            return True
        ow = _words(o)
        # Mostly the same words — at least two shared, so one common word ("measures",
        # "recession") does not make a new lesson look taught.
        shared = tw & ow
        if len(shared) >= 2 and len(shared) / min(len(tw), len(ow)) >= 0.75:
            return True
    return False


def choose(taught: list, themes: frozenset = frozenset(), promised: str = None) -> str:
    """This week's lesson: last week's promise if it is still new; otherwise the first new
    topic that fits this week's main story; otherwise the next new one in order."""
    taught = list(HISTORY) + list(taught or [])
    if promised and not is_taught(promised, taught):
        return promised
    fresh = [t for t, _, _ in CURRICULUM if not is_taught(t, taught)]
    if not fresh:                                    # a year in: start a second pass
        return CURRICULUM[len(taught) % len(CURRICULUM)][0]
    for t, _, tags in CURRICULUM:
        if t in fresh and tags & set(themes):
            return t
    return fresh[0]


def next_after(taught: list, this_week: str) -> str:
    """The topic promised for next week: the next new one in order."""
    return choose(list(taught or []) + [this_week])


def drifted_into(text: str, topic: str, taught: list) -> list:
    """Old lessons a learning segment keeps returning to (key word used twice or more)."""
    low = (text or "").lower()
    own = {k.lower() for k in keywords_for(topic)}
    out = []
    for old in list(HISTORY) + list(taught or []):
        for k in TAUGHT_KEYWORDS.get(old, []) or keywords_for(old):
            k = k.lower()
            if k in own or any(k in o or o in k for o in own):
                continue
            if len(re.findall(r"\b" + re.escape(k), low)) >= 2 and old not in out:
                out.append(old)
    return out


_EXTRA_THEMES = {
    "leadership": r"\b(?:ceo|chief executive|steps? down|resign\w*|succession|exit)\b",
    "politics": r"\b(?:election|midterms?|congress|senate|democrats?|republicans?)\b",
    "ipo": r"\bipo\b",
    "earnings": r"\b(?:earnings|results|quarter|guidance)\b",
    "housing": r"\b(?:housing|home prices|mortgage|down payment)\b",
}


def themes_for(text: str, base: frozenset = frozenset()) -> frozenset:
    """News themes plus the lesson-pairing ones the generic theme list lacks."""
    return frozenset(base) | {k for k, rx in _EXTRA_THEMES.items() if re.search(rx, text or "", re.I)}


_KEEP_CAPS = {"FHSA", "RRSP", "TFSA", "CEO", "IPO", "CPI", "ETF", "ETFS", "MER", "GDP", "OPEC", "EV", "EBITDA", "AI",
              "US", "ACB", "ROE", "P/E", "VIX", "OK"}
_PROPER = {"canada", "canadian", "home", "buyers'"}


def spoken(title: str) -> str:
    """A curriculum title as a person would say it: "free cash flow, the number behind
    the earnings", not "Free Cash Flow, The Number Behind The Earnings"."""
    out = []
    for w in str(title or "").split():
        bare = w.strip(",.:;—-()").upper()
        if bare in _KEEP_CAPS or (any(ch.isdigit() for ch in w) and w.upper() == w):
            out.append(w)
        elif w.lower().strip(",") in _PROPER and out and out[-1] in ("RRSP", "the"):
            out.append(w)
        else:
            out.append(w.lower())
    return " ".join(out).replace("Home buyers' plan", "Home Buyers' Plan")
