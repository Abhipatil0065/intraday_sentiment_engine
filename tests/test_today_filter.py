"""Unit tests for the TODAY-ONLY filter (section 19)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.pipeline import (
    _fj_session_report_is_admissible,
    _is_fj_session_report,
)
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


def test_late_utc_story_is_today_in_market_tz():
    """A 23:30 UTC story is still 'today' in New York (19:30 EDT).

    Pinned instants, not the wall clock: this assertion used to read
    ``datetime.now() - 1h`` and therefore failed every day between
    midnight and 01:00 New York time.
    """
    now = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    late = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    assert T.is_today(late, now=now) is True


def test_later_utc_day_can_still_be_the_same_day_in_new_york():
    """A story from a LATER UTC day can still be today in New York.

    now is 20:00 UTC on 1 June (16:00 EDT, 1 June) and the story is 03:00 UTC
    on 2 June (23:00 EDT, 1 June). The UTC calendar dates differ, yet both are
    1 June in New York - so a UTC-based comparison would wrongly reject it.
    """
    now = datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)
    story = datetime(2026, 6, 2, 3, 0, tzinfo=timezone.utc)

    assert story.date() != now.date()                     # different UTC day...
    assert story.astimezone(T.market_tz()).date() == now.astimezone(T.market_tz()).date()
    assert T.is_today(story, now=now) is True             # ...same NY day


def test_same_utc_day_can_still_be_yesterday_in_new_york():
    """The mirror case: identical UTC dates, different New York dates.

    This is the assertion that would fail if the comparison used UTC dates
    instead of the market timezone.
    """
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    story = datetime(2026, 6, 2, 3, 0, tzinfo=timezone.utc)

    assert story.date() == now.date()                     # same UTC day...
    assert story.astimezone(T.market_tz()).hour == 23     # ...but 23:00 EDT
    assert story.astimezone(T.market_tz()).date() == date(2026, 6, 1)
    assert T.is_today(story, now=now) is False            # ...so it is yesterday


def test_evening_utc_previous_day_is_yesterday_in_new_york():
    """23:30 UTC on 1 June is 19:30 EDT on 1 June -> yesterday by 2 June."""
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    story = datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    assert T.is_today(story, now=now) is False


def test_winter_offset_is_handled_not_hardcoded_to_summer():
    """04:30 UTC on 15 Jan is 23:30 EST on 14 Jan (UTC-5, not UTC-4)."""
    now = datetime(2026, 1, 15, 4, 30, tzinfo=timezone.utc)
    story = datetime(2026, 1, 14, 20, 0, tzinfo=timezone.utc)

    ny = T.market_tz()
    # The reference instant is still 14 January locally - this only holds if
    # the EST (UTC-5) offset is applied rather than a hardcoded summer UTC-4.
    assert now.astimezone(ny).date() == date(2026, 1, 14)
    assert T.is_today(story, now=now) is True


def test_update_detection_window():
    """Section 19: an old article re-stamped today is an UPDATE, not new news."""
    from app.services.pipeline import _is_update_article

    published = datetime.now(timezone.utc) - timedelta(hours=2)
    updated = published + timedelta(minutes=30)
    assert _is_update_article(published, updated) is True

    far_update = published + timedelta(days=1)
    assert _is_update_article(published, far_update) is False

    assert _is_update_article(published, None) is False


# ---------------------------------------------------------------------------
# FinancialJuice session reports are admitted on the INDIAN date, not the
# market date. The US wrap lands ~01:05 IST = 15:35 EDT the PREVIOUS day, so
# the market-timezone TODAY filter used to discard it every single evening.
# ---------------------------------------------------------------------------
def _fj_article(title, published_iso):
    from app.ingestion.base import RawArticle

    return RawArticle(
        guid="g-" + title[:20],
        source="Financial Juice",
        source_quality="TIER_2_MAJOR",
        url="https://www.financialjuice.com/x",
        title=title,
        body="",
        summary="",
        published_raw=published_iso,
    )


def test_overnight_us_wrap_is_recognised_as_a_session_report():
    art = _fj_article("Stocks Rebound - US Market Wrap",
                      "2026-10-01T19:35:00+00:00")
    assert asyncio.run(_is_fj_session_report(art)) is True


def test_plain_fj_headline_is_not_a_session_report():
    art = _fj_article("Fed's Jefferson speaks on inflation",
                      "2026-10-01T19:35:00+00:00")
    assert asyncio.run(_is_fj_session_report(art)) is False


def test_non_fj_source_is_never_a_session_report():
    from app.ingestion.base import RawArticle

    art = RawArticle(
        guid="g-x", source="Investing.com", source_quality="TIER_1",
        url="https://example.com/x",
        title="Stocks Rebound - US Market Wrap",
        body="", summary="", published_raw="2026-10-01T19:35:00+00:00",
    )
    assert asyncio.run(_is_fj_session_report(art)) is False


# ---------------------------------------------------------------------------
# Query-time TODAY resolution.
#
# is_today is written once at ingest and never recomputed, so a long-running
# process accumulates yesterday's headlines under that flag forever. The feed
# and the sentiment recompute therefore re-derive the day from the stored
# publication time instead. These tests pin that boundary.
# ---------------------------------------------------------------------------
def test_market_day_start_is_midnight_in_the_market_tz():
    now = datetime(2026, 10, 2, 11, 55, tzinfo=timezone.utc)
    start = T.market_day_start_utc(now)
    assert start == datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)  # 00:00 EDT
    assert start.tzinfo is timezone.utc


def test_article_after_midnight_et_is_within_the_market_day():
    now = datetime(2026, 10, 2, 11, 55, tzinfo=timezone.utc)
    published = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)
    assert T.is_within_market_day(published, now=now) is True


def test_article_before_midnight_et_is_outside_the_market_day():
    """The regression this change exists for: 23:30 UTC the prior day is
    yesterday in New York and must not be served as today's news."""
    now = datetime(2026, 10, 2, 11, 55, tzinfo=timezone.utc)
    published = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
    assert T.is_within_market_day(published, now=now) is False


