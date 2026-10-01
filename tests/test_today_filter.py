"""Unit tests for the TODAY-ONLY filter (section 19)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.utils import timeutils as T


@pytest.fixture(autouse=True)
def _market_tz(monkeypatch):
    """Pin the market timezone so results are deterministic anywhere."""
    monkeypatch.setattr(T.settings, "market_timezone", "America/New_York")


def test_today_article_accepted():
    now = datetime.now(timezone.utc)
    assert T.is_today(now) is True


def test_yesterday_article_rejected():
    """The single most important guarantee: never present old news as today."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    assert T.is_today(yesterday) is False


def test_two_days_ago_rejected():
    now = datetime.now(timezone.utc)
    assert T.is_today(now - timedelta(days=2)) is False


def test_missing_timestamp_is_never_today():
    assert T.is_today(None) is False


def test_two_days_ago_exceeds_max_age():
    now = datetime.now(timezone.utc)
    assert T.within_max_age(now - timedelta(days=2), now=now) is False


def test_recent_within_max_age():
    now = datetime.now(timezone.utc)
    assert T.within_max_age(now - timedelta(hours=2), now=now) is True


def test_far_future_rejected_as_bad_clock():
    """A timestamp hours in the future signals a broken feed, not real news."""
    now = datetime.now(timezone.utc)
    assert T.within_max_age(now + timedelta(hours=6), now=now) is False


def test_iso_with_z_suffix():
    dt, label = T.parse_timestamp("2026-06-01T14:30:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour == 14


def test_rfc822_format():
    """RSS pubDate uses RFC-822; it must parse to the correct instant."""
    dt, label = T.parse_timestamp("Mon, 01 Jun 2026 14:30:00 GMT")
    assert dt == datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)


def test_unparseable_timestamp_returns_none():
    """We never fabricate a publication time."""
    dt, label = T.parse_timestamp("not a real timestamp at all")
    assert dt is None


def test_empty_timestamp_returns_none():
    assert T.parse_timestamp("")[0] is None
    assert T.parse_timestamp(None)[0] is None


def test_article_from_tonight_market_date_is_today():
    """A story published at 23:30 UTC is still 'today' in New York."""
    now = datetime.now(timezone.utc)
    late = (now - timedelta(hours=1)).astimezone(T.market_tz())
    assert T.is_today(late) is True


def test_update_detection_window():
    """Section 19: an old article re-stamped today is an UPDATE, not new news."""
    from app.services.pipeline import _is_update_article

    published = datetime.now(timezone.utc) - timedelta(hours=2)
    updated = published + timedelta(minutes=30)
    assert _is_update_article(published, updated) is True

    far_update = published + timedelta(days=1)
    assert _is_update_article(published, far_update) is False

    assert _is_update_article(published, None) is False