"""Directional cue tables + pure detection functions (no I/O, unit-testable)."""

from __future__ import annotations

import re
from typing import Optional

from app.analysis.heuristics import (
    HIGH_TRIGGERS,
    LOW_TRIGGERS,
    PLURAL_EXEMPT_KEYWORDS,
    PREP_CUES,
    RUMOR_CUES,
    STEM_KEYWORDS,
    VERY_HIGH_TRIGGERS,
    WRAP_CUES,
    CATEGORY_KEYWORDS,
)
from app.analysis.text_match import (
    contains_cue,
    cue_hits,
    keyword_position,
)
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    NewsCategory,
    ReportKind,
    Verification,
)

# Phrases beat single ambiguous words, so each carries a weight.
BULLISH_CUES: list[tuple[str, int]] = [
    ("rate cut", 3), ("cuts rates", 3), ("dovish", 3), ("easing", 2), ("pivot", 2),
    ("weaker than expected", 2), ("missed forecasts", 2), ("below expectations", 2),
    ("slowing", 2), ("cooling", 2), ("fell", 2), ("declined", 2), ("dropped", 2),
    ("slipped", 2), ("eased", 2), ("safe haven", 2), ("inflow", 2), ("inflows", 2),
    ("surge", 2), ("jumped", 2), ("rally", 2), ("boost", 2), ("upbeat", 2),
    ("beats expectations", 2), ("approval", 2), ("stimulus", 2), ("fall", 1),
    ("lower", 1), ("down", 1), ("support", 1), ("record", 1),
]
BEARISH_CUES: list[tuple[str, int]] = [
    ("rate hike", 3), ("raises rates", 3), ("hawkish", 3), ("tightening", 2),
    ("higher for longer", 3), ("hotter than expected", 3), ("above expectations", 2),
    ("rise", 2), ("rose", 2), ("gained", 2), ("climbed", 2), ("higher", 1),
    ("plunge", 3), ("selloff", 3), ("sell-off", 3), ("slump", 2), ("tumble", 2),
    ("outflow", 2), ("outflows", 2), ("sanction", 2), ("escalat", 2), ("default", 3),
    ("downgrade", 2), ("weak", 1), ("concerns", 1), ("uncertainty", 1),
]

PRECIOUS_CUES = ["fed", "rate", "yield", "dollar", "inflation", "safe haven", "gold",
                 "real yields", "etf", "geopolit"]
INDUSTRIAL_CUES = ["china", "manufacturing", "factory", "pmi", "industrial", "solar",
                   "electronics", "copper", "growth", "construction", "demand"]

# Transmission chains used by the deterministic engine (sections 6-8).
TRANSMISSION: dict[str, dict[Asset, str]] = {
    Direction.BULLISH.value: {
        Asset.GOLD: "Supportive news -> lower rate expectations / weaker USD -> lower "
                    "real yields and lower opportunity cost -> bullish XAUUSD",
        Asset.SILVER: "Supportive news -> precious-metals bid (plus any industrial "
                      "read-through) -> bullish XAGUSD",
        Asset.BITCOIN: "Supportive news -> easier policy / stronger risk appetite / "
                       "institutional inflows -> bullish BTCUSD",
    },
    Direction.BEARISH.value: {
        Asset.GOLD: "Negative news -> higher rate expectations / stronger USD -> higher "
                    "real yields and higher opportunity cost -> bearish XAUUSD",
        Asset.SILVER: "Negative news -> precious-metals pressure (plus any industrial "
                      "drag) -> bearish XAGUSD",
        Asset.BITCOIN: "Negative news -> tighter policy / risk-off / outflows -> "
                       "bearish BTCUSD",
    },
}


# A single repeated word must not dominate the directional score: one story can
# legitimately repeat a cue (title + body echo), but never many times.
_MAX_REPEAT_HITS = 2


def score_cues(text: str, cues: list[tuple[str, int]]) -> float:
    """
    Sum the weights of every cue present in the text.

    Cues are matched with inflection tolerance (see
    :func:`app.analysis.text_match.contains_cue`) so that "plunge" still fires
    on "plunges". A cue may legitimately appear more than once; each occurrence
    is counted, capped so a single repeated word cannot dominate the score.
    """
    total = 0.0
    for phrase, weight in cues:
        hits = cue_hits(text, phrase)
        if hits:
            total += weight * min(hits, _MAX_REPEAT_HITS)
    return total


