"""SESSION-AWARE GLOBAL EQUITY CARD (one card, NOW-only, never persisted).

Takes EQUITIES-category news signals for today, filters them to the active
session's region via EQUITY_SESSION_MAP keywords (index names, regional
central banks, regional macro/earnings terms), and scores them with the
SAME sentiment_engine.compute() contract the metal cards use.

Zero regional matches -> neutral 50/50 payload with no_data=True, never an
error. No DB rows are written; the pipeline keeps the result in memory and
main.py serves it inside /api/dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from app.analysis.sentiment_engine import (
    Bias,
    WeightedSignal,
    compute as compute_sentiment,
)
from app.logging_conf import get_logger
from app.market.session_detector import (
    EQUITY_SESSION_MAP,
    SessionName,
    equity_keywords,
    equity_session,
    equity_title,
)
from app.models.enums import Asset, Direction, NewsCategory
from app.utils import timeutils as T

log = get_logger(__name__)


@dataclass
class EquityCardResult:
    """In-memory verdict for the one global equity card."""

    region: str
    title: str
    symbol: str
    score: float = 0.0
    confidence: float = 0.0
    bias: str = Bias.NO_BIAS.value
    mode: str = "heuristic"
    drivers: list = field(default_factory=list)
    contributing_articles: int = 0
    no_data: bool = True
    note: str = ""
    last_updated: str = ""


def _region_of(session: SessionName) -> str:
    if session in (SessionName.ASIA, SessionName.EUROPE, SessionName.US):
        return session.value
    return "US"


def _matches_region(text: str, keywords: Sequence[str]) -> bool:
    lowered = (text or "").lower()
    return any(k and k in lowered for k in keywords)


def region_signals(signals: Sequence[WeightedSignal],
                   session: SessionName) -> list[WeightedSignal]:
    """EQUITIES signals whose headline matches this region's keywords."""
    keywords = equity_keywords(session)
    if not keywords:
        return []
    out = []
    for s in signals:
        if s.category is not NewsCategory.EQUITIES:
            continue
        if _matches_region(s.title, keywords):
            out.append(s)
    return out


def neutral_card(session: SessionName, reason: str = "") -> EquityCardResult:
    """Neutral 50/50 payload for an empty region - never an error."""
    info = EQUITY_SESSION_MAP.get(session, {})
    region = _region_of(session)
    return EquityCardResult(
        region=region,
        title=str(info.get("title", region + " EQUITY")),
        symbol=str(info.get("symbol", "")),
        score=0.0,
        confidence=0.0,
        bias=Bias.NO_BIAS.value,
        mode="heuristic",
        drivers=[],
        contributing_articles=0,
        no_data=True,
        note=reason or "No " + region + " equities news in the window.",
        last_updated=T.stamp(),
    )

def build_equity_card(all_signals: Sequence[WeightedSignal],
                      now: Optional[datetime] = None,
                      session: Optional[SessionName] = None) -> EquityCardResult:
    """Score the active region's EQUITIES signals into one card verdict."""
    active = session or equity_session()
    if active is SessionName.OFF_HOURS:
        active = equity_session()
    matched = region_signals(all_signals, active)
    if not matched:
        return neutral_card(active)
    # Score via BITCOIN's slot (highest EQUITIES relevance, 0.72) purely as
    # a scoring vehicle, then relabel the verdict as the equity region.
    proxy = []
    for s in matched:
        proxy.append(WeightedSignal(
            asset=Asset.BITCOIN, title=s.title, direction=s.direction,
            strength=s.strength, importance=s.importance,
            published_at=s.published_at, relevance=s.relevance,
            persistence=s.persistence, source_quality=s.source_quality,
            session_multiplier=s.session_multiplier, is_primary=s.is_primary,
            event_key=s.event_key, cluster_id=s.cluster_id,
            corroboration=s.corroboration, category=s.category,
            reason=s.reason, transmission=s.transmission,
            report_kind=s.report_kind, mode=s.mode))
    result = compute_sentiment(proxy, Asset.BITCOIN, now=now)
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
    return EquityCardResult(
        region=region,
        title=str(info.get("title", equity_title(active))),
        symbol=str(info.get("symbol", "")),
        score=float(result.score),
        confidence=float(result.confidence),
        bias=bias,
        mode=getattr(result, "mode", "heuristic"),
        drivers=drivers,
        contributing_articles=int(result.contributing_articles),
        no_data=False,
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
        "note": card.note,
        "last_updated": card.last_updated,
    }

