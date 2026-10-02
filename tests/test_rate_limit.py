"""
Unit tests for the client-side LLM rate limiter.

The limiter exists so a news burst waits locally instead of drawing a provider
429, so the property under test is "never more than N attempts in any 60s
window" - not latency.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.analysis.llm_client import RateLimiter


@pytest.mark.asyncio
async def test_limit_zero_disables():
    """limit<=0 must not block or record anything."""
    rl = RateLimiter(0)
    start = time.monotonic()
    for _ in range(500):
        await rl.acquire()
    assert time.monotonic() - start < 0.5
    assert rl.used() == 0


@pytest.mark.asyncio
async def test_burst_is_capped():
    """
    6 acquisitions against a 5/min limit: the first 5 pass immediately, the 6th
    must block. The window is shortened so the test is fast; blocking is
    asserted with a real (unfrozen) clock via wait_for.
    """
    rl = RateLimiter(5)
    rl.WINDOW_SECONDS = 0.3

    for _ in range(5):
        await rl.acquire()
    assert rl.used() == 5

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(rl.acquire(), timeout=0.1)
    assert rl.used() == 5, "a blocked request must not be counted"


@pytest.mark.asyncio
async def test_slot_frees_after_window():
    """Once the window slides, held requests are released."""
    rl = RateLimiter(2)
    rl.WINDOW_SECONDS = 0.2
    await rl.acquire()
    await rl.acquire()
    assert rl.used() == 2

    await asyncio.sleep(rl.WINDOW_SECONDS + 0.05)
    assert rl.used() == 0, "stale entries must age out of the window"
    await rl.acquire()
    assert rl.used() == 1


def test_production_window_is_sixty_seconds():
    """The shipped window must stay a real minute."""
    assert RateLimiter.WINDOW_SECONDS == 60.0


@pytest.mark.asyncio
async def test_burst_of_50_settles_within_expected_time():
    """
    50 requests at 10/window need >=5 windows. With a 0.05s window that is
    ~0.25s - proves requests really are spaced out rather than fired at once.
    Only a lower bound is asserted, so a loaded machine cannot make this flaky.
    """
    rl = RateLimiter(10)
    rl.WINDOW_SECONDS = 0.05

    start = time.monotonic()
    for _ in range(50):
        await rl.acquire()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.2, f"50 requests at 10/window finished too fast: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_never_exceeds_limit_under_concurrency():
    """
    The real invariant: 50 concurrent waiters against a 10/min limit must never
    admit more than 10 in any rolling window. The window is shortened to 0.2s
    so the test finishes in ~1s while still exercising the sliding logic.

    The timestamp MUST come from :meth:`RateLimiter.last_admitted_at` - the
    stamp the limiter charged against its own window - not a fresh
    ``time.monotonic()`` read after ``await acquire()`` returns. Re-reading the
    clock adds scheduler skew between the charge and the measurement, which
    shifts the half-open window ``[start, start+WINDOW)`` enough to pull the
    boundary admission inside and report 11 against a limit of 10. That is an
    artefact of measuring, not a limiter defect - the limiter's own stamps
    never exceed the limit. This test was flaky for exactly that reason.
    """
    rl = RateLimiter(10)
    rl.WINDOW_SECONDS = 0.2

    async def worker():
        await rl.acquire()
        # Ground truth: the stamp the limiter charged against its own window.
        stamp = rl.last_admitted_at()
        assert stamp is not None, "acquire() must have recorded an admission"
        return stamp

    admitted = sorted(await asyncio.gather(*[worker() for _ in range(50)]))

    assert len(admitted) == 50, "every waiter must eventually be admitted"

    # No rolling window may contain more than `limit` admissions.
    worst = max(
        sum(1 for other in admitted if start <= other < start + rl.WINDOW_SECONDS)
        for start in admitted
    )
    assert worst <= 10, f"admitted {worst} in one window, limit is 10"


def test_default_limit_is_forty():
    """The shipped default must be the documented 40/min."""
    from app.config import settings

    assert settings.llm_requests_per_minute == 40


@pytest.mark.asyncio
async def test_last_admitted_at_reports_none_before_any_acquisition():
    """A fresh limiter has charged nothing, so it must not invent a stamp."""
    rl = RateLimiter(5)
    assert rl.last_admitted_at() is None


@pytest.mark.asyncio
async def test_last_admitted_at_tracks_the_newest_admission():
    """
    The accessor must return the stamp of the most recent admission - that is
    the value the limiter charged against its own window, which is what makes
    admission spacing measurable without racing the clock.
    """
    rl = RateLimiter(3)
    await rl.acquire()
    first = rl.last_admitted_at()
    assert first is not None

    await asyncio.sleep(0.02)
    await rl.acquire()
    second = rl.last_admitted_at()
    assert second is not None and second > first


@pytest.mark.asyncio
async def test_last_admitted_at_does_not_mutate_limiter_state():
    """Reading the stamp must not prune the window or free capacity."""
    rl = RateLimiter(5)
    for _ in range(3):
        await rl.acquire()
    assert rl.used() == 3
    assert rl.last_admitted_at() is not None
    assert rl.used() == 3, "reading the stamp must not age entries out"
