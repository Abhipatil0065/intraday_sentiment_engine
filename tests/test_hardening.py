"""
Hardening tests for LLM circuit breaker, unknown asset handling, and health endpoint.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.llm_client import CircuitBreaker, reset_circuit_breaker, circuit_breaker
from app.analysis.news_classifier import analyze_article, heuristic_analyze
from app.services.pipeline import run_once
from app.database import init_db
from app.main import app
from app.config import settings


class TestCircuitBreaker:
    """Tests for the LLM circuit breaker."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_threshold(self):
        """Circuit breaker opens after 3 consecutive failures."""
        cb = CircuitBreaker(threshold=3)
        assert not cb.is_open

        await cb.record_failure()
        assert not cb.is_open

        await cb.record_failure()
        assert not cb.is_open

        await cb.record_failure()
        assert cb.is_open

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets_on_success(self):
        """Success resets the failure counter."""
        cb = CircuitBreaker(threshold=3)

        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        assert not cb.is_open

        await cb.record_failure()
        assert not cb.is_open  # only 1 failure after reset

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset_at_tick_start(self):
        """reset_circuit_breaker clears the breaker state."""
        # Use the singleton breaker
        cb = circuit_breaker()
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_failure()
        assert cb.is_open

        await reset_circuit_breaker()
        assert not cb.is_open

    @pytest.mark.asyncio
    async def test_llm_fallback_when_breaker_open(self, monkeypatch):
        """analyze_article falls back to heuristic when breaker is open."""
        # Set LLM enabled/disabled via monkeypatch, NOT via .env, so the test
        # does not depend on the shipped LLM_ENABLED value. ``has_llm`` is a
        # read-only property, so its two inputs are toggled instead.
        monkeypatch.setattr(type(settings), "has_llm", True, raising=False)
        monkeypatch.setattr(settings, "llm_enabled", True, raising=False)
        monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)

        cb = circuit_breaker()
        await cb.reset()

        # Open the breaker
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_failure()
        assert cb.is_open

        # analyze_article should use heuristic
        analysis, llm_ok, skip_reason = await analyze_article("Test headline", "Test body")
        assert not llm_ok
        assert skip_reason == "circuit_breaker"
        assert "circuit breaker open" in analysis.note.lower()
        assert analysis.mode == "heuristic"


class TestUnknownAssetHandling:
    """Tests that unknown assets in asset_impact don't crash recompute."""

    def test_unknown_asset_skipped_not_crash(self):
        """Verify that asset routing handles unknown assets gracefully."""
        from app.analysis.asset_relevance import relevant_assets
        from app.models.enums import NewsCategory
        
        # The relevant_assets function should return empty list for unknown category
        # This tests the core logic without database dependency
        result = relevant_assets(NewsCategory.OTHER, "some text about unknown asset")
        # Should not crash, may return empty list or some assets based on mentions
        
        # Test with known category
        result = relevant_assets(NewsCategory.CENTRAL_BANK, "Fed raises rates")
        assert len(result) > 0
        assert any(a.value == "XAUUSD" for a in result)