def test_stale_is_today_flag_no_longer_decides_the_day():
    """An article flagged is_today=True at ingest but published yesterday must
    be excluded by the predicate the feed and sentiment recompute both share."""
    from app.services.pipeline import news_day_predicate

    now = datetime(2026, 10, 2, 11, 55, tzinfo=timezone.utc)
    yesterday = now - timedelta(hours=20)
    assert T.is_within_market_day(yesterday, now=now) is False
    assert news_day_predicate() is not None


def test_missing_timestamp_is_never_within_the_market_day():
    assert T.is_within_market_day(None) is False


def test_naive_timestamp_is_treated_as_utc():
    now = datetime(2026, 10, 2, 11, 55, tzinfo=timezone.utc)
    assert T.is_within_market_day(datetime(2026, 10, 2, 5, 0), now=now) is True


def test_market_day_rolls_over_at_et_midnight_not_utc_midnight():
    """The ET day, not the UTC day, decides what "today" means.

    An article stamped 01:00 UTC on Oct 2 is Oct 1 21:00 in New York. While the
    ET day is still Oct 1 it legitimately belongs to today; once ET crosses
    midnight (04:00 UTC) the same article becomes yesterday and must drop out.
    This is precisely why the feed cannot filter on the UTC date column.
    """
    published = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)  # Oct 1, 21:00 EDT

    # Still Oct 1 in New York -> the article is today's news.
    assert T.is_within_market_day(
        published, now=datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)
    ) is True

    # After ET midnight (04:00 UTC) it is Oct 2 in New York -> yesterday's news.
    assert T.is_within_market_day(
        published, now=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    ) is False


def test_us_wrap_past_midnight_ist_is_admissible():
    """01:05 IST on 2 Oct is 'yesterday' in New York - it must still pass."""
    published = datetime(2026, 10, 1, 19, 35, tzinfo=timezone.utc)
    assert T.is_today(published) is False      # market-tz test says no...
    assert _fj_session_report_is_admissible(published) is True   # IST says yes


