"""
ONE-SHOT LLM POLICY: an article is classified at most once, and the verdict
decides its fate.

    HIGH / VERY_HIGH impact  -> stored in the Section B news feed
    MEDIUM / LOW  impact     -> DELETED at ingest, and its guid SUPPRESSED for
                                the rest of the trading day

The suppression half is the part a plain DELETE cannot do on its own:
``news_article`` dedupes on ``guid``, so once the row is gone the same guid
looks brand new on the next feed poll and the engine would pay for - and
delete - the identical headline again on every tick. ``dropped_article`` is
what makes "never comes back" true, while staying scoped to the trading day so
a new day re-admits the story.

Driven through the real ``_ingest_article`` with a stubbed classifier, so the
exact production branch order is exercised.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.analysis.news_classifier import (
    ArticleAnalysis,
    AssetAnalysis,
    is_section_b_verdict,
)
from app.config import settings
from app.database import session_scope
from app.ingestion.base import RawArticle
from app.models.db_models import AssetImpact, DroppedArticle, NewsArticle
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
    Verification,
)
from app.services import pipeline


def _article(title="analyst trims gold forecast", guid=None):
    now = datetime.now(timezone.utc)
    return RawArticle(
        guid=guid or f"guid-{abs(hash(title)) % 10**8}",
        source="Investing.com",
        source_quality="TIER_2_MAJOR",
        url=f"https://example.com/{title[:10]}",
        title=title,
        body="Body text for the article.",
        published_raw=(now - timedelta(minutes=15)).isoformat(),
    )


def _analysis(importance=EconomicImportance.LOW, assets=(), mode="llm"):
    return ArticleAnalysis(
        category=NewsCategory.OTHER,
        importance=importance,
        report_kind=ReportKind.GENERAL,
        verification=Verification.CONFIRMED,
        event_summary="Stub verdict",
        relevance_score=0.5,
        assets=list(assets),
        mode=mode,
        note="stub",
    )


def _asset(asset=Asset.GOLD, strength=None, direction=Direction.BEARISH):
    return AssetAnalysis(
        asset=asset, direction=direction, strength=strength,
        relevance=0.9, persistence=0.7, reason="stub", transmission="stub",
    )


@pytest.fixture
def ingest(monkeypatch):
    """
    Drive the real ingest path once, with a chosen verdict.

    Returns ``(row, stats)``. ``stats._llm_calls`` counts how many times the
    classifier entry point was entered, which is what makes the
    "at most once" half of the policy observable.
    """
    def _run(analysis, *, art=None, use_llm=True, same_day=True):
        calls = {"n": 0}

        async def _analyze(title, body=""):
            calls["n"] += 1
            return analysis, True, "ok"

        if use_llm:
            monkeypatch.setattr(pipeline, "analyze_article", _analyze)
        if not same_day:
            monkeypatch.setattr(pipeline, "_current_market_day_key",
                                lambda: "2999-01-01")
        stats = pipeline.IngestStats()
        with session_scope() as session:
            row = asyncio.run(pipeline._ingest_article(
                art or _article(), session, stats, use_llm))
        stats._llm_calls = calls["n"]
        return row, stats

    return _run


def _counts():
    with session_scope() as session:
        return (session.query(NewsArticle).count(),
                session.query(DroppedArticle).count(),
                session.query(AssetImpact).count())


# --------------------------------------------------------------------------
# The verdict rule itself
# --------------------------------------------------------------------------
def test_article_level_importance_keeps_it():
    assert is_section_b_verdict(
        _analysis(EconomicImportance.HIGH), llm_used=True) is True
    assert is_section_b_verdict(
        _analysis(EconomicImportance.VERY_HIGH), llm_used=True) is True


def test_a_single_high_impact_asset_keeps_a_low_importance_article():
    """The LLM answers with two impact fields; either one can save it."""
    analysis = _analysis(EconomicImportance.LOW,
                          assets=[_asset(strength=ImpactStrength.VERY_HIGH)])
    assert is_section_b_verdict(analysis, llm_used=True) is True


def test_low_everywhere_is_dropped():
    analysis = _analysis(EconomicImportance.LOW,
                         assets=[_asset(strength=ImpactStrength.MEDIUM)])
    assert is_section_b_verdict(analysis, llm_used=True) is False


def test_a_row_the_model_never_judged_is_never_dropped():
    """A heuristic row cannot reach a card anyway (impact gate), so deleting
    it would only empty the feed on a day the model is unavailable."""
    assert is_section_b_verdict(
        _analysis(EconomicImportance.LOW), llm_used=False) is True


# --------------------------------------------------------------------------
# The ingest path: delete + suppress
# --------------------------------------------------------------------------
def test_high_impact_is_stored_in_section_b(ingest):
    row, stats = ingest(_analysis(EconomicImportance.VERY_HIGH,
                                  assets=[_asset(strength=ImpactStrength.VERY_HIGH)]))
    articles, dropped, impacts = _counts()
    assert row is not None
    assert articles == 1 and dropped == 0 and impacts == 1
    assert stats.llm_new_ok == 1 and stats.llm_new_dropped_low == 0
    assert stats.new_today == 1


def test_low_impact_is_deleted_and_suppressed(ingest):
    row, stats = ingest(_analysis(EconomicImportance.LOW,
                                  assets=[_asset(strength=ImpactStrength.LOW)]))
    articles, dropped, impacts = _counts()
    assert row is None
    assert articles == 0, "the row must not survive its own verdict"
    assert dropped == 1, "the guid must be suppressed for the trading day"
    assert impacts == 0, "cascades: no impact rows outlive the article"
    assert stats.llm_new_dropped_low == 1
    assert stats.new_today == 1, "a deleted article was still processed"
    assert stats.llm_new_ok == 0, "buckets are mutually exclusive"
    assert stats.verify_new_article_counters() is True


def test_suppressed_guid_is_skipped_without_a_second_model_call(ingest):
    """One call, not one per tick: the second sighting of the same guid is
    skipped before the classifier is entered."""
    analysis = _analysis(EconomicImportance.LOW,
                         assets=[_asset(strength=ImpactStrength.LOW)])
    art = _article(title="analyst trims gold forecast", guid="dup-guid")

    first_row, first_stats = ingest(analysis, art=art)
    assert first_row is None and first_stats._llm_calls == 1

    second_row, second_stats = ingest(analysis, art=_article(
        title="analyst trims gold forecast", guid="dup-guid"))
    assert second_row is None
    assert second_stats.skipped_dropped == 1
    assert second_stats._llm_calls == 0, "no model call for a suppressed guid"
    assert second_stats.new_today == 0, "and nothing new was processed"
    assert _counts()[1] == 1, "still exactly one suppression row"


def test_a_new_trading_day_readmits_the_story(ingest):
    """The suppression is scoped to the trading day that wrote it."""
    analysis = _analysis(EconomicImportance.LOW,
                         assets=[_asset(strength=ImpactStrength.LOW)])
    art = _article(title="analyst trims gold forecast", guid="day-guid")
    ingest(analysis, art=art)

    # Simulate the day rollover the way the engine does: a different key.
    row, stats = ingest(analysis, art=_article(
        title="analyst trims gold forecast", guid="day-guid"), same_day=False)
    assert stats.skipped_dropped == 0
    assert stats._llm_calls == 1, "the model gets its one call again"


def test_heuristic_row_is_kept_not_dropped(ingest):
    """Batch-cap heuristics: no model verdict, so the delete rule does not
    apply even when the classifier's own score is low."""
    row, stats = ingest(_analysis(EconomicImportance.LOW), use_llm=False)
    assert row is not None, "a heuristic row must survive"
    assert stats.llm_new_skipped_batch == 1
    assert stats.llm_new_dropped_low == 0
    assert _counts()[1] == 0


