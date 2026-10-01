"""
SENTIMENT AGGREGATOR (sections 10, 11).

Converts weighted per-asset contributions into a strictly BINARY verdict:
BULLISH or BEARISH. There are only two views and the split always sums to
100%.

Uncertainty is expressed through ``confidence`` and the ``conflicts`` list,
never through a third "neutral" state. When the two sides are close the
verdict still names the leading side, but confidence falls and the opposing
drivers are listed, so a weak call is visibly weak rather than hidden behind
a neutral label.

Note: individual news items and calendar events may still carry NEUTRAL or
MIXED directions. That is an input-level statement ("this item does not take
a side") and is deliberately NOT forced into a fake bullish/bearish lean.
Only the final verdict is binary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from app.analysis.recency_engine import recency_weight
from app.logging_conf import get_logger
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    ReportKind,
)

log = get_logger(__name__)

IMPORTANCE_MULT = {
    EconomicImportance.VERY_HIGH: 1.00,
    EconomicImportance.HIGH: 0.70,
    EconomicImportance.MEDIUM: 0.42,
    EconomicImportance.LOW: 0.18,
}
STRENGTH_MULT = {
    ImpactStrength.VERY_HIGH: 1.00,
    ImpactStrength.HIGH: 0.72,
    ImpactStrength.MEDIUM: 0.45,
    ImpactStrength.LOW: 0.20,
}
QUALITY_MULT = {
    "TIER_1_OFFICIAL": 1.15,
    "TIER_2_MAJOR": 1.00,
    "TIER_3_CRYPTO": 0.92,
    "TIER_4_OTHER": 0.82,
}
# Repeat coverage of an already-counted event is damped, not dropped.
REPEAT_DAMPING = 0.25


@dataclass
class WeightedSignal:
    """One news item's contribution to one asset."""

    asset: Asset
    title: str
    direction: Direction
    strength: ImpactStrength
    importance: EconomicImportance
    published_at: Optional[datetime]
    relevance: float = 0.0
    persistence: float = 0.5
    source_quality: str = "TIER_2_MAJOR"
    session_multiplier: float = 1.0
    is_primary: bool = True  # False when merged into an existing event
    event_key: str = ""
    reason: str = ""
    transmission: str = ""
    report_kind: ReportKind = ReportKind.GENERAL

    # filled by compute()
    recency: float = 0.0
    final_weight: float = 0.0
    magnitude: float = 0.0


@dataclass
class SentimentResult:
    asset: Asset
    bull_pct: float
    bear_pct: float
    label: Direction
    net_bias: float
    confidence: float
    conflicts: list[dict] = field(default_factory=list)
    drivers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    contributing_articles: int = 0


def _normalise(bull: float, bear: float) -> tuple[float, float]:
    """
    Force an exact 100% two-way split between BULLISH and BEARISH.

    The bearish side is derived from the ALREADY-ROUNDED bullish value so the
    two published percentages always sum to exactly 100.0.
    """
    total = bull + bear
    if total <= 0:
        return 50.0, 50.0
    b = round(bull / total * 100.0, 1)
    return b, round(100.0 - b, 1)

def compute(signals: Sequence[WeightedSignal], asset: Asset) -> SentimentResult:
    """
    Aggregate every weighted signal for one asset into a binary verdict.

    Exactly one of BULLISH / BEARISH is returned. Confidence carries the
    "how sure" signal instead of a neutral label.
    """
    relevant = [s for s in signals if s.asset is asset]
    if not relevant:
        return SentimentResult(
            asset=asset, bull_pct=50.0, bear_pct=50.0,
            label=Direction.BULLISH, net_bias=0.0, confidence=0.0,
            contributing_articles=0,
        )

    bull = bear = directionless = 0.0
    used = 0
    bull_items: list[tuple[float, WeightedSignal]] = []
    bear_items: list[tuple[float, WeightedSignal]] = []

    for s in relevant:
        rec = recency_weight(s.published_at)
        imp = IMPORTANCE_MULT.get(s.importance, 0.42)
        rel = max(0.0, min(1.0, s.relevance))
        per = max(0.05, min(1.0, s.persistence))
        qual = QUALITY_MULT.get((s.source_quality or "").upper(), 0.82)
        sess = max(0.5, min(1.5, s.session_multiplier))

        weight = rec * imp * rel * per * qual * sess
        if not s.is_primary:
            weight *= REPEAT_DAMPING

        s.recency = rec
        s.final_weight = weight
        if weight <= 0:
            continue

        magnitude = weight * STRENGTH_MULT.get(s.strength, 0.45)
        s.magnitude = magnitude

        if s.direction is Direction.BULLISH:
            bull += magnitude
            bull_items.append((magnitude, s))
        elif s.direction is Direction.BEARISH:
            bear += magnitude
            bear_items.append((magnitude, s))
        else:
            # An item that takes no side is never forced onto one. It is
            # counted against conviction instead; MIXED items are explicitly
            # conflicted, so they count double.
            directionless += magnitude * (1.4 if s.direction is Direction.MIXED else 1.0)
        used += 1

    # ---- conflict detection (section 10) ----
    conflicts: list[dict] = []
    if bull > 0 and bear > 0:
        top_bull = max(bull_items, key=lambda x: x[0])
        top_bear = max(bear_items, key=lambda x: x[0])
        if bull > bear:
            resolution = "Bullish side currently has more weight."
        elif bear > bull:
            resolution = "Bearish side currently has more weight."
        else:
            resolution = "Both sides carry similar weight."
        conflicts.append({
            "bullish_driver": top_bull[1].title[:160],
            "bullish_weight": round(top_bull[0], 3),
            "bearish_driver": top_bear[1].title[:160],
            "bearish_weight": round(top_bear[0], 3),
            "resolution": resolution,
        })

    drivers = [s.title for _, s in sorted(bull_items, key=lambda x: -x[0])[:3]]
    risks = [s.title for _, s in sorted(bear_items, key=lambda x: -x[0])[:3]]

    directional = bull + bear
    if directional <= 0:
        # Nothing took a side at all. Report an even split with zero
        # confidence rather than inventing a direction.
        return SentimentResult(
            asset=asset, bull_pct=50.0, bear_pct=50.0,
            label=Direction.BULLISH, net_bias=0.0, confidence=0.0,
            drivers=drivers, risks=risks,
            contributing_articles=used,
        )

    b_pct, s_pct = _normalise(bull, bear)
    net = b_pct - s_pct

    # ---- binary verdict: BULLISH or BEARISH, never anything else ----
    label = Direction.BULLISH if net >= 0 else Direction.BEARISH

    # ---- confidence carries the uncertainty the neutral label used to ----
    conviction = min(1.0, abs(net) / 60.0)
    breadth = min(1.0, used / 8.0)
    agreement = 1.0 - (2.0 * min(bull, bear) / directional)
    total_flow = directional + directionless
    certainty = directional / total_flow if total_flow > 0 else 0.0

    confidence = round(
        max(0.05, min(0.95, (0.40 * conviction + 0.28 * breadth
                             + 0.22 * agreement) * certainty)),
        3,
    )

    return SentimentResult(
        asset=asset, bull_pct=b_pct, bear_pct=s_pct, label=label,
        net_bias=round(net, 1), confidence=confidence,
        conflicts=conflicts, drivers=drivers, risks=risks,
        contributing_articles=used,
    )
