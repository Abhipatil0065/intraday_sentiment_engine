"""Directional cue tables + pure detection functions (no I/O, unit-testable)."""

from __future__ import annotations

import re

from app.analysis.heuristics import (
    CATEGORY_KEYWORDS,
    HIGH_TRIGGERS,
    LOW_TRIGGERS,
    PLURAL_EXEMPT_KEYWORDS,
    PREP_CUES,
    RUMOR_CUES,
    STEM_KEYWORDS,
    VERY_HIGH_TRIGGERS,
    WRAP_CUES,
)
from app.analysis.text_match import (
    contains_cue,
    contains_phrase,
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
    ("slipped", 2), ("ease", 2), ("safe haven", 2), ("inflow", 2), ("inflows", 2),
    ("surge", 2), ("jumped", 2), ("rally", 2), ("boost", 2), ("upbeat", 2),
    ("beats expectations", 2), ("beat expectations", 2), ("beats forecast", 2), ("beat forecast", 2), ("approval", 2), ("stimulus", 2), ("fall", 1),
    ("lower", 1), ("down", 1), ("support", 1), ("record", 1),
    ("beats earnings", 2), ("raises guidance", 2),
    # BTC-specific bullish cues
    ("funding rate reset", 4), ("positive funding", 3), ("short squeeze", 5),
    ("shorts squeezed", 4), ("exchange outflow", 4), ("whale withdrawal", 3),
    ("stablecoin mint", 4), ("tether mint", 4), ("usdt mint", 4),
    ("open interest surge", 3), ("oi surge", 3),
    # GOLD-specific bullish cues (real yields, microstructure, central bank)
    ("real yields fall", 4), ("real yields drop", 4), ("tips yields fall", 3), ("tips yields drop", 3),
    ("real yield decline", 3), ("breakevens rise", 2), ("breakevens widen", 2), ("inflation expectations rise", 2),
    ("gold funding rate reset", 3), ("gold positive funding", 2), ("gold short squeeze", 5),
    ("gold shorts squeezed", 4), ("gold exchange outflow", 3), ("gold outflow", 3),
    ("gold open interest surge", 3), ("gold oi surge", 3),
    ("gold etf inflow", 4), ("gld inflow", 3), ("gold fund inflow", 3),
    ("central bank buying", 5), ("central bank buys", 5), ("reserve accumulation", 4),
    ("gold reserves rise", 4), ("official sector buying", 4), ("central bank gold buying", 5),
    # EQUITY/DAY TRADING microstructure cues (intraday)
    ("0dte", 4), ("zero dte", 4), ("gamma squeeze", 5), ("call wall", 3),
    ("vwap reclaim", 3), ("above vwap", 2), ("opening drive", 3),
    ("opening range break", 4), ("breakout", 3), ("momentum", 2),
    ("max pain", 2), ("pin risk", 3), ("call buying", 3),
    ("put wall", 3), ("call wall broken", 4),
]
BEARISH_CUES: list[tuple[str, int]] = [
    ("rate hike", 3), ("raises rates", 3), ("hawkish", 3), ("tightening", 2),
    ("higher for longer", 3), ("hotter than expected", 3), ("above expectations", 2),
    ("rise", 2), ("rose", 2), ("gained", 2), ("climbed", 2), ("higher", 1),
    ("plunge", 3), ("selloff", 3), ("sell-off", 3), ("slump", 2), ("tumble", 2),
    ("outflow", 2), ("outflows", 2), ("sanction", 2), ("escalat", 2), ("default", 3),
    ("downgrade", 2), ("weak", 1), ("concerns", 1), ("uncertainty", 1),
    ("collapses", 2),
    ("liquidation", 6), ("liquidated", 4),
    ("hacked", 4), ("exploit", 4), ("stolen", 4),
    # BTC-specific bearish cues
    ("funding rate spike", 5), ("negative funding", 4), ("long liquidation", 6),
    ("longs liquidated", 5), ("exchange inflow", 4), ("whale deposit", 3),
    ("stablecoin redemption", 4), ("tether redemption", 4),
    ("open interest drop", 3), ("oi drop", 3), ("oi falls", 3),
    # GOLD-specific bearish cues (real yields, microstructure, central bank)
    ("real yields rise", 4), ("real yields climb", 4), ("tips yields rise", 3), ("tips yields climb", 3),
    ("real yield increase", 3), ("real yield spike", 3), ("breakevens fall", 2), ("breakevens narrow", 2), ("inflation expectations fall", 2),
    ("gold funding rate spike", 4), ("negative gold funding", 3), ("gold long liquidation", 6),
    ("gold longs liquidated", 5), ("gold exchange inflow", 3), ("gold inflow", 3),
    ("gold open interest drop", 3), ("gold oi drop", 3), ("gold oi falls", 3),
    ("gold etf outflow", 4), ("gld outflow", 3), ("gold fund outflow", 3), ("gold etf redemption", 3),
    ("central bank selling", 5), ("central bank sells", 5), ("reserve reduction", 4),
    ("gold reserves fall", 4), ("official sector selling", 4), ("central bank gold selling", 5),
    # EQUITY/DAY TRADING microstructure cues (intraday)
    ("0dte", 4), ("zero dte", 4), ("gamma squeeze", 5), ("put wall", 3),
    ("vwap reject", 3), ("below vwap", 2), ("opening drive", 3),
    ("opening range break", 4), ("breakdown", 3), ("momentum", 2),
    ("max pain", 2), ("pin risk", 3), ("put buying", 3),
    ("call wall", 3), ("put wall broken", 4),
]

