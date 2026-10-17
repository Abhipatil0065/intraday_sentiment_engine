"""
Tests for the same-day economic calendar filter.

The dashboard calendar must show every event scheduled for today in market
timezone - released and upcoming - and nothing from other days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.economic_impact import (
    CalendarEvent,
    resolve_calendar_signals,
    to_calendar_summary,
)
from app.models.enums import Asset

# Pinned clock for deterministic "today" boundaries.
from tests.fixed_clock import FIXED_NOW as NOW, freeze_now


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    freeze_now(monkeypatch)


def _event(when_utc: datetime, **kwargs) -> CalendarEvent:
    base = dict(
        title="CPI y/y",
        country="USD",
        when_utc=when_utc,
        impact="High",
        forecast=3.0,
        previous=2.9,
    )
    base.update(kwargs)
    return CalendarEvent(**base)


def _today_rows(when_utc: datetime, now_utc: datetime, actual=None):
    """Run the filter + summary pipeline for one event, return dashboard rows."""
    event = _event(when_utc=when_utc, actual=actual)
    signals = resolve_calendar_signals([event], horizon_hours=48, now=now_utc)
    return to_calendar_summary(signals, Asset.GOLD)


def test_today_released_events_appear():
    """A released event scheduled for today appears with mode=RELEASED."""
    when = NOW + timedelta(hours=1)  # today, 1 hour ahead of now
    rows = _today_rows(when, NOW, actual=1.5)
    assert len(rows) == 1
    assert rows[0]["mode"] == "RELEASED"
    assert rows[0]["actual"] == 1.5


def test_today_upcoming_events_appear():
    """An upcoming event scheduled for today appears with mode=UPCOMING."""
    when = NOW.replace(hour=18, minute=0)  # today 18:00 UTC
    now = NOW.replace(hour=12, minute=0)   # now = today 12:00 UTC
    rows = _today_rows(when, now, actual=None)
    assert len(rows) == 1
    assert rows[0]["mode"] == "UPCOMING"
    assert rows[0]["actual"] is None


def test_yesterday_events_excluded():
    """An event from yesterday does not appear, regardless of actual."""
    when = (NOW - timedelta(days=1)).replace(hour=14, minute=0)
    rows = _today_rows(when, NOW, actual=2.0)
    assert rows == []


def test_tomorrow_events_excluded():
    """An event from tomorrow does not appear, regardless of actual."""
    when = (NOW + timedelta(days=1)).replace(hour=10, minute=0)
    rows = _today_rows(when, NOW, actual=1.0)
    assert rows == []


def test_market_timezone_boundary():
    """
    An event at 2026-10-17 02:00 UTC with market_timezone=America/New_York
    is 2026-10-16 22:00 ET - yesterday in market timezone - so excluded.
    Tests the _is_today boundary directly since the pipeline's 2-hour
    freshness check would also exclude events this far from now.
    """
    from app.analysis.economic_impact import _is_today

    event = CalendarEvent(
        title="Test Event", country="USD",
        when_utc=datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc),
        impact="High",
    )
    # now = midday UTC on Oct 17 = morning ET on Oct 17 (today in ET)
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    assert not _is_today(event, now, "America/New_York")
