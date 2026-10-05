"""Live source probe - reports how many articles each feed has for TODAY.

Run:  python -m tests.probe_sources
Useful for validating a newly added feed before wiring it into .env.

READ THE STATUS COLUMN CAREFULLY
--------------------------------
``STALE`` does NOT mean a feed is broken. Most of these publishers are WEEKDAY
US sources, so overnight and at weekends they legitimately publish nothing new
and the strict today-filter reports zero. A probe run at 02:00 New York on a
Monday shows Kitco (last article Friday), the Fed (Friday) and MarketWatch
(Saturday) as STALE even though all three are perfectly healthy.

``QUIET`` marks that case: the feed resolves, carries a normal volume of
articles, and its newest item is recent enough to look alive - it simply has
nothing from TODAY. ``DEAD`` is the real failure: no usable timestamp anywhere,
or nothing inside a generous window. Only DEAD justifies removing a source.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.ingestion.news_fetcher import build_sources
from app.utils.timestamp_parse import is_today, parse_timestamp

# A feed whose newest article is older than this is treated as genuinely dead.
# Wide enough to clear a weekend, narrow enough to catch a feed that died.
DEAD_AFTER = timedelta(days=4)


async def probe() -> None:
    sources = build_sources()
    results = await asyncio.gather(
        *(s.fetch() for s in sources), return_exceptions=True
    )
    print(f"{'STATUS':7} {'SOURCE':20} {'TOTAL':>6} {'TODAY':>6}  LATEST")
    print("-" * 78)
    for source, result in zip(sources, results):
        if isinstance(result, Exception):
            print(f"{'ERROR':7} {source.name:20} {'-':>6} {'-':>6}  "
                  f"{type(result).__name__}")
            continue
        parsed = [(parse_timestamp(a.published_raw)[0], a.title) for a in result]
        parsed = [(d, t) for d, t in parsed if d is not None]
        today = [d for d, _ in parsed if is_today(d)]
        latest = max(parsed)[0] if parsed else None
        latest_s = latest.isoformat() if latest else "n/a"

        if today:
            status = "LIVE"
        elif latest is None:
            status = "DEAD"
        elif datetime.now(timezone.utc) - latest > DEAD_AFTER:
            status = "DEAD"
        else:
            # Resolves, has volume, recent-ish - just nothing from today.
            status = "QUIET"
        print(f"{status:7} {source.name:20} {len(result):6} {len(today):6}  {latest_s}")

    print("\nLIVE = publishing today.  QUIET = healthy, nothing from today yet "
          "(normal overnight/weekend).\nDEAD = genuinely stale or unparseable - "
          "investigate before removing.  ERROR = fetch failed, engine fails soft.")


if __name__ == "__main__":
    asyncio.run(probe())