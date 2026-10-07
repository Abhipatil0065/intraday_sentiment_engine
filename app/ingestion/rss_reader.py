"""RSS / Atom reader."""

from __future__ import annotations

import asyncio

import feedparser
import httpx

from app.config import settings
from app.ingestion.base import RawArticle, SourceSpec, strip_html
from app.logging_conf import get_logger

log = get_logger(__name__)


def _entry_text(entry) -> tuple[str, str, str | None, str | None]:
    """(title, body, published_raw, updated_raw) from a feed entry."""
    title = strip_html(getattr(entry, "title", "") or "")

    body_parts: list[str] = []
    content = getattr(entry, "content", None) or []
    for c in content:
        if isinstance(c, dict) and c.get("value"):
            body_parts.append(strip_html(c["value"]))
    if not body_parts:
        for key in ("summary", "description", "subtitle"):
            val = getattr(entry, key, None)
            if val:
                body_parts.append(strip_html(val))
    body = " ".join(p for p in body_parts if p).strip()

    published = None
    for key in ("published", "published_parsed", "pubDate", "dc_date", "created"):
        val = getattr(entry, key, None)
        if val:
            published = val
            break

    updated = None
    for key in ("updated", "updated_parsed", "modified", "lastBuildDate"):
        val = getattr(entry, key, None)
        if val and val != published:
            updated = val
            break

    # Pass values through UNCHANGED - *_parsed fields are time.struct_time
    # objects, and stringifying them destroys the type information the
    # timestamp parser needs.
    return title, body, published, updated


class RssSource:
    """RSS 2.0 / Atom feed reader."""

    kind = "rss"

    def __init__(self, spec: SourceSpec) -> None:
        self.name = spec.name
        self.quality = spec.quality
        self.url = spec.url
        self.weight = spec.weight

    async def fetch(self) -> list[RawArticle]:
        headers = {"User-Agent": settings.http_user_agent, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers=headers,
            follow_redirects=True,
        ) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            raw_bytes = resp.content

        # feedparser is CPU-bound but fast; keep it off the event loop.
        return await asyncio.to_thread(self._parse, raw_bytes)

    def _parse(self, raw_bytes: bytes) -> list[RawArticle]:
        feed = feedparser.parse(raw_bytes)
        articles: list[RawArticle] = []
        for entry in feed.entries:
            title, body, published, updated = _entry_text(entry)
            if not title:
                continue
            art = RawArticle(
                guid="",
                source=self.name,
                source_quality=self.quality,
                url=(getattr(entry, "link", "") or "").strip(),
                title=title,
                body=body[: settings.article_fetch_max_chars],
                summary=body[:400],
                published_raw=published,
                updated_raw=updated,
            )
            articles.append(art.ensure_guid())
        log.debug("%s: parsed %d entries", self.name, len(articles))
        return articles