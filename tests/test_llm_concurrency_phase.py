"""Tests for the concurrent LLM pre-classification phase (added 2026-10-21).

The tick's model allowance is wall-clock, not a count, and one call costs
20-40s on the live endpoint. A sequential loop therefore spent the whole
90s budget on 2-5 articles even though LLM_MAX_CONCURRENT slots were
configured and never used. Phase A classifies the LLM-bound articles
concurrently, phase B ingests the results.

These tests pin:
  * the candidates are the first N NEW articles of the sorted batch;
  * the calls really do overlap (N articles take ~1 call time, not N);
  * an article whose call was cut off by the deadline is NOT retried;
  * a verdict computed in phase A is the one phase B uses.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.ingestion.base import RawArticle
from app.services.pipeline import _preclassify_for_llm


def _article(title: str, minutes_ago: int = 1) -> RawArticle:
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return RawArticle(
        guid=f"guid-{title}", source="TestWire", source_quality="TIER_2_MAJOR",
        url=f"https://example.test/{title}", title=title, body="body text",
        published_raw=stamp.isoformat(),
    ).ensure_guid()


def _settings(**overrides):
    class _S:
        has_llm = True
        llm_batch_limit = 15
        llm_tick_budget_seconds = 90.0
    for key, value in overrides.items():
        setattr(_S, key, value)
    return _S


class _FakeSession:
    """Just enough session for the suppressed-guid preload."""

    def scalars(self, *args, **kwargs):
        return self

    def all(self):
        return []


async def _test_phase_a_overlaps_calls():
    """Four 0.4s calls in parallel must not take 1.6s."""
    calls: list[str] = []

    async def fake_analyze(title: str, body: str = ""):
        calls.append(title)
        await asyncio.sleep(0.4)
        return ("ANALYSIS", True, "ok")

    articles = [_article(f"a{i}") for i in range(4)]

    with patch("app.services.pipeline.settings", _settings()):
        with patch("app.analysis.news_classifier.analyze_article", fake_analyze):
            started = time.monotonic()
            results = await _preclassify_for_llm(
                articles, _FakeSession(), set(), 15, 90.0)
            elapsed = time.monotonic() - started

    assert len(calls) == 4
    assert len(results) == 4
    assert all(v is not None for v in results.values())
    # Sequential would be 1.6s; concurrent with 4 slots is ~0.4s.
    assert elapsed < 1.0, f"calls did not overlap (took {elapsed:.2f}s)"


async def _test_phase_a_respects_the_batch_limit():
    """Only the first N articles of the batch may be sent to the model."""
    calls: list[str] = []

    async def fake_analyze(title: str, body: str = ""):
        calls.append(title)
        return ("ANALYSIS", True, "ok")

    articles = [_article(f"a{i}") for i in range(10)]

    with patch("app.services.pipeline.settings", _settings()):
        with patch("app.analysis.news_classifier.analyze_article", fake_analyze):
            results = await _preclassify_for_llm(
                articles, _FakeSession(), set(), 3, 90.0)

    assert len(calls) == 3
    assert calls == ["a0", "a1", "a2"]
    assert len(results) == 3


async def _test_phase_a_skips_known_guids():
    """Articles the tick already has must not be sent to the model."""
    calls: list[str] = []

    async def fake_analyze(title: str, body: str = ""):
        calls.append(title)
        return ("ANALYSIS", True, "ok")

    articles = [_article("known"), _article("fresh")]

    with patch("app.services.pipeline.settings", _settings()):
        with patch("app.analysis.news_classifier.analyze_article", fake_analyze):
            results = await _preclassify_for_llm(
                articles, _FakeSession(), {"guid-known"}, 15, 90.0)

    assert calls == ["fresh"]
    assert "guid-known" not in results
    assert results["guid-fresh"][0] == "ANALYSIS"


async def _test_deadline_cut_off_is_not_retried():
    """A candidate cut off by the deadline is marked, not left for phase B.

    Phase B must be able to tell "we ran out of time" from "the model said
    no", because the first must not be paid for twice.
    """
    async def slow_analyze(title: str, body: str = ""):
        await asyncio.sleep(5)
        return ("ANALYSIS", True, "ok")

    articles = [_article("slow")]

    with patch("app.services.pipeline.settings", _settings()):
        with patch("app.analysis.news_classifier.analyze_article", slow_analyze):
            results = await _preclassify_for_llm(
                articles, _FakeSession(), set(), 15, 0.3)

    assert "guid-slow" in results
    assert results["guid-slow"] is None


async def _test_no_llm_configured_is_a_noop():
    """With the model disabled, phase A must not touch anything."""

    async def fake_analyze(title: str, body: str = ""):
        raise AssertionError("must not call the provider")

    articles = [_article("a0")]

    with patch("app.services.pipeline.settings", _settings(has_llm=False)):
        with patch("app.analysis.news_classifier.analyze_article", fake_analyze):
            results = await _preclassify_for_llm(
                articles, _FakeSession(), set(), 15, 90.0)

    assert results == {}


def test_phase_a_overlaps_calls():
    asyncio.run(_test_phase_a_overlaps_calls())


def test_phase_a_respects_the_batch_limit():
    asyncio.run(_test_phase_a_respects_the_batch_limit())


def test_phase_a_skips_known_guids():
    asyncio.run(_test_phase_a_skips_known_guids())


def test_deadline_cut_off_is_not_retried():
    asyncio.run(_test_deadline_cut_off_is_not_retried())


def test_no_llm_configured_is_a_noop():
    asyncio.run(_test_no_llm_configured_is_a_noop())


def test_cooldown_after_a_dead_provider():
    """A tick where the provider answers nothing must cool the next one down.

    Otherwise every tick spends its whole 90s budget on an endpoint that is
    down: the tick runs long, produces nothing, and hammers the provider.
    """
    from app.services import pipeline

    async def dead_analyze(title: str, body: str = ""):
        return ("HEURISTIC", False, "error")

    async def _run():
        pipeline._llm_cooldown_until = 0.0
        articles = [_article("a0")]
        with patch("app.services.pipeline.settings", _settings()):
            with patch("app.analysis.news_classifier.analyze_article", dead_analyze):
                first = await pipeline._preclassify_for_llm(
                    articles, _FakeSession(), set(), 15, 90.0)
                cooled = pipeline._llm_cooldown_until > 0

                async def forbidden(title: str, body: str = ""):
                    raise AssertionError("must not call the provider")

                with patch("app.analysis.news_classifier.analyze_article",
                           forbidden):
                    second = await pipeline._preclassify_for_llm(
                        articles, _FakeSession(), set(), 15, 90.0)
        pipeline._llm_cooldown_until = 0.0
        return first, cooled, second

    first, cooled, second = asyncio.run(_run())

    assert first, "the first tick must still try the provider"
    assert cooled, "a tick with no verdict must start a cooldown"
    assert second == {}, "the next tick must not spend a call while cooling down"


def test_semaphore_survives_a_new_event_loop():
    """The concurrency semaphore must be rebound when the loop changes.

    The scheduler runs each tick in a fresh event loop (asyncio.run() in a
    worker thread). An asyncio.Semaphore binds to the loop that first had to
    WAIT on it, so a cached one used from the next tick's loop raises
    "... is bound to a different event loop" - which the classifier reports as
    a provider failure for a request that never left the process.
    """
    from app.analysis import llm_client, news_classifier

    async def _contend(limit: int) -> int:
        """Run more callers than the semaphore allows, so it has to wait."""
        sem = news_classifier._llm_semaphore()
        counter = {"now": 0, "peak": 0}

        async def _one():
            async with sem:
                counter["now"] += 1
                counter["peak"] = max(counter["peak"], counter["now"])
                await asyncio.sleep(0.05)
                counter["now"] -= 1

        await asyncio.gather(*[_one() for _ in range(limit)])
        return counter["peak"]

    # First loop: the semaphore blocks and binds to this loop.
    peak_one = asyncio.run(_contend(6))
    assert peak_one <= news_classifier.LLM_MAX_CONCURRENT
    assert peak_one > 1, "the semaphore never blocked, so nothing was proven"

    # Second loop: the cached semaphore belongs to the FIRST loop. It must be
    # rebound, not reused - otherwise every waiting caller raises.
    peak_two = asyncio.run(_contend(6))
    assert peak_two <= news_classifier.LLM_MAX_CONCURRENT
    assert peak_two > 1

    # The same guard applies to the client-side semaphore.
    async def _client_contend() -> int:
        sem = llm_client._semaphore_fixed()
        async with sem:
            return 1

    asyncio.run(_client_contend())
    asyncio.run(_client_contend())
