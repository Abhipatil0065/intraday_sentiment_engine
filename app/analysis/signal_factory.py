"""
SIGNAL FACTORY (phase 1).

The production path used to build ``WeightedSignal`` objects inline in
``pipeline._recompute_sentiment``. Two of the fields that matter most to the
weight - ``category`` (which selects the decay half-life) and ``corroboration``
(which rewards multi-source confirmation) - were never passed, so both silently
fell back to their dataclass defaults:

    category       -> NewsCategory.OTHER  -> the 6h default half-life, for
                       EVERY article, so a 72h crypto ruling decayed exactly
                       as fast as a 3h CPI print.
    corroboration  -> 1.0, always, so a story confirmed by five independent
                       outlets carried the same weight as an unconfirmed one.

This module builds those signals correctly, in one reviewable place, so the
harness and the pipeline cannot drift apart again. It adds no new field and
changes no existing name: it only fills in fields that already existed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.analysis.event_clustering import corroboration_multiplier
from app.config import settings
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
)


def resolve_corroboration(independent_sources: int) -> float:
    """
    Capped corroboration boost for a cluster, or 1.0 when the feature is off.

    Gated by ``corroboration_enabled`` so the whole phase can be disabled from
    config without touching this call site again.
    """
    if not settings.corroboration_enabled:
        return 1.0
    return corroboration_multiplier(int(independent_sources or 1))


def build_signal(
    *,
    asset: Asset,
    title: str,
    direction: Direction,
    strength: ImpactStrength = ImpactStrength.MEDIUM,
    importance: EconomicImportance = EconomicImportance.MEDIUM,
    published_at: Optional[datetime] = None,
    relevance: float = 0.0,
    persistence: float = 0.5,
    source_quality: str = "TIER_2_MAJOR",
    source_name: str = "",
    category: NewsCategory = NewsCategory.OTHER,
    session_multiplier: float = 1.0,
    is_primary: bool = True,
    event_key: str = "",
    cluster_id: str = "",
    independent_sources: int = 1,
    report_kind: ReportKind = ReportKind.GENERAL,
    reason: str = "",
    transmission: str = "",
    mode: str = "heuristic",
) -> "object":
    """
    Build one correctly-populated ``WeightedSignal``.

    ``cluster_id`` defaults to ``event_key`` when omitted: in production the
    event key IS the cluster identity, and passing it is what makes
    ``contributing_clusters`` a real count instead of always zero.
    """
    from app.analysis.sentiment_engine import WeightedSignal

    resolved_cluster = cluster_id or event_key or ""

    return WeightedSignal(
        asset=asset,
        title=title,
        direction=direction,
        strength=strength,
        importance=importance,
        published_at=published_at,
        relevance=relevance,
        persistence=persistence,
        source_quality=source_quality,
        # --- the two fields the inline builder forgot ---
        category=category,
        cluster_id=resolved_cluster,
        corroboration=resolve_corroboration(independent_sources),
        # -------------------------------------------------
        session_multiplier=session_multiplier,
        is_primary=is_primary,
        event_key=event_key,
        reason=reason,
        transmission=transmission,
        report_kind=report_kind,
        mode=mode,
    )