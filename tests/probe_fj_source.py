"""Live test of the FinancialJuice headless source.

Run:  python -m tests.probe_fj_source
"""

from __future__ import annotations

import asyncio

from app.ingestion.base import parse_source_config
from app.ingestion.news_fetcher import build_sources
from app.utils.timestamp_parse import is_today, parse_timestamp


async def main() -> None:
    spec = parse_source_config(
        "Financial Juice|fj|https://www.financialjuice.com/|0.9|TIER_2_MAJOR"
    )
    from app.ingestion.financialjuice import FinancialJuiceSource

    src = FinancialJuiceSource(spec[0])
    print(f"source={src.name} available={src.available()}")
    arts = await src.fetch()
    print(f"headlines extracted: {len(arts)}\n")
    today = 0
    for a in arts[:20]:
        dt, _ = parse_timestamp(a.published_raw)
        ok = is_today(dt)
        today += bool(ok)
        stamp = dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "NO-TIMESTAMP"
        print(f"  [{'TODAY' if ok else 'skip '}] {stamp}  {a.title[:78]}")
    print(f"\ntoday-dated: {today}/{len(arts)}")
    _ = build_sources  # keep import meaningful for future assertions


if __name__ == "__main__":
    asyncio.run(main())