"""
EVENT CLUSTERING + CAPPED CORROBORATION (improvement item 4).

The defect this replaces
------------------------
The old deduplicator collapsed near-duplicate titles into one event key, but a
cluster still produced one weighted signal PER ARTICLE. Five outlets covering
a single FOMC decision therefore cast five votes into the aggregate, and the
verdict drifted toward whichever story was syndicated most widely.

The model now
-------------
A cluster is ONE event with ONE vote. Extra INDEPENDENT sources add only a
small, hard-capped boost (``corroboration_per_source`` per source, capped at
``corroboration_max_boost``), so corroboration confirms a story without
out-voting a genuinely different one. Repeats from the SAME outlet never
raise the boost.

Similarity blends normalised-title overlap with shared macro ENTITIES, which
is what actually identifies "the same event" when two outlets word it very
differently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "it",
    "its", "this", "that", "these", "those", "will", "would", "could", "should",
    "has", "have", "had", "not", "than", "then", "so", "if", "up", "down",
    "out", "says", "say", "said", "new", "after", "over", "into", "more",
    "most", "other", "amid", "about", "may", "his", "her", "their", "our",
    "us", "he", "she", "they", "latest", "report", "reports", "update",
}

# Macro entities. Two headlines about different central banks or different
# assets are NOT the same event even if they share generic vocabulary.
_ENTITIES = {
    "fed", "fomc", "ecb", "boe", "boj", "pboc", "powell", "lagarde",
    "cpi", "pce", "ppi", "nfp", "nonfarm", "payrolls", "payroll", "gdp",
    "pmi", "ism", "gold", "silver", "bitcoin", "btc", "xau", "xag",
    "treasury", "dollar", "dxy", "etf", "sec", "cftc", "opec", "oil",
    "tariff", "sanctions", "recession", "inflation", "yields",
}

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalise_tokens(title: str) -> set[str]:
    """Content words, numbers stripped: "CPI 3.2%" and "CPI 3.4%" are one event."""
    text = _NUM_RE.sub(" ", (title or "").lower())
    text = _PUNCT_RE.sub(" ", text)
    return {w for w in _WORD_RE.findall(text) if w not in _STOPWORDS and len(w) > 2}


def extract_entities(title: str) -> set[str]:
    """Macro entities named in the headline - the real event fingerprint."""
    text = _PUNCT_RE.sub(" ", (title or "").lower())
    return set(_WORD_RE.findall(text)) & _ENTITIES


def corroboration_multiplier(independent_sources: int) -> float:
    """
    Weight multiplier for a cluster, from its number of independent sources.

    1 source -> 1.0 (no boost). 2 -> 1.10. Capped at 1.30 by default. A cluster
    covered by twenty outlets is still ONE event, not twenty.
    """
    if independent_sources <= 1:
        return 1.0
    per = float(settings.corroboration_per_source)
    cap = float(settings.corroboration_max_boost)
    boost = min(cap, per * (independent_sources - 1))
    return round(1.0 + boost, 4)


@dataclass
class ClusterMatch:
    """Outcome of matching one headline against the live cluster index."""

    cluster_id: str
    canonical_title: str
    is_new_cluster: bool
    matched_title: Optional[str] = None
    similarity: float = 0.0
    independent_sources: int = 1
    corroboration: float = 1.0

    @property
    def is_new_event(self) -> bool:
        """Back-compat alias for the pre-existing dedupe API."""
        return self.is_new_cluster


@dataclass
class _Cluster:
    cluster_id: str
    tokens: set[str]
    entities: set[str]
    canonical_title: str
    first_seen: datetime
    sources: set[str] = field(default_factory=set)


def _blend(a: set[str], b: set[str]) -> float:
    """Jaccard + containment blend - rescues padded headlines, avoids false merges."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    jaccard = len(a & b) / union if union else 0.0
    containment = len(a & b) / min(len(a), len(b))
    return 0.5 * jaccard + 0.5 * containment


class EventClusterer:
    """
    Stateful, in-memory index of clusters inside a rolling time window.

    ``match`` returns the existing cluster id when a headline belongs to a known
    event, otherwise a new id. The caller adds ONE weighted signal per cluster
    and multiplies it by ``corroboration``.
    """

    def __init__(self, threshold: Optional[float] = None,
                 window_hours: Optional[int] = None) -> None:
        self.threshold = (threshold if threshold is not None
                          else settings.dedup_similarity_threshold)
        self.window = timedelta(
            hours=window_hours if window_hours is not None
            else settings.dedup_time_window_hours)
        self._clusters: list[_Cluster] = []

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.window
        self._clusters = [c for c in self._clusters if c.first_seen >= cutoff]

    def match(self, title: str, published_at: datetime, *, source: str = "",
              now: Optional[datetime] = None) -> ClusterMatch:
        now = now or published_at
        self._prune(now)
        tokens = normalise_tokens(title)
        entities = extract_entities(title)

        best: Optional[_Cluster] = None
        best_score = 0.0
        for cluster in self._clusters:
            score = _blend(tokens, cluster.tokens)
            # Shared macro entities are strong evidence of the same event.
            if entities and cluster.entities:
                shared = len(entities & cluster.entities)
                if shared:
                    score = min(1.0, score + 0.15 * shared)
            if score > best_score:
                best, best_score = cluster, score

        if best is not None and best_score >= self.threshold:
            if source:
                best.sources.add(source)
            log.debug("Cluster match %.2f -> %s", best_score, best.cluster_id)
            return ClusterMatch(
                cluster_id=best.cluster_id,
                canonical_title=best.canonical_title,
                is_new_cluster=False,
                matched_title=best.canonical_title,
                similarity=round(best_score, 3),
                independent_sources=max(1, len(best.sources)),
                corroboration=corroboration_multiplier(len(best.sources)),
            )

        cluster_id = (f"{published_at.timestamp():.0f}-"
                      f"{abs(hash(frozenset(tokens | entities))):x}")
        self._clusters.append(_Cluster(
            cluster_id=cluster_id,
            tokens=tokens,
            entities=entities,
            canonical_title=title,
            first_seen=published_at,
            sources={source} if source else set(),
        ))
        return ClusterMatch(
            cluster_id=cluster_id,
            canonical_title=title,
            is_new_cluster=True,
            independent_sources=1,
            corroboration=1.0,
        )

    def seed_from_titles(self, titles: Iterable[str],
                         published_at: datetime) -> None:
        """Rebuild the index from already-stored articles after a restart."""
        for title in titles:
            self.match(title, published_at, now=published_at)