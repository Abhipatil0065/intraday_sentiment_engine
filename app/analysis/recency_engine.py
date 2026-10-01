"""
RECENCY ENGINE (section 4).

Recency alone must NOT decide importance - a major central-bank announcement
from hours ago can outweigh a minor headline from five minutes ago. This
module only produces the *recency* factor; it is multiplied by economic
importance, asset relevance and expected persistence downstream.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.logging_conf import get_logger
from app.models.enums import Direction, EconomicImportance, ImpactStrength
from app.utils import timeutils as T

log = get_logger(__name__)

# Buckets from the specification (upper bound minutes -> weight).
RECENCY_BUCKETS: list[tuple[float, float]] = [
    (15, 1.00),
    (30, 0.90),
    (60, 0.80),
    (120, 0.65),
    (240, 0.50),
    (480, 0.30),
]
RECENCY_FLOOR = 0.15  # 8h+


def recency_weight(published_at: Optional[datetime], *, now: Optional[datetime] = None) -> float:
    """0.15 - 1.00 based on age. Unparseable timestamps get the floor."""
    hours = T.age_hours(published_at, now=now)
    if hours is None:
        return RECENCY_FLOOR
    if hours < 0:
        return 1.0  # within clock skew; treat as brand new

    minutes = hours * 60.0
    for upper, weight in RECENCY_BUCKETS:
        if minutes <= upper:
            return weight
    return RECENCY_FLOOR


# Section 5: economic importance multipliers.
IMPORTANCE_WEIGHT: dict[EconomicImportance, float] = {
    EconomicImportance.VERY_HIGH: 1.00,
    EconomicImportance.HIGH: 0.70,
    EconomicImportance.MEDIUM: 0.42,
    EconomicImportance.LOW: 0.18,
}

# Section 5: impact strength multipliers.
STRENGTH_WEIGHT: dict[ImpactStrength, float] = {
    ImpactStrength.VERY_HIGH: 1.00,
    ImpactStrength.HIGH: 0.72,
    ImpactStrength.MEDIUM: 0.45,
    ImpactStrength.LOW: 0.20,
}

# Magnitude of directional contribution by strength. Items that take no side
# (neutral/mixed) are never given a direction - they only reduce confidence.
NEUTRAL_DAMPING = 0.45


def directional_sign(direction: Direction) -> float:
    if direction is Direction.BULLISH:
        return 1.0
    if direction is Direction.BEARISH:
        return -1.0
    return 0.0


def compute_final_weight(
    *,
    published_at: Optional[datetime],
    importance: EconomicImportance,
    strength: ImpactStrength,
    asset_relevance: float,
    persistence: float,
    source_quality: str = "TIER_2_MAJOR",
    session_multiplier: float = 1.0,
    now: Optional[datetime] = None,
) -> dict[str, float]:
    """
    FINAL NEWS WEIGHT = Recency x Importance x Relevance x Persistence
                         x SourceQuality x SessionRelevance

    Returns the individual factors plus the product, all for transparency in
    the dashboard (section 3 requires showing recency/economic-importance/
    asset-relevance per item).
    """
    from app.ingestion.news_fetcher import quality_multiplier

    rec = recency_weight(published_at, now=now)
    imp = IMPORTANCE_WEIGHT.get(importance, 0.42)
    rel = max(0.0, min(1.0, asset_relevance))
    per = max(0.05, min(1.0, persistence))
    qual = quality_multiplier(source_quality)
    sess = max(0.5, min(1.5, session_multiplier))

    final = rec * imp * rel * per * qual * sess
    return {
        "recency": rec,
        "economic_importance": imp,
        "asset_relevance": rel,
        "persistence": per,
        "source_quality": qual,
        "session_relevance": sess,
        "final_weight": final,
    }


def signed_contribution(
    *,
    direction: Direction,
    strength: ImpactStrength,
    final_weight: float,
    session_progress: float = 1.0,
) -> float:
    """
    Signed contribution to an asset's bull/bear score.

    Very-high-strength news keeps influence across the whole session; medium
    strength fades as the session progresses (section 17 decay).
    """
    mag = STRENGTH_WEIGHT.get(strength, 0.45) * final_weight
    sign = directional_sign(direction)
    if sign == 0.0:
        return 0.0
    # Fade factor: high-strength signals persist longer intraday.
    fade = (1.0 - session_progress * (1.0 - mag)) if mag < 0.8 else 1.0
    return sign * mag * final_weight * 2.0 * max(0.25, fade)


def neutral_contribution(final_weight: float, strength: ImpactStrength) -> float:
    return NEUTRAL_DAMPING * STRENGTH_WEIGHT.get(strength, 0.45) * final_weight * 2.0