"""
SENTIMENT AGGREGATOR (items 2 + 6 + 7 + 10).

WHAT THIS REPLACES
------------------
The old engine ended with:

    label = Direction.BULLISH if net >= 0 else Direction.BEARISH

That forced a directional answer out of EVERY input, including an empty one,
and the forced 100% bull/bear split made a coin-flip look confident.

THE NEW CONTRACT
----------------
    score      -100 .. +100   signed, continuous, honest about magnitude
    confidence   0 .. 1       how much the evidence actually supports it
    bias        BULLISH | BEARISH | NO_BIAS

NO_BIAS is a first-class answer, returned whenever confidence OR abs(score)
falls under a configurable floor. The engine is allowed to say "I do not know",
which is the most important thing a day-trading sentiment tool can do.

FINAL WEIGHT FORMULA (item 6 - one magnitude, one event weight)
-------------------------------------------------------------
    magnitude    = ONE measured cue score + ONE event-type weight
                   (previously counted three times: strength, persistence and
                   importance were all derived from the same cue total)

    decay        = 0.5 ** (age_hours / half_life(category))      <- item 7

    weight       = decay
                 x relevance          (already independent of the cue score)
                 x persistence        (derived from EVENT TYPE, not the cue
                                      total, so it is not a second copy of
                                      magnitude)
                 x source_quality
                 x session_multiplier
                 x corroboration      (one cluster = one vote, item 4)

    contribution = weight x strength_multiplier x direction_sign

    score        = 100 * (bull - bear) / (bull + bear)        -> -100..+100

    confidence   = f(conviction, breadth, agreement, certainty), gated so a
                   thin or conflicted read can never report high confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Sequence

from app.logging_conf import get_logger
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
)

log = get_logger(__name__)


class Bias(str, Enum):
    """The three answers the engine may give. NO_BIAS is a real answer."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NO_BIAS = "NO_BIAS"


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


@dataclass
class WeightedSignal:
    """One event's contribution to one asset."""

    asset: Asset
    title: str
    direction: Direction
    strength: ImpactStrength = ImpactStrength.MEDIUM
    importance: EconomicImportance = EconomicImportance.MEDIUM
    published_at: Optional[datetime] = None
    relevance: float = 0.0
    persistence: float = 0.5
    source_quality: str = "TIER_2_MAJOR"
    session_multiplier: float = 1.0
    is_primary: bool = True
    event_key: str = ""
    cluster_id: str = ""
    corroboration: float = 1.0
    category: NewsCategory = NewsCategory.OTHER
    reason: str = ""
    transmission: str = ""
    report_kind: ReportKind = ReportKind.GENERAL
    mode: str = "heuristic"

    # filled by compute()
    recency: float = 0.0
    final_weight: float = 0.0
    magnitude: float = 0.0
    contribution: float = 0.0


@dataclass
class Driver:
    """One headline's contribution, for the manual trader's audit trail."""

    title: str
    contribution: float
    direction: str
    weight: float


@dataclass
class SentimentResult:
    """One asset's verdict. Never forces a direction."""

    asset: Asset
    score: float = 0.0
    confidence: float = 0.0
    bias: Bias = Bias.NO_BIAS
    mode: str = "heuristic"
    drivers: list[Driver] = field(default_factory=list)
    conflict: Optional[Driver] = None
    # Both sides, each capped at 3, for the dashboard's side-by-side boxes.
    # Additive only: `drivers` above still carries the single leading side and
    # `conflict` the single strongest opposing headline, so the decision log
    # and hit-rate tooling are unaffected.
    bull_drivers: list[Driver] = field(default_factory=list)
    bear_drivers: list[Driver] = field(default_factory=list)
    delta_60m: float = 0.0
    event_risk: bool = False
    event_risk_note: str = ""
    contributing_articles: int = 0
    contributing_clusters: int = 0
    note: str = ""


def decay_weight(published_at: Optional[datetime], category: NewsCategory,
                 now: Optional[datetime] = None) -> float:
    """
    Exponential decay with a per-category half-life (item 7).

    Replaces the single fixed recency ladder. A CPI surprise matters for
    hours; an ETF flow or a regulatory ruling stays relevant for a day or more.
    ``weight = 0.5 ** (age_hours / half_life)`` - smooth, and a category's
    half-life is a single config value.
    """
    from app.config import settings

    half_lives = settings.half_lives
    # parsed_table() lowercases its keys, so the lookup key must match that.
    key = (category.value if category else "").strip().lower()
    half_life = half_lives.get(key, settings.decay_default_hours)
    half_life = max(0.25, float(half_life))

    if published_at is None:
        return 0.15
    hours = _age_hours(published_at, now)
    if hours < 0:
        return 1.0  # within clock skew: treat as brand new
    return max(0.0, min(1.0, 0.5 ** (hours / half_life)))


