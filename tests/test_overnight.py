"""
Tests for the overnight catch-up ("morning brief").

Covers the off-window resolution (shutdown marker, database fallback, cap,
minimum gap), the LLM classification pass (write-back, skip rules, circuit
breaker, heuristic mode, dry-run), the report banding, and idempotency.

All file paths are retargeted into ``tmp_path`` so the tests never touch the
real ``data/`` directory, and the LLM call is stubbed so no provider request
is ever made.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.analysis import llm_client
from app.analysis.news_classifier import ArticleAnalysis, AssetAnalysis
from app.config import settings
from app.database import session_scope
from app.models.db_models import (
    ArticleEvent,
    AssetImpact,
    NewsArticle,
    NewsEvent,
    PipelineRun,
)
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
    Verification,
)
from app.services import overnight

NOW = datetime(2026, 10, 12, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def paths(tmp_path, monkeypatch):
    """Retarget every file the brief writes into the test's temp dir."""
    state = tmp_path / "state"
    reports = tmp_path / "reports"
    state.mkdir()
    reports.mkdir()
    monkeypatch.setattr(overnight, "SHUTDOWN_MARKER", state / "overnight_boundary.json")
    monkeypatch.setattr(overnight, "BRIEF_STATE", state / "morning_brief_state.json")
    monkeypatch.setattr(overnight, "BRIEF_LOCK", state / "morning_brief.lock")
    monkeypatch.setattr(overnight, "REPORT_DIR", reports)
    return {"state": state, "reports": reports}


@pytest.fixture()
def llm_on(monkeypatch):
    """Pretend the LLM is configured (property + its two inputs)."""
    monkeypatch.setattr(type(settings), "has_llm", True, raising=False)
    monkeypatch.setattr(settings, "llm_enabled", True, raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)


def _stub_analysis(importance: EconomicImportance = EconomicImportance.HIGH,
                   asset: Asset = Asset.GOLD,
                   direction: Direction = Direction.BULLISH) -> ArticleAnalysis:
    analysis = ArticleAnalysis(
        category=NewsCategory.CENTRAL_BANK,
        importance=importance,
        report_kind=ReportKind.GENERAL,
        verification=Verification.CONFIRMED,
        event_title="stubbed event",
        event_summary="stubbed summary",
        relevance_score=0.85,
        mode="llm",
        note="stubbed classification",
    )
    analysis.assets = [AssetAnalysis(
        asset=asset,
        direction=direction,
        strength=ImpactStrength.HIGH,
        relevance=0.8,
        persistence=0.7,
        reason="stub reason",
        transmission="policy -> yields -> asset",
        metal_character=None,
        speaker_tier=1.0,
    )]
    return analysis


def install_stub(monkeypatch, importance=EconomicImportance.HIGH):
    """Patch the module-level LLM entry point (no provider is contacted)."""
    calls: list[str] = []

    async def fake_analyze_article(title: str, body: str = ""):
        calls.append(title)
        return _stub_analysis(importance), True, "ok"

    monkeypatch.setattr(overnight, "analyze_article", fake_analyze_article)
    return calls


def add_article(session, *, minutes_ago: float, title: str = "Overnight headline",
                importance: str = "LOW", llm_used: bool = False,
                published: bool = True, impacts: bool = True,
                guid: str | None = None) -> NewsArticle:
    """One stored article (optionally with an old heuristic asset_impact)."""
    when = NOW - timedelta(minutes=minutes_ago)
    row = NewsArticle(
        guid=guid or f"guid-{title[:20]}-{minutes_ago}",
        source="TestWire",
        source_quality="TIER_2_MAJOR",
        url=f"http://example.test/{abs(hash(title)) % 9999}",
        title=title,
        body="body text about the fed and gold",
        summary="",
        # created_at is the ingest time: rows without a publish time are
        # placed in the window by it, so it must sit inside the window too.
        created_at=when,
        published_at_utc=(when.replace(tzinfo=None) if published else None),
        is_today=True,
        category="Other",
        importance=importance,
        analysis_mode="heuristic" if not llm_used else "llm",
        llm_used=llm_used,
    )
    session.add(row)
    session.flush()
    if impacts:
        session.add(AssetImpact(
            article_id=row.id,
            asset="XAUUSD",
            direction="NEUTRAL",
            strength="LOW",
            relevance=0.1,
            persistence=0.5,
            reason="old heuristic verdict",
            transmission="none",
        ))
    session.commit()
    session.refresh(row)
    return row


