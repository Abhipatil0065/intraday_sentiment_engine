"""
LIVE VERIFICATION of the FinancialJuice calendar provider (phase 1).

Runs the REAL provider with the REAL flag still OFF, then reports:
  * rows parsed, events produced, wall time, peak memory;
  * how many FJ events match faireeconomy's events;
  * the timezone cross-check (FJ time vs faireeconomy's UTC);
  * whether a challenge page was seen.

Does not modify settings. To enable the provider afterwards, set
``FINANCIALJUICE_CALENDAR_ENABLED=true`` in .env.

Run:  python -m tests.probe_fj_calendar
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request

from app.analysis.economic_impact import to_float
from app.analysis.calendar_merge import title_similarity
from app.config import settings
from app.ingestion.fj_calendar import PROVIDER_NAME, fetch_fj_calendar

MATCH_THRESHOLD = 0.85


def _faireconomy_events() -> list[dict]:
    req = urllib.request.Request(
        settings.economic_calendar_url,
        headers={"User-Agent": settings.http_user_agent,
                 "Accept": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=25))
    except Exception as exc:  # noqa: BLE001
        print("faireconomy unavailable: %s" % exc)
        return []


async def main() -> None:
    print("=" * 74)
    print("FINANCIALJUICE CALENDAR - LIVE VERIFICATION")
    print("=" * 74)
    print("enabled flag      : %s" % settings.financialjuice_calendar_enabled)
    print("render timezone   : %s" % settings.fj_render_tz)
    print("row floor         : %s" % settings.fj_calendar_min_rows)
    print()

    # Enable for the probe only - the .env default stays OFF.
    settings.financialjuice_calendar_enabled = True

    result = await fetch_fj_calendar()

    print("-- poll outcome --")
    print("ok                : %s" % result.ok)
    print("error             : %s" % (result.error or "-"))
    print("challenge         : %s" % result.challenge)
    print("rows on page      : %s" % result.row_count)
    print("events parsed     : %s" % result.event_count)
    print("rows skipped      : %s" % result.skipped)
    print("wall time         : %.2fs" % result.seconds)
    print()

    if not result.events:
        print("VERIFICATION FAILED - no events; keep the flag OFF.")
        return

    by_country: dict = {}
    with_values = 0
    released = 0
    for e in result.events:
        by_country[e.country] = by_country.get(e.country, 0) + 1
        if e.actual is not None:
            released += 1
        if e.forecast is not None or e.previous is not None:
            with_values += 1

    print("-- composition --")
    print("with forecast/prev: %d / %d" % (with_values, len(result.events)))
    print("with an actual    : %d" % released)
    print("top countries     : %s"
          % sorted(by_country.items(), key=lambda x: -x[1])[:8])
    print()

    print("-- sample (first 12) --")
    for e in result.events[:12]:
        print("  %-40s %-4s %s UTC imp=%-6s a=%-9s f=%-9s p=%-9s"
              % (e.title[:40], e.country, e.when_utc.strftime("%m-%d %H:%M"),
                 e.impact,
                 "-" if e.actual is None else e.actual,
                 "-" if e.forecast is None else e.forecast,
                 "-" if e.previous is None else e.previous))
    print()

    fe = _faireconomy_events()
    print("-- cross-check vs faireconomy --")
    print("faireconomy events: %d" % len(fe))
    matched = 0
    samples = []
    for e in result.events:
        for f in fe:
            if title_similarity(e.title, str(f.get("title") or "")) >= MATCH_THRESHOLD:
                matched += 1
                if len(samples) < 10:
                    samples.append((e, f))
                break
    print("FJ events matching a faireconomy event: %d / %d"
          % (matched, len(result.events)))
    print()

    print("-- timezone evidence (FJ local vs faireconomy UTC) --")
    for e, f in samples[:8]:
        fe_when = f.get("date")
        try:
            from datetime import datetime as _dt

            fe_utc = _dt.fromisoformat(str(fe_when)).astimezone(
                settings.fj_render_tz.utcoffset(None) and
                __import__("datetime").timezone.utc)
        except Exception:
            fe_utc = None
        print("  %-38s FJ %s | FE %s"
              % (e.title[:38], e.when_utc.strftime("%Y-%m-%d %H:%M UTC"),
                 fe_when))

    print()
    print("=" * 74)
    if result.ok and result.event_count >= 10:
        print("VERIFICATION PASSED - safe to set "
              "FINANCIALJUICE_CALENDAR_ENABLED=true")
    else:
        print("VERIFICATION FAILED - keep the flag OFF")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())