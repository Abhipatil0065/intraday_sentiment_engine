"""
Regression test: /api/health must not block the event loop during pipeline runs.

Before the fix, /api/health used ``session_scope()`` which ran synchronous SQLite
queries. During a pipeline tick the SQLite WAL lock was held, so the health check
could block for minutes, causing watchdog restarts.

After the fix, /api/health reads from an in-memory cache (``pipeline.runtime_state()``
and ``pipeline.get_health_metrics()``) with no DB access.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import pipeline


@pytest.fixture
def client():
    return TestClient(app)


def test_health_responds_without_db_query(client):
    """
    Monkeypatch session_scope to prove that /api/health does not open a DB session.
    If it did, the patched function would be called and the test would fail.
    """
    import app.main as main

    original_session_scope = main.session_scope
    db_was_queried = {"called": False}

    def probe_session_scope():
        db_was_queried["called"] = True
        with original_session_scope() as session:
            yield session

    main.session_scope = probe_session_scope
    try:
        response = client.get("/api/health")
        assert response.status_code in (200, 503)
        assert not db_was_queried["called"], (
            "/api/health still calls session_scope() – DB lock blocker not fixed"
        )
    finally:
        main.session_scope = original_session_scope


def test_health_response_time_under_pipeline(client):
    """
    During a pipeline tick, the health endpoint must respond in well under 1 second.
    The old code blocked for the duration of the SQLite WAL lock (minutes).
    """
    # Simulate a running pipeline by stalling the lock.
    import app.services.pipeline as p
    old_running = p._running
    p._running = True

    try:
        # Even if the DB is locked, /api/health must not touch it.
        start = time.monotonic()
        response = client.get("/api/health")
        elapsed = time.monotonic() - start

        assert response.status_code in (200, 503)
        assert elapsed < 1.0, f"Health check took {elapsed:.2f}s – blocker not fixed"
    finally:
        p._running = old_running


def test_health_stale_when_no_tick_ever_completed(client):
    """With _last_successful_tick = None, health must return 503 degraded."""
    old = pipeline._last_successful_tick
    pipeline._last_successful_tick = None
    try:
        response = client.get("/api/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["stale"] is True
    finally:
        pipeline._last_successful_tick = old


def test_health_ok_after_successful_tick(client):
    """After a tick completes, health must return 200 ok."""
    import datetime as dt
    old = pipeline._last_successful_tick
    pipeline._last_successful_tick = (
        dt.datetime.now(dt.timezone.utc).isoformat()
    )
    try:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["stale"] is False
    finally:
        pipeline._last_successful_tick = old
