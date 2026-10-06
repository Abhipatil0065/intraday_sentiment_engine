"""SESSION-AWARE GLOBAL EQUITY CARD (one card, NOW-only, never persisted).

Takes equity-relevant news signals for today, filters them to the active
session's region via EQUITY_SESSION_MAP keywords (index names, regional
central banks, regional macro/earnings terms), and scores them with the
SAME sentiment_engine.compute() contract the metal cards use - BINARY BIAS:
the label is the sign of the net news score, never NO_BIAS / NEUTRAL.

Zero regional matches -> 50/50 payload (no_data=True) that CARRIES FORWARD
the last bias published for that region (stale=True), falling back to the
newest article's direction and then DEFAULT_BIAS - never an error, never a
withheld verdict. State is keyed per region ("EQUITY:<region>") so a session
switch cannot reuse another region's history. No DB rows are written; the
pipeline keeps the result in memory and main.py serves it inside
/api/dashboard.

Task 7: equity card has its OWN relevance table (EQUITY_RELEVANCE in config),
separate from BITCOIN's slot. Inputs: EQUITIES + central bank + macro data +
earnings + geopolitical + banking stress + risk sentiment categories,
filtered by active region keywords. Direction: hawkish policy, hot inflation,
rising yields, escalation -> bearish; cuts, easing, earnings beats, strong
growth data -> bullish. Surprise, tier, decay and repeat rules from earlier
tasks apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from app.analysis.sentiment_engine import (
    DEFAULT_BIAS,
    Bias,
    WeightedSignal,
    compute as compute_sentiment,
    payload_score,
    recall_bias,
)
from app.analysis.release_surprise import (
    combine_prints_for_asset,
    parse_all,
    equity_direction,
)
from app.logging_conf import get_logger
from app.market.session_detector import (
    EQUITY_SESSION_MAP,
    SessionName,
    equity_keywords,
    equity_session,
    equity_title,
)
from app.models.enums import Asset, Direction, NewsCategory, ReportKind
from app.config import EQUITY_RELEVANCE
from app.utils import timeutils as T

log = get_logger(__name__)


@dataclass
class EquityCardResult:
    """In-memory verdict for the one global equity card (always directional)."""

    region: str
    title: str
    symbol: str
    score: float = 0.0
    # The pre-clamp net score, for logging only: `score` is what the payload
    # shows, so a tie-break can never contradict its badge.
    raw_score: float = 0.0
    confidence: float = 0.0
    bias: str = Bias.BULLISH.value
    mode: str = "heuristic"
    drivers: list = field(default_factory=list)
    contributing_articles: int = 0
    no_data: bool = True
    # True when the region had no signals and the bias was carried forward
    # from region history (or the newest article / DEFAULT_BIAS).
    stale: bool = False
    note: str = ""
    last_updated: str = ""


def _region_of(session: SessionName) -> str:
    if session in (SessionName.ASIA, SessionName.EUROPE, SessionName.US):
        return session.value
    return "US"


def _state_key(region: str) -> str:
    """Carry-forward namespace for one region (mirrors the frontend flip key)."""
    return f"EQUITY:{region}"


def _matches_region(text: str, keywords: Sequence[str]) -> bool:
    lowered = (text or "").lower()
    return any(k and k in lowered for k in keywords)


# Categories the equity card listens to (Task 7).
EQUITY_CATEGORIES = {
    NewsCategory.EQUITIES,
    NewsCategory.CENTRAL_BANK,
    NewsCategory.INTEREST_RATES,
    NewsCategory.INFLATION,
    NewsCategory.EMPLOYMENT,
    NewsCategory.GDP,
    NewsCategory.TREASURY_YIELDS,
    NewsCategory.USD_DXY,
    NewsCategory.GEOPOLITICS,
    NewsCategory.RISK_SENTIMENT,
    NewsCategory.BANKING_FINANCIAL_STRESS,
}


def _equity_relevance(category: NewsCategory) -> float:
    """Equity card's own relevance table (config.EQUITY_RELEVANCE)."""
    return EQUITY_RELEVANCE.get(category.value, 0.10)


def region_signals(signals: Sequence[WeightedSignal],
                   session: SessionName) -> list[WeightedSignal]:
    """Equity-relevant signals whose headline matches this region's keywords."""
    keywords = equity_keywords(session)
    if not keywords:
        return []
    out = []
    for s in signals:
        if s.category not in EQUITY_CATEGORIES:
            continue
        if _matches_region(s.title, keywords):
            # Apply equity card's own relevance, but preserve 0.0 for suppressed signals
            equity_rel = _equity_relevance(s.category)
            if s.relevance == 0.0:
                equity_rel = 0.0
            out.append(WeightedSignal(
                asset=Asset.BITCOIN,  # proxy asset for scoring engine
                title=s.title,
                direction=s.direction,
                strength=s.strength,
                importance=s.importance,
                published_at=s.published_at,
                relevance=equity_rel,
                persistence=s.persistence,
                source_quality=s.source_quality,
                session_multiplier=s.session_multiplier,
                is_primary=s.is_primary,
                event_key=s.event_key,
                cluster_id=s.cluster_id,
                corroboration=s.corroboration,
                category=s.category,
                reason=s.reason,
                transmission=s.transmission,
                report_kind=s.report_kind,
                mode=s.mode,
                surprise_weight=s.surprise_weight,
            ))
    return out


