"""
TEXT-QUALITY RULES (improvement item 5).

Four passes over a headline before its cues are trusted:

1. NEGATION      - "no rate cut", "rules out", "unlikely to cut" cancel the
                   cue they sit next to instead of reinforcing it.
2. CERTAINTY     - "reportedly", "sources say", "could", "may" reduce weight;
                   "announces", "confirms", "approves" keep full weight.
3. PRICED-IN     - "as expected", "in line with", "widely anticipated" downweight;
                   "unexpectedly", "surprise", "more than forecast" upweight.
4. PRICE_ACTION  - headlines that merely REPORT a move ("gold plunges 2%")
                   are tagged and given near-zero directional weight, so they
                   can never reinforce the bias they are describing.

All multipliers are bounded and read from config. Every function is pure.
"""

from __future__ import annotations

import re

from app.analysis.text_match import contains_cue, contains_phrase
from app.config import settings

# --------------------------------------------------------------------------
# 1. NEGATION
# --------------------------------------------------------------------------
# A negator cancels a target cue only when the cue falls within
# NEGATION_WINDOW_TOKENS tokens AFTER it, so a negation can never reach across
NEGATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no", ("rate cut", "rate hike", "recession", "default", "sanction", "stimulus")),
    ("rules out", ("rate cut", "rate hike", "recession")),
    ("ruled out", ("rate cut", "rate hike", "recession")),
    ("unlikely to", ("cut", "hike")),
    ("unlikely", ("recession", "to rise", "to fall")),
    ("dismissed", ("rate cut", "rate hike")),
    ("denies", ("rate cut", "rate hike", "default")),
    ("denied", ("rate cut", "rate hike", "default")),
    ("not expected", ("cut", "hike")),
    ("no longer", ("cut", "hike", "expected")),
    ("hopes fade", ("rate cut", "cut")),
    ("won't", ("cut", "hike")),
    ("will not", ("cut", "hike")),
    ("without", ("a rate cut", "a rate hike", "approval")),
    ("abandoned", ("rate cut", "rate hike")),
    ("scrapped", ("rate cut", "rate hike")),
    ("on hold", ("rate cut", "rate hike")),
    # "rate cut hopes fade" puts the cue BEFORE the negator, so this pair is
    # matched with a reversed window.
    ("rate cut hopes fade", ()),
    ("cut hopes fade", ()),
    ("hopes of a cut fade", ()),
)

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def negation_applies(text: str) -> bool:
    """
    True when the headline negates a directional cue it refers to.

    "no rate cut" and "Fed rules out a hike" are TRUE; a bare "rate cut" and a
    negation on a different sentence are FALSE.
    """
    if not text:
        return False
    lowered = text.lower()
    tokens = _tokens(lowered)
    if not tokens:
        return False
    window = int(settings.negation_window_tokens)
    for negator, targets in NEGATION_RULES:
        words = negator.split()
        span = len(words)
        for i in range(len(tokens) - span + 1):
            if not all(contains_cue(tokens[i + k], words[k]) for k in range(span)):
                continue
            if not targets:
                # A self-contained negator phrase ("hopes fade") cancels
                # outright: there is no separate cue to look for.
                return True
            end = min(len(tokens), i + window + span + 1)
            region = " ".join(tokens[i:end])
            # A sentence boundary ends the negator's reach: "no rate cut. Gold
            # rallies." must not let the negation cancel the second clause.
            region = region.split(".")[0].split(",")[0]
            if any(contains_cue(region, t) for t in targets):
                return True
    return False


def neutralise_on_negation(text: str) -> str:
    """
    Blank out negated cue phrases so they cannot score.

    Returns the text with the negated span removed, so cue scoring sees only
    the clauses that actually assert something.
    """
    if not text:
        return ""
    if not negation_applies(text):
        return text
    out = text
    for negator, targets in NEGATION_RULES:
        for target in targets:
            out = re.sub(
                rf"(?<!\w){re.escape(negator)}\s+[^.;]{{0,40}}?{re.escape(target)}(?!\w)",
                " ", out, flags=re.IGNORECASE,
            )
    return out


# --------------------------------------------------------------------------
# 2. CERTAINTY
# --------------------------------------------------------------------------
HEDGE_CUES: tuple[str, ...] = (
    "reportedly", "sources say", "source says", "people familiar", "could",
    "might", "would", "rumor", "rumour", "allegedly", "unconfirmed",
    "expected to", "is said to", "are said to", "in talks", "considering",
    "weighing", "mulling", "speculation",
)
CONFIRMATION_CUES: tuple[str, ...] = (
    "announces", "announced", "confirms", "confirmed", "approves", "approved",
    "signs", "signed", "official", "officials",
)


