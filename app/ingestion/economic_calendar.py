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
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

import httpx

from app.analysis.calendar_merge import MergeStats, merge_events
from app.analysis.economic_impact import (
    CalendarEvent,
    classify_event,
    to_float,
)
from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import EconomicImportance

log = get_logger(__name__)

# Phase 8 conditional-request state, keyed by URL. In-memory on purpose: an
# ETag is only valid for the process lifetime, and a stale one from a previous
# run could suppress a real change.
_ETAGS: dict[str, str] = {}
_LAST_ROWS: dict[str, list] = {}


def get_etag(url: str) -> Optional[str]:
    return _ETAGS.get(url)


def set_etag(url: str, etag: str) -> None:
    _ETAGS[url] = etag


def clear_etag(url: str) -> None:
    _ETAGS.pop(url, None)


def get_cached_rows(url: str) -> list:
    """
    The last parsed rows for a URL, used to answer a 304.

    Returning the previous rows is what makes a conditional request safe: a 304
    means "identical to what you already have", so the previous parse is still
    exactly right. ``copy`` keeps a caller mutating the returned list from
    corrupting the stored copy.
    """
    return list(_LAST_ROWS.get(url, []))


def remember_rows(url: str, events) -> None:
    _LAST_ROWS[url] = list(events)


def reset_http_state() -> None:
    """Clear validators and remembered rows, so tests never inherit them."""
    _ETAGS.clear()
    _LAST_ROWS.clear()

# Feed "impact" words -> importance RANK (an int, not the enum).
#
# These MUST be integers. EconomicImportance is a plain str Enum, so comparing
# two members with < compares their VALUES alphabetically: "HIGH" < "MEDIUM"
# evaluates True, which silently dropped EVERY high-impact faireconomy event
# while the log showed no error. Ranking numerically makes the comparison
# mean what it looks like it means.
_IMPORTANCE_FLOOR = {
    "high": 3,
    "medium": 2,
    "holiday": 0,
    "low": 1,
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

    # Phase 8: conditional request. Sending the stored ETag means an unchanged
    # feed costs a 304 with no body instead of the full payload - which is the
    # cheapest possible way to stay under faireconomy's rate limit. FJ is a
    # browser render and cannot send one, so this only helps the JSON provider.
    etag = get_etag(url)
    headers = {
        "User-Agent": settings.http_user_agent,
        "Accept": "application/json",
    }
    if etag and settings.calendar_conditional_requests:
        headers["If-None-Match"] = etag

    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers=headers,
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)

        if resp.status_code == 304 and etag:
            # Unchanged since the last poll. Reuse the parsed rows rather than
            # treating "no body" as "no events".
            log.debug("Economic calendar: %s unchanged (304)", url)
            return get_cached_rows(url) or []

        resp.raise_for_status()
        new_etag = resp.headers.get("ETag")
        if new_etag:
            set_etag(url, new_etag)
        else:
            # The server stopped sending a validator; keeping the old one would
            # make the next request send a stale If-None-Match forever.
            clear_etag(url)
        events = await asyncio.to_thread(_parse_feed, resp.text)
        # Remembered so a later 304 can be answered from this parse.
        remember_rows(url, events)
        return events


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
        2,
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
        if _IMPORTANCE_FLOOR.get(event.impact.lower(), 0) < floor:
            continue
        if event.event_key in seen:
            continue
        seen.add(event.event_key)
        events.append(event)
    return events


async def fetch_calendar() -> tuple[list[CalendarEvent], list[str]]:
    """
    Fetch the calendar from every ENABLED provider, then merge.

    Returns ``(events, errors)``. Never raises: a calendar outage must not stop
    the news pipeline, and neither must ONE provider failing - the others are
    still used, and the merge simply sees fewer rows from the failed one.

    Providers run in ``calendar_providers`` order, which is also merge
    PRIORITY. Phase 8: if every provider fails and a cache exists within
    ``calendar_cache_max_age_days``, the cached events are served with a stale
    note rather than an empty list.
    """
    if not settings.economic_calendar_enabled:
        return [], []

    order = settings.calendar_provider_list or ["faireconomy"]
    errors: list[str] = []
    batches: list[tuple[str, list[CalendarEvent]]] = []

    for name in order:
        if name == "financialjuice":
            events, provider_errors = await _fetch_fj()
        elif name == "faireconomy":
            events, provider_errors = await _fetch_faireconomy()
        else:
            errors.append(f"economic calendar: unknown provider {name!r} - skipped")
            continue
        errors.extend(provider_errors)
        batches.append((name, events))
        log.info("Economic calendar provider %s -> %d event(s)", name, len(events))

    stats = MergeStats()
    merged = merge_events(batches, stats)

    if not merged:
        cached = _load_cached_events()
        if cached:
            log.warning(
                "Economic calendar: every provider failed; serving %d CACHED "
                "event(s) (stale) rather than an empty calendar",
                len(cached))
            return cached, errors

    if merged:
        # Only a genuinely good payload is cached, so a partial outage is never
        # frozen into disk as the "last good" answer.
        try:
            from app.ingestion.calendar_cache import save_cache

            save_cache(merged)
        except Exception as exc:  # noqa: BLE001 - cache must never break a poll
            log.debug("calendar cache save skipped: %s", exc)

    if stats.merged or stats.disagreements:
        log.info("Economic calendar merge: %s", stats.as_dict())
    return merged, errors


