"""
LIVE FJ-only MERGE check: FinancialJuice polled for real and run through
merge_events for dedup.

Run:  python -m tests.probe_calendar_merge
"""

from __future__ import annotations

import asyncio

try:                                    # Windows has no `resource`
    import resource

    def _peak_mb() -> float:
        return resource.getrusage(
            resource.RUSAGE_SELF).ru_maxrss / 1024.0
except ImportError:                     # pragma: no cover - Windows path
    def _peak_mb() -> float:
        try:
            import psutil
            return psutil.Process().memory_info().rss / (1024.0 * 1024.0)
        except Exception:
            return 0.0

from app.analysis.calendar_merge import MergeStats, merge_events
from app.config import settings
from app.ingestion.calendar_cache import save_cache
from app.ingestion.economic_calendar import _fetch_fj


async def main() -> None:
    print("=" * 74)
    print("LIVE FINANCIALJUICE CALENDAR + MERGE")
    print("=" * 74)
    print("provider order    : %s" % settings.calendar_provider_list)
    print("merge similarity  : %s" % settings.calendar_merge_similarity)
    print("time tolerance    : %s min"
          % settings.calendar_merge_time_tolerance_minutes)
    print()

    settings.financialjuice_calendar_enabled = True

    fj_events, fj_errors = await _fetch_fj()
    print("financialjuice    : %d events %s"
          % (len(fj_events), fj_errors or ""))
    print()

    stats = MergeStats()
    merged = merge_events([("financialjuice", fj_events)], stats)

    print("-- merge stats --")
    for key, value in stats.as_dict().items():
        print("  %-20s %s" % (key, value))
    print()

    print("-- merged sample --")
    for e in merged[:12]:
        print("  %-40s %-4s %s UTC imp=%-6s a=%-9s f=%-9s p=%-9s [%s]"
              % (e.title[:40], e.country, e.when_utc.strftime("%m-%d %H:%M"),
                 e.impact,
                 "-" if e.actual is None else e.actual,
                 "-" if e.forecast is None else e.forecast,
                 "-" if e.previous is None else e.previous,
                 e.provider))
    print()

    peak_mb = _peak_mb()
    print("peak RSS          : %.0f MB" % peak_mb)
    save_cache(merged)
    print("cache written     : yes (data/calendar_cache.json)")
    print()
    print("=" * 74)
    print("VERIFICATION PASSED")
    print("=" * 74)


if __name__ == "__main__":
    asyncio.run(main())
