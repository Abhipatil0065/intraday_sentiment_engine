"""
EVENT-TYPE POLARITY RESOLUTION (validation fixes VAL-4 and VAL-5).

THE DEFECT THIS FIXES
---------------------
``EVENT_SIGNALS`` matched phrases without reading polarity, in two ways:

1. BARE SUBJECT PHRASES. ``"risk appetite"`` sits in the RISK_ON cue list, so
   "risk appetite FADED" matched RISK_ON exactly as "risk appetite IMPROVED"
   did - both produced a risk-on reading.
2. CATEGORY FALLBACK. "Dollar index fell" matched no phrase at all, so the
   category fallback (USD/DXY -> STRONG_USD) fired and asserted a strong dollar
   for a headline that said the dollar FELL.

Both are the same mistake: deciding an event's DIRECTION without reading the
verb that carries it.

THE MODEL
---------
For each opposing event pair, subject and verb are matched separately:

    subject + up verb   -> the positive event   (e.g. STRONG_USD)
    subject + down verb -> the negative event   (e.g. WEAK_USD)
    subject + no verb   -> NO event, never a guessed direction

A verb may appear BEFORE or AFTER the subject, so both windows are checked.
When an up and a down verb both appear the item is genuinely two-sided and
NEITHER event is emitted - that is what lets the aggregator report NO_BIAS
instead of inventing a lean.

Events whose direction is intrinsic rather than verbal (geopolitical
escalation, a bank failing, a liquidation cascade) are deliberately NOT in
this table: the event IS the signal, so there is no verb to read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.analysis.text_match import contains_cue, contains_plural

# How far from a subject a verb may sit and still modify it.
VERB_WINDOW_TOKENS = 6

# A verb must sit in the SAME clause as its subject. "risk appetite as volatility
# RISES" is the canonical trap: `rises` belongs to `volatility`, but a fixed token
# window around `risk appetite` happily claims it and reports RISK_ON - which is
# the VAL-4 defect recurring in a new costume. Any of these words starts a new
# clause or prepositional phrase, so the scan stops before it.
CLAUSE_BOUNDARIES: frozenset = frozenset({
    "as", "amid", "amidst", "while", "when", "whereas", "after", "before",
    "though", "although", "but", "and", "or", "nor", "since", "unless",
    "once", "if", "because", "that", "which", "who", "whom", "whose",
    "with", "on", "in", "at", "by", "for", "to", "from", "over", "under",
    "into", "onto", "against", "toward", "towards", "than", "during",
    "following", "despite", "except", "whether",
})

_TOKEN_RE = re.compile(r"[a-z0-9']+")

# Rising / strengthening.
UP_VERBS: tuple[str, ...] = (
    "rise", "rises", "rising", "rose", "climb", "climbs", "climbed",
    "higher", "up", "advance", "advances", "advanced", "strengthen",
    "strengthens", "strengthened", "gain", "gains", "gained", "jump",
    "jumps", "jumped", "surge", "surges", "surged", "rally", "rallies",
    "rallied", "soar", "soars", "soared", "improve", "improves", "improved",
    "improving", "recover", "recovers", "recovered", "returns", "returned",
    "boost", "boosts", "boosted", "firm", "firms", "harden", "hardens",
    "tighten", "tightens", "tightened", "hike", "hikes", "hiked",
    "raise", "raises", "raised", "beats", "beat", "record", "hotter",
    "accelerate", "accelerates", "accelerated", "returns to", "rebounds",
)

# Falling / weakening.
DOWN_VERBS: tuple[str, ...] = (
    "fall", "falls", "falling", "fell", "drop", "drops", "dropped",
    "lower", "down", "decline", "declines", "declined", "slide", "slides",
    "slid", "slip", "slips", "slipped", "weaken", "weakens", "weakened",
    "lose", "loses", "lost", "sink", "sinks", "sank", "plunge", "plunges",
    "plunged", "tumble", "tumbles", "tumbled", "spike", "spikes", "spiked",
    "deteriorate", "deteriorates", "deteriorated", "worsen", "worsens",
    "worsened", "fade", "fades", "faded", "fading", "retreat", "retreats",
    "retreated", "ease", "eases", "eased", "soften", "softens", "softened",
    "collapse", "collapses", "collapsed", "miss", "misses", "missed",
    "dampen", "dampens", "dampened", "damp", "damps", "damped",
    "cool", "cools", "cooled", "sour", "sours", "soured",
    "narrow", "narrows", "narrowed", "moderate", "moderates", "moderated",
)


@dataclass(frozen=True)
class PolarityPair:
    """An opposing pair of event types plus the words that decide between them."""

    positive: str            # EventType name for the UP reading
    negative: str            # EventType name for the DOWN reading
    subjects: tuple[str, ...]
    up_verbs: tuple[str, ...] = UP_VERBS
    down_verbs: tuple[str, ...] = DOWN_VERBS
    # Phrases that already encode direction and need no verb check.
    fixed_positive: tuple[str, ...] = ()
    fixed_negative: tuple[str, ...] = ()
    # If True, fixed phrases require a subject to be present in the text.
    fixed_requires_subject: bool = False
# --------------------------------------------------------------------------
# The pairs
# --------------------------------------------------------------------------
POLARITY_PAIRS: tuple[PolarityPair, ...] = (
    # Dollar strength -> bearish metals and crypto.
    PolarityPair(
        positive="STRONG_USD", negative="WEAK_USD",
        subjects=("dollar index", "dollar", "greenback", "dxy", "usd",
                  "currency"),
        fixed_positive=("stronger dollar", "dollar strength"),
        fixed_negative=("weaker dollar", "dollar weakness"),
    ),
    # Treasury yields.
    #
    # NOTE the naming: `positive` means "the VERB went up", not "good for the
    # asset". Yields RISING raises the opportunity cost of holding a
    # non-yielding metal, so an upward verb maps to RISING_YIELDS (bearish
    # metals). This is the same convention as STRONG_USD, where a rising
    # dollar is likewise bearish - the event type names the MOVE, and the
    # transmission map is what assigns the asset impact.
    PolarityPair(
        positive="RISING_YIELDS", negative="FALLING_YIELDS",
        subjects=("treasury yield", "treasury yields", "yield", "yields",
                  "real yield", "real yields", "bond yield", "bond yields",
                  "10-year", "2-year", "gilt", "gilts"),
    ),
    # Policy stance. A HOLD has no direction and deliberately emits neither.
    PolarityPair(
        positive="DOVISH_POLICY", negative="HAWKISH_POLICY",
        subjects=("federal reserve", "central bank", "fed", "fomc", "ecb",
                  "boe", "boj", "pboc", "policy rate", "monetary policy",
                  "interest rate", "rates", "rate"),
        up_verbs=("cut", "cuts", "cutting", "ease", "eases", "easing",
                  "dovish", "pivot", "stimulus", "loosen", "loosens",
                  "loosening", "accommodative", "rate cut"),
        down_verbs=("hike", "hikes", "hiking", "raise", "raises", "raised",
                    "lift", "lifts", "lifted", "tighten", "tightens",
                    "tightening", "hawkish", "restrict", "restricts",
                    "higher for longer", "rate hike"),
    ),
# Inflation prints.
    PolarityPair(
        positive="COOL_INFLATION", negative="HOT_INFLATION",
        subjects=("cpi", "core pce", "pce", "ppi", "core inflation",
                  "inflation", "consumer prices", "price index", "deflator"),
        up_verbs=("cools", "cooled", "cooling", "eases", "eased", "easing",
                  "disinflation", "slows", "slowed", "moderates", "moderated",
                  "below expectations", "missed forecasts", "softer",
                  "benign", "falls", "fell", "lower", "declines"),
        down_verbs=("accelerates", "accelerated", "accelerating", "hotter",
                      "hot", "sticky", "stickier", "firm", "firmly",
                      "resilient", "above expectations", "beats expectations",
                      "higher", "climbs", "climbed", "spikes", "spiked",
                      "rises", "rose", "rising"),
        fixed_positive=("misses forecasts", "miss forecasts", "misses expectations",
                        "miss expectations", "misses estimates", "below forecasts",
                        "below consensus", "below estimates"),
        fixed_negative=("beats forecasts", "beat forecasts", "beat expectations",
                        "beats estimates", "beat estimates", "tops forecasts",
                        "tops estimates", "above forecasts", "above consensus",
                        "above estimates", "exceeds forecasts", "exceeds expectations"),
        fixed_requires_subject=True,
    ),
    # Labour data.
    PolarityPair(
        positive="WEAK_JOBS", negative="STRONG_JOBS",
        subjects=("payrolls", "nonfarm", "nfp", "jobs report", "unemployment",
                  "employment", "jobless claims", "labour market",
                  "labor market", "hiring", "layoffs"),
        up_verbs=("weakens", "weak", "softens", "slows", "misses", "missed",
                  "miss", "contracts", "contracted", "shrinks", "shrank",
                  "cools", "cooled", "eases", "eased", "below expectations",
                  "weak jobs", "deteriorates", "fades"),
        down_verbs=("beats", "beat", "strong", "strengthens", "strengthened",
                    "accelerates", "accelerated", "above expectations",
                    "record jobs", "hiring surge", "robust", "solid",
                    "expands", "stronger", "higher"),
    ),
    # Risk sentiment.
    PolarityPair(
        positive="RISK_ON", negative="RISK_OFF",
        subjects=("risk appetite", "risk sentiment", "investor sentiment",
                  "market sentiment", "risk-on", "risk-off", "risk tone",
                  "risk mood", "risk", "sentiment", "appetite"),
        up_verbs=("improves", "improved", "improving", "rises", "rose",
                  "rising", "returns", "returned", "recovers", "recovered",
                  "boosts", "boosted", "expands", "expanded", "rebound",
                  "rebounds", "rebounded", "calms", "calmer", "relieved",
                  "firms", "strong", "returns to"),
        down_verbs=("fades", "faded", "fading", "falls", "fell", "falling",
                    "deteriorates", "deteriorated", "collapses", "collapsed",
                    "plunges", "plunged", "worsens", "worsened", "sours",
                    "spreads", "vanishes", "evaporates", "evaporated",
                    "sinks", "sank", "weakens", "weakened", "slips",
                    "slipped", "dampens", "dampened"),
        fixed_positive=("risk-on", "stocks rally", "risk appetite improves"),
        fixed_negative=("risk-off", "risk aversion", "flight to quality",
                        "panic selling", "safe haven demand",
                        "safe-haven demand"),
    ),
)


def _tokens(text: str) -> list:
    return _TOKEN_RE.findall((text or "").lower())


def _find_subject_index(tokens: list, subject: str) -> int | None:
    """Index of the token where a multi-word subject starts, or None."""
    words = subject.split()
    if len(words) == 1:
        for i, tok in enumerate(tokens):
            if contains_cue(tok, words[0]):
                return i
        return None
    for i in range(len(tokens) - len(words) + 1):
        if all(contains_cue(tokens[i + k], words[k]) for k in range(len(words))):
            return i
    return None


def _scan_verbs(tokens: list, start: int, length: int, verbs) -> int:
    """
    Count verb hits that genuinely modify this subject.

    Leading and trailing windows are scanned, but a CLAUSE_BOUNDARY token ends
    the scan in that direction. That single rule is what keeps "risk appetite as
    volatility RISES" from reading the volatility verb as a risk-appetite verb.
    """
    hits = 0

    # Trailing window: from the end of the subject forwards, stopping at a clause
    # boundary or the end of the text.
    for i in range(start + length, len(tokens)):
        if tokens[i] in CLAUSE_BOUNDARIES:
            break
        if i - start >= VERB_WINDOW_TOKENS:
            break
        if any(contains_cue(tokens[i], v) for v in verbs):
            hits += 1

    # Leading window: backwards from the start of the subject, same two stops.
    for i in range(start - 1, -1, -1):
        if tokens[i] in CLAUSE_BOUNDARIES:
            break
        if start - i > VERB_WINDOW_TOKENS:
            break
        if any(contains_cue(tokens[i], v) for v in verbs):
            hits += 1

    return hits


def resolve_pair(lowered: str, pair: PolarityPair) -> str | None:
    """
    Return the EventType name this pair implies, or None if ambiguous.

    None is a real answer: a headline naming the subject with no directional
    verb ("the dollar index") genuinely does not say which way it went, and
    guessing here is what produced VAL-5.
    """
    # If fixed phrases require a subject, check for one first.
    if pair.fixed_requires_subject:
        tokens = _tokens(lowered)
        has_subject = any(_find_subject_index(tokens, s) is not None
                          for s in pair.subjects)
        if not has_subject:
            # Skip fixed phrases; fall through to normal subject/verb logic.
            pass
        else:
            for phrase in pair.fixed_negative:
                if contains_plural(lowered, phrase):
                    return pair.negative
            for phrase in pair.fixed_positive:
                if contains_plural(lowered, phrase):
                    return pair.positive
    else:
        for phrase in pair.fixed_negative:
            if contains_plural(lowered, phrase):
                return pair.negative
        for phrase in pair.fixed_positive:
            if contains_plural(lowered, phrase):
                return pair.positive

    tokens = _tokens(lowered)
    if not tokens:
        return None

    up_hits = down_hits = 0
    matched = False
    for subject in pair.subjects:
        index = _find_subject_index(tokens, subject)
        if index is None:
            continue
        matched = True
        length = len(subject.split())
        up_hits += _scan_verbs(tokens, index, length, pair.up_verbs)
        down_hits += _scan_verbs(tokens, index, length, pair.down_verbs)

    if not matched:
        return None
    if up_hits and not down_hits:
        return pair.positive
    if down_hits and not up_hits:
        return pair.negative
    # Both, or neither: genuinely two-sided or directionless. Emit NOTHING so
    # the aggregator is free to answer NO_BIAS.
    return None


def resolve_all(lowered: str) -> list:
    """EventType names implied by polarity, in table order."""
    out: list = []
    for pair in POLARITY_PAIRS:
        name = resolve_pair(lowered, pair)
        if name and name not in out:
            out.append(name)
    return out


# --------------------------------------------------------------------------
# DIRECT ASSET MOVES ("gold rose", "bitcoin slipped")
# --------------------------------------------------------------------------
# The mirror-symmetry audit found this hole: POLARITY_PAIRS holds MACRO subjects
# only (the dollar, yields, the Fed, ...), so a headline naming the ASSET itself
# matched nothing. "Gold rose sharply on haven demand" resolved to no event,
# produced no transmission for XAUUSD, and scored 0.0 - while detect_direction()
# correctly called it BULLISH. The two halves of the engine disagreed, and the
# per-asset half (the one that actually produces a verdict) was the broken one.
#
# A direct statement about an asset's own price is the strongest evidence a
# headline can carry, so it is resolved here and applied as an override by
# transmission_map.resolve().
ASSET_PRICE_SUBJECTS: dict = {
    "XAUUSD": ("gold", "xau", "bullion"),
    "XAGUSD": ("silver", "xag"),
    "BTCUSD": ("bitcoin", "btc"),
}

# Words that mean the asset is NOT the subject of the verb. "gold ETF inflows"
# is a FLOW story, not a statement that gold rose, and must not be overridden.
_FLOW_CONTEXT = (
    "etf", "inflow", "outflow", "flows", "holding", "holdings", "reserve",
    "buyback", "accumulation", "trust", "fund", "launch", "listing",
)


def direct_asset_moves(lowered: str) -> dict:
    """
    Assets whose OWN price is the subject of a directional verb.

    Returns {asset_name: +1|-1}. An asset whose verb is missing, ambiguous, or
    attached to a flow noun is simply absent, so the caller leaves the normal
    transmission-map verdict in place.
    """
    tokens = _tokens(lowered)
    out: dict = {}
    for asset, subjects in ASSET_PRICE_SUBJECTS.items():
        up_hits = down_hits = 0
        for subject in subjects:
            words = subject.split()
            for i in range(len(tokens) - len(words) + 1):
                if not all(contains_cue(tokens[i + k], words[k])
                           for k in range(len(words))):
                    continue
                # A flow noun ANYWHERE between the asset and its verb means the
                # verb belongs to the flow, not to the price: "gold ETF HOLDINGS
                # rose" is an inflow story, not a statement that gold rose.
                trailing = tokens[i + len(words):i + len(words)
                                   + VERB_WINDOW_TOKENS]
                if any(contains_cue(w, f) for w in trailing
                       for f in _FLOW_CONTEXT):
                    continue
                up_hits += _scan_verbs(tokens, i, len(words), UP_VERBS)
                down_hits += _scan_verbs(tokens, i, len(words), DOWN_VERBS)
        if up_hits and not down_hits:
            out[asset] = 1
        elif down_hits and not up_hits:
            out[asset] = -1
    return out
