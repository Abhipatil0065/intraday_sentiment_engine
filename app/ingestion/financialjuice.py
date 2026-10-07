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
from datetime import datetime, timedelta, timezone
from hashlib import sha1 as key_hash
from zoneinfo import ZoneInfo

from app.config import settings
from app.ingestion.base import RawArticle, strip_html
from app.ingestion.chromium_lock import chromium_slot
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

    #: Below this many headlines a scrape is treated as failed. The homepage
    #: renders ~20 on first paint, so a genuine load never returns fewer; a
    #: smaller number means the SPA had not hydrated when we read the DOM.
    MIN_HEADLINES = 15

    #: Each scroll appends ~20 more headlines (verified live: 23 -> 184).
    #: Three rounds gives a ~80-item window, comfortably wider than the
    #: ~21 headlines published per 3-minute poll cycle, so a session report
    #: can no longer fall out of view between two consecutive runs.
    SCROLL_ROUNDS = 3

    #: Pause after each scroll to let the next page of items stream in.
    SCROLL_SETTLE_MS = 1800

    #: A session report's body is not in the feed - only its teaser is, and for
    #: the "Morning Juice" preps that teaser is EMPTY. The full text (Sentiment,
    #: Docket, Speakers) lives on the article page, so those reports are opened
    #: individually. Verified: US Session Prep = 3238 chars on the article page
    #: vs 0 chars in the feed.
    ARTICLE_MAX_CHARS = 4000

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

        articles = []
        # Shared render slot. The calendar provider renders the same site, and
        # two concurrent renders measured at ~1.0 GB RSS; the news feed has
        # priority, so the calendar defers rather than competing for memory.
        async with chromium_slot(
                wait_seconds=settings.chromium_lock_wait_seconds) as slot:
            if not slot:
                log.warning("Financial Juice skipped this cycle: chromium "
                            "busy with another render; deferring to the next "
                            "news poll.")
                return []
            async with async_playwright() as pw:
                return await self._collect(pw)

    async def _collect(self, pw):
        """
        Do the actual render. The caller already holds the shared Chromium
        slot and has opened the Playwright context, so this method only drives
        the page.
        """
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        try:
            # A thin read means the SPA had not hydrated. Reload once
            # rather than returning a short list: a headline missed now is
            # invisible forever, so retrying beats a silent gap.
            articles = []
            for attempt in range(2):
                ctx = await browser.new_context(
                    user_agent=settings.http_user_agent,
                    viewport={"width": 1440, "height": 2400},
                    locale="en-US",
                    # Match the zone _parse_fj_time assumes, so the
                    # rendered wall-clock and the parsed stamp agree.
                    timezone_id=settings.display_timezone,
                )
                try:
                    html = await self._render_and_scroll(ctx)
                finally:
                    await ctx.close()

                articles = _extract(html, self.name, self.quality)
                log.info(
                    "Financial Juice: extracted %d headlines (try %d)",
                    len(articles), attempt + 1)
                if len(articles) >= self.MIN_HEADLINES:
                    break
                if attempt == 0:
                    log.warning(
                        "Financial Juice: only %d headlines (< %d); "
                        "reloading once in case the SPA loaded slowly",
                        len(articles), self.MIN_HEADLINES)

            # Second pass: the feed only carries a teaser (sometimes an
            # empty one), so open the handful of session reports and
            # store their full Sentiment/Docket/Speakers text.
            if articles:
                await self._enrich_session_reports(browser, articles)
        finally:
            await browser.close()

        return articles

    async def _render_and_scroll(self, ctx):
        """Render the homepage, scroll the feed, return the resulting HTML."""
        page = await ctx.new_page()
        await page.goto(self.url, timeout=45000, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=self.wait_ms)
        except Exception:  # networkidle often never settles on SPAs
            pass
        await page.wait_for_timeout(2500)

        # The feed is an infinite-scroll list: only the newest ~20 headlines
        # exist in the DOM on first paint. Without scrolling, a session report
        # published between two polls is pushed out of the window and lost for
        # good - exactly how the 2 Oct US Market Wrap went missing. Each scroll
        # appends ~20 more items (verified live: 23 -> 184 headlines).
        for _ in range(self.SCROLL_ROUNDS):
            before = await page.eval_on_selector_all(
                "div.headline-item", "els => els.length")
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(self.SCROLL_SETTLE_MS)
            after = await page.eval_on_selector_all(
                "div.headline-item", "els => els.length")
            if after <= before:
                # Nothing loaded: the list is exhausted, so stop early rather
                # than burning the remaining settle time.
                break

        return await page.content()

    async def _fetch_article_text(self, ctx, url: str) -> str:
        """
        Open one article page and return its body text ('' on any failure).

        ``div.entry-content`` is the article body (verified live): it holds the
        Sentiment paragraph, the Docket and the Speakers list, and excludes the
        site chrome. Everything is best-effort - a failure here must never
        discard a headline we already hold, so the caller keeps what it has.
        """
        if not url or "/News/" not in url:
            return ""
        page = None
        try:
            page = await ctx.new_page()
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # SPAs frequently never go idle
                pass
            await page.wait_for_timeout(1500)
            el = await page.query_selector("div.entry-content")
            if el is None:
                return ""
            text = (await el.inner_text()).strip()
            return text[: self.ARTICLE_MAX_CHARS]
        except Exception as exc:  # network hiccup, layout change, timeout
            log.warning("Financial Juice: article fetch failed for %s (%s)",
                        url[:90], exc)
            return ""
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _enrich_session_reports(self, browser, articles: list) -> int:
        """
        Replace the feed teaser with the FULL text for session reports only.

        The feed carries a teaser (and for the "Morning Juice" preps, an empty
        one), so scoring the reports on feed data alone under-reads them. Only
        the five session reports are opened - never the ~70 general headlines -
        so this costs a handful of page loads per cycle.
        """
        from app.analysis.fj_sessions import (
            classify_fj_session,
            is_financial_juice,
        )

        targets = [
            a for a in articles
            if is_financial_juice(a.source)
            and classify_fj_session(a.title or "", a.body or "") is not None
        ]
        if not targets:
            return 0

        ctx = await browser.new_context(
            user_agent=settings.http_user_agent,
            viewport={"width": 1440, "height": 2400},
            locale="en-US",
            timezone_id=settings.display_timezone,
        )
        upgraded = 0
        try:
            for art in targets:
                full = await self._fetch_article_text(ctx, art.url)
                if not full:
                    continue
                # Only replace when the page genuinely has more to say;
                # otherwise keep the teaser we already scraped.
                if len(full) > len(art.body or ""):
                    previous = len(art.body or "")
                    art.body = full[: settings.article_fetch_max_chars]
                    art.summary = full[:300]
                    upgraded += 1
                    log.info("Financial Juice: session report body %d -> %d "
                             "chars (%s)", previous, len(art.body),
                             art.title[:60])
        finally:
            await ctx.close()

        if upgraded:
            log.info("Financial Juice: enriched %d/%d session reports with "
                     "full article text", upgraded, len(targets))
        return upgraded
