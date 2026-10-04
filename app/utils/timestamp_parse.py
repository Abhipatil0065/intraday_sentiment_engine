"""Timestamp parsing and today-window helpers (companion to timeutils)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)


def parse_timestamp(
    raw: Optional[str],
) -> tuple[Optional[datetime], Optional[str]]:
    """
    Parse a publisher timestamp into an aware UTC datetime.

    Returns (utc_datetime, tz_label_used). ``utc_datetime`` is None when the
    value is absent or unparseable - a publication time is never fabricated.

    ISO-8601 is attempted first, then RFC-822 (the RSS/Atom standard), then a
    few common human-readable formats.
    """
    if not raw:
        return None, None

    # feedparser exposes *_parsed fields as time.struct_time in UTC.
    if isinstance(raw, time.struct_time):
        return (
            datetime(*raw[:6], tzinfo=timezone.utc).astimezone(timezone.utc),
            "utc",
        )

    text = str(raw).strip()

    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), "assumed-utc"
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(text)
        if dt is not None:
            label = dt.tzname() or "unknown"
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc), label
    except Exception:  # noqa: BLE001 - malformed input is expected
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%b %d, %Y %H:%M:%S", "%b %d, %Y %H:%M",
        "%B %d, %Y %H:%M", "%d %b %Y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p",
    ):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc), "assumed-utc"
        except ValueError:
            continue

    log.warning("Unparseable timestamp: %r", text)
    return None, None
def _market_tz():
    from app.utils.timeutils import market_tz
    return market_tz()


def _market_day_start_utc(now: Optional[datetime] = None) -> datetime:
    from app.utils.timeutils import market_day_start_utc
    return market_day_start_utc(now)


def is_within_market_day(
    utc_dt: Optional[datetime], *, now: Optional[datetime] = None
) -> bool:
    """
    True when a timestamp falls on or after today's market-day start.

    This is the in-Python twin of the SQL predicate the feed uses
    (``pipeline.news_day_predicate``). Both compare against the same
    ``market_day_start_utc`` boundary, so the database and this helper can never
    disagree about where the trading day starts - which is exactly the drift
    that let yesterday's headlines linger in today's feed.
    """
    if utc_dt is None:
        return False
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(timezone.utc) >= _market_day_start_utc(now)


def is_today(utc_dt: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    """
    Strict TODAY test in the market timezone.

    Qualifies only if the market-local calendar date equals today's market
    date, so a 23:30 UTC story already past midnight in Asia is handled
    correctly.
    """
    if utc_dt is None:
        return False
    ref = now or datetime.now(timezone.utc)
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    tz = _market_tz()
    return utc_dt.astimezone(tz).date() == ref.astimezone(tz).date()


def within_rolling_window(utc_dt: Optional[datetime], *,
                          now: Optional[datetime] = None) -> bool:
    """
    Rolling-window admission test (improvement item 3).

    Replaces the strict "same market-timezone calendar date" gate. A rolling
    window admits the overnight US wrap by CONSTRUCTION, which is why the old
    FinancialJuice / IST date carve-out is no longer needed: a report published
    at 01:05 IST is only a few hours old, so it passes on age alone.

    Rules, in order:
      * an unparseable / absent timestamp is REJECTED (never fabricated)
      * a timestamp more than ``NEWS_WINDOW_HOURS`` old is rejected
      * a timestamp far in the future is rejected (bad feed / bad clock)

    Timezone-independent by construction, which is the whole point: an article
    is admitted on how RECENT it is, not on which calendar day it happened to
    land in.
    """
    if utc_dt is None:
        return False
    ref = now or datetime.now(timezone.utc)
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)

    hours = age_hours(utc_dt, now=ref)
    if hours is None:
        return False
    # >2h in the future means a broken feed or a broken clock, not news.
    if hours < -2:
        return False

    window = float(getattr(settings, "news_window_hours", 18.0))
    if window <= 0:
        return True
    return hours <= window


def age_hours(utc_dt: Optional[datetime], *, now: Optional[datetime] = None) -> Optional[float]:
    if utc_dt is None:
        return None
    ref = now or datetime.now(timezone.utc)
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return (ref - utc_dt).total_seconds() / 3600.0


def within_max_age(utc_dt: Optional[datetime], *, now: Optional[datetime] = None) -> bool:
    hours = age_hours(utc_dt, now=now)
    if hours is None or hours < -2:  # >2h in the future = bad clock/bad feed
        return False
    return hours <= settings.max_article_age_hours