def test_fj_session_reports_are_exempt(ingest):
    """Section A boxes are prep/wrap reports, not news: the policy never
    touches them."""
    analysis = _analysis(EconomicImportance.LOW,
                         assets=[_asset(strength=ImpactStrength.LOW)])
    now = datetime.now(timezone.utc)
    art = RawArticle(
        guid="fj-wrap", source="Financial Juice", source_quality="TIER_1_OFFICIAL",
        url="https://www.financialjuice.com/x",
        title="US Market Wrap - US Session Prep",
        body="", published_raw=(now - timedelta(minutes=15)).isoformat(),
    )
    row, stats = ingest(analysis, art=art)
    assert row is not None, "a LOW verdict must not delete a session report"
    assert stats.llm_new_dropped_low == 0


def test_flag_off_keeps_everything(ingest, monkeypatch):
    monkeypatch.setattr(settings, "drop_low_impact_articles", False)
    row, stats = ingest(_analysis(EconomicImportance.LOW))
    assert row is not None
    assert _counts()[1] == 0
    assert stats.llm_new_dropped_low == 0


def test_suppression_rows_are_dropped_by_the_retention_purge():
    """A suppression only has to outlive its trading day."""
    with session_scope() as session:
        session.add(DroppedArticle(
            guid="old-guid",
            dropped_day="2020-01-01",
            title="Ancient headline",
            importance="LOW",
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        ))
        session.add(DroppedArticle(
            guid="today-guid",
            dropped_day="2999-01-01",
            title="Today",
            importance="LOW",
        ))
    with session_scope() as session:
        pipeline.purge_old_news(session, keep_days=2)
    with session_scope() as session:
        guids = {g for (g,) in session.execute(
            select(DroppedArticle.guid)).all()}
    assert "old-guid" not in guids, "stale suppression rows are trimmed"
    assert "today-guid" in guids, "today's suppression rows survive"
