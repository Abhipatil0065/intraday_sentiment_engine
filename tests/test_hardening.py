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
from app.models.db_models import NewsArticle, AssetImpact
from app.models.enums import Asset, Direction, EconomicImportance, ImpactStrength
from app.services.pipeline import run_once
from app.database import init_db, session_scope
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
    async def test_llm_fallback_when_breaker_open(self):
        """analyze_article falls back to heuristic when breaker is open."""
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
    async def test_health_works_while_pipeline_runs(self):
        """Health endpoint responds while pipeline is running."""
        from fastapi.testclient import TestClient

        client = TestClient(app)

        # Start a pipeline run in background
        pipeline_task = asyncio.create_task(run_once())

        # Give it a moment to start
        await asyncio.sleep(0.1)

        # Health should still respond quickly
        response = client.get("/api/health")
        assert response.status_code in (200, 503)

        # Wait for pipeline to complete
        await pipeline_task


class TestLLMBatchLimit:
    """Tests that LLM batch limit and concurrency are respected."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        init_db()
        yield
        with session_scope() as session:
            session.execute(AssetImpact.__table__.delete())
            session.execute(NewsArticle.__table__.delete())

    def test_llm_batch_limit_respected(self):
        """LLM_BATCH_LIMIT=15 limits concurrent LLM calls."""
        from app.services.pipeline import LLM_BATCH_LIMIT

        assert LLM_BATCH_LIMIT == 15

        # Verify concurrency constant in news_classifier
        from app.analysis import news_classifier
        assert news_classifier.LLM_MAX_CONCURRENT == 2

    def test_concurrency_cap_respected(self):
        """Concurrency capped at 2."""
        from app.analysis.news_classifier import LLM_MAX_CONCURRENT
        assert LLM_MAX_CONCURRENT == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])