PRECIOUS_CUES = ["fed", "rate", "yield", "dollar", "inflation", "safe haven", "gold",
                 "real yields", "etf", "geopolit"]
INDUSTRIAL_CUES = ["china", "manufacturing", "factory", "pmi", "industrial", "solar",
                   "electronics", "copper", "growth", "construction", "demand"]

# Central Bank Speaker Tiering (for gold/metals - highest impact speakers)
# Multiplier applied to signal weight when a specific speaker is detected
SPEAKER_TIER: dict[str, float] = {
    # Fed - highest impact
    "powell": 1.50, "chair powell": 1.50, "jerome powell": 1.50,
    "williams": 1.20, "john williams": 1.20, "ny fed": 1.20, "new york fed": 1.20,
    "barkin": 1.10, "thomas barkin": 1.10, "richmond fed": 1.10,
    "bullard": 1.10, "james bullard": 1.10, "st louis fed": 1.10, "st. louis fed": 1.10,
    "bostic": 1.10, "raphael bostic": 1.10, "atlanta fed": 1.10,
    "daly": 1.10, "mary daly": 1.10, "san francisco fed": 1.10,
    "goolsbee": 1.10, "austan goolsbee": 1.10, "chicago fed": 1.10,
    "kashkari": 1.10, "neel kashkari": 1.10, "minneapolis fed": 1.10,
    "collins": 1.10, "susan collins": 1.10, "boston fed": 1.10,
    "harker": 1.05, "patrick harker": 1.05, "philadelphia fed": 1.05,
    "schmid": 1.05, "jeffrey schmid": 1.05, "kansas city fed": 1.05,
    "hammack": 1.05, "beth hammack": 1.05, "cleveland fed": 1.05,
    "kugler": 1.05, "adriana kugler": 1.05, "governor": 1.05,
    # ECB
    "lagarde": 1.30, "christine lagarde": 1.30, "ecb president": 1.30,
    "schnabel": 1.15, "isabel schnabel": 1.15, "ecb executive": 1.15,
    "centeno": 1.10, "mario centeno": 1.10, "portugal central bank": 1.10,
    "nagel": 1.10, "joachim nagel": 1.10, "bundesbank": 1.10,
    "panetta": 1.10, "fabio panetta": 1.10, "italy central bank": 1.10,
    "de guindos": 1.10, "luis de guindos": 1.10, "ecb vice": 1.10,
    # BoE
    "bailey": 1.20, "andrew bailey": 1.20, "boe governor": 1.20,
    "pill": 1.15, "huw pill": 1.15, "boe chief economist": 1.15,
    "ramsden": 1.10, "dave ramsden": 1.10, "boe deputy": 1.10,
    "mann": 1.10, "catherine mann": 1.10, "boe mpc": 1.10,
    "greene": 1.10, "megan greene": 1.10, "boe external": 1.10,
    # BoJ
    "ueda": 1.25, "kazuo ueda": 1.25, "boj governor": 1.25,
    "himino": 1.15, "ryozo himino": 1.15, "boj deputy": 1.15,
    "nakagawa": 1.10, "junko nakagawa": 1.10, "boj policy board": 1.10,
    # PBOC / China
    "pan gongsheng": 1.15, "pbgoc governor": 1.15, "pbgoc": 1.15,
    "zhu hexin": 1.10, "pbgoc deputy": 1.10,
    # General
    "central banker": 1.00, "fed speaker": 1.05, "fed official": 1.05,
    "ecb speaker": 1.05, "ecb official": 1.05,
    "boe speaker": 1.05, "boe official": 1.05,
    "boj speaker": 1.05, "boj official": 1.05,
}

