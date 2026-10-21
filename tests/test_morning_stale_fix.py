"""
The morning STALE banner: root-cause fix and its regression tests.

The banner "STALE: no successful pipeline tick in the last 15 minutes.
Dashboard showing carried-forward data." appeared every morning on this
machine. The chain was:

  1. The freshness stamp was written ONCE per tick, right after ingestion.
     A boot tick whose LLM classification phase outlasts HEALTH_STALE_MINUTES
     therefore looked stale for the whole back half of its run.
  2. The morning brief and the first tick started at the SAME instant, so
     they fought over SQLite's single writer: "database is locked" on the
     tick's inserts, and the brief's commits stalled on the 30s busy timeout.
  3. The brief's final pass ran a full pipeline tick ON the event loop,
     which blocked /api/health for minutes.
  4. The watchdog treats a 503 /api/health as "the engine is dead" (because
     Invoke-WebRequest throws on 4xx/5xx and the catch block cannot tell a
     503 answer from a refused connection) - so it killed the engine exactly
     when the pipeline reported itself stale, resetting the tick clock and
     looping.

These tests pin the fix for each link in that chain.
"""

from __future__ import annotations

import datetime as dt
import http.server
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.services import pipeline

_PS1 = Path(__file__).resolve().parent.parent / "tools" / "dashboard_service.ps1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ago(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def stamp_file(tmp_path, monkeypatch):
    """Redirect the persisted freshness stamp to a temp file.

    Without this the tests would overwrite the real
    data/last_successful_tick.txt of the running engine.
    """
    target = tmp_path / "last_successful_tick.txt"
    monkeypatch.setattr(pipeline, "_LAST_TICK_FILE", target)
    return target


@pytest.fixture
def pipeline_state():
    """Snapshot/restore the pipeline's global tick + pause state."""
    saved = (
        pipeline._last_successful_tick,
        pipeline._last_update,
        pipeline._last_heartbeat_mono,
        pipeline._running,
        pipeline._tick_started_at,
        pipeline._engine_paused,
    )
    yield
    (
        pipeline._last_successful_tick,
        pipeline._last_update,
        pipeline._last_heartbeat_mono,
        pipeline._running,
        pipeline._tick_started_at,
        pipeline._engine_paused,
    ) = saved


# ---------------------------------------------------------------------------
# 1) The freshness stamp is a HEARTBEAT, not a one-shot event
# ---------------------------------------------------------------------------

def test_refresh_tick_stamp_advances_the_stamp(stamp_file, pipeline_state):
    """A heartbeat must move the stamp forward and persist it."""
    pipeline._last_successful_tick = _ago(600).isoformat()

    pipeline._refresh_tick_stamp(force=True)

    assert pipeline._last_successful_tick is not None
    assert dt.datetime.fromisoformat(pipeline._last_successful_tick) > _ago(60)
    assert stamp_file.read_text(encoding="utf-8").strip() == \
        pipeline._last_successful_tick


def test_refresh_tick_stamp_is_throttled(stamp_file, pipeline_state):
    """A heartbeat inside the throttle window must not move the stamp.

    Without the throttle a 600-article batch would mean 600 file writes.
    """
    pipeline._last_successful_tick = _ago(600).isoformat()
    first = pipeline._last_successful_tick

    pipeline._refresh_tick_stamp(force=True)
    moved = pipeline._last_successful_tick
    assert moved != first

    # Second call without force - inside TICK_HEARTBEAT_SECONDS, so no move.
    pipeline._refresh_tick_stamp()
    assert pipeline._last_successful_tick == moved


def test_slow_but_alive_tick_is_not_stale(stamp_file, pipeline_state):
    """THE regression.

    A tick that has been running for 20 minutes (its LLM phase is slow) but
    is demonstrably alive - it heartbeated a minute ago - must NOT be
    reported as stale. Before the fix the stamp was frozen at the start of
    the tick, so exactly this state raised the red banner.
    """
    pipeline._running = True
    pipeline._tick_started_at = _ago(20 * 60)
    pipeline._last_successful_tick = _ago(60).isoformat()

    stale, reason = pipeline.compute_stale(pipeline.HEALTH_STALE_MINUTES)
    assert stale is False
    assert reason is None