def certainty_multiplier(text: str) -> float:
    """
    1.0 for a confirmed action, reduced for hedged language.

    The most severe hedge wins, so a later "will" cannot mask an earlier
    "reportedly".
    """
    if not text:
        return 1.0
    lowered = text.lower()
    hedge_hits = sum(1 for c in HEDGE_CUES if contains_cue(lowered, c))
    if hedge_hits == 0:
        return 1.0
    # A confirmation marker rescues a single soft hedge.
    if hedge_hits == 1 and any(contains_cue(lowered, c) for c in CONFIRMATION_CUES):
        return 1.0
    floor = float(settings.hedge_floor)
    mult = float(settings.hedge_multiplier) ** min(hedge_hits, 3)
    return max(floor, round(mult, 3))


# --------------------------------------------------------------------------
# 3. PRICED-IN / SURPRISE
# --------------------------------------------------------------------------
PRICED_IN_CUES: tuple[str, ...] = (
    "as expected", "in line with", "as forecast", "as anticipated",
    "widely anticipated", "as predicted", "as planned", "as consensus",
    "meets expectations", "priced in", "fully priced", "no surprise",
)
SURPRISE_CUES: tuple[str, ...] = (
    "unexpectedly", "surprise", "more than forecast", "above forecast",
    "below forecast", "much larger", "far larger", "bigger than expected",
    "overshoot", "blows past", "sharply",
)


def priced_in_multiplier(text: str) -> float:
    """
    <1.0 when the news was already priced in, >1.0 on genuine surprise.
    """
    if not text:
        return 1.0
    lowered = text.lower()
    priced = sum(1 for c in PRICED_IN_CUES if contains_cue(lowered, c))
    surprise = sum(1 for c in SURPRISE_CUES if contains_cue(lowered, c))

    mult = 1.0
    if priced:
        mult *= max(float(settings.priced_in_floor),
                    float(settings.priced_in_multiplier) ** min(priced, 2))
    if surprise:
        mult *= float(settings.surprise_multiplier)
    return round(max(0.1, min(2.0, mult)), 3)


# --------------------------------------------------------------------------
# 4. PRICE_ACTION
# --------------------------------------------------------------------------
PRICE_ACTION_VERBS: tuple[str, ...] = (
    "plunge", "plunges", "plunged", "surge", "surges", "surged", "rally",
    "rallies", "rallied", "jump", "jumps", "jumped", "soar", "soars", "soared",
    "tumble", "tumbles", "tumbled", "slump", "slumps", "slumped", "sink",
    "sinks", "sank", "climb", "climbs", "climbed", "gain", "gains", "gained",
    "fall", "falls", "fell", "rise", "rises", "rose", "drop", "drops", "dropped",
    "slide", "slides", "slid", "spike", "spikes", "spiked",
)
PRICE_ACTION_ASSETS: tuple[str, ...] = (
    "gold", "silver", "bitcoin", "btc", "xau", "xag", "price", "prices",
)
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")
_LEVEL_RE = re.compile(r"\b\d{4,6}\b")


def is_price_action(text: str) -> bool:
    """
    True when the headline only REPORTS a price move.

    Requires a price verb, a price subject AND a quoted figure, so a
    central-bank decision is not mistaken for a price report.
    """
    if not text:
        return False
    lowered = text.lower()
    has_verb = any(contains_cue(lowered, v) for v in PRICE_ACTION_VERBS)
    has_subject = any(contains_phrase(lowered, a) for a in PRICE_ACTION_ASSETS)
    has_figure = bool(_PERCENT_RE.search(lowered)) or bool(
        _LEVEL_RE.search(lowered))
    return has_verb and has_subject and has_figure


def directional_multiplier(text: str) -> float:
    """
    NEWS-ONLY bias (Task 2): always 1.0.

    Price-action headlines used to be damped near zero here; that price
    path is removed from the bias so news alone decides the weight.
    Kept as a named function so callers and schema stay unchanged.
    """
    return 1.0


def analyse(text: str) -> dict:
    """One call returning every text-quality verdict for a headline."""
    return {
        "negated": negation_applies(text),
        "price_action": is_price_action(text),
        "certainty": certainty_multiplier(text),
        "priced_in": priced_in_multiplier(text),
        "directional": directional_multiplier(text),
    }
