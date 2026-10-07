"""
Timezone + trading-date helpers.

The TODAY-ONLY filter is the single most important correctness requirement:
we must never present yesterday's news as today's. All date maths in the
engine funnels through this module so there is exactly one definition of
"today" and one place to audit it.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings

log = logging.getLogger(__name__)

_TZ_ABBREV = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "EASTERN": "America/New_York",
    "GMT": "Europe/London",
    "BST": "Europe/London",
    "UTC": "UTC",
    "Z": "UTC",
    "IST": "Asia/Kolkata",
    "CEST": "Europe/Berlin",
    "CET": "Europe/Berlin",
    "JST": "Asia/Tokyo",
    "HKT": "Asia/Hong_Kong",
    "AEST": "Australia/Sydney",
}


def market_tz() -> ZoneInfo:
    return ZoneInfo(settings.market_timezone)


def display_tz() -> ZoneInfo:
    return ZoneInfo(settings.display_timezone)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_market() -> datetime:
    """Current wall-clock time in the market timezone."""
    return now_utc().astimezone(market_tz())


def market_today() -> date:
    """The trading date used by the TODAY-ONLY filter."""
    return now_market().date()


def market_day_start_utc(now: datetime | None = None) -> datetime:
    """
    The UTC instant at which today's market session began.

    Market-local midnight converted to UTC. This is the ONE boundary that
    decides where the trading day starts, so the news feed, the sentiment
    recompute and the article counter cannot drift apart.

    ``published_at_utc`` is what gets compared against this, never a stored
    "is today" flag: that flag is written once at ingest and never recomputed,
    so it silently accumulates yesterday's headlines on a long-running process.
    """
    ref = now or now_utc()
    local = ref.astimezone(market_tz())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def resolve_tz(raw: str | None) -> timezone | None:
    """
    Resolve an arbitrary timezone string.

    Handles IANA names ("Europe/London"), common abbreviations ("EDT") and
    fixed offsets ("+05:30"). Returns None when unresolvable - the caller
    must then treat the timestamp as UNVERIFIED rather than guess.
    """
    if not raw:
        return None
    candidate = str(raw).strip()
    upper = candidate.upper()
    if upper in _TZ_ABBREV:
        candidate = _TZ_ABBREV[upper]
# --- timestamp parsing / display helpers -----------------------------------
# Re-exported from timestamp_parse to maintain a single public API in timeutils.
from app.utils.timestamp_parse import (
    parse_timestamp,
    age_hours,
    within_max_age,
    within_rolling_window,
    is_today,
    is_within_market_day,
)


def describe_for_display(utc_dt: datetime | None) -> str:
    if utc_dt is None:
        return "Data unavailable."
    return utc_dt.astimezone(display_tz()).strftime("%Y-%m-%d %H:%M:%S %Z")


def local_clock(utc_dt: datetime | None) -> str:
    """HH:MM in the display timezone - used heavily by the news feed."""
    if utc_dt is None:
        return "--:--"
    return utc_dt.astimezone(display_tz()).strftime("%H:%M")


def stamp() -> str:
    return now_utc().astimezone(display_tz()).strftime("%H:%M:%S")