# Gold-specific session profile enhancements
# These are ADDITIONAL multipliers on top of CATEGORY_SESSION_WEIGHT
GOLD_SESSION_BOOST: dict[str, dict[str, float]] = {
    # SessionName -> Category -> multiplier
    "EUROPE": {
        "Central Bank": 1.15,      # London open = major CB speeches
        "Interest Rates": 1.15,    # UK/EU rate decisions
        "Inflation": 1.15,         # UK/EU CPI
        "Treasury Yields": 1.15,   # Gilt/Bund moves
        "ETF Flows": 1.20,         # London ETF trading
    },
    "US": {
        "Central Bank": 1.25,      # Fed decisions/speeches
        "Interest Rates": 1.25,    # FOMC, dot plot
        "Inflation": 1.25,         # US CPI/PCE
        "Employment": 1.20,        # NFP, jobless claims
        "Treasury Yields": 1.25,   # US real yields / TIPS
        "USD/DXY": 1.20,           # Dollar index
        "ETF Flows": 1.25,         # GLD/IAU heavy US volume
        "Banking/Financial Stress": 1.15,
    },
    "ASIA": {
        "Central Bank": 1.05,      # BoJ, PBOC
        "China": 1.20,             # China data
        "Industrial Demand": 1.15, # Silver industrial
    },
}

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

# How much one subject/verb inversion moves the score.
_INVERSION_WEIGHT = 2.0

# --------------------------------------------------------------------------
# Subject-aware inversion via TOKEN DISTANCE (replaces a fixed 24-char regex)
# --------------------------------------------------------------------------
# The old regex bounded the subject and verb by 24 CHARACTERS, which broke on
# long phrasing ("The 10-year Treasury yield rose to a four-week high") and on
# short-but-legitimate ones. Distance is measured in TOKENS instead, and the
# window is configurable (INVERSION_MAX_TOKENS), so it can be tuned without
# touching code.
YIELD_SUBJECTS: tuple[str, ...] = (
    "yield", "yields", "treasury", "dollar", "usd", "dxy", "currency",
    "greenback", "real yield", "real yields", "bond yield", "bond yields",
)
UP_VERBS: tuple[str, ...] = (
    "rise", "rises", "rising", "rose", "climb", "climbs", "climbed", "higher",
    "up", "advance", "advances", "advanced", "strengthen", "strengthens",
    "strengthened", "gain", "gains", "gained", "jump", "jumps", "jumped",
)
METAL_SUBJECTS: tuple[str, ...] = (
    "gold", "silver", "bitcoin", "btc", "xau", "xag",
)
# Equity subjects: when these are paired with up/down verbs, the metal-centric
# cue polarity must be inverted (stocks rising is bullish for equities, not
# bearish like yields rising is for metals).
EQUITY_SUBJECTS: tuple[str, ...] = (
    "stock", "stocks", "share", "shares", "s&p", "spx", "nasdaq", "ndx",
    "dow", "russell", "nyse", "equity", "equities", "earnings", "profit",
    "companies", "corporate",
)
# Context words that flip "lower" from bullish (lower yields -> good for metals)
# to bearish for equities (lower earnings/guidance -> bad for equities).
_EQUITY_LOWER_CONTEXT: tuple[str, ...] = (
    "guidance", "guide", "forecast", "expectations",
)
DOWN_VERBS: tuple[str, ...] = (
    "fall", "falls", "fell", "drop", "drops", "dropped", "slump", "slumps",
    "slumped", "tumble", "tumbles", "tumbled", "decline", "declines",
    "declined", "sink", "sinks", "sank", "slide", "slides", "slid", "ease",
    "eases", "eased", "soften", "softens", "softened",
)

