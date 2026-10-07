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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.analysis.sentiment_engine import WeightedSignal
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

    Uses diminishing returns: boost = per_source * sum(1/log2(i+1)) for i=2..n,
    capped at corroboration_max_boost. 1 source -> 1.0 (no boost).
    Ten duplicates do not add up to ten times the weight.
    """
    if independent_sources <= 1:
        return 1.0
    import math
    per = float(settings.corroboration_per_source)
    cap = float(settings.corroboration_max_boost)
    # Diminishing returns: 1/log2(n+1) scaling for sources 2, 3, 4...
    boost = sum(per / math.log2(i + 1) for i in range(2, independent_sources + 1))
    boost = min(cap, boost)
    return round(1.0 + boost, 4)


def cluster_signals(signals: list, window_hours: float = 0.5) -> list:
    """
    Cluster signals by normalized title within a time window.

    This implements near-duplicate clustering at SCORING time (not ingestion time).
    Each cluster contributes ONE signal (the highest-weight member) with a
    corroboration boost based on the number of independent sources in the cluster.

    Args:
        signals: List of WeightedSignal objects
        window_hours: Time window for clustering (default 0.5 hours = 30 minutes)

    Returns:
        List of clustered WeightedSignal objects with updated corroboration
    """
    if not signals:
        return []

    # Group by asset first
    by_asset: dict = {}
    for s in signals:
        by_asset.setdefault(s.asset, []).append(s)

    clustered: list[WeightedSignal] = []
    window = timedelta(hours=window_hours)

    for asset_signals in by_asset.values():
        # Sort by published time (handle timezone-aware and naive datetimes)
        def get_sort_key(s):
            dt = s.published_at
            if dt is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        asset_signals.sort(key=get_sort_key)

        # Simple clustering: group by normalized title within time window
        clusters: list[list[WeightedSignal]] = []
        for s in asset_signals:
            if s.published_at is None:
                clusters.append([s])
                continue

            matched = False
            for cluster in clusters:
                # Check if this signal belongs to an existing cluster
                # Use the first signal in cluster as representative
                rep = cluster[0]
                if rep.published_at is None:
                    continue
                # Time window check (normalize both to UTC)
                s_dt = s.published_at
                rep_dt = rep.published_at
                if s_dt.tzinfo is None:
                    s_dt = s_dt.replace(tzinfo=timezone.utc)
                if rep_dt.tzinfo is None:
                    rep_dt = rep_dt.replace(tzinfo=timezone.utc)
                if abs((s_dt - rep_dt).total_seconds()) > window.total_seconds():
                    continue
                # Title similarity check using normalised tokens
                s_tokens = normalise_tokens(s.title)
                rep_tokens = normalise_tokens(rep.title)
                if not s_tokens or not rep_tokens:
                    continue
                union = len(s_tokens | rep_tokens)
                inter = len(s_tokens & rep_tokens)
                jaccard = inter / union if union else 0.0
                # Also check entity overlap
                s_entities = extract_entities(s.title)
                rep_entities = extract_entities(rep.title)
                entity_boost = 0.0
                if s_entities and rep_entities:
                    shared = len(s_entities & rep_entities)
                    if shared:
                        entity_boost = 0.15 * shared
                similarity = min(1.0, jaccard + entity_boost)
                if similarity >= 0.72:  # Use same threshold as dedup
                    cluster.append(s)
                    matched = True
                    break

            if not matched:
                clusters.append([s])

        # For each cluster, keep the highest-weight signal and apply corroboration boost
        for cluster in clusters:
            if len(cluster) == 1:
                clustered.append(cluster[0])
            else:
                # Find the signal with the highest final weight
                best = max(cluster, key=lambda s: max(0, s.final_weight))
                # Count unique sources in this cluster
                sources = {s.source_quality for s in cluster}
                # Also consider source_name if available
                source_names = {getattr(s, 'source_name', '') for s in cluster if getattr(s, 'source_name', '')}
                independent_sources = len(sources | source_names)
                # Apply corroboration boost
                boost = corroboration_multiplier(independent_sources)
                # Create a new signal with the boosted corroboration
                new_signal = WeightedSignal(
                    asset=best.asset,
                    title=best.title,
                    direction=best.direction,
                    strength=best.strength,
                    importance=best.importance,
                    published_at=best.published_at,
                    relevance=best.relevance,
                    persistence=best.persistence,
                    source_quality=best.source_quality,
                    category=best.category,
                    cluster_id=best.cluster_id,
                    corroboration=max(best.corroboration, boost),
                    session_multiplier=best.session_multiplier,
                    is_primary=best.is_primary,
                    event_key=best.event_key,
                    reason=best.reason,
                    transmission=best.transmission,
                    report_kind=best.report_kind,
                    mode=best.mode,
                    surprise_weight=best.surprise_weight,
                )
                clustered.append(new_signal)

    return clustered


@dataclass
class ClusterMatch:
    """Outcome of matching one headline against the live cluster index."""

    cluster_id: str
    canonical_title: str
    is_new_cluster: bool
    matched_title: str | None = None
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

    def __init__(self, threshold: float | None = None,
                 window_hours: int | None = None) -> None:
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
              now: datetime | None = None) -> ClusterMatch:
        now = now or published_at
        self._prune(now)
        tokens = normalise_tokens(title)
        entities = extract_entities(title)

        best: _Cluster | None = None
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