"""
Test for verify_new_article_counters() - ensures the four new-article
LLM buckets are mutually exclusive and sum to new_today.

Each new article lands in EXACTLY ONE bucket:
  llm_new_ok            - LLM classified successfully
  llm_new_failed        - LLM call failed (503 / timeout / malformed)
  llm_new_skipped_breaker - skipped because the circuit breaker was open
  llm_new_skipped_batch  - skipped because the per-tick batch cap was hit
"""
import pytest
from app.services.pipeline import IngestStats


def _make(new_today, ok, failed, batch, breaker):
    stats = IngestStats()
    stats.new_today = new_today
    stats.llm_new_ok = ok
    stats.llm_new_failed = failed
    stats.llm_new_skipped_batch = batch
    stats.llm_new_skipped_breaker = breaker
    return stats


def test_verify_new_article_counters_all_ok():
    """All articles processed successfully with LLM."""
    assert _make(10, 10, 0, 0, 0).verify_new_article_counters() is True


def test_verify_new_article_counters_all_failed():
    """All articles failed LLM classification (real failures, no breaker)."""
    assert _make(10, 0, 10, 0, 0).verify_new_article_counters() is True


def test_verify_new_article_counters_all_breaker():
    """All articles skipped because the circuit breaker was open.

    A breaker skip is its own bucket - it is NOT also counted in
    llm_new_failed, so the four buckets stay mutually exclusive.
    """
    assert _make(10, 0, 0, 0, 10).verify_new_article_counters() is True


def test_verify_new_article_counters_mixed():
    """Mix of ok, failed, breaker-skip and batch-capped articles."""
    assert _make(20, 8, 7, 2, 3).verify_new_article_counters() is True


def test_verify_new_article_counters_large_tick():
    """25 new articles spread across all four buckets."""
    assert _make(25, 10, 8, 4, 3).verify_new_article_counters() is True


def test_verify_new_article_counters_mismatch():
    """Counters that don't add up should return False."""
    # Total = 5+3+1 = 9 != 10
    assert _make(10, 5, 3, 1, 0).verify_new_article_counters() is False


def test_verify_new_article_counters_double_count_detected():
    """A breaker skip counted in BOTH failed and breaker must fail.

    This is the regression test for the original double-counting bug:
    10 new articles, 5 real failures + 5 breaker skips, but the breaker
    skips were ALSO added to llm_new_failed (failed=10, breaker=5).
    The naive sum 0+10+0+5 = 15 != 10, so the invariant catches it.
    """
    assert _make(10, 0, 10, 0, 5).verify_new_article_counters() is False


def test_verify_new_article_counters_zero():
    """Zero new articles should pass."""
    assert _make(0, 0, 0, 0, 0).verify_new_article_counters() is True


def test_buckets_are_mutually_exclusive_for_real_counting():
    """Mirror the exact branch logic in _ingest_article.

    For a stream of new articles with known (llm_ok, skip_reason) and
    use_llm flags, the per-article bucket increments must place every
    article in exactly one bucket and the four buckets must sum to the
    number of new articles.
    """
    # (use_llm, llm_ok, skip_reason)
    articles = [
        (True,  True,  None),           # ok
        (True,  True,  None),           # ok
        (True,  False, "error"),        # failed
        (True,  False, "timeout"),      # failed
        (True,  False, "circuit_breaker"),  # breaker skip
        (True,  False, "circuit_breaker"),  # breaker skip
        (False, False, "batch_cap"),    # batch cap
        (False, False, "batch_cap"),    # batch cap
        (False, False, "batch_cap"),    # batch cap
    ]
    stats = IngestStats()
    stats.new_today = len(articles)
    for use_llm, llm_ok, skip_reason in articles:
        if use_llm:
            if llm_ok:
                stats.llm_new_ok += 1
            elif skip_reason == "circuit_breaker":
                stats.llm_new_skipped_breaker += 1
            else:
                stats.llm_new_failed += 1
        else:
            stats.llm_new_skipped_batch += 1

    # 2 ok + 2 failed + 3 batch + 2 breaker = 9 == new_today
    assert stats.llm_new_ok == 2
    assert stats.llm_new_failed == 2
    assert stats.llm_new_skipped_batch == 3
    assert stats.llm_new_skipped_breaker == 2
    assert stats.verify_new_article_counters() is True
    # And the raw sum of all four buckets equals the article count.
    assert (stats.llm_new_ok + stats.llm_new_failed +
            stats.llm_new_skipped_batch +
            stats.llm_new_skipped_breaker) == stats.new_today


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
