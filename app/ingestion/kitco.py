"""
Kitco News source.

kitco.com publishes no RSS/Atom feed (verified live: the conventional feed paths
404, and ``/news/rss/KitcoNews.xml`` returns an empty document) and declares no
feed in its HTML <head>. The listing page IS server-rendered though, and every
article URL embeds its publication DATE:

    https://www.kitco.com/news/article/2026-10-01/gold-holds-firm-...

so headline, link and date can all be read straight from the listing HTML - no
per-article page load needed. The listing also renders a short teaser beside
each headline; that teaser becomes the article body, because the sentiment
classifier needs real prose and it is already on the page we fetched. Opening
every article as well would mean ~15 extra requests per 180-second poll cycle
for text we already hold.

TIMESTAMP CAVEAT: the URL yields a DATE, not a time - the listing renders no
clock. A date is published here as 12:00 UTC, not 00:00 UTC: MARKET_TIMEZONE
is America/New_York, so 00:00 UTC falls on the PREVIOUS calendar day there and
the today-filter would silently discard every Kitco article as "yesterday".
Noon UTC keeps the same market-local day for every zone from UTC-5 to UTC+7.
This is a deliberate, documented approximation - the only alternative is to
drop Kitco entirely, which would cost the single most XAUUSD/XAGUSD-relevant
feed in the set.

Register with::

    Kitco|kitco|https://www.kitco.com/news/|1.0|TIER_2_MAJOR
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.ingestion.base import RawArticle, SourceSpec, strip_html
from app.logging_conf import get_logger

log = get_logger(__name__)

# /news/article/2026-10-01/some-headline-slug
_ARTICLE_RE = re.compile(r"/news/article/(\d{4}-\d{2}-\d{2})/([a-z0-9][a-z0-9-]*)")

# Below this an anchor is a thumbnail/pager link, not a headline.
_HEADLINE_MIN_CHARS = 25
# A teaser must add at least this many characters beyond the headline before it
# counts as real body prose worth scoring.
_TEASER_MIN_CHARS = 40
# How far up the DOM to look for the teaser that follows a headline.
_TEASER_LOOKUP_DEPTH = 3


def _date_to_stamp(date_text: str) -> str | None:
    """``'2026-10-01'`` -> ``'2026-10-01T12:00:00+00:00'``.

    Noon UTC, not midnight - see the TIMESTAMP CAVEAT in the module docstring.
    Returns None for an impossible date so the article is dropped rather than
    being given a fabricated timestamp.
    """
    try:
        stamp = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        log.warning("Kitco: unparseable date in URL: %r", date_text)
        return None
    return stamp.replace(hour=12, tzinfo=timezone.utc).isoformat()


class KitcoSource:
    """Kitco News - listing scraper keyed on the date embedded in article URLs."""

    kind = "kitco"

    def __init__(self, spec: SourceSpec) -> None:
        self.name = spec.name
        self.quality = spec.quality
        self.url = spec.url
        self.weight = spec.weight

    async def fetch(self) -> list[RawArticle]:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
        ) as client:
            resp = await client.get(self.url)
            resp.raise_for_status()
            html = resp.text

        # BeautifulSoup is CPU-bound; keep it off the event loop.
        return await asyncio.to_thread(self._parse, html, self.url)

    def _parse(self, html: str, base_url: str) -> list[RawArticle]:
        soup = BeautifulSoup(html, "lxml")
        found: dict[str, RawArticle] = {}

        for anchor in soup.find_all("a", href=True):
            match = _ARTICLE_RE.search(anchor["href"])
            if not match:
                continue

            headline = strip_html(anchor.get_text(" "))
            if len(headline) < _HEADLINE_MIN_CHARS:
                continue

            # Canonicalise: tracking/AMP query strings would otherwise give the
            # same story two different GUIDs and defeat de-duplication.
            url = urljoin(base_url, anchor["href"]).split("?")[0]
            if url in found:
                continue  # the teaser block repeats the headline's own link

            teaser = self._teaser(anchor, headline, url)
            found[url] = RawArticle(
                guid="",
                source=self.name,
                source_quality=self.quality,
                url=url,
                title=headline,
                body=teaser,
                summary=teaser[:400],
                published_raw=_date_to_stamp(match.group(1)),
            )

        articles = [art.ensure_guid() for art in found.values()]
        log.debug("%s: extracted %d articles", self.name, len(articles))
        return articles

    def _teaser(self, anchor, headline: str, url: str) -> str:
        """Grab the prose the listing renders alongside a headline.

        Kitco nests every card inside a container that holds MANY article links
        (verified: the third ancestor up carries 5), so the walk stops at the
        first ancestor shared with another story. Without that guard a card
        picks up a SIBLING's teaser and files one story's wording under another
        story's headline - which is far worse than having no body at all,
        because the classifier would score the wrong text.

        The headline itself is then removed from what is left, and anything
        shorter than a real sentence is discarded rather than padded.
        """
        own_text = ""
        node = anchor.parent
        for _ in range(_TEASER_LOOKUP_DEPTH):
            if node is None:
                break
            links = {
                urljoin(self.url, a["href"]).split("?")[0]
                for a in node.find_all("a", href=True)
                if _ARTICLE_RE.search(a["href"])
            }
            if len(links) > 1:
                break  # container shared with other stories - stop here
            if links and url not in links:
                break
            own_text = strip_html(node.get_text(" "))

        if not own_text:
            return ""

        # Drop the headline and any leading category chip, keeping only the
        # extra prose the teaser adds.
        residual = own_text
        if headline in residual:
            residual = residual.split(headline, 1)[1]
        residual = residual.strip(" -–—|")

        if len(residual) < _TEASER_MIN_CHARS:
            return ""
        return residual[: settings.article_fetch_max_chars]