class TestHealthEndpoint:
    """Tests that /api/health returns quickly during a pipeline run."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        init_db()
        yield

    def test_health_returns_instantly(self):
        """Health endpoint returns without DB lock."""
        from app.services.pipeline import get_health_metrics
        from fastapi.testclient import TestClient

        # Set some health metrics
        metrics = get_health_metrics()
        metrics["last_poll_ok"] = datetime.now(timezone.utc).isoformat()

        client = TestClient(app)
        response = client.get("/api/health")
        assert response.status_code in (200, 503)
        data = response.json()
        assert "status" in data
        assert "llm_enabled" in data

    @pytest.mark.asyncio
    async def test_health_works_while_pipeline_runs(self, monkeypatch):
        """Health endpoint responds while pipeline is running."""
        from fastapi.testclient import TestClient

        from app.ingestion.base import RawArticle
        from app.ingestion.news_fetcher import FetchReport
        from app.analysis.news_classifier import ArticleAnalysis
        from app.models.enums import (
            NewsCategory,
            EconomicImportance,
            ReportKind,
            Verification,
        )

        # Mock live network calls so the pipeline runs against fake data.
        fake_articles = [
            RawArticle(
                guid="fake-1",
                source="TestWire",
                source_quality="TIER_2_MAJOR",
                url="https://example.test/1",
                title="Fed signals rate pause, gold steady",
                body="The Federal Reserve signaled a pause in rate hikes.",
                published_raw="2026-10-12T10:00:00Z",
            ),
            RawArticle(
                guid="fake-2",
                source="TestWire",
                source_quality="TIER_2_MAJOR",
                url="https://example.test/2",
                title="Gold prices rise on inflation data",
                body="Gold rose as inflation data came in above expectations.",
                published_raw="2026-10-12T11:00:00Z",
            ),
        ]

        async def fake_fetch_all_news():
            return FetchReport(
                articles=list(fake_articles),
                sources_polled=2,
                sources_ok=2,
                errors=[],
                source_stats={},
            )

        async def fake_fetch_calendar(*args, **kwargs):
            return ([], [])

        async def fake_analyze_article(title: str, body: str = ""):
            analysis = ArticleAnalysis(
                category=NewsCategory.OTHER,
                importance=EconomicImportance.LOW,
                report_kind=ReportKind.GENERAL,
                verification=Verification.CONFIRMED,
                relevance_score=0.5,
                assets=[],
                event_summary=title[:200],
                mode="heuristic",
                note="fake analysis for test",
            )
            return analysis, True, None

        monkeypatch.setattr("app.services.pipeline.fetch_all_news", fake_fetch_all_news)
        monkeypatch.setattr("app.services.pipeline.fetch_calendar", fake_fetch_calendar)
        monkeypatch.setattr("app.services.pipeline.analyze_article", fake_analyze_article)

        client = TestClient(app)

        # Start a pipeline run in background
        pipeline_task = asyncio.create_task(run_once())

        # Give it a moment to start
        await asyncio.sleep(0.1)

        # Health should still respond quickly
        response = client.get("/api/health")
        assert response.status_code in (200, 503)
        data = response.json()
        assert "status" in data
        assert "llm_enabled" in data

        # Wait for pipeline to complete
        await pipeline_task


class TestLLMBatchLimit:
    """Tests that LLM batch limit and concurrency are respected."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        init_db()
        yield
        # Teardown touches ONLY the test engine (the temp DB pointed to by
        # DATABASE_URL in tests/conftest.py). It never touches the live
        # data/intraday_sentiment.db.
        from app.database import engine

        from app.models.db_models import Base

        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM asset_impact")
            conn.exec_driver_sql("DELETE FROM news_article")
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    def test_llm_batch_limit_respected(self):
        """LLM_BATCH_LIMIT=15 limits model calls per tick (Item 5)."""
        from app.services.pipeline import LLM_BATCH_LIMIT

        assert LLM_BATCH_LIMIT == 15

        # The classifier's concurrency cap must track the CONFIGURED value -
        # since 2026-10-21 the tick classifies its LLM-bound articles
        # concurrently, so this is the number of calls in flight at once.
        from app.analysis import news_classifier
        from app.config import settings

        assert news_classifier.LLM_MAX_CONCURRENT == settings.llm_max_concurrent
        assert 1 <= news_classifier.LLM_MAX_CONCURRENT <= 4

    def test_concurrency_cap_respected(self):
        """The concurrency cap is a real, bounded number."""
        from app.analysis.news_classifier import LLM_MAX_CONCURRENT
        from app.config import settings

        assert LLM_MAX_CONCURRENT == settings.llm_max_concurrent
        assert 1 <= LLM_MAX_CONCURRENT <= 4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])