def test_dead_tick_still_goes_stale(stamp_file, pipeline_state):
    """The heartbeat must not make a genuinely dead engine look healthy.

    A tick that stopped heartbeating (crashed, killed, hung) keeps aging
    and goes stale on its own.
    """
    pipeline._running = False
    pipeline._tick_started_at = None
    pipeline._last_successful_tick = _ago(
        pipeline.HEALTH_STALE_MINUTES * 60 + 60).isoformat()

    stale, reason = pipeline.compute_stale(pipeline.HEALTH_STALE_MINUTES)
    assert stale is True
    assert reason == "no_successful_tick"


def test_heartbeat_keeps_health_endpoint_ok(client, stamp_file, pipeline_state):
    """The health endpoint must agree: a heartbeat keeps it 200/ok."""
    pipeline._running = True
    pipeline._tick_started_at = _ago(18 * 60)
    pipeline._last_successful_tick = _ago(30).isoformat()

    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is False
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# 2) A deliberately PAUSED engine is not a STALE engine
# ---------------------------------------------------------------------------

def test_paused_engine_is_not_stale(client, pipeline_state):
    """The operator stopping the engine is a decision, not a failure.

    The dashboard already shows a PAUSED banner for it; raising STALE on
    top of that is noise, and on a machine paused overnight it is a
    permanent false alarm.
    """
    pipeline._running = False
    pipeline._tick_started_at = None
    pipeline._last_successful_tick = _ago(10 * 3600).isoformat()
    pipeline.set_paused(True)
    try:
        stale, reason = pipeline.compute_stale(pipeline.HEALTH_STALE_MINUTES)
        assert stale is False
        assert reason is None

        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["stale"] is False
    finally:
        pipeline.set_paused(False)


def test_unpausing_restores_the_stale_rule(pipeline_state):
    """The pause exemption must not leak: once resumed, the rule applies."""
    pipeline._running = False
    pipeline._tick_started_at = None
    pipeline._last_successful_tick = _ago(
        pipeline.HEALTH_STALE_MINUTES * 60 + 60).isoformat()

    pipeline.set_paused(True)
    assert pipeline.compute_stale(pipeline.HEALTH_STALE_MINUTES)[0] is False
    pipeline.set_paused(False)
    assert pipeline.compute_stale(pipeline.HEALTH_STALE_MINUTES)[0] is True


# ---------------------------------------------------------------------------
# 3) Boot order: prime tick FIRST, then the morning brief
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_sequence_runs_tick_before_brief(monkeypatch):
    """The brief judges headlines that must ALREADY be in the database.

    It can only do that after the first tick's ingest, so the two must run
    in that order - and never at the same time, because they share one
    SQLite writer.
    """
    order: list[str] = []

    async def fake_tick():
        order.append("tick")

    async def fake_brief():
        order.append("brief")

    monkeypatch.setattr(main, "_scheduled_tick", fake_tick)
    monkeypatch.setattr(main, "_boot_morning_brief", fake_brief)
    monkeypatch.setattr(main.settings, "morning_brief_on_boot", True)

    await main._boot_sequence()

    assert order == ["tick", "brief"]


@pytest.mark.asyncio
async def test_boot_sequence_skips_brief_when_paused(monkeypatch):
    """A paused engine must not spend 30 minutes of LLM on a catch-up."""
    order: list[str] = []

    async def fake_tick():
        order.append("tick")

    async def fake_brief():
        order.append("brief")

    monkeypatch.setattr(main, "_scheduled_tick", fake_tick)
    monkeypatch.setattr(main, "_boot_morning_brief", fake_brief)
    monkeypatch.setattr(main.settings, "morning_brief_on_boot", True)
    monkeypatch.setattr(main, "_engine_paused", True)
    try:
        await main._boot_sequence()
    finally:
        monkeypatch.setattr(main, "_engine_paused", False)

    assert order == ["tick"]


@pytest.mark.asyncio
async def test_boot_sequence_never_raises(monkeypatch):
    """A failing prime tick must not stop the API from serving."""

    async def boom():
        raise RuntimeError("fetch failed")

    monkeypatch.setattr(main, "_scheduled_tick", boom)
    monkeypatch.setattr(main.settings, "morning_brief_on_boot", True)

    await main._boot_sequence()   # must not raise


