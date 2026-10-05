"""
Shared Chromium lock.

Measured, not assumed: running the news provider and the calendar provider
concurrently put 12 extra processes and ~1.0 GB of extra RSS on the machine at
peak (see ``tests/probe_chromium_overlap.py``). Two full browser renders in
flight at once is the single heaviest thing this service does, so the renders
are serialised through one lock.

The lock is an ASYNC LOCK, so waiting costs no thread and cannot deadlock the
event loop. It is a no-op unless two callers genuinely contend, and a waiter
gives up rather than holding the caller indefinitely - a calendar refresh is
worth less than the news fetch it is queued behind, and it will simply render
on the next poll.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.logging_conf import get_logger

log = get_logger("chromium_lock")

# Render wall-clock ceiling before a waiter stops waiting and skips its render.
DEFAULT_WAIT_SECONDS = 60.0

_lock: asyncio.Lock | None = None
_stats = {"acquired": 0, "waited": 0, "skipped": 0, "max_wait_seconds": 0.0}


def _get_lock() -> asyncio.Lock:
    """
    Return the process-wide lock.

    Created lazily and bound to the running loop on first use, because an
    ``asyncio.Lock`` constructed at import time can end up attached to a
    different loop than the one the app actually runs on.
    """
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


@asynccontextmanager
async def chromium_slot(wait_seconds: float = DEFAULT_WAIT_SECONDS):
    """
    Serialise one browser render against every other render in this process.

    ``yielded`` is True when the slot was actually obtained and False when the
    wait budget ran out, so the caller can SKIP its render and report it as a
    deferred poll rather than sit on a browser that is not coming back.
    """
    lock = _get_lock()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=wait_seconds)
    except asyncio.TimeoutError:
        _stats["skipped"] += 1
        log.warning("Chromium busy for %.0fs - skipping this render and "
                    "leaving it to the next poll.", wait_seconds)
        yield False
        return

    started = asyncio.get_event_loop().time()
    _stats["acquired"] += 1
    if _stats["acquired"] > 1:
        waited = asyncio.get_event_loop().time() - started
        _stats["waited"] += 1
        _stats["max_wait_seconds"] = max(_stats["max_wait_seconds"], 0.0)
        log.debug("Chromium slot acquired after queueing")
    try:
        yield True
    finally:
        lock.release()


def chromium_stats() -> dict:
    """Render-slot counters, for diagnostics and tests."""
    return dict(_stats)


def reset_lock_state() -> None:
    """Drop the lock so tests never inherit a held or contended lock."""
    global _lock
    _lock = None
    _stats.update({"acquired": 0, "waited": 0, "skipped": 0,
                   "max_wait_seconds": 0.0})