def detect_direction(text: str) -> Direction:
    """
    Net directional read from weighted cue scores.

    Subject-aware inversion: "Treasury YIELDS RISE" is bearish gold even
    though the verb "rise" is in the bullish-verb list, because the subject is
    a yield/currency. Checking the word *before* the verb avoids the most
    damaging class of naive-substring error.
    """
    bull = score_cues(text, BULLISH_CUES)
    bear = score_cues(text, BEARISH_CUES)

    # Invert yield/currency "up" verbs - higher yields/USD pressure metals.
    for m in re.finditer(
        r"\b(yields?|treasur\w*|dollar|usd|dxy|currency)\b[^.;]{0,24}?"
        r"\b(rise|rose|rising|climb\w*|higher|up|advance|strengthen\w*|gain\w*)\b",
        text,
    ):
        bull = max(0.0, bull - 2.0)
        bear += 2.0

    # Invert metal "down" verbs - falling gold/silver/btc is bearish.
    for m in re.finditer(
        r"\b(gold|silver|bitcoin|btc)\b[^.;]{0,24}?"
        r"\b(fall\w*|fell|drop\w*|slump\w*|tumble\w*|decline\w*|sink\w*)\b",
        text,
    ):
        bull = max(0.0, bull - 2.0)
        bear += 2.0

    if bull == 0 and bear == 0:
        return Direction.NEUTRAL
    if abs(bull - bear) < 1.0:
        return Direction.MIXED
    return Direction.BULLISH if bull > bear else Direction.BEARISH


def detect_category(text: str) -> NewsCategory:
    """
    Pick the dominant category.

    Ties are broken by which keyword appears EARLIEST in the headline, since
    publishers lead with the most specific driver ("China factory output" is a
    China story, not a generic commodity-supply story).

    Keywords are matched as whole words: "credits" is not the banking keyword
    "credit", and "bank" is not the crypto keyword "ban". The few keywords that
    are deliberately stems are listed in ``STEM_KEYWORDS`` and matched with a
    start-anchored prefix test instead.
    """
    lowered = text.lower()
    best, best_hits, best_pos = NewsCategory.OTHER, 0, 10**9
    for category, keywords in CATEGORY_KEYWORDS.items():
        if category is NewsCategory.OTHER:
            continue
        hits = 0
        first_pos = 10**9
        for k in keywords:
            pos = keyword_position(lowered, k, stems=STEM_KEYWORDS,
                                   plural_exempt=PLURAL_EXEMPT_KEYWORDS)
            if pos != -1:
                hits += 1
                first_pos = min(first_pos, pos)
        if hits > best_hits or (hits == best_hits and hits > 0 and first_pos < best_pos):
            best, best_hits, best_pos = category, hits, first_pos
    return best


def detect_importance(text: str, category: NewsCategory) -> EconomicImportance:
    lowered = text.lower()
    # Whole-word matching throughout, using the SAME keyword policy as category
    # detection: "war" must not fire on "warn"/"toward", "credit" must not fire
    # on "credits", but "sanctions" and "strikes" must still be recognised.
    pos = lambda k: keyword_position(  # noqa: E731 - tiny local shorthand
        lowered, k, stems=STEM_KEYWORDS, plural_exempt=PLURAL_EXEMPT_KEYWORDS)
    if any(pos(t) != -1 for t in VERY_HIGH_TRIGGERS):
        return EconomicImportance.VERY_HIGH
    # Filler/minor items stay LOW regardless of any macro keyword they contain.
    if any(pos(t) != -1 for t in LOW_TRIGGERS):
        return EconomicImportance.LOW
    if category in (NewsCategory.CENTRAL_BANK, NewsCategory.INFLATION,
                    NewsCategory.BANKING_FINANCIAL_STRESS):
        return EconomicImportance.HIGH
    if any(pos(t) != -1 for t in HIGH_TRIGGERS):
        return EconomicImportance.HIGH
    return EconomicImportance.MEDIUM


def detect_report_kind(text: str) -> ReportKind:
    lowered = text.lower()
    if any(contains_cue(lowered, c) for c in WRAP_CUES):
        return ReportKind.WRAP
    if any(contains_cue(lowered, c) for c in PREP_CUES):
        return ReportKind.PREP
    return ReportKind.GENERAL


def detect_verification(text: str) -> Verification:
    if any(contains_cue(text.lower(), c) for c in RUMOR_CUES):
        return Verification.UNVERIFIED
    return Verification.CONFIRMED


def detect_metal_character(text: str) -> Optional[str]:
    """Which side of silver's dual nature is driving the move (section 7)."""
    lowered = text.lower()
    precious = sum(1 for c in PRECIOUS_CUES if contains_cue(lowered, c))
    industrial = sum(1 for c in INDUSTRIAL_CUES if contains_cue(lowered, c))
    if precious and industrial:
        return "BOTH"
    if precious:
        return "PRECIOUS"
    if industrial:
        return "INDUSTRIAL"
    return None