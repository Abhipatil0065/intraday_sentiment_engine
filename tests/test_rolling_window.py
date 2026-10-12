"""
ROLLING NEWS WINDOW (improvement item 3).

Replaces the "same market-timezone calendar date" gate with a configurable
rolling window (default 18h). These tests protect the two guarantees that
matter most: an unparseable timestamp is never admitted, and an old article
cannot leak into today's verdict no matter which calendar day it landed in.
"""

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.utils import timeutils as T

NOW = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------
def test_brand_new_article_is_admitted():
    assert T.within_rolling_window(NOW - timedelta(minutes=5), now=NOW) is True


def test_article_inside_the_window_is_admitted():
    assert T.within_rolling_window(NOW - timedelta(hours=10), now=NOW) is True


def test_article_beyond_the_window_is_rejected():
    age = float(settings.news_window_hours) + 2
    assert T.within_rolling_window(NOW - timedelta(hours=age), now=NOW) is False


def test_previous_calendar_day_can_still_be_admitted():
    """The point of the change: recency, not the calendar date, decides."""
    yesterday_evening = (NOW - timedelta(days=1)).replace(hour=22)
    assert T.within_rolling_window(yesterday_evening, now=NOW) is True


def test_two_day_old_article_is_rejected():
    assert T.within_rolling_window(NOW - timedelta(days=2), now=NOW) is False


def test_unparseable_timestamp_is_rejected_not_fabricated():
    assert T.within_rolling_window(None, now=NOW) is False


def test_future_dated_article_is_rejected():
    assert T.within_rolling_window(NOW + timedelta(days=1), now=NOW) is False


def test_naive_timestamp_is_treated_as_utc():
    assert T.within_rolling_window(NOW.replace(tzinfo=None), now=NOW) is True


# --------------------------------------------------------------------------
# Window is configurable
# --------------------------------------------------------------------------
def test_window_is_configurable(monkeypatch):
    old = float(settings.news_window_hours)
    try:
        monkeypatch.setattr(settings, "news_window_hours", 2.0, raising=False)
        assert T.within_rolling_window(NOW - timedelta(hours=5), now=NOW) is False
        monkeypatch.setattr(settings, "news_window_hours", 48.0, raising=False)
        assert T.within_rolling_window(NOW - timedelta(hours=5), now=NOW) is True
    finally:
        monkeypatch.setattr(settings, "news_window_hours", old, raising=False)


def test_zero_window_disables_the_time_gate():
    """A zero window means "judge on the max-age cap alone", not "reject all"."""
    from app.config import settings as s
    old = float(s.news_window_hours)
    try:
        s.news_window_hours = 0.0
        assert T.within_rolling_window(NOW - timedelta(days=3), now=NOW) is True
    finally:
        s.news_window_hours = old


def test_default_window_is_eighteen_hours():
    """
    The DECLARED default is 18h.

    The effective value follows NEWS_WINDOW_HOURS, and the shipped .env tunes
    it to 24h for live day trading, so asserting on ``settings`` would test
    the deployment's env rather than the default. Everything else in this
    file reads the effective value on purpose.
    """
    from app.config import Settings

    assert Settings.model_fields["news_window_hours"].default == 18.0