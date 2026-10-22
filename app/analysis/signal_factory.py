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
    published_at: datetime | None = None,
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
    surprise_weight: float = 1.0,
    metal_character: str | None = None,
    speaker_tier: float = 1.0,
    age_reference_utc: datetime | None = None,
    chart_scope: str = "",
) -> object:
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
        # Task 3: surprise size of an ACTUAL scheduled-data print (1.0 for
        # every other signal).
        surprise_weight=surprise_weight,
        # Section 7: silver's dual-nature character, carried through so the
        # weight path can reward a characterised silver story.
        metal_character=metal_character,
        # Central bank speaker tier multiplier.
        speaker_tier=speaker_tier,
        # Overnight admission: age an off-window headline from the moment the
        # engine judged it, not from the publication the laptop slept through.
        age_reference_utc=age_reference_utc,
        # The chart that listed this story ("" for a story with no chart).
        chart_scope=chart_scope,
    )


# ---------------------------------------------------------------------------
# IMPROVEMENT 5: Gold and Silver cross-asset bleed.
# ---------------------------------------------------------------------------
# A high-relevance, high-strength central-bank / rates / inflation / USD /
# treasury / banking-stress story moves BOTH precious metals in the same
# direction. The primary signal is built for the article's own asset; this
# helper builds a secondary, lower-weight copy for the PAIR so the bleed
# reaches the aggregator without the article having to be classified twice.
# The copy is deliberately down-weighted (settings.gold_silver_bleed on BOTH
# relevance AND persistence) so it never outranks a direct XAGUSD story about
# silver itself, and it is flagged is_primary=False so the dashboard can tell
# a bleed from a real silver headline. Bitcoin and the equity proxy are
# excluded: they are not in BLEED_PAIR, so build_bleed_signal() returns None
# for them automatically.
#
# THE BLEED FACTOR IS A CONFIG KNOB NOW. It used to be the literal 0.40
# written into the two multiplications below, while settings.gold_silver_bleed
# sat in config.py read by nothing - so "tune the bleed" changed nothing. The
# value is read here, once, at call time.

BLEED_CATEGORIES = {
    NewsCategory.CENTRAL_BANK,
    NewsCategory.INTEREST_RATES,
    NewsCategory.INFLATION,
    NewsCategory.USD_DXY,
    NewsCategory.TREASURY_YIELDS,
    NewsCategory.BANKING_FINANCIAL_STRESS,
}

BLEED_PAIR = {
    Asset.GOLD: Asset.SILVER,
    Asset.SILVER: Asset.GOLD,
}


def bleed_factor() -> float:
    """The configured cross-asset bleed weight (settings.gold_silver_bleed)."""
    return float(settings.gold_silver_bleed)


def build_bleed_signal(primary: WeightedSignal) -> WeightedSignal | None:
    """
    Build a secondary cross-asset bleed signal for the precious-metal pair.

    Returns None when the primary signal is not eligible (wrong asset, wrong
    category, below the relevance floor, or below HIGH strength) - the caller
    can unconditionally append the result and let the None do the filtering.

    CHART-SCOPED STORIES NEVER BLEED. A story listed on the gold chart is the
    gold card's news; copying it onto the silver card would put gold chart
    news on the silver card, which is exactly what the chart scope exists to
    prevent. A story listed on BOTH charts already reaches both cards on its
    own (its own impact rows exist for both), so it loses nothing here.
    """
    if primary.chart_scope:
        return None
    if primary.asset not in BLEED_PAIR:
        return None
    if primary.category not in BLEED_CATEGORIES:
        return None
    if primary.relevance < 0.70:
        return None
    if primary.strength not in (ImpactStrength.HIGH,
                                ImpactStrength.VERY_HIGH):
        return None

    target_asset = BLEED_PAIR[primary.asset]
    bleed = bleed_factor()
    # Build the bleed through build_signal(), never by constructing
    # WeightedSignal directly: that class is imported lazily inside
    # build_signal and is not in this module's namespace. Instantiating it
    # here raised NameError on every eligible signal and killed the whole
    # sentiment recompute (and therefore the tick).
    return build_signal(
        asset=target_asset,
        title=primary.title,
        direction=primary.direction,
        strength=primary.strength,
        importance=primary.importance,
        published_at=primary.published_at,
        relevance=primary.relevance * bleed,
        persistence=primary.persistence * bleed,
        source_quality=primary.source_quality,
        category=primary.category,
        session_multiplier=primary.session_multiplier,
        is_primary=False,
        event_key=primary.event_key,
        cluster_id=primary.cluster_id,
        report_kind=primary.report_kind,
        reason=primary.reason,
        transmission=primary.transmission,
        mode=primary.mode,
        surprise_weight=primary.surprise_weight,
        metal_character=primary.metal_character,
        # The bleed must age with its primary: an overnight macro headline
        # admitted this morning bleeds to the pair with the same freshness.
        age_reference_utc=primary.age_reference_utc,
        # A bleed copy is only ever built for an unscoped story, so the scope
        # travels as empty - but it is carried explicitly so the field can
        # never be lost by a future change.
        chart_scope=primary.chart_scope,
    )