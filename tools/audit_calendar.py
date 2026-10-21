"""
CALENDAR AUDIT.

Prints every calendar event with the country DERIVED from its title, the
region the whitelist assigned it, whether it was kept or dropped and why -
and flags every derivation that is not trustworthy.

    python tools/audit_calendar.py            # the persisted cache
    python tools/audit_calendar.py --live     # a live FinancialJuice poll

WHY THE COUNTRY MATTERS
-----------------------
FJ renders no country column, so the country is derived from the event title.
That derivation is the key that decides which region - and therefore which
weight and which sensitivity row - a release is scored against, so it is
audited rather than trusted.

A derivation is LOW CONFIDENCE when it came from the GENERIC fallback list
("cpi", "pmi", "gdp", ...): the title named no country at all and was
attributed to the US because it mentions an indicator. That answer is usually
right and always unproven - "Swiss CPI" and "US CPI" contain the same words.

Only SCHEDULED MACRO RELEASES are read. No price, ever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analysis.calendar_config import classify_region, load_calendar_config
from app.analysis.economic_impact import CalendarEvent
from app.ingestion import fj_calendar
from app.ingestion.calendar_cache import load_cache

#: Words that name a country the hints table does NOT know. A title with one
#: of them cannot have been derived correctly unless another hint matched
#: first - which is exactly what the audit is looking for.
UNKNOWN_COUNTRY_WORDS = (
    "taiwan", "singapore", "mexico", "israel", "poland", "indonesia",
    "thailand", "vietnam", "philippines", "malaysia", "saudi", "emirates",
    "qatar", "kuwait", "africa", "nigeria", "egypt", "kenya", "argentina",
    "chile", "colombia", "peru", "romania", "hungary", "czech", "greece",
    "ireland", "belgium", "austria", "sweden", "norway", "denmark", "finland",
    "iceland", "bangladesh", "sri lanka", "nepal", "pakistan", "iran", "iraq",
    "jordan", "lebanon", "ukraine", "russia", "belarus", "kazakhstan",
    "venezuela", "bolivia", "paraguay", "uruguay", "morocco", "tunisia",
    "ethiopia", "ghana", "croatia", "serbia", "bulgaria", "slovakia",
    "slovenia", "estonia", "latvia", "lithuania", "ecuador", "brunei",
    "cambodia", "myanmar", "laos", "mongolia", "tajikistan", "turkmenistan",
    "uzbekistan", "kyrgyzstan", "armenia", "georgia", "azerbaijan", "kosovo",
    "moldova", "cyprus", "malta",
)


def _events_from_cache(days: int) -> list[CalendarEvent]:
    events = load_cache()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [e for e in events
            if e.when_utc is not None and e.when_utc >= cutoff]


def _events_live() -> list[CalendarEvent]:
    result = asyncio.run(fj_calendar.fetch_fj_calendar())
    if not result.ok:
        raise SystemExit(f"live poll failed: {result.error}")
    return list(result.events)


def audit(events: list[CalendarEvent], days: int = 7) -> int:
    cfg = load_calendar_config()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    print("=" * 116)
    print("CALENDAR AUDIT - FinancialJuice feed")
    print(f"generated {now.isoformat()} | {len(events)} event(s) | "
          f"window: {cutoff.date()} .. now")
    print("=" * 116)
    print(f"{'WHEN (UTC)':17s} {'CCY':5s} {'CONF':6s} {'REGION':6s} "
          f"{'FATE':6s} {'IMPACT':7s} TITLE / REASON")
    print("-" * 116)

    kept = dropped = low_conf = 0
    flags: list[tuple[str, str, str]] = []
    for event in sorted(events, key=lambda e: (e.when_utc or now, e.title)):
        when = event.when_utc
        code, needle, is_generic = fj_calendar.guess_country_detail(event.title)
        region, reason = classify_region(event, cfg)
        region_code = region.code if region else "-"
        if region:
            kept += 1
        else:
            dropped += 1
        confidence = "LOW" if is_generic else "high"
        if is_generic:
            low_conf += 1
        stamp = when.strftime("%Y-%m-%d %H:%M") if when else "?"
        detail = event.title if region else f"{event.title}  <- {reason}"
        print(f"{stamp:17s} {event.country or '(none)':5s} {confidence:6s} "
              f"{region_code:6s} {'KEPT' if region else 'DROP':6s} "
              f"{event.impact:7s} {detail}")

        if not event.country:
            flags.append(("UNRESOLVED", event.title,
                          "no country marker found - the row was dropped"))
        elif code and code != event.country:
            flags.append(("MISMATCH", event.title,
                          f"stored {event.country} but the hints say {code} "
                          f"via '{needle}'"))
        elif is_generic:
            flags.append(("LOW-CONFIDENCE", event.title,
                          f"country {event.country} came from the GENERIC "
                          f"fallback ('{needle}') - the title names no country"))
        else:
            lowered = event.title.lower()
            for word in UNKNOWN_COUNTRY_WORDS:
                if word in lowered:
                    flags.append(("UNKNOWN-COUNTRY-WORD", event.title,
                                  f"the title mentions '{word}', which is not "
                                  f"in the hints table, but the country was "
                                  f"set to {event.country} via '{needle}'"))
                    break

    print("-" * 116)
    print(f"kept {kept} | dropped {dropped} | low-confidence derivations "
          f"{low_conf}")

    print()
    print("=" * 116)
    print(f"DERIVATION FLAGS ({len(flags)})")
    print("=" * 116)
    if not flags:
        print("none")
    for kind, title, note in flags:
        print(f"[{kind:20s}] {title}\n{'':22s} {note}")
    return len(flags)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="poll FinancialJuice instead of reading the cache")
    parser.add_argument("--days", type=int, default=7,
                        help="how many days back to audit (default 7)")
    args = parser.parse_args()

    events = _events_live() if args.live else _events_from_cache(args.days)
    if not events:
        raise SystemExit("no calendar events to audit (cache empty? "
                         "try --live)")
    audit(events, args.days)


if __name__ == "__main__":
    main()
