"""Live source probe - reports how many articles each feed has for TODAY.

Run:  python -m tests.probe_sources
Useful for validating a newly added feed before wiring it into .env.
"""

from __future__ import annotations

import asyncio

from app.ingestion.news_fetcher import build_sources
from app.utils.timestamp_parse import is_today, parse_timestamp


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
        latest = max(parsed)[0].isoformat() if parsed else "n/a"
        status = "LIVE" if today else "STALE"
        print(f"{status:7} {source.name:20} {len(result):6} {len(today):6}  {latest}")


if __name__ == "__main__":
    asyncio.run(probe())