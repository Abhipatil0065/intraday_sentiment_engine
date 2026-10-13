"""
REAL COUNTER PROOF: pytest that runs the real pipeline loop with a stub LLM.

This test verifies the pipeline counter invariant:
    llm_new_ok + llm_new_failed + llm_new_skipped_batch +
    llm_new_dropped_low == new_today

The last bucket is the one-shot policy: the stub classifies unmatched
headlines LOW, and those articles are DELETED and suppressed rather than
stored - still new_today articles, just in their own bucket.

It uses a stub LLM that always succeeds, so we can verify the counters
without depending on an external LLM provider.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from app.services.pipeline import run_once, IngestStats
from app.database import init_db, session_scope
from app.models.db_models import NewsArticle, NewsEvent, AssetImpact, ArticleEvent, SentimentSnapshot, PipelineRun, Alert
from sqlalchemy import delete


class StubLLMClient:
    """Stub LLM that returns deterministic analysis."""
    
    @staticmethod
    async def analyze_article(title: str, body: str):
        from app.analysis.news_classifier import ArticleAnalysis
        from app.models.enums import NewsCategory, EconomicImportance, ReportKind, Verification
        from app.analysis.llm_schema import AssetAnalysis
        from app.models.enums import Asset, Direction, ImpactStrength
        
        # Simple deterministic classification based on keywords
        title_lower = title.lower()
        
        if "fed" in title_lower or "fomc" in title_lower or "rate" in title_lower:
            category = NewsCategory.CENTRAL_BANK
            importance = EconomicImportance.VERY_HIGH
            assets = [
                AssetAnalysis(asset=Asset.GOLD, direction=Direction.BEARISH, strength=ImpactStrength.VERY_HIGH, relevance=0.9, persistence=0.9, reason="Fed hawkish", transmission="Higher rates pressure gold"),
                AssetAnalysis(asset=Asset.SILVER, direction=Direction.BEARISH, strength=ImpactStrength.VERY_HIGH, relevance=0.85, persistence=0.9, reason="Fed hawkish", transmission="Higher rates pressure silver"),
                AssetAnalysis(asset=Asset.BITCOIN, direction=Direction.BEARISH, strength=ImpactStrength.VERY_HIGH, relevance=0.8, persistence=0.9, reason="Fed hawkish", transmission="Higher rates pressure btc"),
            ]
        elif "gold" in title_lower:
            category = NewsCategory.COMMODITY_SUPPLY_DEMAND
            importance = EconomicImportance.HIGH
            assets = [
                AssetAnalysis(asset=Asset.GOLD, direction=Direction.BULLISH, strength=ImpactStrength.HIGH, relevance=1.0, persistence=0.8, reason="Gold specific", transmission="Direct gold impact"),
            ]
        elif "bitcoin" in title_lower or "btc" in title_lower or "crypto" in title_lower:
            category = NewsCategory.BITCOIN_ETF_FLOWS
            importance = EconomicImportance.HIGH
            assets = [
                AssetAnalysis(asset=Asset.BITCOIN, direction=Direction.BULLISH, strength=ImpactStrength.HIGH, relevance=1.0, persistence=0.8, reason="Crypto specific", transmission="Direct btc impact"),
            ]
        else:
            category = NewsCategory.OTHER
            importance = EconomicImportance.LOW
            assets = []
        
        analysis = ArticleAnalysis(
            category=category,
            importance=importance,
            report_kind=ReportKind.GENERAL,
            verification=Verification.CONFIRMED,
            relevance_score=0.8,
            assets=assets,
            event_summary=title[:200],
            mode="llm",
            note="Stub LLM analysis",
        )
        return analysis, True, None  # (analysis, llm_ok, skip_reason)
    
    @staticmethod
    async def health_check():
        return {"ok": True, "detail": "stub"}
    
    @staticmethod
    async def reset_circuit_breaker():
        pass
    
    @staticmethod
    def circuit_breaker():
        class CB:
            is_open = False
        return CB()
    
    @staticmethod
    def requests_in_window():
        return 0
    
    @staticmethod
    def llm_status():
        return "stub"


@pytest.mark.asyncio
async def test_pipeline_counter_invariant_with_stub_llm():
    """
    Run the real pipeline loop with a stub LLM and verify the counter invariant.
    
    The invariant: llm_new_ok + llm_new_failed + llm_new_skipped_batch == new_today
    
    This test uses the actual database, ingestion, and pipeline code - only the
    LLM client is stubbed to return deterministic results.
    """
    init_db()
    
    # Clear any existing data for a clean test
    with session_scope() as session:
        session.execute(delete(NewsArticle))
        session.execute(delete(NewsEvent))
        session.execute(delete(AssetImpact))
        session.execute(delete(ArticleEvent))
        session.execute(delete(SentimentSnapshot))
        session.execute(delete(PipelineRun))
        session.execute(delete(Alert))
    
    # Patch the LLM client with our stub
    with patch('app.services.pipeline.llm_client', StubLLMClient):
        with patch('app.analysis.news_classifier.llm_client', StubLLMClient):
            with patch('app.services.pipeline.settings') as mock_settings:
                # Ensure LLM is enabled for this test
                mock_settings.has_llm = True
                mock_settings.llm_batch_limit = 100  # High limit so all articles use LLM
                # One-shot policy flags: read explicitly because the whole
                # settings object is a mock here and any unknown attribute
                # would come back truthy.
                mock_settings.drop_low_impact_articles = True
                mock_settings.only_llm_verdict_may_drop = True

                stats = await run_once()

    # Verify the invariant. Under the one-shot policy the stub's LOW-importance
    # articles are DELETED, so they land in llm_new_dropped_low - a fifth
    # mutually exclusive bucket - and the sum must still equal new_today.
    total_llm_accounted = (stats.llm_new_ok + stats.llm_new_failed +
                           stats.llm_new_skipped_batch +
                           stats.llm_new_dropped_low)
    assert total_llm_accounted == stats.new_today, (
        f"Counter invariant violated: llm_new_ok({stats.llm_new_ok}) + "
        f"llm_new_failed({stats.llm_new_failed}) + "
        f"llm_new_skipped_batch({stats.llm_new_skipped_batch}) + "
        f"llm_new_dropped_low({stats.llm_new_dropped_low}) = "
        f"{total_llm_accounted} != new_today({stats.new_today})"
    )

    # Backlog re-classification is retired with the one-shot policy: an
    # article is classified at most once, so there is no second pass to count.
    assert stats.llm_backlog_reclassified == 0

    # Verify we got some articles (stub LLM should classify them all)
    assert stats.fetched > 0, "Should have fetched articles"
    assert stats.new_today >= 0, "Should have processed articles"

    print(f"Pipeline stats: fetched={stats.fetched} new={stats.new_today} "
          f"llm_ok={stats.llm_new_ok} llm_fail={stats.llm_new_failed} "
          f"llm_skip_batch={stats.llm_new_skipped_batch} "
          f"dropped_low={stats.llm_new_dropped_low}")


@pytest.mark.asyncio
async def test_pipeline_counter_invariant_multiple_ticks():
    """
    Run multiple pipeline ticks and verify invariant holds on each tick.
    """
    init_db()
    
    # Clear any existing data
    with session_scope() as session:
        session.execute(delete(NewsArticle))
        session.execute(delete(NewsEvent))
        session.execute(delete(AssetImpact))
        session.execute(delete(ArticleEvent))
        session.execute(delete(SentimentSnapshot))
        session.execute(delete(PipelineRun))
        session.execute(delete(Alert))
    
    with patch('app.services.pipeline.llm_client', StubLLMClient):
        with patch('app.analysis.news_classifier.llm_client', StubLLMClient):
            with patch('app.services.pipeline.settings') as mock_settings:
                mock_settings.has_llm = True
                mock_settings.llm_batch_limit = 100
                mock_settings.drop_low_impact_articles = True
                mock_settings.only_llm_verdict_may_drop = True

                for tick in range(3):
                    stats = await run_once()

                    # Verify invariant on each tick
                    total_llm_accounted = (
                        stats.llm_new_ok + stats.llm_new_failed +
                        stats.llm_new_skipped_batch +
                        stats.llm_new_dropped_low)
                    assert total_llm_accounted == stats.new_today, (
                        f"Tick {tick+1}: Counter invariant violated: "
                        f"{total_llm_accounted} != {stats.new_today}"
                    )

                    print(f"Tick {tick+1}: fetched={stats.fetched} new={stats.new_today} "
                          f"llm_ok={stats.llm_new_ok} invariant=OK")


if __name__ == "__main__":
    # Run manually for quick verification
    asyncio.run(test_pipeline_counter_invariant_with_stub_llm())
    print("Single tick test passed!")
    asyncio.run(test_pipeline_counter_invariant_multiple_ticks())
    print("Multi-tick test passed!")