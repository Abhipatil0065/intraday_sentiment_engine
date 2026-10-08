"""SQLAlchemy ORM models (section 26 - DATABASE)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class NewsArticle(Base):
    """One ingested news item plus its today-filter verdict."""

    __tablename__ = "news_article"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guid: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    source: Mapped[str] = mapped_column(String(160), index=True)
    source_quality: Mapped[str] = mapped_column(String(32), default="TIER_4_OTHER")
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")

    published_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_tz_label: Mapped[str | None] = mapped_column(String(48))
    is_today: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_updated_article: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at_source: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    category: Mapped[str] = mapped_column(String(48), default="Other", index=True)
    importance: Mapped[str] = mapped_column(String(24), default="LOW")
    report_kind: Mapped[str] = mapped_column(String(24), default="GENERAL", index=True)
    verification: Mapped[str] = mapped_column(String(24), default="CONFIRMED")
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    analysis_mode: Mapped[str] = mapped_column(String(24), default="heuristic")
    analysis_note: Mapped[str] = mapped_column(Text, default="")
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    impacts: Mapped[list[AssetImpact]] = relationship(
        back_populates="article", cascade="all, delete-orphan")
    event_links: Mapped[list[ArticleEvent]] = relationship(
        back_populates="article", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_article_pub_today", "published_at_utc", "is_today"),)


class NewsEvent(Base):
    """One real-world event; many articles may collapse into it (section 16)."""

    __tablename__ = "news_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    canonical_headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")

    category: Mapped[str] = mapped_column(String(48), default="Other")
    importance: Mapped[str] = mapped_column(String(24), default="LOW")
    verification: Mapped[str] = mapped_column(String(24), default="CONFIRMED")
    direction: Mapped[str] = mapped_column(String(16), default="NEUTRAL")

    article_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_updated_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    confirmed_by_followup: Mapped[bool] = mapped_column(Boolean, default=False)

    links: Mapped[list[ArticleEvent]] = relationship(
        back_populates="event", cascade="all, delete-orphan")


class ArticleEvent(Base):
    __tablename__ = "article_event"

    article_id: Mapped[int] = mapped_column(
        ForeignKey("news_article.id", ondelete="CASCADE"), primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("news_event.id", ondelete="CASCADE"), primary_key=True)
    similarity: Mapped[float] = mapped_column(Float, default=0.0)

    article: Mapped[NewsArticle] = relationship(back_populates="event_links")
    event: Mapped[NewsEvent] = relationship(back_populates="links")


class AssetImpact(Base):
    """Per-asset directional assessment for a single article (sections 6-8)."""

    __tablename__ = "asset_impact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("news_article.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_event.id", ondelete="SET NULL"), index=True)
    asset: Mapped[str] = mapped_column(String(12), index=True)

    direction: Mapped[str] = mapped_column(String(16))
    strength: Mapped[str] = mapped_column(String(16), default="LOW")
    relevance: Mapped[float] = mapped_column(Float, default=0.0)
    persistence: Mapped[float] = mapped_column(Float, default=0.5)

    reason: Mapped[str] = mapped_column(Text, default="")
    transmission: Mapped[str] = mapped_column(Text, default="")
    metal_character: Mapped[str | None] = mapped_column(String(32))  # silver dual-demand

    recency_weight: Mapped[float] = mapped_column(Float, default=0.0)
    importance_weight: Mapped[float] = mapped_column(Float, default=0.0)
    final_weight: Mapped[float] = mapped_column(Float, default=0.0)
    signed_contribution: Mapped[float] = mapped_column(Float, default=0.0)

    price_confirmation: Mapped[str] = mapped_column(String(16), default="UNAVAILABLE")
    session: Mapped[str] = mapped_column(String(16), default="OFF_HOURS")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article: Mapped[NewsArticle] = relationship(back_populates="impacts")

    __table_args__ = (
        Index("ix_impact_article_asset", "article_id", "asset", unique=True),
    )


class SentimentSnapshot(Base):
    """
    Materialised per-asset sentiment at a point in time.

    Item 2 replaced bull_pct/bear_pct/label with a signed score, a confidence
    and a three-state bias that may be NO_BIAS. The explainability fields
    (item 10) are stored alongside so the dashboard never has to recompute
    them.
    """

    __tablename__ = "sentiment_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(12), index=True)
    scope: Mapped[str] = mapped_column(String(24), index=True)  # CURRENT / PREP / WRAP

    score: Mapped[float] = mapped_column(Float, default=0.0)      # -100..+100
    confidence: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    bias: Mapped[str] = mapped_column(String(16), default="NO_BIAS")
    mode: Mapped[str] = mapped_column(String(24), default="heuristic")
    delta_60m: Mapped[float] = mapped_column(Float, default=0.0)
    event_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    event_risk_note: Mapped[str] = mapped_column(Text, default="")
    # Dampened score for display/cue strength (raw score used for bias/flip/stability)
    damped_score: Mapped[float] = mapped_column(Float, default=0.0)

    drivers: Mapped[list] = mapped_column(JSON, default=list)
    # Both sides separately, so the dashboard can show a bullish box beside a
    # bearish box. "drivers" above still holds only the leading side.
    bull_drivers: Mapped[list] = mapped_column(JSON, default=list)
    bear_drivers: Mapped[list] = mapped_column(JSON, default=list)
    conflict: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    contributing_articles: Mapped[int] = mapped_column(Integer, default=0)

    price_note: Mapped[str] = mapped_column(Text, default="")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)


class BiasSnapshot(Base):
    """
    Append-only audit log for manual review (phase 2).

    One row per asset per poll, written AFTER the verdict is published, so it
    can only ever describe a decision that was already shown to the reader.

    NEWS-ONLY BY DESIGN. This table records what the engine believed and why -
    the bias, the score, the confidence, and the headlines that drove it. It
    holds no price and no price-derived number of any kind. Price comparison is
    done manually against this log; nothing here is ever used to score, weight,
    validate or adjust a verdict.

    Separate from ``sentiment_snapshot``, which is a rebuildable view of NOW.
    This one is durable history and is never rewritten.
    """

    __tablename__ = "bias_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)
    # Same instant in the display timezone, 12-hour clock, for reading by hand.
    timestamp_ist: Mapped[str] = mapped_column(String(32), default="")
    asset: Mapped[str] = mapped_column(String(12), index=True)

    bias: Mapped[str] = mapped_column(String(16), default="NO_BIAS")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # The display split, captured verbatim so a manual reader compares against
    # exactly what was shown and never a re-derived number.
    bullish_pct: Mapped[float] = mapped_column(Float, default=50.0)
    bearish_pct: Mapped[float] = mapped_column(Float, default=50.0)

    article_count: Mapped[int] = mapped_column(Integer, default=0)
    # Age of the newest signal behind this verdict, in minutes. Null when the
    # asset has no usable signal at all.
    last_signal_age_min: Mapped[float | None] = mapped_column(
        Float, nullable=True)

    # Top 3 drivers each side, with the weight each carried.
    bull_drivers: Mapped[list] = mapped_column(JSON, default=list)
    bear_drivers: Mapped[list] = mapped_column(JSON, default=list)

    __table_args__ = (Index("ix_bias_snapshots_asset_ts", "asset",
                            "timestamp_utc"),)


class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_article.id", ondelete="CASCADE"))
    asset: Mapped[str] = mapped_column(String(12), default="")
    headline: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(24), default="HIGH")
    direction: Mapped[str] = mapped_column(String(16), default="NEUTRAL")
    sentiment_delta: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)


class PipelineRun(Base):
    """Bookkeeping for the live-updating dashboard (section 21)."""

    __tablename__ = "pipeline_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="running")
    articles_fetched: Mapped[int] = mapped_column(Integer, default=0)
    articles_new: Mapped[int] = mapped_column(Integer, default=0)
    articles_rejected: Mapped[int] = mapped_column(Integer, default=0)
    llm_errors: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
