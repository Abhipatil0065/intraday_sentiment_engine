"""
ALERT SYSTEM (section 22).

Fires when a VERY HIGH economic-importance event or a VERY HIGH impact
strength signal appears. Each alert records the estimated sentiment delta so
the dashboard can show the initial impact before the full recompute.
"""

from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.database import session_scope
from app.logging_conf import get_logger
from app.models.db_models import Alert, AssetImpact, NewsArticle
from app.models.enums import Asset, Direction, EconomicImportance, ImpactStrength

log = get_logger(__name__)


def evaluate_and_emit(session, stats) -> list[Alert]:
    """
    Scan today's very-high-signal rows and persist alerts for new ones.

    Idempotent: an alert is only created once per article.
    """
    created: list[Alert] = []
    want_importance = settings.alert_on_importance.upper()
    want_strength = settings.alert_on_strength.upper()

    candidates = session.scalars(
        select(NewsArticle)
        .where(NewsArticle.is_today.is_(True))
        .where(
            (NewsArticle.importance == EconomicImportance.VERY_HIGH.value)
            | (NewsArticle.importance == want_importance)
        )
        .order_by(NewsArticle.published_at_utc.desc())
        .limit(30)
    ).all()

    for row in candidates:
        existing = session.scalar(
            select(Alert).where(Alert.article_id == row.id)
        )
        if existing is not None:
            continue

        strong = [i for i in row.impacts
                  if i.strength == ImpactStrength.VERY_HIGH.value
                  or i.strength == want_strength]
        if not strong:
            continue

        top = max(strong, key=lambda i: i.relevance * i.importance_weight)
        direction = Direction(top.direction)
        delta = _estimate_delta(top)

        alert = Alert(
            article_id=row.id,
            asset=top.asset,
            headline=row.title[:280],
            severity="VERY_HIGH",
            direction=direction.value,
            sentiment_delta=delta,
            reason=top.reason[:500] or top.transmission[:500],
        )
        session.add(alert)
        created.append(alert)
        log.warning(
            "HIGH-IMPACT NEWS | Asset: %s | Event: %s | Impact: %s | "
            "Initial sentiment impact: %+.1f",
            top.asset, row.title[:80], direction.value, delta,
        )

    if created:
        session.flush()
    return created


def _estimate_delta(impact: AssetImpact) -> float:
    """
    Expected shift in net bias if this item is taken at full weight.

    Scaled to the 0-100 percentage-point scale used by the dashboard.
    """
    sign = 1.0 if impact.direction == Direction.BULLISH.value else (
        -1.0 if impact.direction == Direction.BEARISH.value else 0.0
    )
    magnitude = impact.relevance * impact.importance_weight * impact.persistence
    return round(sign * magnitude * 40.0, 1)


def recent_alerts(session, limit: int = 20) -> list[Alert]:
    return list(session.scalars(
        select(Alert).order_by(Alert.created_at.desc()).limit(limit)
    ).all())