async def _fetch_fj() -> tuple[list[CalendarEvent], list[str]]:
    """FinancialJuice leg. Isolated: returns errors, never raises."""
    from app.ingestion.fj_calendar import fetch_fj_calendar

    try:
        result = await fetch_fj_calendar()
    except Exception as exc:  # noqa: BLE001 - isolation is the point
        return [], [f"economic calendar (financialjuice): "
                    f"{type(exc).__name__}: {exc}"]
    if result.ok:
        return result.events, []
    # A disabled or backed-off provider is NOT an error worth surfacing as a
    # pipeline error - it is expected operational state.
    if result.error in ("disabled", "backoff"):
        return [], []
    return [], [f"economic calendar (financialjuice): {result.error}"]


async def _fetch_faireconomy() -> tuple[list[CalendarEvent], list[str]]:
    """
    The original provider, unchanged in behaviour.

    Phase 2 asked whether the next-week feed could be enabled: it cannot.
    ``ff_calendar_nextweek.json`` returns a reproducible HTTP 404, so
    faireconomy publishes this week only. The config value stays empty rather
    than pointing at a URL that does not exist - that is why FinancialJuice
    earns its place, since it renders several days at once.

    429 HANDLING: this feed rate-limits under repeated polling. Rather than
    retrying into the limit, a 429 starts a cooldown (honouring Retry-After
    when the server sends one). During the cooldown the fetch is skipped
    entirely and FinancialJuice plus the cache carry the calendar.
    """
    if faireconomy_in_backoff():
        log.info("Economic calendar: faireconomy in 429 backoff - "
                 "relying on other providers and the cache")
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
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                retry_after = exc.response.headers.get("Retry-After")
                minutes = note_rate_limit(retry_after)
                message = (f"economic calendar ({label}): HTTP 429 rate limited; "
                           f"backing off {minutes} min")
                errors.append(message)
                log.warning("%s (Retry-After=%s)", message, retry_after or "-")
            else:
                errors.append(f"economic calendar ({label}): HTTP {status}")
                log.warning("Economic calendar %s failed: HTTP %s", label, status)
        except Exception as exc:  # noqa: BLE001 - calendar is optional
            errors.append(f"economic calendar ({label}): "
                          f"{type(exc).__name__}: {exc}")
            log.warning("Economic calendar %s failed: %s", label, exc)

    merged: dict[str, CalendarEvent] = {}
    for batch in results:
        for event in batch:
            merged.setdefault(event.event_key, event)
    events = sorted(merged.values(), key=lambda e: e.when_utc)
    if events:
        clear_rate_limit()
    log.info("Economic calendar: %d events from %d feed(s)", len(events),
             len([u for _, u in urls if u]))
    return events, errors


# --------------------------------------------------------------------------
# 429 cooldown state (module-level, per process)
# --------------------------------------------------------------------------
_fe_backoff_until: Optional[datetime] = None
_fe_failure_logged = False


def faireconomy_in_backoff(now: Optional[datetime] = None) -> bool:
    """True while faireconomy is cooling off after a 429."""
    if _fe_backoff_until is None:
        return False
    return (now or datetime.now(timezone.utc)) < _fe_backoff_until


def note_rate_limit(retry_after: Optional[str] = None) -> int:
    """
    Start the cooldown and return the number of minutes chosen.

    A ``Retry-After`` header wins when present and parseable, because the
    server knows when it will accept requests again; otherwise the configured
    default applies. Either way it is CAPPED by
    ``calendar_fe_429_max_retry_after_minutes`` so an absurd or hostile value
    cannot park the calendar indefinitely.

    Logs the FIRST failure and the recovery, not every poll.
    """
    global _fe_backoff_until, _fe_failure_logged
    minutes = float(settings.calendar_fe_429_backoff_minutes)
    if retry_after:
        try:
            header_minutes = float(retry_after) / 60.0
        except (TypeError, ValueError):
            header_minutes = 0.0
        if header_minutes > 0:
            minutes = header_minutes
    cap = float(settings.calendar_fe_429_max_retry_after_minutes)
    minutes = max(1.0, min(minutes, cap))
    _fe_backoff_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    if not _fe_failure_logged:
        log.warning("faireeconomy rate limited (HTTP 429); cooling off %.0f "
                    "min. FinancialJuice and the cache cover the gap.",
                    minutes)
        _fe_failure_logged = True
    return int(minutes)


def clear_rate_limit() -> None:
    """Called on a successful fetch; logs the recovery once."""
    global _fe_backoff_until, _fe_failure_logged
    if _fe_failure_logged:
        log.info("faireeconomy: rate limit cleared, resuming normal polling.")
    _fe_backoff_until = None
    _fe_failure_logged = False


def reset_provider_state() -> None:
    """Clear all module state so tests do not leak into one another."""
    from app.ingestion import fj_calendar

    global _fe_backoff_until, _fe_failure_logged
    _fe_backoff_until = None
    _fe_failure_logged = False
    fj_calendar.reset_state_for_tests()


def _load_cached_events() -> list[CalendarEvent]:
    """Last good payload, if it is inside the allowed age (phase 8)."""
    if not settings.calendar_cache_enabled:
        return []
    if not settings.calendar_serve_stale_on_failure:
        return []
    try:
        from app.ingestion.calendar_cache import load_cache

        return load_cache(max_age_days=int(settings.calendar_cache_max_age_days))
    except Exception as exc:  # noqa: BLE001 - cache must never break a poll
        log.debug("calendar cache unavailable: %s", exc)
        return []