"""Phase 3: Ingestion audit - live sources + calendar merge."""
import asyncio
import os
import sys

# Set mock mode first for the mock source test
os.environ["USE_MOCK_SOURCE"] = "true"

from app.config import settings
from app.ingestion.base import parse_source_config
from app.ingestion.rss_reader import RssSource
from app.ingestion.mock_source import MockSource
from app.utils.timestamp_parse import parse_timestamp, is_today
from app.analysis.economic_impact import CalendarEvent
from app.analysis.calendar_merge import merge_events, MergeStats, title_similarity, same_release
from datetime import datetime, timezone, timedelta


print("=" * 70)
print("PHASE 3: INGESTION AUDIT")
print("=" * 70)

# --- 3a: Mock source (offline) ---
print("\n--- 3a: Mock Source (USE_MOCK_SOURCE=true) ---")
async def test_mock():
    src = MockSource()
    arts = await src.fetch()
    print(f"  Source: {src.name}, Quality: {src.quality}, Type: {src.kind}")
    print(f"  Articles fetched: {len(arts)}")
    today = sum(1 for a in arts if is_today(parse_timestamp(a.published_raw)[0]))
    print(f"  Today-dated: {today}/{len(arts)}")
    for a in arts[:5]:
        print(f"    - {a.title[:70]}")
    return len(arts)

mock_count = asyncio.run(test_mock())

# --- 3b: Live RSS source (Fed Press Releases - TIER_1) ---
print("\n--- 3b: Live RSS - Fed Press Releases ---")
async def test_fed_rss():
    spec = parse_source_config(
        "Fed Press Releases|rss|https://www.federalreserve.gov/feeds/press_all.xml|1.2|TIER_1_OFFICIAL"
    )
    src = RssSource(spec[0])
    print(f"  Source: {src.name}, URL: {src.url}")
    try:
        arts = await asyncio.wait_for(src.fetch(), timeout=15)
        print(f"  Articles fetched: {len(arts)}")
        today = 0
        for a in arts[:10]:
            dt, _ = parse_timestamp(a.published_raw)
            ok = is_today(dt) if dt else False
            today += int(bool(ok))
            ds = dt.strftime("%m-%d %H:%M UTC") if dt else "NO-TIMESTAMP"
            label = "TODAY" if ok else "skip "
            print(f"    [{label}] {ds}  {a.title[:68]}")
        print(f"  Today-dated: {today}/{len(arts)}")
        return len(arts), today
    except asyncio.TimeoutError:
        print(f"  TIMEOUT after 15s")
        return 0, 0
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return 0, 0

fed_count, fed_today = asyncio.run(test_fed_rss())

# --- 3c: Live RSS source (Bloomberg Markets) ---
print("\n--- 3c: Live RSS - Bloomberg Markets ---")
async def test_bloomberg_rss():
    spec = parse_source_config(
        "Bloomberg Markets|rss|https://feeds.bloomberg.com/markets/news.rss|1.0|TIER_2_MAJOR"
    )
    src = RssSource(spec[0])
    print(f"  Source: {src.name}, URL: {src.url}")
    try:
        arts = await asyncio.wait_for(src.fetch(), timeout=15)
        print(f"  Articles fetched: {len(arts)}")
        today = 0
        for a in arts[:10]:
            dt, _ = parse_timestamp(a.published_raw)
            ok = is_today(dt) if dt else False
            today += int(bool(ok))
            ds = dt.strftime("%m-%d %H:%M UTC") if dt else "NO-TIMESTAMP"
            label = "TODAY" if ok else "skip "
            print(f"    [{label}] {ds}  {a.title[:68]}")
        print(f"  Today-dated: {today}/{len(arts)}")
        return len(arts), today
    except asyncio.TimeoutError:
        print(f"  TIMEOUT after 15s")
        return 0, 0
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return 0, 0

bl_count, bl_today = asyncio.run(test_bloomberg_rss())

# --- 3d: Calendar merge test (synthetic) ---
print("\n--- 3d: Calendar Merge (synthetic test) ---")
now = datetime.now(timezone.utc)

fj_events = [
    CalendarEvent(title="US CPI m/m", country="USD", when_utc=now, impact="High",
                  forecast=0.3, previous=0.2, actual=0.4, provider="financialjuice"),
    CalendarEvent(title="Nonfarm Payrolls", country="USD", when_utc=now + timedelta(hours=2),
                  impact="Very High", forecast=180, previous=175, actual=190, provider="financialjuice"),
]
fe_events = [
    CalendarEvent(title="CPI m/m (Median)", country="USD", when_utc=now, impact="High",
                  forecast=0.3, previous=0.2, actual=0.4, provider="faireconomy"),
    CalendarEvent(title="US Non Farm Payrolls", country="USD", when_utc=now + timedelta(hours=2),
                  impact="Very High", forecast=180, previous=175, actual=190, provider="faireconomy"),
]

stats = MergeStats()
merged = merge_events([("financialjuice", fj_events), ("faireconomy", fe_events)], stats)
print(f"  Input: {len(fj_events)} FJ + {len(fe_events)} FE = {len(fj_events)+len(fe_events)} total")
print(f"  Merged events: {len(merged)}")
print(f"  Stats: {stats.as_dict()}")
print(f"  Merged samples:")
for e in merged:
    print(f"    - {e.title[:40]:<40} {e.country:<4} {e.when_utc.strftime('%m-%d %H:%M')} imp={e.impact:<6} a={e.actual} f={e.forecast} p={e.previous} [{e.provider}]")

# Test similarity threshold
print(f"\n  Similarity(FJ 'US CPI m/m', FE 'CPI m/m (Median)'): {title_similarity('US CPI m/m', 'CPI m/m (Median)'):.3f}")
print(f"  Similarity(FJ 'Nonfarm Payrolls', FE 'US Non Farm Payrolls'): {title_similarity('Nonfarm Payrolls', 'US Non Farm Payrolls'):.3f}")

# --- 3e: Newsquawk ---
print("\n--- 3e: Newsquawk ---")
print("  NOTE: app/ingestion/newsquawk.py does NOT exist (stale tab reference)")
print("  Newsquawk is not configured as a source in NEWS_SOURCES")

# --- 3f: FinancialJuice calendar ---
print("\n--- 3f: FinancialJuice Calendar ---")
print("  Requires Playwright + Chromium (browser-based render)")
print("  Config: FINANCIALJUICE_CALENDAR_ENABLED=true, FJ_CALENDAR_TIMEOUT_SECONDS=40")
try:
    from playwright.async_api import async_playwright
    has_playwright = True
except ImportError:
    has_playwright = False
    print("  Playwright not installed in venv")

if has_playwright:
    print("  Playwright available - will test in live section below")
else:
    print("  Skipped (Playwright not available)")

print("\n" + "=" * 70)
print("PHASE 3 COMPLETE")
print("=" * 70)