_TOKEN_SPLIT = re.compile(r"[a-z0-9']+")


def _token_list(text: str) -> list[str]:
    return _TOKEN_SPLIT.findall((text or "").lower())


def detect_inversion_subjects(
    text: str,
    subjects: tuple[str, ...] = YIELD_SUBJECTS,
    verbs: tuple[str, ...] = UP_VERBS,
    max_tokens: int | None = None,
) -> list[str]:
    """
    Subjects that are followed closely by one of `verbs`.

    Returns each matched subject once. The subject and verb must sit within
    ``max_tokens`` tokens of each other (default from config), which is what
    makes this robust to long phrasing: a subject and verb separated by a full
    clause are treated as unrelated, exactly as a reader would treat them.
    """
    if not text:
        return []
    if max_tokens is None:
        from app.config import settings
        max_tokens = int(settings.inversion_max_tokens)

    tokens = _token_list(text)
    if not tokens:
        return []

    matched: list[str] = []
    for index, token in enumerate(tokens):
        for subject in subjects:
            if not contains_cue(token, subject):
                continue
            window = tokens[index:index + max_tokens + len(subject.split()) + 1]
            for verb in verbs:
                if any(contains_cue(w, verb) for w in window):
                    matched.append(subject)
                    break
            break
    # De-duplicate while preserving order.
    seen: set = set()
    return [s for s in matched if not (s in seen or seen.add(s))]


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