class _FakeScheduler:
    """Minimal stand-in for APScheduler's job pause/resume API."""

    def __init__(self):
        self.paused = False
        self.resumed = False

    def pause_job(self, job_id):
        self.paused = True

    def resume_job(self, job_id):
        self.paused = False
        self.resumed = True


def test_scheduler_job_stays_paused_during_the_boot_sequence(monkeypatch):
    """A scheduled tick must never interleave with the morning brief."""
    fake = _FakeScheduler()
    monkeypatch.setattr(main, "_scheduler", fake)
    monkeypatch.setattr(main, "_boot_in_progress", True)
    monkeypatch.setattr(main, "_engine_paused", False)

    main._apply_pause_to_scheduler()
    assert fake.paused is True
    assert fake.resumed is False

    # ...and it is handed back once the boot sequence is done.
    monkeypatch.setattr(main, "_boot_in_progress", False)
    main._apply_pause_to_scheduler()
    assert fake.paused is False
    assert fake.resumed is True


def test_scheduler_job_stays_paused_while_the_engine_is_paused(monkeypatch):
    """A paused engine keeps the job paused."""
    fake = _FakeScheduler()
    monkeypatch.setattr(main, "_scheduler", fake)
    monkeypatch.setattr(main, "_boot_in_progress", False)
    monkeypatch.setattr(main, "_engine_paused", True)

    main._apply_pause_to_scheduler()
    assert fake.paused is True
    assert fake.resumed is False


# ---------------------------------------------------------------------------
# 3b) The brief's final pass must not block the event loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_brief_final_pass_runs_off_the_event_loop(monkeypatch):
    """A full tick on the event loop makes /api/health stop answering.

    The brief's final pass used to ``await pipeline.run_once()`` directly,
    which runs the whole tick (fetch + LLM classification + sentiment
    recompute) ON the event loop - minutes of blocked loop, long enough for
    the watchdog to declare the server dead and kill it mid-brief.
    """
    import threading
    from app.services import overnight, pipeline

    caller_thread = threading.get_ident()
    seen: dict[str, int] = {}

    async def fake_run_once():
        seen["thread"] = threading.get_ident()
        return pipeline.IngestStats(fetched=1, new_today=1)

    monkeypatch.setattr(pipeline, "run_once", fake_run_once)

    stats = await overnight._run_pipeline_off_loop()

    assert stats.new_today == 1
    assert seen["thread"] != caller_thread, (
        "the brief's final pass must run in a worker thread, not on the "
        "event loop"
    )


