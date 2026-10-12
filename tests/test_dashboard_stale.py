"""
The /api/dashboard payload must apply the SAME staleness rule as /api/health.

Regression: ``build_payload()`` used its own inline rule that flagged STALE
whenever ``last_successful_tick`` was None - including the first minutes
after a restart, while the startup priming tick was still legitimately in
flight. Users saw "STALE: no successful pipeline tick in the last 15
minutes. Dashboard showing carried-forward data." on a perfectly healthy,
freshly restarted engine whose data was live.

``/api/health`` already exempted that window. Both now share
``pipeline.compute_stale()``, and these tests pin the invariant that the two
endpoints can never disagree.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import pipeline


@pytest.fixture
def client():
    return TestClient(app)


def _set_state(last_tick, running, started_at):
    """Point the pipeline's in-memory tick state, returning a restore hook."""
    saved = (
        pipeline._last_successful_tick,
        pipeline._running,
        pipeline._tick_started_at,
    )
    pipeline._last_successful_tick = last_tick
    pipeline._running = running
    pipeline._tick_started_at = started_at

    def restore():
        (
            pipeline._last_successful_tick,
            pipeline._running,
            pipeline._tick_started_at,
        ) = saved

    return restore


def _ago(seconds):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# The regression itself: startup tick in flight is NOT stale
# ---------------------------------------------------------------------------

def test_dashboard_not_stale_while_first_tick_in_flight(client):
    """Fresh restart: priming tick running, nothing completed yet.

    This is exactly the state that used to raise the red STALE banner on a
    healthy engine. The dashboard must show live data, not carried-forward
    warnings, until the grace window lapses.
    """
    restore = _set_state(last_tick=None, running=True, started_at=_ago(60))
    try:
        payload = client.get("/api/dashboard").json()
        status = payload["status"]
        assert status["stale"] is False
        assert status["stale_reason"] is None
        # The frontend also renders the DB pill off last_poll_ok; it stays
        # empty (falsy) while no tick has completed, which is expected.
        assert status["last_poll_ok"] == ""
        assert payload["health"]["status"] == "ok"
    finally:
        restore()


def test_dashboard_stale_when_no_tick_running_and_none_completed(client):
    """No tick ever completed AND none running => genuinely stale."""
    restore = _set_state(last_tick=None, running=False, started_at=None)
    try:
        payload = client.get("/api/dashboard").json()
        status = payload["status"]
        assert status["stale"] is True
        assert status["stale_reason"] == "no_tick_completed"
    finally:
        restore()


def test_dashboard_stale_when_tick_hung(client):
    """Tick running past MAX_TICK_DURATION_SECONDS => stale even with a
    recently completed tick on record (max_instances=1 blocks the next tick)."""
    restore = _set_state(
        last_tick=_ago(60).isoformat(),
        running=True,
        started_at=_ago(pipeline.MAX_TICK_DURATION_SECONDS + 10),
    )
    try:
        payload = client.get("/api/dashboard").json()
        status = payload["status"]
        assert status["stale"] is True
        assert status["stale_reason"] == "tick_hung"
    finally:
        restore()


def test_dashboard_stale_when_last_successful_tick_too_old(client):
    """Last completed tick older than HEALTH_STALE_MINUTES => stale."""
    restore = _set_state(
        last_tick=(_ago(pipeline.HEALTH_STALE_MINUTES * 60 + 60)).isoformat(),
        running=False,
        started_at=None,
    )
    try:
        payload = client.get("/api/dashboard").json()
        status = payload["status"]
        assert status["stale"] is True
        assert status["stale_reason"] == "no_successful_tick"
    finally:
        restore()


def test_dashboard_not_stale_after_fresh_tick(client):
    """A recently completed tick => not stale."""
    restore = _set_state(last_tick=_ago(30).isoformat(), running=False, started_at=None)
    try:
        payload = client.get("/api/dashboard").json()
        status = payload["status"]
        assert status["stale"] is False
        assert status["stale_reason"] is None
    finally:
        restore()


def test_dashboard_tick_running_with_recent_completed_tick(client):
    """A healthy engine mid-tick (previous tick recently completed)."""
    restore = _set_state(last_tick=_ago(120).isoformat(), running=True, started_at=_ago(30))
    try:
        payload = client.get("/api/dashboard").json()
        status = payload["status"]
        assert status["stale"] is False
    finally:
        restore()


# ---------------------------------------------------------------------------
# The invariant the fix establishes: endpoints can never disagree
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "last_tick,running,started_at",
    [
        (None, True, _ago(60)),                     # startup tick in flight
        (None, False, None),                        # never ticked, nothing running
        (_ago(60).isoformat(), True, _ago(pipeline.MAX_TICK_DURATION_SECONDS + 10)),  # hung
        (_ago(20 * 60).isoformat(), False, None),   # last tick too old
        (_ago(30).isoformat(), False, None),        # fresh
        (_ago(120).isoformat(), True, _ago(30)),    # mid-tick, recent history
    ],
)
def test_dashboard_agrees_with_health(client, last_tick, running, started_at):
    """status.stale on the dashboard must equal (health == 503) for every state."""
    restore = _set_state(last_tick=last_tick, running=running, started_at=started_at)
    try:
        health = client.get("/api/health")
        payload = client.get("/api/dashboard").json()
        assert payload["status"]["stale"] is (health.status_code == 503), (
            "endpoints disagree: /api/health says "
            f"{health.status_code} but dashboard stale={payload['status']['stale']}"
        )
        # stale_reason is consistent with the flag
        if payload["status"]["stale"]:
            assert payload["status"]["stale_reason"] is not None
            assert payload["health"]["status"] == "degraded"
        else:
            assert payload["status"]["stale_reason"] is None
            assert payload["health"]["status"] == "ok"
    finally:
        restore()


# ---------------------------------------------------------------------------
# Unit tests on the shared helper itself
# ---------------------------------------------------------------------------

def test_compute_stale_parse_error():
    """A corrupt timestamp must degrade to stale, not crash the payload."""
    restore = _set_state(last_tick="not-an-iso-timestamp", running=False, started_at=None)
    try:
        stale, reason = pipeline.compute_stale()
        assert stale is True
        assert reason == "parse_error"
    finally:
        restore()


def test_health_stale_minutes_shared_constant():
    """app.main.HEALTH_STALE_MINUTES aliases the pipeline constant."""
    import app.main as main

    assert main.HEALTH_STALE_MINUTES == pipeline.HEALTH_STALE_MINUTES == 15