def _age_hours(published_at: datetime, now: Optional[datetime] = None) -> float:
    from app.utils import timeutils as T
    return T.age_hours(published_at, now=now) or 0.0


def _signal_weight(s: WeightedSignal, now: Optional[datetime]) -> float:
    """
    The single final weight for one signal.

    Multipliers are independent by construction: relevance from the
    (category x asset) matrix, persistence from the event type, quality from
    the source tier, decay from the category half-life. The cue magnitude
    enters exactly once, through STRENGTH_MULT at the end.
    """
    decay = decay_weight(s.published_at, s.category, now)
    rel = max(0.0, min(1.0, s.relevance))
    per = max(0.05, min(1.0, s.persistence))
    qual = QUALITY_MULT.get((s.source_quality or "").upper(), 0.82)
    sess = max(0.5, min(1.5, s.session_multiplier))
    corr = max(1.0, float(s.corroboration or 1.0))
    return decay * rel * per * qual * sess * corr


def _mode_for(signals: Sequence[WeightedSignal]) -> str:
    modes = {s.mode for s in signals if s.mode}
    if not modes:
        return "heuristic"
    if len(modes) == 1:
        return modes.pop()
    return "mixed"


def _aggregate(signals: Sequence[WeightedSignal],
               now: Optional[datetime] = None) -> dict:
    """Shared weighted-sum core. Returns raw totals and per-side items."""
    bull = bear = directionless = 0.0
    bull_items: list[tuple[float, WeightedSignal]] = []
    bear_items: list[tuple[float, WeightedSignal]] = []
    used = 0
    clusters: set[str] = set()

    for s in signals:
        weight = _signal_weight(s, now)
        s.recency = weight
        s.final_weight = weight
        if weight <= 0:
            continue

        magnitude = weight * STRENGTH_MULT.get(s.strength, 0.45)
        s.magnitude = magnitude
        if s.direction is Direction.BULLISH:
            sign = 1.0
        elif s.direction is Direction.BEARISH:
            sign = -1.0
        else:
            sign = 0.0
        s.contribution = sign * magnitude

        if sign > 0:
            bull += magnitude
            bull_items.append((magnitude, s))
        elif sign < 0:
            bear += magnitude
            bear_items.append((magnitude, s))
        else:
            # An item that takes no side is never forced onto one. It counts
            # against conviction instead; MIXED counts double.
            directionless += magnitude * (1.4 if s.direction is Direction.MIXED
                                          else 1.0)
        if s.cluster_id:
            clusters.add(s.cluster_id)
        used += 1

    return {"bull": bull, "bear": bear, "directionless": directionless,
            "bull_items": bull_items, "bear_items": bear_items,
            "used": used, "clusters": len(clusters)}
    conflict: Optional[Driver] = None
    delta_60m: float = 0.0
    event_risk: bool = False
    event_risk_note: str = ""
def _score_from(bull: float, bear: float) -> float:
    """Map the two weighted totals onto a signed -100..+100 score."""
    total = bull + bear
    if total <= 0:
        return 0.0
    return round(100.0 * (bull - bear) / total, 1)


def _confidence_from(bull: float, bear: float, directionless: float,
                     used: int) -> float:
    """
    How much the evidence supports the reading.

    Blends conviction (how one-sided), breadth (how many distinct events),
    agreement (how much the two sides fight) and certainty (how much of the
    input was directional at all), then clamps.
    """
    directional = bull + bear
    if directional <= 0:
        return 0.0

    net = abs(bull - bear) / directional
    conviction = min(1.0, net / 0.6)
    breadth = min(1.0, used / 8.0)
    agreement = 1.0 - (2.0 * min(bull, bear) / directional)
    total_flow = directional + directionless
    certainty = directional / total_flow if total_flow > 0 else 0.0

    confidence = (0.40 * conviction + 0.28 * breadth + 0.22 * agreement) * certainty
    return round(max(0.0, min(0.95, confidence)), 3)


def _decide(score: float, confidence: float) -> Bias:
    """
    The NO_BIAS gate - the single most important rule in this module.

    A direction is published only when BOTH the score clears its floor AND the
    confidence clears its floor. Either failure returns NO_BIAS.
    """
    from app.config import settings

    if confidence < float(settings.bias_min_confidence):
        return Bias.NO_BIAS
    if abs(score) < float(settings.bias_min_abs_score):
        return Bias.NO_BIAS
    return Bias.BULLISH if score > 0 else Bias.BEARISH


def _top_drivers(items: Sequence[tuple[float, WeightedSignal]],
                 limit: int = 3) -> list[Driver]:
    """Top N drivers, strongest first, each with its signed contribution."""
    out = [Driver(title=s.title, contribution=round(magnitude, 4),
                  direction=s.direction.value, weight=round(s.final_weight, 4))
           for magnitude, s in items]
    out.sort(key=lambda d: abs(d.contribution), reverse=True)
    return out[:limit]