@pytest.mark.asyncio
async def test_brief_waits_for_an_in_flight_tick(tmp_path, monkeypatch):
    """The brief must not interleave with a running tick.

    They are both writers on one SQLite file; interleaving them produced
    "database is locked" on the tick's inserts and stalled the brief's
    commits on the 30s busy timeout.
    """
    import json

    from app.services import overnight, pipeline

    # Retarget every file the brief writes into the temp dir.
    state = tmp_path / "state"
    reports = tmp_path / "reports"
    state.mkdir()
    reports.mkdir()
    monkeypatch.setattr(overnight, "SHUTDOWN_MARKER",
                        state / "overnight_boundary.json")
    monkeypatch.setattr(overnight, "BRIEF_STATE",
                        state / "morning_brief_state.json")
    monkeypatch.setattr(overnight, "BRIEF_LOCK", state / "morning_brief.lock")
    monkeypatch.setattr(overnight, "REPORT_DIR", reports)
    (state / "overnight_boundary.json").write_text(
        json.dumps({"recorded_at_utc": _ago(8 * 3600).isoformat(), "pid": 0}),
        encoding="utf-8")

    order: list[str] = []

    async def fake_idle(timeout_seconds=None):
        order.append("idle")
        return True

    async def fake_brief(**kwargs):
        order.append("brief")
        return {
            "status": "completed",
            "window": {},
            "headlines": [],
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    async def fake_run_once():
        order.append("final_pass")
        return pipeline.IngestStats(fetched=1, new_today=1)

    monkeypatch.setattr(overnight, "_await_pipeline_idle", fake_idle)
    monkeypatch.setattr(overnight, "run_morning_brief", fake_brief)
    monkeypatch.setattr(pipeline, "run_once", fake_run_once)
    monkeypatch.setattr(overnight.settings, "morning_brief_startup_grace_seconds", 0)

    report = await overnight.run_boot_catchup(now=dt.datetime.now(dt.timezone.utc))

    assert report["status"] == "completed"
    assert order[:2] == ["idle", "brief"], (
        f"the brief must wait for the pipeline to be idle first, got {order}"
    )


# ---------------------------------------------------------------------------
# 3c) An orphaned lock must not block the morning brief
# ---------------------------------------------------------------------------

def test_orphaned_lock_is_taken_over_immediately(tmp_path):
    """A lock left behind by a KILLED engine must not block the brief.

    The lock file records the PID that created it. When that process is
    gone the lock is orphaned and can be taken over at once - the engine is
    killed and replaced whenever the watchdog misreads a 503, and the next
    process then had to skip the morning brief for the whole 30-minute
    staleness window, so the overnight gap was never judged.
    """
    from app.services import overnight

    lock_path = tmp_path / "morning_brief.lock"
    # A PID that is guaranteed not to be running.
    lock_path.write_text("999999999", encoding="utf-8")

    guard = overnight._Lock(lock_path)
    assert guard.acquire() is True
    guard.release()


def test_live_lock_is_not_taken_over(tmp_path):
    """A lock held by a running process must still be respected."""
    from app.services import overnight

    lock_path = tmp_path / "morning_brief.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    guard = overnight._Lock(lock_path)
    assert guard.acquire() is False


# ---------------------------------------------------------------------------
# 4) The watchdog must not kill an engine that answers 503
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Stub503(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"degraded","stale":true}')

    def log_message(self, *args):
        pass


def _extract_ps_function(source: str, name: str) -> str:
    """Pull one function (and its doc comment) out of the .ps1 source."""
    marker = f"function {name} {{"
    start = source.index(marker)
    # Skip a <# ... #> block comment so its braces are not counted.
    body_start = source.index("{", start)
    depth = 0
    for idx in range(body_start, len(source)):
        if source[idx] == "{":
            depth += 1
        elif source[idx] == "}":
            depth -= 1
            if depth == 0:
                return source[start:idx + 1]
    raise AssertionError(f"could not extract {name} from the script")


@pytest.mark.skipif(shutil.which("powershell") is None,
                    reason="PowerShell not available")
def test_watchdog_treats_a_503_health_response_as_alive():
    """/api/health answers 503 when the pipeline is stale.

    The server is ALIVE then - it just says the pipeline is behind. The
    watchdog used to read that as "the engine is dead" (Invoke-WebRequest
    throws on 503 and the catch block could not tell a 503 answer from a
    refused connection), so it killed a healthy engine every time the
    freshness window lapsed - which is what restarted the tick and kept
    the banner up.
    """
    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Stub503)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = _PS1.read_text(encoding="utf-8")
        func = _extract_ps_function(source, "Test-DashboardHealthy")
        strict = _extract_ps_function(source, "Test-DashboardHealthyStrict")
        script = (
            "$ErrorActionPreference='Stop'\n"
            f"$HealthUrl = 'http://127.0.0.1:{port}/api/health'\n"
            f"{func}\n{strict}\n"
            "Write-Output (\"alive=\" + (Test-DashboardHealthy -TimeoutSec 5))\n"
            "Write-Output (\"strict=\" + (Test-DashboardHealthyStrict -TimeoutSec 5))\n"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        out = result.stdout
        assert "alive=True" in out, (
            f"the watchdog must treat a 503 answer as a live engine; got: {out!r}"
        )
        # Strict mode is the "Status" report: 503 is not "ok", so it stays False.
        assert "strict=False" in out, out
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(shutil.which("powershell") is None,
                    reason="PowerShell not available")
def test_watchdog_still_restarts_a_dead_engine():
    """A connection failure must still read as dead - the fix is not a blanket
    'never restart'."""
    port = _free_port()   # nothing is listening here
    source = _PS1.read_text(encoding="utf-8")
    func = _extract_ps_function(source, "Test-DashboardHealthy")
    script = (
        "$ErrorActionPreference='Stop'\n"
        f"$HealthUrl = 'http://127.0.0.1:{port}/api/health'\n"
        f"{func}\n"
        "Write-Output (\"alive=\" + (Test-DashboardHealthy -TimeoutSec 3))\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "alive=False" in result.stdout, result.stdout
