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
    speaker_tier: Mapped[float] = mapped_column(Float, default=1.0)

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

    ``raw_score`` is the engine's pre-clamp net score; ``score`` is what the
    payload publishes. They differ on a tie-break verdict, where ``score`` is
    deliberately nudged to +/- TIEBREAK_LEAN so the badge cannot contradict
    itself. The dashboard split is derived from ``raw_score`` so a card shows
    the real lean instead of the published probe.

    KEEP IN SYNC with _add_missing_sentiment_columns() in database.py, which
    ALTERs both columns onto an existing database. The columns existed in the
    live schema for a while WITHOUT the Mapped attribute here, so SQLAlchemy
    never mapped them and main._block's getattr() fallback silently read
    ``score``. Declaring the attribute is what makes the persisted value
    reachable at all.
    """

    __tablename__ = "sentiment_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String(12), index=True)
    scope: Mapped[str] = mapped_column(String(24), index=True)  # CURRENT / PREP / WRAP

    score: Mapped[float] = mapped_column(Float, default=0.0)      # -100..+100
    # The pre-clamp net score the engine actually computed. The dashboard's
    # bull/bear split is derived from THIS, not from ``score``: on a tie-break
    # verdict ``score`` is a +/- TIEBREAK_LEAN probe, so projecting it would
    # render a real lean as a fake 51/49. NULL only on rows written before the
    # migration, which main._block detects and warns about.
    raw_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # TASK 2: the two aggregate totals behind this verdict, so
    # r = min(bull,bear)/max(bull,bear) is recoverable from history. Without
    # them the net raw_score cannot be decomposed - infinitely many pairs give
    # the same net. Data capture only: the scoring path never reads them, and
    # the flip gates compare against `score`, not these.
    # NULL on rows written before the migration, which main._block must
    # tolerate. KEEP IN SYNC with _add_missing_sentiment_columns().
    bull: Mapped[float | None] = mapped_column(Float, nullable=True)
    bear: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The rolling per-asset baseline this tick was normalised against, from
    # app.analysis.bias_baseline.baseline_score(). NULL during warm-up or when
    # the feature is off. Recorded for the audit trail; the engine never reads
    # it back when scoring.
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    # Number of signals that reached this asset in the window. 0 on a stale
    # (carried-forward) verdict - the point of stale is that nothing new was
    # seen. Mirrors contributing_articles on a fresh verdict.
    signal_count: Mapped[int] = mapped_column(Integer, default=0)
    # True when both sides are heavily weighted (bull > 80 and bear > 80):
    # the news is genuinely torn, so a flip is held to a wider margin.
    contradiction_detected: Mapped[bool] = mapped_column(
        Boolean, default=False)
    # News-velocity burst counts in the last 15 minutes: how many same-side
    # headlines landed together. 0 on a stale verdict. Exposed on the
    # dashboard so a reader can see the momentum behind the score.
    burst_bull: Mapped[int] = mapped_column(Integer, default=0)
    burst_bear: Mapped[int] = mapped_column(Integer, default=0)

    price_note: Mapped[str] = mapped_column(Text, default="")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_label: Mapped[str] = mapped_column(String(8), default="LOW")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)
    # NOTE: no outcomes relationship here. Calibration labels belong to
    # BiasSnapshot (BiasOutcome.bias_snapshot_id). This class used to declare
    # one that paired with a BiasOutcome FK pointing at THIS table - both
    # wrong, and together they made every mapper fail to configure. Nothing
    # reads sentiment_snapshot.outcomes; the labels live on BiasSnapshot.


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

    # Item 1: baseline audit trail. The published ``score`` is
    # baseline-adjusted when a baseline exists; these carry the unadjusted
    # level the engine computed, the reference it was normalised against, and
    # whether an adjustment actually applied. NULL / False during warm-up.
    #
    # KEEP IN SYNC with _add_missing_bias_snapshot_columns() in database.py:
    # the migration creates the columns on an existing database, but without
    # the Mapped attribute here every ``BiasSnapshot(**row)`` raises
    # "'raw_score' is an invalid keyword argument" and the writer's guard
    # silently drops EVERY snapshot row - which is exactly what happened, and
    # why /api/calibration/labels had no snapshot to attach a label to.
    raw_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    adjusted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Manual calibration labels recorded against this snapshot
    # (/api/calibration/labels). Data collection only - the engine never reads
    # these rows when scoring. Snapshots are append-only; cascade exists so a
    # manual purge takes its labels with it rather than orphaning them.
    outcomes: Mapped[list["BiasOutcome"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan")

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


class BiasOutcome(Base):
    """Manual calibration label against a bias snapshot (section 10).

    One row per human label recorded via /api/calibration/labels.
    """

    __tablename__ = "bias_outcome"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Must point at bias_snapshots(id) - the rows calibration labels attach
    # to. It pointed at sentiment_snapshot(id) instead, so every POST died
    # with "FOREIGN KEY constraint failed": a bias_snapshots id is not a
    # sentiment_snapshot id, and with FK enforcement on SQLite rejects the
    # insert outright. Same contract as the CREATE TABLE in
    # _add_missing_bias_outcome_table() (database.py).
    bias_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("bias_snapshots.id", ondelete="CASCADE"), index=True)
    asset: Mapped[str] = mapped_column(String(12), index=True)
    date_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    bias: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    outcome: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    snapshot: Mapped["BiasSnapshot"] = relationship(back_populates="outcomes")


class DroppedArticle(Base):
    """A low-impact article deleted at ingest, suppressed for its trading day.

    One row per guid deleted under the one-shot policy. Two jobs:

    1. It makes the deletion SURVIVE the deletion. ``news_article`` is deduped
       by ``guid``, so once the article row is gone the same guid looks brand
       new on the next feed poll - the engine would pay for the same headline
       and delete it again, forever. Checking this table first is what makes
       "never comes back" true.
    2. It is SCOPED BY TRADING DAY (``dropped_day``, the market-local date),
       so a genuinely new day re-admits the story while a source that keeps
       re-serving it today is skipped for free.

    Purged by ``purge_old_news`` on the same retention window as the articles
    themselves, so it cannot grow without bound.
    """

    __tablename__ = "dropped_article"

    guid: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Market-local trading-day key ("2026-10-13"). A suppression only lasts as
    # long as its day: the row stops matching once the day rolls over.
    dropped_day: Mapped[str] = mapped_column(String(12), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(160), default="")
    # Verdict that condemned it, for the audit trail.
    importance: Mapped[str] = mapped_column(String(24), default="LOW")
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)