def neutral_card(session: SessionName, reason: str = "") -> EquityCardResult:
    """
    50/50 payload for an empty region - never an error, never NO_BIAS.

    The BIAS carries forward the last verdict published for THIS region
    (stale=True); with no region history the answer is DEFAULT_BIAS.
    """
    info = EQUITY_SESSION_MAP.get(session, {})
    region = _region_of(session)
    carried = recall_bias(_state_key(region))
    bias = carried or DEFAULT_BIAS
    return EquityCardResult(
        region=region,
        title=str(info.get("title", region + " EQUITY")),
        symbol=str(info.get("symbol", "")),
        score=0.0,
        confidence=0.0,
        bias=bias.value if isinstance(bias, Bias) else str(bias),
        mode="heuristic",
        drivers=[],
        contributing_articles=0,
        no_data=True,
        stale=True,
        note=reason or ("No " + region + " equities news in the window; "
                        "carrying forward the last published bias."),
        last_updated=T.stamp(),
    )


def build_equity_card(all_signals: Sequence[WeightedSignal],
                      now: Optional[datetime] = None,
                      session: Optional[SessionName] = None) -> EquityCardResult:
    """Score the active region's equity-relevant signals into one card verdict."""
    active = session or equity_session()
    if active is SessionName.OFF_HOURS:
        active = equity_session()
    matched = region_signals(all_signals, active)
    if not matched:
        return neutral_card(active)

    proxy = []
    for s in matched:
        # Task 3b: an ACTUAL scheduled-data print is read with the equity
        # card's OWN direction column (equity_direction) - hot inflation/yields
        # bearish, strong growth/jobs/PMI bullish, higher unemployment/claims
        # bearish - never with the metals/bitcoin column the asset cards use.
        direction = s.direction
        if s.report_kind is ReportKind.ACTUAL:
            text = f"{s.title} {s.reason or ''}"
            prints = parse_all(text)
            if prints:
                direction, _ = combine_prints_for_asset(
                    Asset.BITCOIN, prints, s.direction,
                    use_equity_column=True, text=text)
        proxy.append(WeightedSignal(
            asset=Asset.BITCOIN, title=s.title, direction=direction,
            strength=s.strength, importance=s.importance,
            published_at=s.published_at, relevance=s.relevance,
            persistence=s.persistence, source_quality=s.source_quality,
            session_multiplier=s.session_multiplier, is_primary=s.is_primary,
            event_key=s.event_key, cluster_id=s.cluster_id,
            corroboration=s.corroboration, category=s.category,
            reason=s.reason, transmission=s.transmission,
            report_kind=s.report_kind, mode=s.mode,
            surprise_weight=s.surprise_weight))
    # Task 4: the equity card reuses the same single tier multiplier table
    # and applies it once per signal, via sentiment_engine._signal_weight
    # (compute). No duplicate tier logic here, so importance and tier never
    # double-count.
    result = compute_sentiment(proxy, Asset.BITCOIN, now=now,
                               history_key=_state_key(_region_of(active)))
    info = EQUITY_SESSION_MAP.get(active, {})
    region = _region_of(active)
    drivers = []
    for d in (result.drivers or [])[:3]:
        direc = d.direction.value if isinstance(d.direction, Direction) else str(d.direction)
        drivers.append({"title": d.title,
                        "contribution": round(float(d.contribution), 4),
                        "direction": direc,
                        "weight": round(float(d.weight), 4)})
    bias = result.bias.value if isinstance(result.bias, Bias) else str(result.bias)
    # Same rule as the asset cards (label/percentage consistency): the payload
    # score must sit on the label's side, so a tie-break that named the other
    # side publishes the small lean instead of the raw (contradicting) score.
    # The raw value is kept alongside it for logging only. Idempotent - compute
    # applies the same rule - so this is the card's own copy of the guarantee.
    score = payload_score(result.raw_score, result.bias)
    return EquityCardResult(
        region=region,
        title=str(info.get("title", equity_title(active))),
        symbol=str(info.get("symbol", "")),
        score=float(score),
        raw_score=float(result.raw_score),
        confidence=float(result.confidence),
        bias=bias,
        mode=getattr(result, "mode", "heuristic"),
        drivers=drivers,
        contributing_articles=int(result.contributing_articles),
        no_data=False,
        stale=bool(getattr(result, "stale", False)),
        note="",
        last_updated=T.stamp(),
    )


def to_payload(card: EquityCardResult) -> dict:
    """Plain dict for /api/dashboard (matches schemas.EquityCard)."""
    return {
        "region": card.region,
        "title": card.title,
        "symbol": card.symbol,
        "score": float(card.score),
        "confidence": float(card.confidence),
        "bias": card.bias,
        "mode": card.mode,
        "drivers": list(card.drivers),
        "contributing_articles": int(card.contributing_articles),
        "no_data": bool(card.no_data),
        "stale": bool(card.stale),
        "note": card.note,
        "last_updated": card.last_updated,
    }