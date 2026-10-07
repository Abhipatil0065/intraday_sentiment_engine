"""
Test for verify_new_article_counters() - ensures LLM buckets sum to new_today.
"""
import pytest
from app.services.pipeline import IngestStats


def test_verify_new_article_counters_all_ok():
    """All articles processed successfully with LLM."""
    stats = IngestStats()
    stats.new_today = 10
    stats.llm_new_ok = 10
    stats.llm_new_failed = 0
    stats.llm_new_skipped_batch = 0
    stats.llm_new_skipped_breaker = 0
    assert stats.verify_new_article_counters() is True


def test_verify_new_article_counters_all_failed():
    """All articles failed LLM classification."""
    stats = IngestStats()
    stats.new_today = 10
    stats.llm_new_ok = 0
    stats.llm_new_failed = 10
    stats.llm_new_skipped_batch = 0
    stats.llm_new_skipped_breaker = 5  # subset of failed
    assert stats.verify_new_article_counters() is True


def test_verify_new_article_counters_mixed():
    """Mix of OK, failed, and batch-capped articles."""
    stats = IngestStats()
    stats.new_today = 20
    stats.llm_new_ok = 8
    stats.llm_new_failed = 7
    stats.llm_new_skipped_batch = 5
    stats.llm_new_skipped_breaker = 3  # subset of failed
    assert stats.verify_new_article_counters() is True


def test_verify_new_article_counters_large_tick():
    """Test with 25 new articles (simulating a large tick)."""
    stats = IngestStats()
    stats.new_today = 25
    stats.llm_new_ok = 10
    stats.llm_new_failed = 10
    stats.llm_new_skipped_batch = 5
    stats.llm_new_skipped_breaker = 4  # subset of failed
    assert stats.verify_new_article_counters() is True


def test_verify_new_article_counters_mismatch():
    """Counters that don't add up should return False."""
    stats = IngestStats()
    stats.new_today = 10
    stats.llm_new_ok = 5
    stats.llm_new_failed = 3
    stats.llm_new_skipped_batch = 1
    stats.llm_new_skipped_breaker = 0
    # Total = 5+3+1 = 9 != 10
    assert stats.verify_new_article_counters() is False


def test_verify_new_article_counters_zero():
    """Zero new articles should pass."""
    stats = IngestStats()
    stats.new_today = 0
    stats.llm_new_ok = 0
    stats.llm_new_failed = 0
    stats.llm_new_skipped_batch = 0
    stats.llm_new_skipped_breaker = 0
    assert stats.verify_new_article_counters() is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])