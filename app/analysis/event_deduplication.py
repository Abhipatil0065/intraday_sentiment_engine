"""
EVENT DEDUPLICATION (section 16).

Many publishers cover one story with several headlines. Those must collapse
into a SINGLE event so the same macro impulse is not counted three times
and triple-counted in the sentiment aggregate.

Method: token-set Jaccard similarity on normalised title tokens, restricted
to articles inside a rolling time window. No external service required.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at", "by",
    "with", "from", "as", "is", "are", "was", "were", "be", "been", "it", "its",
    "this", "that", "these", "those", "will", "would", "could", "should", "has",
    "have", "had", "not", "than", "then", "so", "if", "up", "down", "out",
    "says", "say", "said", "new", "after", "over", "into", "more", "most", "other",
    "amid", "about", "may", "his", "her", "their", "our", "us", "he", "she", "they",
}

# Numbers are stripped: "CPI 3.2%" and "CPI 3.4%" are the same event.
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalise_tokens(title: str) -> set[str]:
    text = _NUM_RE.sub(" ", title.lower())
    text = _PUNCT_RE.sub(" ", text)
    return {w for w in _WORD_RE.findall(text) if w not in _STOPWORDS and len(w) > 2}


def similarity(a: set[str], b: set[str]) -> float:
    """Jaccard similarity in 0.0-1.0."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _containment(a: set[str], b: set[str]) -> float:
    """Overlap relative to the smaller set - catches 'Fed holds rates' vs
    'Fed holds rates steady amid inflation worries' where Jaccard is diluted."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class DedupeResult:
    event_key: str
    canonical_title: str
    is_new_event: bool
    matched_title: str | None = None
    score: float = 0.0


class EventDeduplicator:
    """
    Stateful, in-memory index of today's events.

    ``match`` returns the existing event key when a title belongs to a known
    event, otherwise a brand-new key. Only one representative headline per
    event carries weight in the sentiment aggregate.
    """

    def __init__(self, threshold: float | None = None,
                 window_hours: int | None = None) -> None:
        self.threshold = threshold if threshold is not None else settings.dedup_similarity_threshold
        self.window = timedelta(
            hours=window_hours if window_hours is not None else settings.dedup_time_window_hours
        )
        self._events: list[tuple[set[str], str, str, datetime]] = []

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.window
        self._events = [e for e in self._events if e[3] >= cutoff]

    def match(self, title: str, published_at: datetime,
              now: datetime | None = None) -> DedupeResult:
        """
        Find the event this title belongs to, or mint a new key.

        ``now`` is the clock the 12h prune window is measured from. STEP 9.1:
        callers SHOULD pass it - ``_ingest_article`` passes the tick's clock.
        When it is omitted it falls back to ``published_at``, which used to
        make the window drift forward with each incoming article: a newer
        duplicate kept an old event alive indefinitely, and the index never
        fully forgot yesterday's stories. The fallback is kept so the unit
        tests and any standalone caller that has no tick to offer still work,
        but it is now documented as the degenerate case rather than the default.
        """
        if now is None:
            log.debug(
                "EventDeduplicator.match() called without `now`; falling back "
                "to published_at for the prune clock, which drifts the window")
            now = published_at
        self._prune(now)
        tokens = normalise_tokens(title)

        best_key, best_title, best_score = None, None, 0.0
        for ev_tokens, key, ev_title, _ts in self._events:
            jac = similarity(tokens, ev_tokens)
            cont = _containment(tokens, ev_tokens)
            # Blend: containment rescues headline padding, Jaccard avoids
            # false merges from a single shared word.
            score = 0.5 * jac + 0.5 * cont
            if score > best_score:
                best_key, best_title, best_score = key, ev_title, score

        if best_key and best_score >= self.threshold:
            log.debug("Dedup match %.2f -> %s", best_score, best_key)
            return DedupeResult(
                event_key=best_key,
                canonical_title=best_title or title,
                is_new_event=False,
                matched_title=best_title,
                score=round(best_score, 3),
            )

        key = f"{published_at.timestamp():.0f}-{abs(hash(frozenset(tokens))):x}"
        self._events.append((tokens, key, title, published_at))
        return DedupeResult(event_key=key, canonical_title=title, is_new_event=True)

    def seed_from_titles(self, titles: Iterable[str],
                         published_at: datetime) -> None:
        """Rebuild the index from already-stored articles after a restart."""
        for t in titles:
            self.match(t, published_at, now=published_at)