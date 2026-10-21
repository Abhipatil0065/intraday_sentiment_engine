"""
ECONOMIC CALENDAR INGESTION.

Fetches scheduled macro releases via FinancialJuice and normalises them into
``CalendarEvent`` records. The provider is deliberately tolerant: every network
or schema problem degrades to "no calendar today" rather than an exception that
could take the pipeline down.

REGION WHITELIST
----------------
Before the events reach the sentiment engine they pass through
``app.analysis.calendar_config.filter_events``: only events from the regions
listed in ``app/config/calendar_bias.json`` (default: USA, Eurozone, China,
Japan, UK) survive, and only when their title matches that region's event
list. The filter is a switch (``calendar_region_filter_enabled``): with it
off, the behaviour is EXACTLY what it was before the filter existed.

A broken config file must never change a published verdict, so the filter
fails SAFE: it logs the error and returns the events unfiltered.

WHAT THE FILTER DOES NOT HIDE
------------------------------
A high-impact release outside the whitelist - a Swiss CPI, an RBA decision -
still moves the market. It simply must not SCORE anything. Those events are
therefore kept aside (see :func:`filtered_out_events`) and still raise the
EVENT RISK note, labelled "other region".
"""

from __future__ import annotations

from app.analysis.calendar_merge import MergeStats, merge_events
from app.analysis.economic_impact import CalendarEvent
from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

#: The events the whitelist dropped on the most recent poll, with the reason.
#: They contribute ZERO to every score; they exist only so the EVENT RISK
#: note can still warn about a market-moving print from another region.
_filtered_out: list[tuple[CalendarEvent, str]] = []


def filtered_out_events() -> list[tuple[CalendarEvent, str]]:
    """
    The events the region whitelist dropped on the last poll, with reasons.

    Read-only copy: the caller cannot mutate the poll's state.
    """
    return list(_filtered_out)


def apply_region_filter(events: list[CalendarEvent]
                        ) -> list[CalendarEvent]:
    """
    Drop every event outside the configured region whitelist.

    Returns the events unchanged when the filter is switched off, and also
    when the config file cannot be read - a config typo must not silently
    change what a published verdict is made of.
    """
    global _filtered_out

    if not settings.calendar_region_filter_enabled:
        _filtered_out = []
        return events

    from app.analysis.calendar_config import (
        CalendarConfigError,
        filter_events,
    )

    try:
        kept, dropped = filter_events(events)
    except CalendarConfigError as exc:
        log.error("Economic calendar: region filter unavailable (%s) - "
                  "serving %d event(s) UNFILTERED", exc, len(events))
        _filtered_out = []
        return events
    except Exception as exc:  # noqa: BLE001 - a filter must never break a poll
        log.error("Economic calendar: region filter failed (%s: %s) - "
                  "serving %d event(s) UNFILTERED",
                  type(exc).__name__, exc, len(events))
        _filtered_out = []
        return events

    _filtered_out = list(dropped)
    if dropped:
        reasons: dict[str, int] = {}
        for _event, reason in dropped:
            reasons[reason] = reasons.get(reason, 0) + 1
        detail = ", ".join(f"{count}x {reason}" for reason, count in
                           sorted(reasons.items(), key=lambda kv: -kv[1]))
        log.info("Economic calendar: region filter kept %d of %d event(s); "
                 "dropped %d (%s)", len(kept), len(events), len(dropped), detail)
    else:
        log.info("Economic calendar: region filter kept %d of %d event(s)",
                 len(kept), len(events))
    return kept


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
                    merged = cached
        except Exception as exc:  # noqa: BLE001 - cache must never break a poll
            log.debug("calendar cache unavailable: %s", exc)

    if merged:
        try:
            from app.ingestion.calendar_cache import save_cache
            save_cache(merged)
        except Exception as exc:  # noqa: BLE001 - cache must never break a poll
            log.debug("calendar cache save skipped: %s", exc)

    # The last 48h of raw events are persisted, so the time decay still works
    # after a restart: the provider only ever exposes TODAY, and an in-memory
    # calendar would lose yesterday's releases - and their decay - on every
    # restart. The history holds the RAW feed; the whitelist and the forced
    # impact are applied to it on every poll, so a config edit still takes
    # effect immediately.
    merged = merge_with_history(merged)

    # The region whitelist is applied HERE - at the calendar input - so every
    # consumer of the calendar (the sentiment engine, the dashboard panels,
    # the event-risk check) sees the same whitelisted set.
    kept = apply_region_filter(merged)

    # Forced impact (config: impact_overrides) runs next and BEFORE any
    # impact weighting, so the weight, the upcoming-release list and the
    # event-risk window all see the forced value.
    kept = apply_impact_overrides_logged(kept)

    return kept, errors


def apply_impact_overrides_logged(events: list[CalendarEvent]
                                  ) -> list[CalendarEvent]:
    """
    Force the impact of the events the config marks as more important, and
    LOG every change: which event, from what, to what, and on which keyword.
    """
    from app.analysis.calendar_config import (
        CalendarConfigError,
        apply_impact_overrides,
    )

    try:
        overridden, changes = apply_impact_overrides(events)
    except CalendarConfigError as exc:
        log.error("Economic calendar: impact overrides unavailable (%s) - "
                  "serving %d event(s) with their own impact", exc, len(events))
        return events
    except Exception as exc:  # noqa: BLE001 - an override must never break a poll
        log.error("Economic calendar: impact overrides failed (%s: %s) - "
                  "serving %d event(s) with their own impact",
                  type(exc).__name__, exc, len(events))
        return events

    for event, original, forced, hits in changes:
        log.info("Economic calendar: impact override - %s (%s) %s -> %s "
                 "(matched %s)", event.title[:60], event.country, original,
                 forced, ", ".join(hits))
    if changes:
        log.info("Economic calendar: %d event(s) had their impact forced",
                 len(changes))
    return overridden


def merge_with_history(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """
    Add the persisted last-48h history to this poll's events.

    The provider only exposes TODAY, so without this the calendar - and with
    it the time decay of yesterday's prints - starts from zero after every
    restart. The history is best-effort: a missing or unreadable file simply
    means no history.
    """
    try:
        from app.ingestion.calendar_cache import (
            load_history,
            merge_history,
            save_history,
        )

        merged = merge_history(events)
        save_history(merged)
        return merged
    except Exception as exc:  # noqa: BLE001 - history must never break a poll
        log.debug("calendar history merge skipped: %s", exc)
        return events


def reset_provider_state() -> None:
    from app.ingestion import fj_calendar
    fj_calendar.reset_state_for_tests()
