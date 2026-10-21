"""
Test for check_and_release_hung_tick() and health response in tick_hung state.
"""
import pytest
from datetime import datetime, timedelta, timezone
from app.services import pipeline


def test_check_and_release_hung_tick_not_running():
    """When no tick is running, function should return False."""
    # Reset state
    pipeline._running = False
    pipeline._tick_started_at = None
    
    result = pipeline.check_and_release_hung_tick()
    assert result is False


def test_check_and_release_hung_tick_within_limit():
    """When tick is running but within time limit, should return False."""
    pipeline._running = True
    pipeline._tick_started_at = datetime.now(timezone.utc) - timedelta(seconds=60)  # 1 minute ago
    
    try:
        result = pipeline.check_and_release_hung_tick()
        assert result is False
        assert pipeline._running is True  # Still running
        assert pipeline._tick_started_at is not None
    finally:
        pipeline._running = False
        pipeline._tick_started_at = None


def test_check_and_release_hung_tick_exceeds_limit():
    """A tick past the hard cap that has STOPPED heartbeating is wedged.

    Elapsed time alone is not enough - a tick with a slow LLM provider
    legitimately runs for 15+ minutes and keeps heartbeating. Releasing
    such a tick let a second tick start while the first was still writing
    to the same SQLite file, which is where the "database is locked"
    failures came from.
    """
    pipeline._running = True
    pipeline._tick_started_at = datetime.now(timezone.utc) - timedelta(seconds=pipeline.MAX_TICK_DURATION_SECONDS + 10)
    old_stamp = pipeline._last_successful_tick
    pipeline._last_successful_tick = (
        datetime.now(timezone.utc)
        - timedelta(seconds=pipeline.HEARTBEAT_STALE_SECONDS + 60)
    ).isoformat()

    try:
        result = pipeline.check_and_release_hung_tick()
        assert result is True
        assert pipeline._running is False  # Released
        assert pipeline._tick_started_at is None
    finally:
        pipeline._running = False
        pipeline._tick_started_at = None
        pipeline._last_successful_tick = old_stamp


def test_check_and_release_keeps_a_slow_but_working_tick():
    """A tick past the hard cap that is still heartbeating must be left alone."""
    pipeline._running = True
    pipeline._tick_started_at = datetime.now(timezone.utc) - timedelta(seconds=pipeline.MAX_TICK_DURATION_SECONDS + 10)
    old_stamp = pipeline._last_successful_tick
    pipeline._last_successful_tick = datetime.now(timezone.utc).isoformat()

    try:
        result = pipeline.check_and_release_hung_tick()
        assert result is False
        assert pipeline._running is True   # still working
        assert pipeline._tick_started_at is not None
    finally:
        pipeline._running = False
        pipeline._tick_started_at = None
        pipeline._last_successful_tick = old_stamp


def test_health_response_tick_hung_state():
    """Health endpoint should return 503 with stale_reason='tick_hung'.

    A tick is hung when it is past the hard cap AND has stopped
    heartbeating - a slow-but-working tick keeps heartbeating and stays
    healthy (see test_morning_stale_fix.py).
    """
    from app.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)

    # Simulate a hung tick
    old_stamp = pipeline._last_successful_tick
    pipeline._running = True
    pipeline._tick_started_at = datetime.now(timezone.utc) - timedelta(seconds=pipeline.MAX_TICK_DURATION_SECONDS + 10)
    pipeline._last_successful_tick = (
        datetime.now(timezone.utc)
        - timedelta(seconds=pipeline.HEARTBEAT_STALE_SECONDS + 60)
    ).isoformat()

    try:
        response = client.get("/api/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["stale"] is True
        assert body["stale_reason"] == "tick_hung"
    finally:
        pipeline._running = False
        pipeline._tick_started_at = None
        pipeline._last_successful_tick = old_stamp


def test_health_response_ok_when_not_hung():
    """Health endpoint should return 200 when tick is running but not hung."""
    from app.main import app
    from fastapi.testclient import TestClient
    
    client = TestClient(app)
    
    # Simulate a running tick (not hung)
    pipeline._running = True
    pipeline._tick_started_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    pipeline._last_successful_tick = datetime.now(timezone.utc).isoformat()
    
    try:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["stale"] is False
    finally:
        pipeline._running = False
        pipeline._tick_started_at = None


def test_health_response_no_tick_ever_completed():
    """Health endpoint should return 503 when no tick has ever completed and none running."""
    from app.main import app
    from fastapi.testclient import TestClient
    
    client = TestClient(app)
    
    old_tick = pipeline._last_successful_tick
    old_running = pipeline._running
    old_started = pipeline._tick_started_at
    
    pipeline._last_successful_tick = None
    pipeline._running = False
    pipeline._tick_started_at = None
    
    try:
        response = client.get("/api/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["stale"] is True
        assert body["stale_reason"] == "no_tick_completed"
    finally:
        pipeline._last_successful_tick = old_tick
        pipeline._running = old_running
        pipeline._tick_started_at = old_started


if __name__ == "__main__":
    pytest.main([__file__, "-v"])