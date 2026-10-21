"""
THE CALENDAR'S OWN POLL CADENCE.

``FJ_CALENDAR_POLL_SECONDS`` used to be a knob no code read: the FinancialJuice
calendar was re-rendered on every pipeline tick (180s by default) even though
the render costs 10-20 seconds and the site is Cloudflare-sensitive.

The calendar now has its own cadence, tightened to
``FJ_CALENDAR_BURST_SECONDS`` while a high-impact release sits inside its
window, so the actual number is picked up within a tick of the print instead
of waiting out the full interval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.economic_impact import CalendarEvent
from app.config import settings
from app.services import pipeline

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _event(title: str = "CPI y/y", when: datetime | None = None,
           impact: str = "High") -> CalendarEvent:
    return CalendarEvent(title=title, country="USD",
                         when_utc=when if when is not None else NOW,
                         impact=impact, forecast=1.0, previous=1.0, actual=1.0,
                         provider="financialjuice")


@pytest.fixture(autouse=True)
def _restore_state():
    """The cadence helpers read module state; put it back afterwards."""
    events = pipeline._calendar_events
    last = pipeline._calendar_last_fetch_utc
    yield
    pipeline._calendar_events = events
    pipeline._calendar_last_fetch_utc = last


def test_the_knob_is_read_and_has_a_default():
    assert settings.fj_calendar_poll_seconds > 0
    assert settings.fj_calendar_burst_seconds > 0


def test_a_cadence_of_zero_means_every_tick(monkeypatch):
    monkeypatch.setattr(settings, "fj_calendar_poll_seconds", 0, raising=False)
    monkeypatch.setattr(pipeline, "_calendar_events", [_event()])
    monkeypatch.setattr(pipeline, "_calendar_last_fetch_utc", NOW)
    due, wait, cadence = pipeline._calendar_poll_due(NOW)
    assert due is True
    assert cadence == 0


def test_the_calendar_is_not_refetched_before_its_cadence(monkeypatch):
    monkeypatch.setattr(settings, "fj_calendar_poll_seconds", 600,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_burst_seconds", 60,
                        raising=False)
    # A Medium release is not a market-moving print, so no burst window.
    monkeypatch.setattr(pipeline, "_calendar_events",
                        [_event(impact="Medium")])
    monkeypatch.setattr(pipeline, "_calendar_last_fetch_utc", NOW)

    due, wait, cadence = pipeline._calendar_poll_due(
        NOW + timedelta(seconds=120))
    assert due is False
    assert wait == pytest.approx(480, abs=2)
    assert cadence == 600


def test_the_calendar_is_refetched_once_the_cadence_has_elapsed(monkeypatch):
    monkeypatch.setattr(settings, "fj_calendar_poll_seconds", 600,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_burst_seconds", 60,
                        raising=False)
    monkeypatch.setattr(pipeline, "_calendar_events",
                        [_event(impact="Medium")])
    monkeypatch.setattr(pipeline, "_calendar_last_fetch_utc", NOW)

    due, wait, cadence = pipeline._calendar_poll_due(
        NOW + timedelta(seconds=700))
    assert due is True
    assert wait == 0


def test_the_first_poll_is_always_due(monkeypatch):
    monkeypatch.setattr(settings, "fj_calendar_poll_seconds", 600,
                        raising=False)
    monkeypatch.setattr(pipeline, "_calendar_events", [])
    monkeypatch.setattr(pipeline, "_calendar_last_fetch_utc", None)

    due, _wait, _cadence = pipeline._calendar_poll_due(NOW)
    assert due is True


def test_a_release_inside_its_window_tightens_the_cadence(monkeypatch):
    """The actual number must not wait out the full poll interval."""
    monkeypatch.setattr(settings, "fj_calendar_poll_seconds", 600,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_burst_seconds", 60,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_pre_release_minutes", 15,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_post_release_minutes", 30,
                        raising=False)

    # A high-impact release five minutes away.
    imminent = _event("FOMC Rate Decision", when=NOW + timedelta(minutes=5))
    monkeypatch.setattr(pipeline, "_calendar_events", [imminent])
    monkeypatch.setattr(pipeline, "_calendar_last_fetch_utc", NOW)

    # 30s after the last poll: still inside the burst cadence of 60s.
    due, wait, cadence = pipeline._calendar_poll_due(
        NOW + timedelta(seconds=30))
    assert cadence == 60
    assert due is False
    assert wait == pytest.approx(30, abs=2)

    # 90s after it: the burst cadence has elapsed, so the calendar is due.
    due, _wait, _cadence = pipeline._calendar_poll_due(
        NOW + timedelta(seconds=90))
    assert due is True


def test_a_release_outside_its_window_keeps_the_normal_cadence(monkeypatch):
    monkeypatch.setattr(settings, "fj_calendar_poll_seconds", 600,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_burst_seconds", 60,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_pre_release_minutes", 15,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_post_release_minutes", 30,
                        raising=False)

    # Six hours away - not in the window.
    later = _event("FOMC Rate Decision", when=NOW + timedelta(hours=6))
    monkeypatch.setattr(pipeline, "_calendar_events", [later])
    monkeypatch.setattr(pipeline, "_calendar_last_fetch_utc", NOW)

    _due, _wait, cadence = pipeline._calendar_poll_due(
        NOW + timedelta(seconds=120))
    assert cadence == 600


def test_a_medium_impact_release_does_not_open_the_burst_window(monkeypatch):
    monkeypatch.setattr(settings, "fj_calendar_burst_seconds", 60,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_pre_release_minutes", 15,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_post_release_minutes", 30,
                        raising=False)

    soon = _event("Retail Sales m/m", when=NOW + timedelta(minutes=2),
                  impact="Medium")
    monkeypatch.setattr(pipeline, "_calendar_events", [soon])
    monkeypatch.setattr(pipeline, "_calendar_last_fetch_utc", NOW)

    assert pipeline._calendar_release_window(NOW) is False
