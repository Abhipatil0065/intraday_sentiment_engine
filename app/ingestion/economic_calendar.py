"""
ECONOMIC CALENDAR INGESTION.

Fetches scheduled macro releases via FinancialJuice and normalises them into
``CalendarEvent`` records. The provider is deliberately tolerant: every network
or schema problem degrades to "no calendar today" rather than an exception that
could take the pipeline down.
"""

from __future__ import annotations

from app.analysis.calendar_merge import MergeStats, merge_events
from app.analysis.economic_impact import CalendarEvent
from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)


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
    if result.error in ("disabled", "backoff"):
        return [], []
    return [], [f"economic calendar (financialjuice): {result.error}"]


async def fetch_calendar() -> tuple[list[CalendarEvent], list[str]]:
    if not settings.economic_calendar_enabled:
        return [], []

    errors: list[str] = []
    batches: list[tuple[str, list[CalendarEvent]]] = []

    events, provider_errors = await _fetch_fj()
    errors.extend(provider_errors)
    batches.append(("financialjuice", events))
    log.info("Economic calendar provider financialjuice -> %d event(s)", len(events))

    stats = MergeStats()
    merged = merge_events(batches, stats)

    if not merged:
        from app.ingestion.calendar_cache import load_cache
        try:
            if settings.calendar_cache_enabled and settings.calendar_serve_stale_on_failure:
                cached = load_cache(max_age_days=int(settings.calendar_cache_max_age_days))
                if cached:
                    log.warning("Economic calendar: serving %d CACHED event(s) (stale)",
                                len(cached))
                    return cached, errors
        except Exception as exc:  # noqa: BLE001 - cache must never break a poll
            log.debug("calendar cache unavailable: %s", exc)

    if merged:
        try:
            from app.ingestion.calendar_cache import save_cache
            save_cache(merged)
        except Exception as exc:  # noqa: BLE001 - cache must never break a poll
            log.debug("calendar cache save skipped: %s", exc)

    return merged, errors


def reset_provider_state() -> None:
    from app.ingestion import fj_calendar
    fj_calendar.reset_state_for_tests()
