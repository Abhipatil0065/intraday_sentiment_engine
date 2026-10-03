"""
WORD-BOUNDARY PHRASE MATCHING.

Why this module exists
---------------------
The classifier used raw ``phrase in text`` substring tests everywhere. That is
the single largest source of false positives in the engine, because English
embeds one word inside another all the time:

    "Novig CREDITS Sydney Sweeney..."  -> matched the BANKING keyword "credit"
    "bank stress"                    -> matched the CRYPTO keyword "ban"
    "analysts warn of slowdown"      -> matched the GEOPOLITICS keyword "war"

A macro keyword must be a real word in the text, not a fragment of one.

Two matching policies are provided:

``contains_phrase``
    Strict. The phrase must be surrounded by non-word characters, so
    ``"credit"`` does NOT match ``"credits"``. Used for category keywords,
    importance triggers and relevance tokens, where an inflectional form is a
    genuinely different word.

``contains_cue``
    Directional cues. Allows a plain English inflection suffix
    (s / es / ed / ing) so that ``"plunge"`` still matches ``"plunges"`` and
    ``"inflow"`` still matches ``"inflows"``. Verbs must inflect or the
    directional cues would silently stop firing on past-tense headlines.

Compiled patterns are cached because this runs on every article on every
pipeline tick.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Suffixes that turn a base verb/noun into an ordinary inflected form. Kept
# deliberately short: "credit"+"s" -> "credits" is NOT an inflection of a
# banking-stress signal, which is why category keywords use the strict policy.
_INFLECTION = r"(?:s|es|ed|ing)?"


@lru_cache(maxsize=4096)
def _strict_pattern(phrase: str) -> re.Pattern[str]:
    # (?<!\w)/(?!\w) instead of \b so the guard is explicit at string edges
    # and behaves identically for phrases that begin/end with punctuation.
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=4096)
def _cue_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<!\w){re.escape(phrase)}{_INFLECTION}(?!\w)", re.IGNORECASE
    )


def contains_phrase(text: str, phrase: str) -> bool:
    """True only when ``phrase`` appears as a whole word (or whole phrase)."""
    if not text or not phrase:
        return False
    return _strict_pattern(phrase).search(text) is not None


def contains_cue(text: str, phrase: str) -> bool:
    """Like :func:`contains_phrase` but tolerates a plain inflection suffix."""
    if not text or not phrase:
        return False
    return _cue_pattern(phrase).search(text) is not None


@lru_cache(maxsize=1024)
def _stem_pattern(phrase: str) -> re.Pattern[str]:
    """
    Prefix match anchored at a word boundary.

    Reserved for keywords that are deliberately authored as STEMS - e.g.
    "liquidat" is meant to catch "liquidations", "escalat" to catch
    "escalating"/"escalation". Anchoring only the START keeps the match from
    firing on a fragment in the middle of an unrelated word.
    """
    return re.compile(rf"(?<!\w){re.escape(phrase)}", re.IGNORECASE)


def contains_stem(text: str, phrase: str) -> bool:
    """True when ``phrase`` matches the beginning of a word (stem keywords)."""
    if not text or not phrase:
        return False
    return _stem_pattern(phrase).search(text) is not None


@lru_cache(maxsize=4096)
def _plural_pattern(phrase: str) -> re.Pattern[str]:
    """
    Whole word, optionally plural.

    News keywords are overwhelmingly used in the plural ("strikes",
    "sanctions", "yields", "ETFs"), so a strict whole-word-only test silently
    drops real macro stories. Only a plain plural suffix is allowed here - no
    "-ing"/"-ed" - because those can cross into unrelated words.
    """
    return re.compile(
        rf"(?<!\w){re.escape(phrase)}(?:s|es)?(?!\w)", re.IGNORECASE
    )


def contains_plural(text: str, phrase: str) -> bool:
    """True for the word itself or its regular plural form."""
    if not text or not phrase:
        return False
    return _plural_pattern(phrase).search(text) is not None


def find_plural(text: str, phrase: str) -> int:
    """Start index of the first whole-word/plural match, or -1 when absent."""
    if not text or not phrase:
        return -1
    m = _plural_pattern(phrase).search(text)
    return m.start() if m else -1


def cue_hits(text: str, phrase: str) -> int:
    """How many times ``phrase`` occurs as an inflected cue in the text."""
    if not text or not phrase:
        return 0
    return len(_cue_pattern(phrase).findall(text))


def keyword_position(text: str, keyword: str, *,
                     stems: frozenset[str] = frozenset(),
                     plural_exempt: frozenset[str] = frozenset()) -> int:
    """
    Resolve a macro keyword to a start index, or -1 when it is absent.

    This is the single place where the matching POLICY lives, so category
    detection and importance triggers cannot drift apart.

    Policy, in priority order:

    1. ``keyword`` is a declared stem  -> start-anchored prefix match
       ("liquidat" still catches "liquidations").
    2. ``keyword`` is plural-exempt     -> whole word only
       ("credit" must not match "credits").
    3. otherwise                        -> whole word or regular plural
       ("strike" matches "strikes", which headlines use constantly).

    Every branch is bounded by a word boundary at the START, so a keyword can
    never fire from inside the middle of an unrelated word - the defect that
    made "Novig credits Sydney Sweeney" score as a banking-stress story.
    """
    if not text or not keyword:
        return -1
    if keyword in stems:
        return find_stem(text, keyword)
    if keyword in plural_exempt:
        return find_phrase(text, keyword)
    return find_plural(text, keyword)


def find_phrase(text: str, phrase: str) -> int:
    """Start index of the first whole-word match, or -1 when absent."""
    if not text or not phrase:
        return -1
    m = _strict_pattern(phrase).search(text)
    return m.start() if m else -1


def find_stem(text: str, phrase: str) -> int:
    """Start index of the first stem match, or -1 when absent."""
    if not text or not phrase:
        return -1
    m = _stem_pattern(phrase).search(text)
    return m.start() if m else -1