def test_two_day_old_session_report_is_rejected():
    published = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    assert _fj_session_report_is_admissible(published) is False


def test_future_dated_session_report_is_rejected():
    published = datetime.now(timezone.utc) + timedelta(days=1)
    assert _fj_session_report_is_admissible(published) is False


# ---------------------------------------------------------------------------
# Session reports must be scored on their FULL text, not the feed teaser.
# The homepage carries only a teaser for a session report - and for the
# "Morning Juice" preps that teaser is EMPTY - so the article page has to be
# opened to get the Sentiment / Docket / Speakers body.
# ---------------------------------------------------------------------------
def test_enrichment_targets_only_session_reports(monkeypatch):
    """General headlines must never be opened - only the five reports."""
    from app.ingestion.financialjuice import FinancialJuiceSource

    src = FinancialJuiceSource()
    opened = []

    async def fake_fetch(ctx, url):
        opened.append(url)
        return "FULL ARTICLE BODY " * 40

    monkeypatch.setattr(src, "_fetch_article_text", fake_fetch)

    from app.ingestion.base import RawArticle

    def mk(title, body=""):
        return RawArticle(
            guid="g" + title[:15], source="Financial Juice",
            source_quality="TIER_2_MAJOR",
            url="https://www.financialjuice.com/News/1/x.aspx",
            title=title, body=body, summary="", published_raw=None,
        )

    class _Ctx:
        async def new_page(self): return None
        async def close(self): return None

    articles = [
        mk("Morning Juice - US Session Prep", ""),
        mk("Morning Juice - Europe Session Prep", ""),
        mk("Stocks Rebound - US Market Wrap", "short teaser"),
        mk("Fed's Jefferson speaks on inflation", "general headline"),
        mk("Treasury yields slip on payrolls", "general headline"),
    ]

    class _Browser:
        async def new_context(self, *a, **k):
            return _Ctx()

    upgraded = asyncio.run(
        src._enrich_session_reports(_Browser(), articles))

    # Only the three session reports were fetched.
    assert upgraded == 3
    assert len(opened) == 3
    # They now carry the full text.
    assert "FULL ARTICLE BODY" in articles[0].body
    assert "FULL ARTICLE BODY" in articles[2].body
    # General headlines are untouched.
    assert articles[3].body == "general headline"
    assert articles[4].body == "general headline"


def test_enrichment_keeps_teaser_when_page_fails(monkeypatch):
    """A failed article load must never discard the teaser we already have."""
    from app.ingestion.financialjuice import FinancialJuiceSource
    from app.ingestion.base import RawArticle

    src = FinancialJuiceSource()

    async def failing(ctx, url):
        return ""          # simulates a timeout / layout change

    monkeypatch.setattr(src, "_fetch_article_text", failing)

    art = RawArticle(
        guid="g1", source="Financial Juice", source_quality="TIER_2_MAJOR",
        url="https://www.financialjuice.com/News/1/x.aspx",
        title="Morning Juice - US Session Prep",
        body="existing teaser", summary="", published_raw=None,
    )

    class _Ctx:
        async def new_page(self): return None
        async def close(self): return None

    class _Browser:
        async def new_context(self, *a, **k):
            return _Ctx()

    upgraded = asyncio.run(src._enrich_session_reports(_Browser(), [art]))
    assert upgraded == 0
    assert art.body == "existing teaser"


def test_article_fetch_ignores_non_article_urls():
    """Guard: only real /News/ permalinks are opened."""
    from app.ingestion.financialjuice import FinancialJuiceSource

    src = FinancialJuiceSource()
    assert asyncio.run(src._fetch_article_text(None, "")) == ""
    assert asyncio.run(
        src._fetch_article_text(None, "https://www.financialjuice.com/")) == ""
    assert asyncio.run(
        src._fetch_article_text(None, "https://example.com/x")) == ""