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

