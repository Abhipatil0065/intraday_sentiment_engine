"""
TradingView news source.

tradingview.com/news is a client-rendered single-page app: its raw HTML
contains ZERO headlines and it publishes no public RSS/Atom feed. Headlines
only exist after JavaScript executes, so this source requires Playwright
(an optional dependency):

    pip install playwright
    playwright install chromium

It is OPT-IN. Without Playwright the source logs one clear message and yields
nothing - it never breaks the pipeline and never fabricates data. Enable it
by adding this entry to NEWS_SOURCES in .env:

  TradingView News|tradingview_api|https://www.tradingview.com/news/|1.0|TIER_1_OFFICIAL
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.ingestion.base import RawArticle, SourceSpec, strip_html
from app.ingestion.chromium_lock import chromium_slot
from app.logging_conf import get_logger

log = get_logger(__name__)

# Relative-time patterns TradingView renders inside news items
# ("2h ago", "45m ago", "just now").
_TIME_RE = re.compile(
    r"(\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\s+ago)|"
    r"(just now)",
    re.IGNORECASE,
)

# Navigation / boilerplate that is never news.
_SKIP = (
    "sign in", "log in", "subscribe", "newsletter", "privacy policy",
    "terms of", "contact us", "advertise", "download the app", "follow us",
    "cookie", "disclaimer", "about us", "careers", "markets", "screener",
    "watchlist", "charting", "pine script", "broker", "paper trading",
)

# Article links on tradingview.com/news look like:
#   /news/reuters.com,2026:newsml_...:0-headline/
#   /news/te_news:593483:0-headline/
_ARTICLE_HREF = re.compile(r"^/news/")


class TradingViewSource:
    """Renders tradingview.com/news with Playwright and extracts headlines."""

    kind = "tradingview_api"

    def __init__(self, spec: SourceSpec) -> None:
        self.name = spec.name
        self.quality = spec.quality
        self.url = spec.url
        self.wait_ms = 8000

    @staticmethod
    def available() -> bool:
        """True only if Playwright is importable (browser must also exist)."""
        try:
            import playwright.async_api  # noqa: F401
            return True
        except ImportError:
            return False

    async def fetch(self) -> list[RawArticle]:
        if not self.available():
            log.warning(
                "TradingView skipped: Playwright not installed. "
                "Run: pip install playwright && playwright install chromium"
            )
            return []

        from playwright.async_api import async_playwright

        async with chromium_slot(
                wait_seconds=settings.chromium_lock_wait_seconds) as slot:
            if not slot:
                log.warning("TradingView skipped this cycle: chromium busy "
                            "with another render; deferring to the next poll.")
                return []

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    ctx = await browser.new_context(
                        user_agent=settings.http_user_agent,
                        viewport={"width": 1440, "height": 900},
                        locale="en-US",
                        timezone_id=settings.display_timezone,
                    )
                    try:
                        page = await ctx.new_page()
                        await page.goto(
                            self.url, wait_until="networkidle",
                            timeout=settings.http_timeout_seconds * 1000,
                        )
                        # Let the SPA hydrate and stream in headlines.
                        await page.wait_for_timeout(self.wait_ms)
                        html = await page.content()
                    finally:
                        await ctx.close()
                finally:
                    await browser.close()

        return self._parse(html)

    def _parse(self, html: str) -> list[RawArticle]:
        """Parse RENDERED TradingView HTML into RawArticles."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        now = datetime.now(timezone.utc)
        seen: set[str] = set()
        out: list[RawArticle] = []

        # Candidate anchors whose href looks like a TradingView news article.
        candidates: list[tuple] = []
        for a in soup.find_all("a", href=True):
            href = strip_html(a["href"])
            if not _ARTICLE_HREF.match(href):
                continue
            title = strip_html(a.get_text(" "))
            if len(title) < 15:
                continue
            candidates.append((a, title, href))

        # Fall back to headings if no article anchors found.
        if not candidates:
            for tag in soup.find_all(("h1", "h2", "h3")):
                anchor = tag.find("a", href=True)
                if anchor is None:
                    continue
                title = strip_html(tag.get_text(" "))
                href = strip_html(anchor["href"])
                if len(title) < 15 or not _ARTICLE_HREF.match(href):
                    continue
                candidates.append((anchor, title, href))

        for el, title, href in candidates:
            low = title.lower()
            if any(k in low for k in _SKIP):
                continue
            key = low[:80]
            if key in seen:
                continue
            seen.add(key)

            # Resolve relative URLs against the site root.
            if href.startswith("/"):
                href = "https://www.tradingview.com" + href
            elif not href.startswith("http"):
                continue

            # Nearest ancestor carrying a relative timestamp.
            stamp = None
            parent = el.parent
            for _ in range(4):
                if parent is None:
                    break
                match = _TIME_RE.search(strip_html(parent.get_text(" ")))
                if match:
                    stamp = self._to_iso(match.group(0), now)
                    break
                parent = parent.parent

            out.append(RawArticle(
                guid="", source=self.name, source_quality=self.quality,
                url=href,
                title=title, body="", summary="",
                published_raw=stamp,
            ).ensure_guid())

        log.info("TradingView: extracted %d headlines", len(out))
        return out

    @staticmethod
    def _to_iso(text: str | None, now: datetime) -> str | None:
        """Convert '2h ago' / 'just now' into an ISO UTC timestamp."""
        if not text:
            return None
        t = text.strip().lower()
        if "just now" in t:
            return now.isoformat()
        m = _TIME_RE.search(t)
        if not m:
            return None
        found = re.search(r"\d+", m.group(0))
        if not found:
            return None
        num = int(found.group())
        unit = m.group(0).lower()
        if "second" in unit or "sec" in unit:
            delta = timedelta(seconds=num)
        elif "minute" in unit or "min" in unit:
            delta = timedelta(minutes=num)
        elif "hour" in unit or "hr" in unit:
            delta = timedelta(hours=num)
        elif "day" in unit:
            delta = timedelta(days=num)
        else:
            return None
        return (now - delta).isoformat()
