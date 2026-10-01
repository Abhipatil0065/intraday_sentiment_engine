"""
Financial Juice source.

financialjuice.com is a client-side rendered single-page app: its HTML
contains ZERO headlines and it publishes no RSS/Atom feed (verified - every
conventional feed path 404s or returns the SPA shell). Headlines only exist
after JavaScript executes, and the site sits behind Cloudflare.

Therefore this source requires Playwright (an optional dependency):

    pip install playwright
    playwright install chromium

It is OPT-IN. Without Playwright the source logs one clear message and yields
nothing - it never breaks the pipeline and never fabricates data. Enable it
by adding this entry to NEWS_SOURCES in .env:

  Financial Juice|fj|https://www.financialjuice.com/|0.9|TIER_2_MAJOR
"""

from __future__ import annotations

import re
from hashlib import sha1 as key_hash
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.ingestion.base import RawArticle, SourceSpec, strip_html
from app.logging_conf import get_logger

log = get_logger(__name__)

_TIME_TEXT = re.compile(
    r"(\d+\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?)\s+ago)|(just now)",
    re.IGNORECASE,
)

# Navigation / boilerplate links that are never news.
_SKIP = (
    "sign in", "log in", "subscribe", "newsletter", "privacy policy",
    "terms of", "contact us", "advertise", "download the app", "follow us",
    "cookie", "disclaimer", "about us", "careers",
)


def _relative_seconds(text):
    """Convert '3 hours ago' / 'just now' into seconds elapsed.

    Absolute clock times are deliberately NOT handled: guessing a calendar
    date from '14:30' would risk presenting yesterday's news as today's.
    """
    if not text:
        return None
    t = text.strip().lower()
    if "just now" in t:
        return 0.0
    m = _TIME_TEXT.search(t)
    if not m:
        return None
    found = re.search(r"\d+", m.group(0))
    if not found:
        return None
    num = int(found.group())
    unit = m.group(0).lower()
    if "second" in unit:
        return float(num)
    if "minute" in unit or "min" in unit:
        return num * 60.0
    if "hour" in unit or "hr" in unit:
        return num * 3600.0
    return None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Financial Juice renders publication time inside each news item as
# "15:56 Oct 01" (24h clock, month + day). Verified against the live DOM.
_FJ_TIME = re.compile(
    r"(\d{1,2}):(\d{2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})"
)


def _parse_fj_time(text, now, market_today):
    """
    Convert 'HH:MM Mon DD' (as rendered by the site) into an ISO UTC stamp.

    FinancialJuice is a client-side app: it renders publication times in the
    BROWSER timezone, and Playwright inherits the machine timezone. The naive
    wall-clock must therefore be interpreted in that same zone before being
    converted to UTC. Interpreting it as UTC makes every headline appear
    hours in the future and the today-filter then rejects the entire feed.
    """
    if not text:
        return None
    m = _FJ_TIME.search(text)
    if not m:
        return _relative_iso(text, now)

    hour, minute = int(m.group(1)), int(m.group(2))
    month = _MONTHS.get(m.group(3).lower()[:3])
    day = int(m.group(4))
    if month is None or not (0 <= hour <= 23) or not (1 <= day <= 31):
        return None

    local_tz = ZoneInfo(settings.display_timezone)
    now_local = now.astimezone(local_tz)
    candidate = now_local.replace(
        year=now_local.year, month=month, day=day,
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    # The page carries no year: a date more than a day ahead belongs to
    # the previous year.
    if candidate - now_local > timedelta(days=1):
        try:
            candidate = candidate.replace(year=now_local.year - 1)
        except ValueError:  # 29 Feb
            candidate = candidate.replace(year=now_local.year - 1, day=28)

    return candidate.astimezone(timezone.utc).isoformat()


def _relative_iso(text, now):
    """Fallback: '3 hours ago' -> ISO timestamp."""
    elapsed = _relative_seconds(text)
    if elapsed is None:
        return None
    return datetime.fromtimestamp(
        now.timestamp() - elapsed, tz=timezone.utc).isoformat()


def _extract(html, name, quality):
    """Parse RENDERED Financial Juice HTML into RawArticles.

    DOM shape (verified against the live site):
        div.headline-item
            p.headline-title / span.headline-title-nolink   -> headline
            div.headline-content                            -> body
            a sibling span carries the publication time
    Headlines are div/p elements, NOT anchors - an anchor-based extractor
    finds only ads and promos.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(settings.market_timezone)).date()
    seen = set()
    out = []

    items = soup.select("div.headline-item") or soup.select("div.infinite-item")

    for item in items:
        title_el = item.select_one(".headline-title, .headline-title-nolink")
        if title_el is None:
            continue
        title = strip_html(title_el.get_text(" "))
        if len(title) < 15:
            continue
        low = title.lower()
        if any(k in low for k in _SKIP):
            continue

        key = low[:80]
        if key in seen:
            continue
        seen.add(key)

        body_el = item.select_one(".headline-content, .headline-content-container")
        body = strip_html(body_el.get_text(" ")) if body_el else ""

        # Publication time lives in the item's own text as "HH:MM Mon DD".
        stamp = _parse_fj_time(item.get_text(" "), now, today)

        # Many Financial Juice headlines are rendered as <p class=
        # "headline-title-nolink"> with NO anchor at all. Falling back to a
        # single shared URL would give every item an identical GUID and the
        # deduplicator would silently drop all but one, so build a unique
        # permalink from the headline slug instead.
        link = item.select_one("a[href*='http']")
        href = link["href"] if link else ""
        if not href or not item.select_one("a[href*='http']"):
            slug = re.sub(r"[^a-z0-9]+", "-", low).strip("-")[:70]
            href = f"https://www.financialjuice.com/{slug}-{key_hash(key.encode()).hexdigest()[:8]}"

        out.append(RawArticle(
            guid="", source=name, source_quality=quality,
            url=href,
            title=title, body=body[:1200], summary=body[:300],
            published_raw=stamp,
        ).ensure_guid())

    return out

class FinancialJuiceSource:
    """Renders financialjuice.com with Playwright and extracts headlines."""

    kind = "fj"

    def __init__(self, spec=None):
        self.name = spec.name if spec else "Financial Juice"
        self.quality = spec.quality if spec else "TIER_2_MAJOR"
        self.url = spec.url if spec else "https://www.financialjuice.com/"
        self.wait_ms = 6000

    @staticmethod
    def available():
        """True only if Playwright is importable (browser must also exist)."""
        try:
            import playwright.async_api  # noqa: F401
            return True
        except ImportError:
            return False

    async def fetch(self):
        if not self.available():
            log.warning(
                "Financial Juice skipped: Playwright not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
            return []

        from playwright.async_api import async_playwright

        html = ""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )
            try:
                ctx = await browser.new_context(
                    user_agent=settings.http_user_agent,
                    viewport={"width": 1440, "height": 2400},
                    locale="en-US",
                    # Match the zone _parse_fj_time assumes, so the rendered
                    # wall-clock and the parsed timestamp always agree.
                    timezone_id=settings.display_timezone,
                )
                page = await ctx.new_page()
                await page.goto(self.url, timeout=45000,
                                wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle",
                                                   timeout=self.wait_ms)
                except Exception:  # networkidle often never settles on SPAs
                    pass
                await page.wait_for_timeout(2500)
                html = await page.content()
            finally:
                await browser.close()

        articles = _extract(html, self.name, self.quality)
        log.info("Financial Juice: extracted %d headlines", len(articles))
        return articles






