"""
CALENDAR PAYLOAD CACHE (phase 8).

If EVERY provider fails, an empty calendar would silently read as "no macro
releases today" - which is a different and wrong statement. This keeps the last
good payload on disk so a total outage degrades to STALE rather than EMPTY,
with the staleness made explicit in the log.

Hard rules:
  * never serve a cache older than ``max_age_days`` (default 7). A week-old
    calendar is a different week and must not be presented as current;
  * a corrupt or unreadable cache is treated as absent, never fatal;
  * it holds SCHEDULED MACRO RELEASES ONLY - no price, ever.

Standard library only: json + os.replace for an atomic write.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.analysis.economic_impact import CalendarEvent
from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

CACHE_FILE = "data/calendar_cache.json"
_VERSION = 1


def _path() -> Path:
    base = Path(__file__).resolve().parents[2]
    return base / CACHE_FILE


def save_cache(events: list[CalendarEvent]) -> bool:
    """
    Persist the current events. Best-effort: a failure must never break a poll.
    """
    if not settings.calendar_cache_enabled or not events:
        return False
    target = _path()
    payload = {
        "version": _VERSION,
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "events": [
            {
                "title": e.title,
                "country": e.country,
                "when_utc": e.when_utc.isoformat() if e.when_utc else None,
                "impact": e.impact,
                "forecast": e.forecast,
                "previous": e.previous,
                "actual": e.actual,
                "provider": e.provider,
            }
            for e in events
        ],
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: write a temp file in the same directory, then rename. A
        # half-written cache is worse than no cache.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(target.parent),
            delete=False, suffix=".tmp") as fh:
            json.dump(payload, fh)
            temp_name = fh.name
        os.replace(temp_name, target)
        return True
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log.debug("calendar cache save skipped: %s", exc)
        return False


def load_cache(max_age_days: int | None = None
               ) -> list[CalendarEvent]:
    """Last good events, or [] when absent, unreadable, or too old."""
    if not settings.calendar_cache_enabled:
        return []
    days = int(max_age_days if max_age_days is not None
               else settings.calendar_cache_max_age_days)
    target = _path()
    try:
        if not target.exists():
            return []
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a bad cache is simply absent
        log.debug("calendar cache unreadable, ignoring: %s", exc)
        return []

    if data.get("version") != _VERSION:
        return []
    saved_raw = data.get("saved_at_utc")
    if not saved_raw:
        return []
    try:
        saved = datetime.fromisoformat(saved_raw)
    except (TypeError, ValueError):
        return []
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - saved
    if age > timedelta(days=days):
        log.warning("calendar cache is %.1f days old (> %d) - ignoring",
                    age.total_seconds() / 86400.0, days)
        return []

    out: list[CalendarEvent] = []
    for row in data.get("events") or []:
        try:
            when = (datetime.fromisoformat(row["when_utc"])
                    if row.get("when_utc") else None)
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            out.append(CalendarEvent(
                title=row.get("title") or "",
                country=row.get("country") or "",
                when_utc=when,
                impact=row.get("impact") or "Medium",
                forecast=row.get("forecast"),
                previous=row.get("previous"),
                actual=row.get("actual"),
                provider=(row.get("provider") or "cache") + ":stale",
            ))
        except Exception:  # noqa: BLE001 - skip one bad row, keep the rest
            continue
    return out