def write_marker(paths, when: datetime) -> None:
    (paths["state"] / "overnight_boundary.json").write_text(
        json.dumps({"recorded_at_utc": when.isoformat(), "pid": 0}),
        encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit: banding
# ---------------------------------------------------------------------------
def test_impact_band_maps_four_levels_to_three():
    assert overnight.impact_band("VERY_HIGH") == "HIGH"
    assert overnight.impact_band("HIGH") == "HIGH"
    assert overnight.impact_band("MEDIUM") == "MEDIUM"
    assert overnight.impact_band("LOW") == "LOW"
    assert overnight.impact_band("garbage") == "LOW"
    assert overnight.impact_band(None) == "LOW"


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------
def test_window_uses_the_shutdown_marker(paths):
    write_marker(paths, NOW - timedelta(hours=8))
    with session_scope() as session:
        window = overnight.resolve_window(session, now=NOW)
    assert window is not None
    assert window.source == "shutdown_marker"
    assert window.gap_hours == pytest.approx(8.0, abs=0.01)
    assert not window.capped


def test_window_falls_back_to_the_last_pipeline_run(paths):
    with session_scope() as session:
        session.add(PipelineRun(
            started_at=NOW - timedelta(hours=6, minutes=5),
            finished_at=NOW - timedelta(hours=6),
            status="ok",
        ))
        session.commit()
        window = overnight.resolve_window(session, now=NOW)
    assert window is not None
    assert window.source == "last_pipeline_run"
    assert window.gap_hours == pytest.approx(6.0, abs=0.01)


def test_window_falls_back_to_the_newest_article(paths):
    with session_scope() as session:
        session.add(PipelineRun(started_at=NOW - timedelta(hours=30)))
        session.commit()
        add_article(session, minutes_ago=300)
        window = overnight.resolve_window(session, now=NOW)
    assert window is not None
    assert window.source == "newest_article"


def test_empty_database_has_no_window(paths):
    with session_scope() as session:
        assert overnight.resolve_window(session, now=NOW) is None


def test_gap_below_the_minimum_is_not_an_overnight(paths, monkeypatch):
    monkeypatch.setattr(settings, "morning_brief_min_gap_minutes", 90,
                        raising=False)
    write_marker(paths, NOW - timedelta(minutes=30))
    with session_scope() as session:
        assert overnight.resolve_window(session, now=NOW) is None


def test_window_is_capped_at_the_maximum_gap(paths, monkeypatch):
    monkeypatch.setattr(settings, "overnight_max_gap_hours", 24, raising=False)
    write_marker(paths, NOW - timedelta(hours=40))
    with session_scope() as session:
        window = overnight.resolve_window(session, now=NOW)
    assert window is not None and window.capped
    assert window.gap_hours == pytest.approx(24.0, abs=0.01)
    assert window.contains(NOW - timedelta(hours=23))


def test_future_dated_marker_is_ignored(paths):
    write_marker(paths, NOW + timedelta(hours=2))
    with session_scope() as session:
        session.add(PipelineRun(started_at=NOW - timedelta(hours=3),
                                finished_at=NOW - timedelta(hours=3),
                                status="ok"))
        session.commit()
        window = overnight.resolve_window(session, now=NOW)
    assert window is not None
    assert window.source == "last_pipeline_run"
    assert window.gap_hours == pytest.approx(3.0, abs=0.01)


def test_collect_articles_respects_window_bounds(paths):
    write_marker(paths, NOW - timedelta(hours=8))
    with session_scope() as session:
        window = overnight.resolve_window(session, now=NOW)
        add_article(session, minutes_ago=60, title="inside window")
        add_article(session, minutes_ago=60 * 12, title="outside window")
        rows = overnight.collect_articles(session, window)
    titles = [r.title for r in rows]
    assert "inside window" in titles
    assert "outside window" not in titles


def test_articles_without_a_publish_time_are_kept(paths):
    write_marker(paths, NOW - timedelta(hours=8))
    with session_scope() as session:
        window = overnight.resolve_window(session, now=NOW)
        add_article(session, minutes_ago=30, title="no publish time",
                    published=False, impacts=False)
        rows = overnight.collect_articles(session, window)
    assert [r.title for r in rows] == ["no publish time"]


# ---------------------------------------------------------------------------
# Classification pass
# ---------------------------------------------------------------------------
def test_llm_verdict_is_written_back(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    calls = install_stub(monkeypatch)
    with session_scope() as session:
        article = add_article(session, minutes_ago=60)
        report = asyncio.run(overnight.run_morning_brief(now=NOW, session=session))

    assert report["status"] == "completed"
    assert calls and len(calls) == 1
    with session_scope() as verify:
        row = verify.get(NewsArticle, article.id)
        assert row.llm_used is True
        assert row.importance == "HIGH"
        assert row.analysis_mode == "llm"
        impacts = list(row.impacts)
        assert len(impacts) == 1
        assert impacts[0].asset == "XAUUSD"
        assert impacts[0].direction == "BULLISH"
        assert impacts[0].strength == "HIGH"
        assert impacts[0].final_weight > 0


def test_llm_verdict_upgrades_the_event_cluster(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    install_stub(monkeypatch, importance=EconomicImportance.VERY_HIGH)
    with session_scope() as session:
        article = add_article(session, minutes_ago=60, impacts=False)
        event = NewsEvent(event_key="evt-1", canonical_headline="old",
                          importance="LOW", article_count=1)
        session.add(event)
        session.flush()
        session.add(ArticleEvent(article_id=article.id, event_id=event.id))
        session.commit()
        event_id = event.id

        asyncio.run(overnight.run_morning_brief(now=NOW, session=session))
        refreshed = session.get(NewsEvent, event_id)
    assert refreshed.importance == "VERY_HIGH"


def test_already_llm_rows_are_skipped_by_default(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    calls = install_stub(monkeypatch)
    with session_scope() as session:
        add_article(session, minutes_ago=60, llm_used=True, importance="HIGH")
        report = asyncio.run(overnight.run_morning_brief(now=NOW, session=session))
    assert not calls
    assert report["llm"]["kept_stored"] == 1
    assert report["llm"]["llm_judged"] == 0


def test_all_flag_rejudges_llm_rows(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    calls = install_stub(monkeypatch)
    with session_scope() as session:
        add_article(session, minutes_ago=60, llm_used=True, importance="MEDIUM")
        report = asyncio.run(overnight.run_morning_brief(
            now=NOW, session=session, rejudge_all=True))
    assert calls
    assert report["llm"]["llm_judged"] == 1
    assert report["llm"]["kept_stored"] == 0


async def test_breaker_open_stops_spending(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    install_stub(monkeypatch)
    # Open the real breaker instead of patching the property: monkeypatching
    # an instance attribute over a class property leaks past teardown.
    breaker = llm_client.circuit_breaker()
    await breaker.reset()
    for _ in range(3):
        await breaker.record_failure()
    assert breaker.is_open
    try:
        with session_scope() as session:
            add_article(session, minutes_ago=60)
            add_article(session, minutes_ago=70)
            report = await overnight.run_morning_brief(
                now=NOW, session=session)
        assert report["llm"]["breaker_stopped"] is True
        assert report["llm"]["llm_judged"] == 0
        assert report["llm"]["kept_stored"] == 2
    finally:
        await breaker.reset()


def test_heuristic_mode_writes_nothing_but_still_reports(paths, monkeypatch):
    monkeypatch.setattr(type(settings), "has_llm", False, raising=False)
    write_marker(paths, NOW - timedelta(hours=8))
    with session_scope() as session:
        add_article(session, minutes_ago=60, importance="MEDIUM")
        add_article(session, minutes_ago=70, importance="LOW")
        report = asyncio.run(overnight.run_morning_brief(now=NOW, session=session))
    assert report["status"] == "completed"
    assert report["llm"]["enabled"] is False
    assert report["totals"] == {"articles": 2, "HIGH": 0, "MEDIUM": 1, "LOW": 1}


def test_dry_run_changes_no_rows(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    install_stub(monkeypatch)
    with session_scope() as session:
        article = add_article(session, minutes_ago=60)
        report = asyncio.run(overnight.run_morning_brief(
            now=NOW, session=session, dry_run=True))
        row = session.get(NewsArticle, article.id)
    assert report["status"] == "dry_run"
    assert row.llm_used is False
    assert row.importance == "LOW"


# ---------------------------------------------------------------------------
# Idempotency + report
# ---------------------------------------------------------------------------
def test_second_run_is_skipped_until_forced(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    calls = install_stub(monkeypatch)
    with session_scope() as session:
        add_article(session, minutes_ago=60)
        first = asyncio.run(overnight.run_morning_brief(now=NOW, session=session))
        second = asyncio.run(overnight.run_morning_brief(now=NOW, session=session))
        # --force re-runs, but a row the model already judged is not re-sent
        # unless --all is given as well.
        third = asyncio.run(overnight.run_morning_brief(now=NOW, session=session,
                                                        force=True))
        fourth = asyncio.run(overnight.run_morning_brief(
            now=NOW, session=session, force=True, rejudge_all=True))
    assert first["status"] == "completed"
    assert second["status"] == "skipped"
    assert second["reason"] == "already_covered"
    assert third["status"] == "completed"
    assert third["llm"]["kept_stored"] == 1
    assert fourth["llm"]["llm_judged"] == 1
    assert len(calls) == 2  # judged once, then once more under --all


def test_report_bands_and_per_asset(paths):
    write_marker(paths, NOW - timedelta(hours=8))
    window = overnight.OvernightWindow(
        start=NOW - timedelta(hours=8), end=NOW, source="shutdown_marker",
        gap_hours=8.0)
    with session_scope() as session:
        high = add_article(session, minutes_ago=60, importance="VERY_HIGH")
        medium = add_article(session, minutes_ago=120, importance="MEDIUM")
        low = add_article(session, minutes_ago=180, importance="LOW")
        session.refresh(high)
        outcome = overnight.ClassificationOutcome()
        report = overnight.build_report(
            window, [high, medium, low], outcome, dry_run=False,
            rejudge_all=False, calendar=[], now=NOW)
    assert report["totals"]["HIGH"] == 1     # VERY_HIGH bands up
    assert report["totals"]["MEDIUM"] == 1
    assert report["totals"]["LOW"] == 1
    assert report["totals"]["articles"] == 3
    bucket = report["per_asset"]["XAUUSD"]
    assert bucket["high"] == 1 and bucket["medium"] == 1 and bucket["low"] == 1
    assert bucket["neutral"] == 3


def test_render_text_and_report_file(paths, llm_on, monkeypatch):
    write_marker(paths, NOW - timedelta(hours=8))
    install_stub(monkeypatch)
    with session_scope() as session:
        add_article(session, minutes_ago=60)
        report = asyncio.run(overnight.run_morning_brief(now=NOW, session=session))
    text = overnight.render_text(report)
    assert "MORNING BRIEF" in text
    assert "HIGH IMPACT" in text
    path = paths["reports"] / "morning_brief_2026-10-12.json"
    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["totals"]["articles"] == 1


def test_render_text_for_a_skipped_run():
    text = overnight.render_text({"status": "skipped", "reason": "no_overnight_gap"})
    assert "skipped" in text and "no_overnight_gap" in text


def test_calendar_context_includes_window_events(paths, monkeypatch):
    from app.analysis.economic_impact import CalendarEvent
    from app.ingestion import calendar_cache

    write_marker(paths, NOW - timedelta(hours=8))
    window = overnight.OvernightWindow(
        start=NOW - timedelta(hours=8), end=NOW, source="shutdown_marker",
        gap_hours=8.0)
    events = [
        CalendarEvent(title="US CPI y/y", country="USD",
                      when_utc=NOW - timedelta(hours=2),
                      impact="High", forecast=3.0, previous=3.1, actual=3.4),
        CalendarEvent(title="Old release", country="EUR",
                      when_utc=NOW - timedelta(days=3), impact="Low"),
    ]
    monkeypatch.setattr(calendar_cache, "load_cache", lambda: events)
    rows = overnight.calendar_in_window(window)
    assert [r["title"] for r in rows] == ["US CPI y/y"]
    assert rows[0]["released"] is True
    assert rows[0]["actual"] == 3.4


# ---------------------------------------------------------------------------
# Boot sequence (run_boot_catchup)
# ---------------------------------------------------------------------------
def test_boot_catchup_runs_a_final_pipeline_pass(paths, llm_on, monkeypatch):
    from app.services import pipeline

    write_marker(paths, NOW - timedelta(hours=8))
    install_stub(monkeypatch)
    calls = {"run_once": 0}

    async def fake_run_once():
        calls["run_once"] += 1
        return pipeline.IngestStats(fetched=3, new_today=1)

    monkeypatch.setattr(pipeline, "run_once", fake_run_once)

    with session_scope() as session:
        add_article(session, minutes_ago=60)
        report = asyncio.run(overnight.run_boot_catchup(
            now=NOW, startup_grace_seconds=0))

    assert report["status"] == "completed"
    assert calls["run_once"] == 1
    assert report["final_pass"]["new_today"] == 1
    assert report["final_pass"]["errors"] == 0
    assert not (paths["state"] / "morning_brief.lock").exists()


def test_boot_catchup_skips_without_a_gap(paths):
    write_marker(paths, NOW - timedelta(minutes=30))
    report = asyncio.run(overnight.run_boot_catchup(now=NOW))
    assert report["status"] == "skipped"
    assert report["reason"] == "no_overnight_gap"
    assert "final_pass" not in report


def test_boot_catchup_refuses_to_double_spend(paths):
    # A lock that is not stale (fresh mtime) AND is held by a live process
    # must be respected: the second runner declines rather than pay for the
    # same window twice. The lock file records the holder's PID - a PID that
    # is no longer running means the lock was orphaned by a killed engine
    # and can be taken over at once (see test_orphaned_lock_is_taken_over
    # _immediately).
    import os as _os

    (paths["state"] / "morning_brief.lock").write_text(str(_os.getpid()))
    report = asyncio.run(overnight.run_boot_catchup(now=NOW))
    assert report["status"] == "locked"


def test_stale_lock_is_stolen(paths):
    lock = paths["state"] / "morning_brief.lock"
    lock.write_text("12345")
    import os as _os

    _os.utime(lock, (0, 0))  # epoch mtime -> older than the stale window
    (paths["state"] / "overnight_boundary.json").write_text(json.dumps(
        {"recorded_at_utc": (NOW - timedelta(hours=8)).isoformat()}))
    acquired = overnight._Lock(path=lock).acquire()
    try:
        assert acquired is True
    finally:
        overnight._Lock(path=lock).release()


# ---------------------------------------------------------------------------
# Shutdown marker bookkeeping
# ---------------------------------------------------------------------------
def test_record_shutdown_writes_the_marker(paths):
    overnight.record_shutdown(now=NOW - timedelta(hours=9))
    assert (paths["state"] / "overnight_boundary.json").exists()
    with session_scope() as session:
        window = overnight.resolve_window(session, now=NOW)
    assert window is not None and window.source == "shutdown_marker"


def test_missing_marker_and_empty_db_never_raises(paths):
    with session_scope() as session:
        assert overnight.resolve_window(session, now=NOW) is None
