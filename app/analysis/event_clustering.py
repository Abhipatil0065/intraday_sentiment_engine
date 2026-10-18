"""
EVENT CLUSTERING + CAPPED CORROBORATION (improvement item 4).

The defect this replaces
------------------------
The old deduplicator collapsed near-duplicate titles into one event key, but a
cluster still produced one weighted signal PER ARTICLE. Five outlets covering
a single FOMC decision therefore cast five votes into the aggregate, and the
verdict drifted toward whichever story was syndicated most widely.

The model now
------------
A cluster is ONE event with ONE vote. Extra INDEPENDENT sources add only a
small, hard-capped boost (``corroboration_per_source`` per source, capped at
``corroboration_max_boost``), so corroboration confirms a story without
out-voting a genuinely different one. Repeats from the SAME outlet never
raise the boost.

The ingestion-time event identity (``event_key`` / ``cluster_id``) is checked
FIRST and carries NO time window: articles that ingestion dedup filed under
one event collapse here regardless of publish time or outlet wording, so a
republished headline can never re-enter the aggregate as a fresh vote.
Title similarity remains the fallback for signals with no identity.

Similarity blends normalised-title overlap with shared macro ENTITIES, which
is what actually identifies "the same event" when two outlets word it very
differently.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.analysis.sentiment_engine import STRENGTH_MULT, WeightedSignal, _signal_weight, _ensure_aware
from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import Direction

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

# Specific event anchors for the second clustering rule (2026-10-08).
# A central-bank family maps its named spellings onto one family key;
# every name here is already a macro entity in _ENTITIES above, so
# the minutes/decision anchors reuse the existing entity detection.
_CENTRAL_BANK_FAMILIES = (
    ("fed", frozenset({"fed", "fomc", "powell"})),
    ("ecb", frozenset({"ecb", "lagarde"})),
    ("boe", frozenset({"boe"})),
    ("boj", frozenset({"boj"})),
    ("pboc", frozenset({"pboc"})),
)

# Named statistical releases, most specific first. A headline that
# covers several releases ("NFP ... unemployment ...") anchors on
# its PRIMARY release only - the first hit in this order - so a
# payrolls headline and an unemployment headline never share a key.
_RELEASE_EVENTS = (
    ("nfp", frozenset({"nonfarm", "payrolls", "payroll", "nfp"})),
    ("unemployment", frozenset({"unemployment", "jobless"})),
    ("cpi", frozenset({"cpi"})),
    ("ppi", frozenset({"ppi"})),
    ("pce", frozenset({"pce"})),
    ("gdp", frozenset({"gdp"})),
    ("pmi", frozenset({"pmi"})),
    ("ism", frozenset({"ism"})),
)

# Words that turn a central-bank headline into a DECISION event.
# Deliberately narrow: commentary, speeches and forecasts carry no
# decision word and therefore anchor on nothing.
_CB_DECISION_WORDS = frozenset({
    "decision", "decides", "holds", "hold", "hike", "hikes",
    "hiked", "cut", "cuts", "raise", "raises", "raised",
})

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


def event_anchor(title: str) -> str | None:
    """
    The SPECIFIC event a headline is about, or None.

    One key per headline, chosen by priority:
      1. "<bank>_minutes"  - the minutes document AND the bank
         ("Fed minutes show ...", "FOMC Minutes: ...");
      2. "release:<name>"  - a named statistical release (NFP,
         CPI, PPI, PCE, GDP, PMI, ISM, unemployment);
      3. "<bank>_decision" - the bank AND a decision word
         ("ECB holds rates", "Fed cuts rates").

    Returns None for everything else. Loose topics ("gold",
    "tariff", "inflation") never anchor on their own, so two
    unrelated headlines can never merge through this key.
    """
    text = _PUNCT_RE.sub(" ", (title or "").lower())
    words = frozenset(_WORD_RE.findall(text))
    if "minutes" in words:
        for family, names in _CENTRAL_BANK_FAMILIES:
            if names & extract_entities(title):
                return f"{family}_minutes"
    for release, triggers in _RELEASE_EVENTS:
        if triggers & words:
            return f"release:{release}"
    for family, names in _CENTRAL_BANK_FAMILIES:
        if names & words and (words & _CB_DECISION_WORDS):
            return f"{family}_decision"
    return None


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


def _event_identity(s) -> str:
    """
    The ingestion-time cluster identity stamped on a signal.

    ``build_signal`` stamps the NewsEvent key on every article-derived signal
    (and mirrors it into ``cluster_id``), so copies of one story that
    ingestion dedup filed under one event share this identity here. Falls
    back to ``cluster_id`` for builders that only set that field. Returns
    "" for the synthetic families (calendar) that never went through
    ingestion dedup.
    """
    return ((getattr(s, "event_key", "") or getattr(s, "cluster_id", "") or "")
            .strip())


def _collapse_keyed_events(keyed: dict) -> list:
    """
    STEP 9.3: collapse each event_key to exactly one directional vote.

    The dominant direction wins by summed cluster weight, so a reading backed
    by three independent outlets beats one lone take. When the directions
    actually disagreed, the survivor is flagged ``cluster_conflict`` and
    carries ``conflict_weight``: the summed magnitude of the losing side(s)
    - the evidence that genuinely conflicted with the dominant reading.
    compute() uses that (not the survivor's full weight) as the conflicted
    share of the tick's evidence, so a lopsided collapse does not masquerade
    as a torn market and permanently widen the flip margin.
    """
    from dataclasses import replace

    out: list[WeightedSignal] = []
    _now = datetime.now(timezone.utc)
    for event_key, group in keyed.items():
        directions = {s.direction for s in group}
        if len(directions) == 1:
            # No disagreement: ordinary corroboration path, unchanged.
            out.extend(_collapse_members_unkeyed(group))
            continue

        # Weight per direction, using the same weight the score will apply.
        def _w(s):
            try:
                from app.analysis.sentiment_engine import _signal_weight
                return _signal_weight(s, _now)
            except Exception:  # noqa: BLE001 - never break a cluster over a weight
                return float(getattr(s, "relevance", 0.0) or 0.0)

        by_dir: dict = {}
        for s in group:
            by_dir[s.direction] = by_dir.get(s.direction, 0.0) + _w(s)

        winner = max(by_dir, key=lambda d: by_dir[d])
        winners = [s for s in group if s.direction is winner]
        losers = [s for s in group if s.direction is not winner]

        # Conflict weight: magnitude of the losing side(s) only, on the same
        # magnitude basis the score aggregates (_signal_weight x strength).
        loser_weight = 0.0
        for s in losers:
            loser_weight += _w(s) * STRENGTH_MULT.get(s.strength, 0.45)

        # One vote for the dominant reading. Only the winners are passed, so
        # the corroboration path yields exactly ONE signal and its direction is
        # the winner by construction (rather than whichever dict happens to
        # come first). The losing reading is dropped: the event still happened
        # and is still weighted, it just no longer argues both sides.
        collapsed = _collapse_members_unkeyed(winners)
        keep = collapsed[0] if collapsed else None
        if keep is None:
            continue

        flagged = replace(keep, cluster_conflict=True,
                          conflict_weight=loser_weight)
        log.debug(
            "cluster %s: readings disagreed (%s); kept %s and flagged "
            "cluster_conflict with conflict_weight=%.4f",
            event_key,
            "/".join(sorted(d.value for d in directions)),
            winner.value,
            loser_weight,
        )
        out.append(flagged)
    return out


def _collapse_members(members: list) -> list[WeightedSignal]:
    """One vote per direction inside a finished cluster.

    Splits by direction (a hawkish and a dovish headline about one event are
    NOT one event - they are two readings, and merging them manufactures a
    verdict neither source supports), keeps the heaviest member as the
    representative, and applies the capped corroboration boost for the
    cluster's independent sources.

    The representative is picked by the weight scoring actually uses
    (relevance x persistence x quality x session x tier x decay), NEVER by
    ``final_weight``: that field is assigned by
    ``sentiment_engine._aggregate`` AFTER clustering returns, so every signal
    still carries 0.0 here and a ``final_weight``-based selector picked the
    survivor by arbitrary tuple ordering.
    """
    if len(members) == 1:
        return [members[0]]

    # STEP 9.3: one event is one vote, even when its reports disagree.
    #
    # Two headlines filed under the SAME event_key are the same underlying
    # event seen twice. If they carry opposite directions that is not two
    # balanced votes - it is one uncertain reading, and scoring both doubled
    # the evidence for an event whose direction was never actually
    # established. Members that share an event_key now collapse to the
    # dominant direction as a single vote; the losing reading is dropped and
    # the survivor is flagged contradiction_detected so the verdict is
    # reported as low confidence instead of a conviction.
    #
    # Members with NO event_key keep the original two-vote behaviour: those are
    # two genuinely different stories that merely landed in one cluster.
    keyed: dict[str, list] = {}
    unkeyed: list = []
    for s in members:
        k = (getattr(s, "event_key", "") or "").strip()
        if k:
            keyed.setdefault(k, []).append(s)
        else:
            unkeyed.append(s)

    if keyed:
        collapsed = _collapse_keyed_events(keyed)
        if unkeyed:
            collapsed = collapsed + _collapse_members_unkeyed(unkeyed)
        return collapsed

    return _collapse_members_unkeyed(unkeyed)


def _unkeyed_cluster_id(survivor) -> str:
    """Stable generated ID for an unkeyed cluster.

    Deterministic from the survivor's headline and published time, so
    replaying the same tick produces the same ID. The "unkeyed-" prefix
    guarantees it never collides with the ``event_key`` format used by keyed
    clusters.
    """
    seed = f"{survivor.title or ''}|{survivor.published_at or ''}"
    return "unkeyed-" + hashlib.sha1(seed.encode()).hexdigest()[:16]


def _collapse_members_unkeyed(members: list) -> list:
    """Original path: one vote per direction inside a cluster with no event_key."""
    # Split by direction so opposing readings never share a cluster.
    by_dir: dict = {}
    for s in members:
        by_dir.setdefault(s.direction, []).append(s)

    collapsed: list[WeightedSignal] = []
    for direction_members in by_dir.values():
        if len(direction_members) == 1:
            best = direction_members[0]
            from dataclasses import replace
            if not best.cluster_id:
                best = replace(best, cluster_id=_unkeyed_cluster_id(best))
            collapsed.append(best)
            continue
        # Representative = highest ACTUAL weight, recomputed here with the
        # same formula _signal_weight uses downstream. This is the weight
        # that will decide the contribution, so it is the right thing to
        # maximise.
        from app.analysis.sentiment_engine import _signal_weight

        _now = _scoring_now()
        best = max(direction_members,
                   key=lambda s: max(0.0, _signal_weight(s, _now)))
        # Stamp an unkeyed survivor with a stable generated ID so _aggregate
        # counts this cluster as one vote instead of zero.
        from dataclasses import replace
        if not best.cluster_id:
            best = replace(best, cluster_id=_unkeyed_cluster_id(best))
        # Count unique sources in this sub-cluster
        sources = {s.source_quality for s in direction_members}
        source_names = {getattr(s, 'source_name', '') for s in direction_members
                        if getattr(s, 'source_name', '')}
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
        collapsed.append(new_signal)

    return collapsed


def _titles_near_duplicate(a: str, b: str) -> bool:
    """True when two titles describe the same release by token overlap."""
    ta = normalise_tokens(a)
    tb = normalise_tokens(b)
    if not ta or not tb:
        return False
    union = len(ta | tb)
    inter = len(ta & tb)
    jaccard = inter / union if union else 0.0
    containment = inter / min(len(ta), len(tb))
    return (0.5 * jaccard + 0.5 * containment) >= float(
        settings.dedup_similarity_threshold)


def _anchors_compatible(a, b) -> bool:
    """True when two signals are not known to be distinct releases."""
    from app.analysis.calendar_merge import _has_freq_conflict

    ka = event_anchor(a.title)
    kb = event_anchor(b.title)
    # Same specific release anchor -> compatible, subject to frequency guard.
    if ka is not None and ka == kb:
        return not _has_freq_conflict(a.title, b.title)
    # One or both unanchored: compatible unless a clear frequency conflict.
    return not _has_freq_conflict(a.title, b.title)


def _collapse_same_source_repeats(signals: list) -> list:
    """
    Collapse repeat headlines from the SAME source about the SAME release
    on the SAME asset.

    A single outlet re-serving (or re-wording) one story must not cast two
    votes for one asset. Different assets each get their own signal, so a
    headline impacting gold, silver, bitcoin and equity produces four
    signals - one per card.

    Keeps the highest-weight member per distinct release; different releases
    (e.g. CPI m/m vs CPI y/y) are never merged.

    Only signals from the same source, same asset, same direction, near-duplicate
    titles and within a short re-serve window are collapsed - this never
    splits legitimate separate prints (different assets, times, directions,
    anchors or frequencies are preserved by the clustering that ran first).
    """
    RE_SERVE_WINDOW_MINUTES = 30

    by_source_asset: dict = {}
    for s in signals:
        key = (getattr(s, "source_name", "") or "", getattr(s, "asset", None))
        by_source_asset.setdefault(key, []).append(s)

    out: list = []
    now = _scoring_now()
    for members in by_source_asset.values():
        members.sort(
            key=lambda s: max(0.0, _signal_weight(s, now)), reverse=True)
        kept: list = []
        for s in members:
            is_dup = False
            for k in kept:
                # Opposite readings of one story are two readings, not one.
                if s.direction is not k.direction:
                    continue
                # Only collapse genuine same-source re-serves, not separate
                # prints hours apart.
                s_ts = s.published_at
                k_ts = k.published_at
                if s_ts is None or k_ts is None:
                    continue
                s_ts = _ensure_aware(s_ts)
                k_ts = _ensure_aware(k_ts)
                if abs((s_ts - k_ts).total_seconds()) > RE_SERVE_WINDOW_MINUTES * 60:
                    continue
                if (_titles_near_duplicate(s.title, k.title)
                        and _anchors_compatible(s, k)):
                    is_dup = True
                    break
            if is_dup:
                continue
            kept.append(s)
        out.extend(kept)
    return out


def cluster_signals(signals: list, window_hours: float = 0.5) -> list:
    """
    Cluster signals by normalized title within a time window.

    This implements near-duplicate clustering at SCORING time (not ingestion
    time). Each cluster contributes ONE signal (the highest-weight member)
    with a corroboration boost based on the number of independent sources in
    the cluster.

    Identity first: signals that ingestion dedup already filed under one
    event (same ``event_key`` / ``cluster_id``) collapse regardless of
    publish time or wording - one event is one vote for the whole trading
    day. Title similarity is the fallback for identity-less signals only.

    Args:
        signals: List of WeightedSignal objects
        window_hours: Time window for title-similarity clustering (default 0.5 hours = 30 minutes).
                      For fed_minutes/*_minutes and release:* anchors, a 90-minute (1.5h) window is used.

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
    default_window = timedelta(hours=window_hours)
    # 90-minute window for minutes and release anchors
    anchor_window = timedelta(hours=1.5)

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

        # Rule 0 (2026-10-13): the ingestion-time event identity decides
        # FIRST, with no time window. Articles that ingestion dedup filed
        # under one NewsEvent share one event_key, so their signals are ONE
        # vote here too. Without this the 30-minute title rule below
        # re-admitted every republished or reworded copy as a fresh
        # contributor - the reported "Trump: working with Russia" headline
        # counted 2-3 times, each copy adding its own weight to the bull
        # total. Mirrors equity_card._collapse_cross_asset, which already
        # collapses by event_key first.
        #
        # STEP 9.3: the group key is the event ALONE, not (event, direction).
        # Grouping by direction as well meant _collapse_members never saw the
        # disagreement it exists to resolve, so two opposite readings of one
        # event survived as two balanced votes and manufactured a conviction
        # out of an event whose direction was never established. _collapse_members
        # now owns the direction decision and flags the contradiction.
        keyed: dict[str, list[WeightedSignal]] = {}
        unkeyed: list[WeightedSignal] = []
        for s in asset_signals:
            event_id = _event_identity(s)
            if event_id:
                keyed.setdefault(event_id, []).append(s)
            else:
                unkeyed.append(s)
        for members in keyed.values():
            clustered.extend(_collapse_members(members))

        # Title-similarity fallback for identity-less signals (calendar,
        # synthetic builders): group by normalized title within the rolling
        # window, exactly as before.
        clusters: list[list[WeightedSignal]] = []
        for s in unkeyed:
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
                time_diff = abs((s_dt - rep_dt).total_seconds())

                # Determine which window to use based on anchor type
                s_key = event_anchor(s.title)
                rep_key = event_anchor(rep.title)
                # Use 90-min window for minutes and release anchors
                use_anchor_window = False
                if s_key and rep_key and s_key == rep_key:
                    if s_key.endswith("_minutes") or s_key.startswith("release:"):
                        use_anchor_window = True
                current_window = anchor_window if use_anchor_window else default_window

                if time_diff > current_window.total_seconds():
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
                # Rule 2 (event-anchored join, 2026-10-08): the SAME
                # specific event is ONE cluster even when the outlets
                # word it too differently for the 0.72 title rule to
                # fire. All three conditions must hold:
                #   * same direction - a hawkish and a dovish reading
                #     of one event are two readings, not one event;
                #   * inside the time window (checked above, exactly
                #     like the title rule - the anchor never extends
                #     the window);
                #   * the same specific event key from event_anchor().
                event_matched = False
                if s.direction == rep.direction:
                    if s_key is not None and s_key == rep_key:
                        event_matched = True
                if similarity >= 0.72 or event_matched:  # 0.72 = dedup threshold
                    cluster.append(s)
                    matched = True
                    break

            if not matched:
                clusters.append([s])

        # Each finished title cluster keeps one vote per direction (see
        # _collapse_members): the heaviest member survives, carrying the
        # capped corroboration boost for its independent sources.
        for cluster in clusters:
            clustered.extend(_collapse_members(cluster))

    # Same-source repeat collapse: one outlet, one vote per release. Catches
    # identical/same-release BLS (or any single-source) re-serves that ingestion
    # dedup filed under different event_keys so they are counted once.
    clustered = _collapse_same_source_repeats(clustered)

    return clustered


def _scoring_now():
    """
    The "now" the sentiment engine will use when it scores these signals.

    ``cluster_signals`` runs inside ``_recompute_sentiment`` (pipeline.py:724)
    which does NOT pass a ``now``. The engine therefore falls back to the
    wall clock via ``sentiment_engine.compute -> _aggregate(now=None) ->
    decay_weight(published_at, tier, now=None) -> _age_hours -> age_hours``.
    Matching that here keeps the representative selection on exactly the same
    decay curve the aggregate will use, so "highest weight" means highest
    contribution.
    """
    from app.utils import timeutils as T

    return T.now_utc()


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