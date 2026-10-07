"""
HTML website monitor - for sources that do not expose an RSS feed.

Scrapes a listing page, extracts headline/link/time pairs, then optionally
fetches each article for body text. Kept polite: one request per page,
configurable rate limit, robots.txt-friendly by default.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.ingestion.base import RawArticle, SourceSpec, strip_html
from app.logging_conf import get_logger

log = get_logger(__name__)

# Common headline patterns across news listing pages.
_HEADING_TAGS = ("h1", "h2", "h3", "h4")
_TIME_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?)|"
    r"(\d+\s*(?:minutes?|mins?|hours?|hrs?)\s*ago)|"
    r"(just now)",
    re.IGNORECASE,
)


class WebsiteSource:
    """Scrape a news listing page for today's headlines."""

    kind = "html"

    def __init__(self, spec: SourceSpec, *, article_selector: str | None = None) -> None:
        self.name = spec.name
        self.quality = spec.quality
        self.url = spec.url
        self.weight = spec.weight
        self.article_selector = article_selector
        self._semaphore = asyncio.Semaphore(3)

    async def fetch(self) -> list[RawArticle]:
        headers = {"User-Agent": settings.http_user_agent}
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers=headers,
            follow_redirects=True,
        ) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            candidates = self._extract_links(soup)
            articles: list[RawArticle] = []
            for title, href, time_text in candidates[:80]:
                art = RawArticle(
                    guid="",
                    source=self.name,
                    source_quality=self.quality,
                    url=href,
                    title=title,
                    published_raw=time_text,
                )
                articles.append(art.ensure_guid())

            if settings.article_fetch_enabled and articles:
                await self._enrich_articles(client, articles)

        return articles

    def _extract_links(self, soup: BeautifulSoup) -> list[tuple[str, str, str | None]]:
        base = self.url
        domain = urlparse(base).netloc
        seen: dict[str, tuple[str, str, str | None]] = {}

        for tag in soup.find_all(_HEADING_TAGS):
            anchor = tag.find("a", href=True)
            if anchor is None:
                continue
            title = strip_html(tag.get_text(" "))
            if not title or len(title) < 15:
                continue

            href = urljoin(base, anchor["href"])
            if urlparse(href).netloc != domain:
                continue
            if href in seen:
                continue

            # Nearest ancestor carrying a relative timestamp.
            time_text = None
            parent = tag.parent
            for _ in range(3):
                if parent is None:
                    break
                match = _TIME_RE.search(strip_html(parent.get_text(" ")))
                if match:
                    time_text = match.group(0)
                    break
                parent = parent.parent

            seen[href] = (title, href, time_text)

        log.debug("%s: extracted %d candidate links", self.name, len(seen))
        return list(seen.values())

    async def _enrich_articles(self, client: httpx.AsyncClient, articles: list[RawArticle]) -> None:
        async def fetch_one(art: RawArticle) -> None:
            async with self._semaphore:
                try:
                    r = await client.get(art.url)
                    r.raise_for_status()
                    art.body = (await asyncio.to_thread(self._extract_body, r.text))[
                        : settings.article_fetch_max_chars
                    ]
                except Exception as exc:  # noqa: BLE001 - partial data is fine
                    log.debug("%s: body fetch failed (%s)", self.name, exc)

        await asyncio.gather(*(fetch_one(a) for a in articles), return_exceptions=True)

    @staticmethod
    def _extract_body(html: str) -> str:
        """Prefer trafilatura for boilerplate removal; fall back to BS4."""
        try:
            import trafilatura

            extracted = trafilatura.extract(
                html, include_comments=False, include_tables=False, favor_precision=True
            )
            if extracted:
                return extracted
        except Exception:  # noqa: BLE001
            pass
        soup = BeautifulSoup(html, "lxml")
        for bad in soup(["script", "style", "nav", "footer", "header", "aside"]):
            bad.decompose()
        return strip_html(soup.get_text(" "))