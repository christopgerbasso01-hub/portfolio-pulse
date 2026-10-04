"""
Currency-direction checks, shared by the weekly podcast and the daily briefing.

The investor's base currency is CAD and the US-listed holdings are held and
priced in USD, so USD/CAD RISING means a stronger US dollar, a weaker Canadian
dollar, and US holdings worth MORE in CAD. Language models invert this
constantly, and episode 18 did it three ways in one breath:

  * "a firmer greenback compresses the CAD-denominated value of our ETFs"
    (backwards: it raises it);
  * "we're holding them in CAD" (they are held in USD and only translated);
  * the intelligence feeding that episode already said a stronger USD was
    "pressuring the portfolio's USD-heavy exposure".

A prompt rule is a request. This is the deterministic backstop: a conservative
sentence-level classifier. It only judges a sentence when it contains exactly one
direction for the exchange rate AND exactly one direction for the effect on the
CAD value of USD holdings, and the two disagree. Anything ambiguous, any
question, and anything not about USD holdings is left alone, because a checker
that blocks sound scripts gets switched off.

Pure standard library, so both pipelines can import it.
"""
import re

_PUNCT = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "‑": "-", "‐": "-", "–": "-", "—": "-",
})


def _plain(text: str) -> str:
    """Markdown stripped, whitespace collapsed, typographic punctuation made plain."""
    t = text.translate(_PUNCT)
    t = re.sub(r"[*_`]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def split_sentences(text: str) -> list:
    """Sentence split that does not break on 'U.S.' or on decimals."""
    t = _plain(text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(\[$])", t)
    return [p.strip() for p in parts if p.strip()]


_USD = r"(?:u\.?s\.?\s+|american\s+)?(?:dollar|greenback|usd|buck)"
_CAD = r"(?:cad|loonie)"
_MODAL = r"(?:(?:could|may|might|would|will|should|to|can)\s+)?"

# ── what the exchange rate does ─────────────────────────────────────────────
_U_UP = [re.compile(p) for p in (
    rf"\b(?:firm(?:er|ing|s)?|strong(?:er)?|strengthen\w*|higher|rising|rises?|ralli\w+|"
    rf"climb\w*|surg\w*|jump\w*|lift\w*|appreciat\w*|gain\w*|spik\w*)\s+(?:the\s+)?{_USD}\b",
    rf"\b{_USD}\s+{_MODAL}(?:strengthen\w*|firm\w*|ralli\w+|rises?|rising|climb\w*|gain\w*|"
    rf"jumps?|spik\w*|appreciat\w*|lifts?|strength)\b",
    r"\b(?:dollar|usd|greenback)\s+strength\b",
    rf"\busd/cad\s+{_MODAL}(?:rises?|rising|climbs?|climbing|goes\s+up|up|higher|ticks?\s+up|"
    rf"jumps?|moves?\s+(?:up|higher)|lifts?|pushes?\s+higher|ralli\w+|increas\w+|"
    rf"drift\w*\s+(?:up|higher))\b",
    r"\b(?:rise|increase|climb|jump|uptick|move\s+up|move\s+higher)\s+in\s+usd/cad\b",
    r"\b(?:pushes|push|pushing|sends|send|lifts|lift|drives|drive)\s+(?:the\s+)?usd/cad\s+(?:up|higher)\b",
    rf"\b{_CAD}\s+{_MODAL}(?:weaken\w*|weaker|slips?|slid\w*|falls?|fell|softens?|softer|drops?|"
    rf"declin\w*|depreciat\w*|sinks?)\b",
    rf"\b(?:weaker|softer|weakening|weaken(?:s|ed)?)\s+(?:the\s+)?{_CAD}\b",
)]
_U_DN = [re.compile(p) for p in (
    rf"\b(?:weak(?:er|ening)?|soft(?:er|ening)?|fall\w*|declin\w*|lower|drop\w*|slid\w*|slump\w*|"
    rf"dip\w*|sink\w*|weaken\w*)\s+(?:the\s+)?{_USD}\b",
    rf"\b{_USD}\s+{_MODAL}(?:weaken\w*|soften\w*|fall\w*|fell|drops?|dips?|slips?|slid\w*|"
    rf"declin\w*|sinks?|weakness)\b",
    rf"\busd/cad\s+{_MODAL}(?:falls?|falling|drops?|dropping|down|lower|dips?|ticks?\s+down|slips?|"
    rf"declines?|slides?|sinks?|drift\w*\s+(?:down|lower)|moves?\s+(?:down|lower))\b",
    r"\b(?:fall|drop|decline|dip|slide|move\s+down|move\s+lower)\s+in\s+usd/cad\b",
    rf"\b{_CAD}\s+{_MODAL}(?:strengthen\w*|stronger|firm\w*|ralli\w+|appreciat\w*|gains?)\b",
    rf"\b(?:stronger|firmer|strengthening|firming)\s+(?:the\s+)?{_CAD}\b",
)]

# ── what that is said to do to the CAD value of USD holdings ────────────────
_V_DN_COMPOUND = re.compile(
    r"\b(?:increas\w*|add\w*|rais\w*|more|extra|worsen\w*|deepen\w*|bigger|greater|heavier)\s+"
    r"(?:the\s+|a\s+|an\s+)?(?:\w+\s+){0,2}(?:drag|headwind|pressure|hit|loss|drain|damage)\b")
_V_DN_RISK = re.compile(
    r"\b(?:increas\w*|rais\w*|heighten\w*|elevat\w*)\s+(?:the\s+)?(?:risk|chance|odds|likelihood)\s+of\s+"
    r"(?:a\s+)?(?:decline|drop|fall|loss|erosion|hit)\b")
_V_UP_COMPOUND = re.compile(
    r"\b(?:reduc\w*|trim\w*|eas\w*|lessen\w*|cut\w*|shrink\w*|offset\w*|remov\w*|soften\w*)\s+"
    r"(?:the\s+|a\s+|an\s+)?(?:\w+\s+){0,2}(?:drag|headwind|pressure|hit|loss|drain|damage)\b")
_V_DN = re.compile(
    r"\b(?:compress\w*|erod\w*|shav\w*|trims?|trimm\w*|reduc\w*|lower\w*|hurt\w*|drag\w*|"
    r"shrink\w*|decreas\w*|dimin\w*|costs?|costing|loss(?:es)?|lose|loses|losing|headwind\w*|"
    r"pressur(?:es|ed|ing)|(?:would|could|may|might|will|can|to|and)\s+pressure\b|"
    r"pressure\s+(?:the|our|its|their|us)\b|"
    r"weigh\w*\s+on|squeez\w*|worth\s+less|fewer|slash\w*|dent\w*|"
    r"declin\w*|drops?|dropp\w*|falls?|fell|sinks?|slump\w*)\b")
_V_UP = re.compile(
    r"\b(?:add\w*|boost\w*|lift\w*|rais\w*|increas\w*|inflat\w*|gain\w*|tailwind\w*|"
    r"worth\s+more|more\s+cad|benefit\w*|help\w*|cushion\w*|bonus|swell\w*)\b")

# The sentence must be about what the exchange rate does to VALUE. Without this,
# "reduces diversification against a USD strength cycle" or "volatility could hurt
# our positions" read as FX claims.
_SCOPE_VALUE = re.compile(
    r"\b(?:value|worth|translat\w*|drag|cad-denominated|in cad|cad terms|net worth|usd notional|"
    r"erod\w*|compress\w*|tailwind\w*|headwind\w*|pressur(?:es|ed|ing)|"
    r"(?:would|could|may|might|will|can|to|and)\s+pressure|pressure\s+(?:the|our|its|their|us))\b")

# A sentence is only about USD holdings if it says so.
_SCOPE_CCY  = re.compile(r"\b(?:usd|us dollar|u\.s\. dollar|greenback|dollar|cad|loonie)\b")
_SCOPE_HOLD = re.compile(
    r"\b(?:holdings?|assets?|notional|positions?|etfs?|exposure|book|translat\w*|leveraged|"
    r"cad-denominated|cad value|value in cad|net worth|usd-denominated|usd-heavy)\b")

_US_THING = re.compile(r"\b(?:etfs?|leveraged|usd|u\.s\.|us|american|fang|nvidia|nasdaq|s&p|"
                       r"tech|indices|index|indexes|semiconductor)\b")
_HELD_IN_CAD = re.compile(
    r"\b(?:we|we're|we are|i|you|our)\b[^.?!]{0,25}\b(?:hold|holds|holding|own|owns|owning|held|"
    r"invested)\b[^.?!]{0,60}\bin cad\b")


def _spans_removed(text: str, patterns) -> tuple:
    """Return (matched_any, text with the matches blanked out)."""
    hit = False
    for p in patterns:
        if p.search(text):
            hit = True
            text = p.sub(" ", text)
    return hit, text


def classify(sentence: str):
    """Return a reason string if the sentence gets the FX direction wrong, else None."""
    s = _plain(sentence).lower()
    if "?" in s:                         # a question is not a claim
        return None
    # "the Canadian dollar" must not be read as the US dollar
    s = re.sub(r"canadian\s+dollar", "cad", s)
    # "$2,500 CAD gain" is an amount, not the Canadian dollar gaining
    s = re.sub(r"(?:\$\s?)?\d[\d,\.]*\s*(?:k|m|b|bn)?\s*cad\b", " amount ", s)
    s = re.sub(r"\bcad\s*\$\s?\d[\d,\.]*", " amount ", s)
    if not (_SCOPE_CCY.search(s) and _SCOPE_HOLD.search(s) and _SCOPE_VALUE.search(s)):
        return None

    up_hit = any(p.search(s) for p in _U_UP)
    dn_hit = any(p.search(s) for p in _U_DN)
    if up_hit == dn_hit:                 # neither, or both = ambiguous
        return None

    # blank out the rate phrase so its own verbs ("lifts the USD") are not
    # then read as the effect on value
    _, rest = _spans_removed(s, _U_UP if up_hit else _U_DN)

    v_dn = v_up = False
    if _V_DN_RISK.search(rest):
        v_dn = True
        rest = _V_DN_RISK.sub(" ", rest)
    if _V_DN_COMPOUND.search(rest):
        v_dn = True
        rest = _V_DN_COMPOUND.sub(" ", rest)
    if _V_UP_COMPOUND.search(rest):
        v_up = True
        rest = _V_UP_COMPOUND.sub(" ", rest)
    v_dn = v_dn or bool(_V_DN.search(rest))
    v_up = v_up or bool(_V_UP.search(rest))
    if v_dn == v_up:
        return None

    if up_hit and v_dn:
        return ("says a stronger US dollar (USD/CAD rising) lowers the CAD value of our USD "
                "holdings — it RAISES it")
    if dn_hit and v_up:
        return ("says a weaker US dollar (USD/CAD falling) raises the CAD value of our USD "
                "holdings — it LOWERS it")
    return None


def held_in_cad_reason(sentence: str):
    s = _plain(sentence).lower()
    if "?" in s or "cash" in s:
        return None
    if _HELD_IN_CAD.search(s) and _US_THING.search(s):
        return ("says US-listed holdings are held in CAD — they are held and priced in USD, "
                "and only translated to CAD for display")
    return None


def fx_problems(text: str) -> list:
    """[(sentence, reason)] for every sentence that misstates the currency."""
    out = []
    for sent in split_sentences(text):
        why = classify(sent) or held_in_cad_reason(sent)
        if why:
            out.append((sent, why))
    return out


# "USD translation drag", "FX headwind", "translation loss". For a CAD-based
# investor holding USD assets the genuine currency RISK is a STRONGER CAD, and
# the briefing used this wording for the opposite (a stronger USD). The phrase
# is not wrong in every sentence, so verification does not flag it; but fed to a
# model as source material it teaches the inversion, so input scrubbing drops it.
_DRAG_WORDING = re.compile(
    r"\b(?:translation|fx|currency|usd)\s+(?:translation\s+)?(?:drag|headwind|loss(?:es)?|hit)\b"
    r"|\btranslation\s+(?:drag|loss(?:es)?|hit)\b", re.I)


def scrub_fx_sentences(text: str, strict_wording: bool = False) -> tuple:
    """Drop the sentences fx_problems would flag; return (clean_text, removed).

    For INPUT hygiene: the briefing is fed to the podcast, and a wrong sentence
    there is copied faithfully. Removing a sentence loses a little colour and
    cannot add an error. strict_wording additionally drops sentences that frame
    currency as a "translation drag" (see _DRAG_WORDING).
    """
    if not text:
        return text, []
    bad = {s for s, _ in fx_problems(text)}
    if strict_wording:
        bad |= {s for s in split_sentences(text) if _DRAG_WORDING.search(_plain(s))}
    if not bad:
        return text, []
    kept = [s for s in split_sentences(text) if s not in bad]
    return " ".join(kept), sorted(bad)
