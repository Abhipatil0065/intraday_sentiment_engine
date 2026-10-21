"""
POLICY SCOPE: whose central bank a policy headline is about, and how strongly
that bank's stance reaches the US-asset complex.

THE DEFECT THIS FIXES
---------------------
The transmission map's policy rows are written for the US central bank:

    HAWKISH_POLICY -> XAUUSD: sign -1, magnitude 1.0, gold's #1 ranked driver

The chain that makes a hawkish Fed bearish gold is a US chain: hawkish Fed
-> higher US rate expectations -> higher US real yields -> more expensive
to hold a non-yielding metal. NONE of that holds for another central bank.

A live example (2026-10-21, gold card, 16:33 UTC):

    "Australian Dollar hits one-mounth high as hawkish RBA gives boost"

was carried into the gold card as a Tier-1, VERY_HIGH, CONFIRMED bearish
signal with 0.88 relevance, and took 0.695 of a 2.180 bear pool - 32% of
the card's entire bearish evidence - and flipped the badge from BULLISH to
BEARISH on the fresh-Tier-1 override. An RBA that turns hawkish does not
raise US real yields, and a stronger AUD is not a stronger dollar. The word
"hawkish" was doing all the work; the central bank it belonged to was
never read.

WHAT THIS MODULE KNOWS
----------------------
Two facts, and nothing else:

1. Which central bank a headline names - the US one, a foreign one, or
   both. A headline that names the Fed AND the RBA is a US-policy headline
   for these cards, because the Fed is the one that moves US rates.

2. How strongly a FOREIGN central bank's policy stance reaches gold,
   silver, bitcoin and the S&P. The answer is: weakly, and with an
   approximate sign, because the channel runs through the dollar rather
   than through US rates.

THE SCALE
---------
``FOREIGN_CB_POLICY_SCALE`` is 0.30 - the same "not a listed driver for
this asset" weight the map already uses for a mechanism it does not model
(``DRIVER_RANKINGS`` default). The US central bank keeps the full channel.

Nothing else is attenuated here. A "hawkish Fed" headline is a Tier-1
event for gold and stays one; a "hawkish RBA" headline is a Tier-2
development with a weak channel, and that is what the card says.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Who is the headline about?
# --------------------------------------------------------------------------
# Whole-word tokens. "fed" is the Fed in a financial headline; the map's
# category keywords already treat it that way.
US_CENTRAL_BANK_TOKENS: tuple[str, ...] = (
    "federal reserve",
    "fed",
    "fomc",
    "powell",
    "bostic",
    "warsh",
    "u.s. central bank",
    "us central bank",
    "american central bank",
)

# Central banks whose policy does NOT set US rates. Their stance reaches
# the US-asset complex only through the dollar, and weakly.
FOREIGN_CENTRAL_BANK_TOKENS: tuple[str, ...] = (
    "rba",
    "reserve bank of australia",
    "ecb",
    "european central bank",
    "boe",
    "bank of england",
    "boj",
    "bank of japan",
    "pboc",
    "peoples bank of china",
    "rbnz",
    "reserve bank of new zealand",
    "boc",
    "bank of canada",
    "snb",
    "swiss national bank",
    "bok",
    "bank of korea",
    "riksbank",
    "sarb",
    "norges bank",
    "bank of russia",
    "banco de mexico",
    # The descriptive forms: "the Australian central bank turns hawkish".
    "australian central bank",
    "european central bank",
    "japanese central bank",
    "chinese central bank",
    "swiss central bank",
    "canadian central bank",
    "korean central bank",
    "swedish central bank",
    "new zealand central bank",
    "mexican central bank",
    "russian central bank",
    "british central bank",
)

# The event types that carry a CENTRAL-BANK STANCE. A foreign central bank
# speaking on any of them is attenuated; every other mechanism (a dollar
# move, a yield move, a flow, a print) is read on its own terms.
POLICY_EVENT_TYPES: frozenset[str] = frozenset({
    "HAWKISH_POLICY",
    "DOVISH_POLICY",
    "RECESSION_CUT",
    "RATE_REPRICING",
})

# How much of the US central bank's channel survives when the headline is
# about somebody else's central bank.
FOREIGN_CB_POLICY_SCALE = 0.30


def _compile(tokens: tuple[str, ...]) -> re.Pattern:
    # Longest first so "federal reserve" wins over "fed" inside it, and
    # word-boundary anchored so "fed" does not match "feds" and "rba" does
    # not match "sarb".
    parts = sorted(tokens, key=len, reverse=True)
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(p) for p in parts) + r")(?!\w)")


_US_RE = _compile(US_CENTRAL_BANK_TOKENS)
_FOREIGN_RE = _compile(FOREIGN_CENTRAL_BANK_TOKENS)


def names_us_central_bank(text: str) -> bool:
    """True when the text names the US central bank."""
    return bool(_US_RE.search((text or "").lower()))


def names_foreign_central_bank(text: str) -> bool:
    """True when the text names a central bank other than the US one."""
    return bool(_FOREIGN_RE.search((text or "").lower()))


def foreign_policy_scope(text: str) -> bool:
    """True when the headline is about a FOREIGN central bank only.

    A headline naming both the Fed and the RBA is NOT in foreign scope: the
    Fed sets the rates that price these assets, so the full channel applies.

    Only the TITLE is read here (the signal carries no body), so a story that
    names its central bank only in the body text is not detected. The
    classifier's own stance rule (``heuristics.VERY_HIGH_TRIGGERS``) covers
    that case separately: a stance adjective alone never makes a headline
    release-grade, whoever the central bank turns out to be.
    """
    lowered = (text or "").lower()
    return bool(_FOREIGN_RE.search(lowered)) and not _US_RE.search(lowered)


def policy_event_scale(event_type: str, text: str) -> float:
    """How strongly one detected event reaches the asset, for POLICY events.

    1.0 for every mechanism and for the US central bank; ``FOREIGN_CB_POLICY_SCALE``
    for a central-bank stance that belongs to somebody else.
    """
    if event_type not in POLICY_EVENT_TYPES:
        return 1.0
    if foreign_policy_scope(text):
        return FOREIGN_CB_POLICY_SCALE
    return 1.0
