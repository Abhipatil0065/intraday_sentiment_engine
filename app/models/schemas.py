"""Pydantic schemas - the API contract between engine and dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    MarketRegime,
    NewsCategory,
    PriceConfirmation,
    ReportKind,
    SessionName,
    Verification,
)


class NewsItemIn(BaseModel):
    """A freshly ingested, pre-analysis news item."""

    guid: str
    source: str
    source_quality: str = "TIER_4_OTHER"
    url: str
    title: str
    body: str = ""
    summary: str = ""
    published_at_utc: Optional[datetime] = None
    published_tz_label: Optional[str] = None
    updated_at_source: Optional[datetime] = None


class AssetImpactOut(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    asset: Asset
    direction: Direction
    strength: ImpactStrength
    relevance: float = 0.0
    persistence: float = 0.5
    reason: str = ""
    transmission: str = ""
    metal_character: Optional[str] = None
    recency_weight: float = 0.0
    importance_weight: float = 0.0
    final_weight: float = 0.0
    signed_contribution: float = 0.0
    price_confirmation: PriceConfirmation = PriceConfirmation.UNAVAILABLE
    session: SessionName = SessionName.OFF_HOURS


class NewsItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    url: str
    title: str
    summary: str = ""
    published_at_utc: Optional[datetime] = None
    published_tz_label: Optional[str] = None
    display_time: str = ""
    category: NewsCategory = NewsCategory.OTHER
    importance: EconomicImportance = EconomicImportance.LOW
    report_kind: ReportKind = ReportKind.GENERAL
    verification: Verification = Verification.CONFIRMED
    relevance_score: float = 0.0
    analysis_mode: str = "heuristic"
    analysis_note: str = ""
    is_today: bool = True
    impacts: list[AssetImpactOut] = Field(default_factory=list)
    event_key: Optional[str] = None
    event_article_count: int = 1


class SentimentBlock(BaseModel):
    """One asset's directional scores. Always sums to 100%."""

    asset: Asset
    scope: str = "CURRENT"
    bull_pct: float = 0.0
    bear_pct: float = 0.0
    label: Direction = Direction.BULLISH
    net_bias: float = 0.0
    confidence: float = 0.0
    conflicting_signals: int = 0
    drivers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    contributing_articles: int = 0
    price_note: str = ""
    last_updated: str = ""


class CalendarEventOut(BaseModel):
    """One scheduled/released macro print and how it reads for one asset."""

    title: str
    country: str = ""
    when_utc: datetime
    impact: str = ""
    mode: str = "EXPECTED"          # RELEASED (actual known) / EXPECTED
    direction: Direction = Direction.NEUTRAL
    strength: ImpactStrength = ImpactStrength.MEDIUM
    importance: EconomicImportance = EconomicImportance.MEDIUM
    forecast: Optional[float] = None
    previous: Optional[float] = None
    actual: Optional[float] = None
    surprise: Optional[float] = None
    reason: str = ""
    transmission: str = ""


class AssetView(BaseModel):
    """Full dashboard payload for one asset: PREP + WRAP + CURRENT."""

    asset: Asset
    label: str
    symbol: str
    market_prep: SentimentBlock
    market_wrap: SentimentBlock
    current: SentimentBlock
    economic_calendar: list[CalendarEventOut] = Field(default_factory=list)


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset: str = ""
    headline: str = ""
    severity: str = "HIGH"
    direction: str = "NEUTRAL"
    sentiment_delta: float = 0.0
    reason: str = ""
    created_at: datetime


class PipelineStatus(BaseModel):
    running: bool = False
    last_run: Optional[str] = None
    last_updated: str = ""
    new_news_since_last_update: int = 0
    articles_today: int = 0
    events_today: int = 0
    sources_polled: int = 0
    sources_ok: int = 0
    analysis_mode: str = "heuristic"
    llm_enabled: bool = False
    poll_interval_seconds: int = 0
    errors: list[str] = Field(default_factory=list)


class DashboardPayload(BaseModel):
    date: str
    current_time: str
    market_timezone: str
    display_timezone: str
    current_session: SessionName
    previous_session: SessionName
    next_session: SessionName
    regime: MarketRegime
    regime_confidence: float = 0.0
    regime_drivers: list[str] = Field(default_factory=list)
    assets: list[AssetView] = Field(default_factory=list)
    news_feed: list[NewsItemOut] = Field(default_factory=list)
    alerts: list[AlertOut] = Field(default_factory=list)
    status: PipelineStatus
    disclaimer: str = (
        "NEWS-BASED INTRADAY SENTIMENT. Not a price prediction, not a trading "
        "signal, and not financial advice."
    )