def detect_direction(text: str, equity: bool = False) -> Direction:
    """
    Net directional read from weighted cue scores.

    Subject-aware inversion: "Treasury YIELDS RISE" is bearish gold even
    though the verb "rise" is in the bullish-verb list, because the subject is
    a yield/currency. Proximity is measured in TOKENS (configurable), not
    characters, so long phrasing still inverts correctly.

    When ``equity=True``, equity-specific subjects (stocks, S&P, earnings, etc.)
    are also inverted: "stocks rise" is BULLISH for equities even though "rise"
    is a metal-bearish cue, and "lower guidance" is BEARISH for equities even
    though "lower yields" is bullish for metals.

    Negation and the text-quality multipliers are applied here so that a
    negated or hedged headline cannot produce a directional read at all.
    """
    from app.analysis import text_quality as tq

    # A negated cue must not score: "rules out a rate cut" is not a rate cut.
    scored = tq.neutralise_on_negation(text)

    bull = score_cues(scored, BULLISH_CUES)
    bear = score_cues(scored, BEARISH_CUES)

    for subject in detect_inversion_subjects(scored, YIELD_SUBJECTS, UP_VERBS):
        bull = max(0.0, bull - _INVERSION_WEIGHT)
        bear += _INVERSION_WEIGHT

    for subject in detect_inversion_subjects(scored, METAL_SUBJECTS, DOWN_VERBS):
        bull = max(0.0, bull - _INVERSION_WEIGHT)
        bear += _INVERSION_WEIGHT

    # Symmetric case: the generic verb tables list "rise" as bearish (it
    # usually describes yields or the dollar), so a RISING metal must be
    # corrected the other way or "gold rose" reads bearish. Without this the
    # metal-down rule below would only ever be half the story.
    for subject in detect_inversion_subjects(scored, METAL_SUBJECTS, UP_VERBS):
        bull += _INVERSION_WEIGHT
        bear = max(0.0, bear - _INVERSION_WEIGHT)

    if equity:
        # Equity subjects: stocks/S&P rising is bullish for equities (not
        # bearish like yields rising is for metals). Uses full-text contains_cue
        # instead of detect_inversion_subjects because compound subjects like
        # "s&p 500" survive the tokeniser's [a-z0-9']+ split and would be
        # missed by the token-by-token match.
        _equity_lower = scored.lower()
        _has_equity = any(contains_cue(_equity_lower, s) for s in EQUITY_SUBJECTS)
        if _has_equity:
            if any(contains_cue(_equity_lower, v) for v in UP_VERBS):
                bull += _INVERSION_WEIGHT
                bear = max(0.0, bear - _INVERSION_WEIGHT)
            if any(contains_cue(_equity_lower, v) for v in DOWN_VERBS):
                bull = max(0.0, bull - _INVERSION_WEIGHT)
                bear += _INVERSION_WEIGHT
        # "lower" is bullish for metals (lower yields), but "guide lower" /
        # "lower earnings guidance" is bearish for equities.
        if (contains_cue(_equity_lower, "lower")
                and any(contains_cue(_equity_lower, ctx) for ctx in _EQUITY_LOWER_CONTEXT)):
            bull = max(0.0, bull - 1.0)
            bear += 1.0

    if bull == 0 and bear == 0:
        return Direction.NEUTRAL

    # Certainty + priced-in scale the magnitude, never the sign.
    # NEWS-ONLY bias (Task 2): directional_multiplier() is a constant 1.0,
    # so the price-action path contributes no weight here.
    quality = (tq.certainty_multiplier(text)
               * tq.priced_in_multiplier(text)
               * tq.directional_multiplier(text))
    bull *= quality
    bear *= quality

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
    pos = lambda k: keyword_position(
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


# Capitalised "May" followed by a number or a data-release name is the MONTH,
# not the rumor modal (Task 4b): "May CPI rises to 3.4%" is not a rumor.
# Case-insensitive match for "may" but only "May" (capitalized) is treated as month.
_MAY_MONTH_RE = re.compile(
    r"(?<!\w)May(?=\s+(?:\d|CPI\b|NFP\b|PMI\b|PCE\b|PPI\b|FOMC\b))")


def _may_is_rumor(text: str) -> bool:
    """True only for the modal 'may'; the month 'May' is skipped."""
    for m in re.finditer(r"(?<!\w)may(?!\w)", text or "", flags=re.IGNORECASE):
        matched = m.group(0)
        # Only "May" (capitalized) followed by month indicators is a month, not a rumor
        if matched == "May" and _MAY_MONTH_RE.match(text, m.start()):
            continue
        return True
    return False


def detect_verification(text: str) -> Verification:
    """Whole-word rumor cues only (Task 4b); 'may' is handled by _may_is_rumor."""
    if not text:
        return Verification.CONFIRMED
    lowered = text.lower()
    if any(contains_phrase(lowered, c) for c in RUMOR_CUES if c != "may"):
        return Verification.UNVERIFIED
    if _may_is_rumor(text):
        return Verification.UNVERIFIED
    return Verification.CONFIRMED


def detect_metal_character(text: str) -> str | None:
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


def detect_speaker_tier(text: str) -> float:
    """
    Detect central bank speaker tier from text and return weight multiplier.
    
    Higher tier speakers (Fed Chair, ECB President, etc.) get larger multipliers
    because their words move markets more.
    """
    if not text:
        return 1.0
    lowered = text.lower()
    best_tier = 1.0
    for speaker, tier in SPEAKER_TIER.items():
        if speaker in lowered:
            if tier > best_tier:
                best_tier = tier
    return best_tier