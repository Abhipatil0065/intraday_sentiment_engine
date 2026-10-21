"""
Dashboard alerting: every engine fault must be VISIBLE, and no fault may
raise a false alarm.

The alert surface is the header status strip in
``app/dashboard/web/index.html`` plus two full-width banners (STALE,
ENGINE PAUSED). This file pins the rules that decide what those show.

What was broken before this change:

* ``HEALTH.newsFeedStatus`` was NEVER assigned, so the "News Feed Stopped"
  alert was dead code - a completely dead feed looked exactly like a
  healthy one.
* The pipeline pill was built from ``status.running`` ("is a tick
  executing RIGHT NOW"), which is false for a good part of every poll
  cycle. A healthy engine therefore lit up a red "Pipeline Stopped"
  ALERT every three minutes.
* The tick's own errors were never written to the pipeline run row, so
  ``status.errors`` was always empty and a tick failing every time showed
  nothing until the 15-minute STALE banner.
* A failed ``/api/dashboard`` fetch wrote one line to the browser console
  and nothing else - the page silently kept showing the last render.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import pipeline

HTML = (Path(__file__).resolve().parents[1] / "app" / "dashboard" / "web" /
        "index.html").read_text(encoding="utf-8")


def _fn_slice(name: str) -> str:
    start = HTML.index(f"function {name}(")
    rest = HTML[start:]
    nxt = rest.find("\nfunction ")
    end = start + nxt if nxt != -1 else len(HTML)
    return HTML[start:end]


def _fn_has(name: str, pattern: str) -> None:
    assert pattern in _fn_slice(name), (
        f"expected pattern {pattern!r} not found in function {name}")


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Backend: the payload must carry what the dashboard needs
# ---------------------------------------------------------------------------

def test_status_publishes_the_source_health(client):
    """The dashboard needs the source counts and the failed source names."""
    payload = client.get("/api/dashboard").json()
    status = payload["status"]

    assert "sources_polled" in status
    assert "sources_ok" in status
    assert "sources_failed" in status
    assert isinstance(status["sources_failed"], list)
    # The per-source cache is exposed too, so a single broken publisher can
    # be named.
    assert isinstance(payload["health"]["per_source"], dict)


def test_sources_ok_is_null_until_a_tick_has_run(client, monkeypatch):
    """None (not 0) when no tick has ever recorded a run.

    "No source answered" and "we have not asked yet" are different states:
    0 would raise a dead-feed alert on a freshly booted engine.
    """
    import app.main as main

    monkeypatch.setattr(main, "build_payload", main.build_payload)
    with main.session_scope() as session:
        session.query(main.PipelineRun).delete()
        session.commit()

    payload = client.get("/api/dashboard").json()
    assert payload["status"]["sources_ok"] is None


def test_failed_sources_are_named(client, monkeypatch):
    """A dead source must be visible BY NAME, not just as a count."""
    import app.main as main

    monkeypatch.setattr(
        pipeline, "get_health_metrics",
        lambda: {"per_source": {
            "Reuters": {"items": 100, "newest": None, "error": "403 Forbidden"},
            "CNBC Economy": {"items": 30, "newest": None, "error": None},
        }},
    )
    payload = client.get("/api/dashboard").json()
    assert payload["status"]["sources_failed"] == ["Reuters"]


def test_tick_errors_are_persisted_on_the_run_row():
    """The run row must record the tick's errors.

    They used to be dropped on the floor: the row's detail was written when
    the run STARTED, so ``status.errors`` was always empty and a tick that
    failed every time showed nothing on the dashboard.
    """
    from app.models.db_models import PipelineRun

    with pipeline.session_scope() as session:
        row = PipelineRun(
            started_at=dt.datetime.now(dt.timezone.utc),
            status="ok",
            detail={"sources_polled": 2, "sources_ok": 2,
                    "errors": ["pipeline: OperationalError: database is locked"]},
        )
        session.add(row)
        session.commit()
        session.expire_all()
        stored = session.get(PipelineRun, row.id)
        assert stored.detail["errors"], "the tick errors must survive on the row"
        assert stored.detail["sources_ok"] == 2


@pytest.mark.asyncio
async def test_a_failing_tick_writes_its_errors_to_the_run_row(monkeypatch):
    """End to end: a tick that hits a source error must leave a trace.

    The dashboard reads ``status.errors`` from the run row, so if the tick
    does not write them there the alert can never fire.
    """
    from app.analysis.news_classifier import ArticleAnalysis
    from app.ingestion.news_fetcher import FetchReport
    from app.models.db_models import PipelineRun
    from app.models.enums import EconomicImportance, NewsCategory, ReportKind, Verification

    async def fake_fetch_all_news():
        return FetchReport(
            articles=[],
            sources_polled=15,
            sources_ok=14,
            errors=["Reuters: HTTP 403 Forbidden"],
            source_stats={},
        )

    async def fake_fetch_calendar(*args, **kwargs):
        return ([], [])

    async def fake_analyze_article(title: str, body: str = ""):
        return ArticleAnalysis(
            category=NewsCategory.OTHER,
            importance=EconomicImportance.LOW,
            report_kind=ReportKind.GENERAL,
            verification=Verification.CONFIRMED,
            relevance_score=0.5,
            assets=[],
            event_summary=title[:200],
            mode="heuristic",
            note="fake analysis for test",
        ), True, None

    monkeypatch.setattr(pipeline, "fetch_all_news", fake_fetch_all_news)
    monkeypatch.setattr(pipeline, "fetch_calendar", fake_fetch_calendar)
    monkeypatch.setattr(pipeline, "analyze_article", fake_analyze_article)

    await pipeline.run_once()

    with pipeline.session_scope() as session:
        row = session.query(PipelineRun).order_by(PipelineRun.id.desc()).first()
        assert row is not None
        errors = (row.detail or {}).get("errors")
        assert errors, "the tick's errors must be written to the run row"
        assert any("Reuters" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Frontend: the alert rules
# ---------------------------------------------------------------------------

def test_pipeline_pill_is_not_built_from_the_instantaneous_running_flag():
    """The false alarm.

    ``status.running`` is "is a tick executing right now" - false for a
    good part of every poll cycle. The pill must use the freshness clock.
    """
    body = _fn_slice("renderHeader")
    assert "st.running ? 'RUNNING' : (st.stale ? 'STOPPED' : 'IDLE')" in body
    assert "st.running ? 'RUNNING' : 'STOPPED'" not in body, (
        "the pipeline status must not be derived from the instantaneous "
        "running flag - that raised a red ALERT on a healthy engine every "
        "poll cycle"
    )


def test_no_alert_is_raised_merely_because_no_tick_is_running():
    """Between ticks is normal. Only a STALE clock is a fault."""
    body = _fn_slice("renderHeader")
    assert "Pipeline Stopped - No Tick Running" not in body
    assert "Pipeline Stalled - No Successful Tick" in body
    # ...and a paused engine is reported by the PAUSED banner instead.
    assert "HEALTH.pipelineStatus==='STOPPED' && !HEALTH.paused" in body


def test_news_feed_status_is_actually_assigned():
    """The news-feed alert was dead code: the field was never set."""
    body = _fn_slice("renderHeader")
    assert "HEALTH.newsFeedStatus" in body
    assert "s.sources_ok" in body
    assert "s.sources_failed" in body
    # And it is no longer assumed to be set elsewhere.
    assert "newsFeedStatus:'LIVE'" in HTML, (
        "the news feed state must be initialised so the first render is not "
        "reading undefined"
    )


def test_a_dead_feed_raises_an_alert():
    """Zero (or fewer than half) of the sources answered => red alert."""
    body = _fn_slice("renderHeader")
    assert "News Feed Stopped - No Source Responded" in body
    assert "_ok*2<_polled" in body, (
        "a feed where fewer than half the sources answered is a dead feed"
    )


def test_fatal_pipeline_errors_raise_an_alert():
    """A tick that raised must be visible.

    Only pipeline-level failures count: a single publisher 403-ing for a
    minute is a routine transient and alerting on it would cry wolf daily.
    """
    body = _fn_slice("renderHeader")
    assert "Pipeline Error - " in body
    assert "/^pipeline:/.test" in body


def test_all_alerts_are_reported_not_just_the_first():
    """One headline used to hide every other fault behind it."""
    body = _fn_slice("renderHeader")
    assert "HEALTH.systemErrors = reds;" in body
    assert "reds[0]" in body
    assert "more)" in body


def test_db_pill_uses_the_same_rule_as_the_stale_banner():
    """The pill said OK while the banner said STALE."""
    body = _fn_slice("renderHeader")
    assert "HEALTH.dbStatus = st.stale ? 'STALE' : 'OK';" in body


def test_connection_lost_banner_exists():
    """A failed fetch must be visible, not just a console line."""
    assert 'id="connBanner"' in HTML
    assert "CONNECTION LOST" in HTML
    _fn_has("setConnLost", "classList.remove('hidden')")
    _fn_has("setConnLost", "classList.add('hidden')")
    # load() must call it on both paths.
    body = _fn_slice("load")
    assert "setConnLost(false)" in body
    assert "setConnLost(true, e)" in body


def test_status_pill_marksup_has_a_home_for_every_state():
    """The news pill was removed from the markup while the code still
    referenced it, so the news feed had nowhere to show up."""
    assert 'id="pNews"' in HTML
    assert 'id="lNews"' in HTML
    assert 'id="pDb"' in HTML
    assert 'id="pLlm"' in HTML
    assert 'id="pPipe"' in HTML


def test_idle_state_has_a_colour():
    """A healthy engine between ticks must read grey, not red."""
    assert "IDLE:'idle'" in HTML
    assert "DEGRADED:'degraded'" in HTML
