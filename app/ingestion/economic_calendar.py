"""
ECONOMIC CALENDAR INGESTION.

Fetches scheduled macro releases from a keyless public JSON feed and
normalises them into ``CalendarEvent`` records.

The provider is deliberately tolerant: it is a free third-party feed, so every
network or schema problem degrades to "no calendar today" rather than an
exception that could take the pipeline down. The engine never invents a
release that the feed did not report.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.analysis.economic_impact import (
    CalendarEvent,
    classify_event,
    to_float,
)
from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import EconomicImportance

log = get_logger(__name__)

# Feed "impact" words -> engine importance floor.
_IMPORTANCE_FLOOR = {
    "high": EconomicImportance.HIGH,
    "medium": EconomicImportance.MEDIUM,
    "holiday": EconomicImportance.LOW,
    "low": EconomicImportance.LOW,
}


def _parse_when(raw: str) -> Optional[datetime]:
    """ISO-8601 with offset -> aware UTC datetime, or None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalise(row: dict) -> Optional[CalendarEvent]:
    """One provider row -> CalendarEvent, or None if unusable."""
    if not isinstance(row, dict):
        return None
    title = str(row.get("title") or "").strip()
    when = _parse_when(str(row.get("date") or ""))
    if not title or when is None:
        return None
    return CalendarEvent(
        title=title,
        country=str(row.get("country") or "").strip().upper(),
        when_utc=when,
        impact=str(row.get("impact") or "Medium").strip(),
        forecast=to_float(row.get("forecast")),
        previous=to_float(row.get("previous")),
        actual=to_float(row.get("actual")),   # absent in most free feeds
    )


async def _fetch_one(url: str) -> list[CalendarEvent]:
    if not url:
        return []
    headers = {
        "User-Agent": settings.http_user_agent,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers=headers,
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return await asyncio.to_thread(_parse_feed, resp.text)


def _parse_feed(body: str) -> list[CalendarEvent]:
    """Provider JSON -> normalised, de-duplicated event list."""
    import json

    try:
        rows = json.loads(body)
    except (ValueError, TypeError) as exc:
        log.warning("Economic calendar feed is not valid JSON: %s", exc)
        return []
    if not isinstance(rows, list):
        return []

    floor = _IMPORTANCE_FLOOR.get(
        (settings.economic_calendar_min_importance or "MEDIUM").lower(),
        EconomicImportance.MEDIUM,
    )

    seen: set[str] = set()
    events: list[CalendarEvent] = []
    for row in rows:
        event = _normalise(row)
        if event is None:
            continue
        # Keep only events we can actually reason about, at or above the
        # configured importance floor. Low prints are noise for these assets.
        if classify_event(event) is None:
            continue
        if _IMPORTANCE_FLOOR.get(event.impact.lower(), EconomicImportance.LOW) < floor:
            continue
        if event.event_key in seen:
            continue
        seen.add(event.event_key)
        events.append(event)
    return events


async def fetch_calendar() -> tuple[list[CalendarEvent], list[str]]:
    """
    Fetch this week + next week.

    Returns ``(events, errors)``. Never raises: a calendar outage must not stop
    the news pipeline.
    """
    if not settings.economic_calendar_enabled:
        return [], []

    errors: list[str] = []
    results: list[list[CalendarEvent]] = []

    urls = [
        ("this week", settings.economic_calendar_url),
        ("next week", settings.economic_calendar_nextweek_url),
    ]
    for label, url in urls:
        if not url:
            continue
        try:
            results.append(await _fetch_one(url))
        except Exception as exc:  # noqa: BLE001 - calendar is optional
            errors.append(f"economic calendar ({label}): {type(exc).__name__}: {exc}")
            log.warning("Economic calendar %s failed: %s", label, exc)

    merged: dict[str, CalendarEvent] = {}
    for batch in results:
        for event in batch:
            merged.setdefault(event.event_key, event)

    events = sorted(merged.values(), key=lambda e: e.when_utc)
    log.info("Economic calendar: %d events from %d feed(s)", len(events),
             len([u for _, u in urls if u]))
    return events, errors