def _strongest_conflict(bull_items: Sequence[tuple[float, WeightedSignal]],
                        bear_items: Sequence[tuple[float, WeightedSignal]]) -> Optional[Driver]:
    """The single opposing headline the trader most needs to know about."""
    if not bull_items or not bear_items:
        return None
    # Whichever side is currently losing, name its biggest driver. Compare on
    # magnitude only - a tie must fall back to the signal, never to the tuple
    # comparison, which would try to order the dataclasses themselves.
    if bull_items[0][0] <= bear_items[0][0]:
        candidates = bull_items
    else:
        candidates = bear_items
    magnitude, s = max(candidates, key=lambda x: x[0])
    return Driver(title=s.title, contribution=round(-magnitude, 4),
                  direction=s.direction.value, weight=round(s.final_weight, 4))


def _delta_60m(signals: Sequence[WeightedSignal], asset: Asset,
               now: Optional[datetime]) -> float:
    """
    How much the score MOVED because of the last hour of news (item 10).

    The score an hour ago is the reading computed from every signal EXCEPT the
    ones published inside the recent window. The delta is the difference, so a
    trader can see at a glance whether the current bias was just made or has
    been standing all session.
    """
    from app.config import settings

    if now is None:
        return 0.0
    cutoff = now - timedelta(minutes=int(settings.delta_window_minutes))
    older = [s for s in signals
             if s.asset is asset and not (s.published_at is not None
                                         and s.published_at >= cutoff)]

    prior = _aggregate(older, now)
    prior_score = _score_from(prior["bull"], prior["bear"])
    current = _aggregate([s for s in signals if s.asset is asset], now)
    current_score = _score_from(current["bull"], current["bear"])
    if prior["used"] == 0:
        # Nothing outside the window means there is no earlier reading at all:
        # the whole current score was made in the last hour.
        return current_score
    return round(current_score - prior_score, 1)
    contributing_articles: int = 0
    contributing_clusters: int = 0
    note: str = ""
def compute(signals: Sequence[WeightedSignal], asset: Asset,
            now: Optional[datetime] = None,
            event_risk: bool = False,
            event_risk_note: str = "") -> SentimentResult:
    """
    Aggregate every weighted signal for one asset into a verdict.

    Always returns a bias, but the bias is allowed to be NO_BIAS. When
    confidence or |score| is under its configured floor the engine says so
    rather than naming a side it cannot support.
    """
    relevant = [s for s in signals if s.asset is asset]
    mode = _mode_for(relevant)

    if not relevant:
        return SentimentResult(
            asset=asset, score=0.0, confidence=0.0, bias=Bias.NO_BIAS,
            mode=mode, contributing_articles=0, contributing_clusters=0,
            event_risk=event_risk, event_risk_note=event_risk_note,
            note="No news reached this asset inside the rolling window.",
        )

    agg = _aggregate(relevant, now)
    bull, bear = agg["bull"], agg["bear"]
    bull_items, bear_items = agg["bull_items"], agg["bear_items"]

    score = _score_from(bull, bear)
    confidence = _confidence_from(bull, bear, agg["directionless"], agg["used"])
    bias = _decide(score, confidence)

    # Drivers are reported on the side the verdict names, ordered by absolute
    # contribution so the biggest mover is first.
    leading = bull_items if score >= 0 else bear_items
    drivers = _top_drivers(leading)
    conflict = _strongest_conflict(bull_items, bear_items)
    # Both sides kept separately for the dashboard, which shows a bullish box
    # and a bearish box side by side rather than only the leading side.
    bull_drivers = _top_drivers(bull_items)
    bear_drivers = _top_drivers(bear_items)

    note = ""
    if bias is Bias.NO_BIAS:
        if bull + bear <= 0:
            note = "Nothing in the window took a directional side."
        else:
            note = ("Evidence is too thin or too conflicted to publish a "
                    "direction; showing drivers only.")

    return SentimentResult(
        asset=asset,
        score=score,
        confidence=confidence,
        bias=bias,
        mode=mode,
        drivers=drivers,
        conflict=conflict,
        bull_drivers=bull_drivers,
        bear_drivers=bear_drivers,
        delta_60m=_delta_60m(list(signals), asset, now),
        event_risk=event_risk,
        event_risk_note=event_risk_note,
        contributing_articles=agg["used"],
        contributing_clusters=agg["clusters"],
        note=note,
    )


def score_from_signals(signals: Sequence[WeightedSignal], asset: Asset,
                       now: Optional[datetime] = None) -> float:
    """Convenience wrapper returning only the signed score."""
    return compute(signals, asset, now=now).score