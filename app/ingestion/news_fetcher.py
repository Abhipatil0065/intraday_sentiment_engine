"""
News fetcher - polls every configured source concurrently and returns a
de-duplicated batch of raw articles.

Each source is isolated: one dead feed never blocks the pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.ingestion.base import (
    QUALITY_MULTIPLIER,
    RawArticle,
    parse_source_config,
)
from app.ingestion.kitco import KitcoSource
from app.ingestion.mock_source import MockSource
from app.ingestion.rss_reader import RssSource
from app.ingestion.website_monitor import WebsiteSource
from app.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class FetchReport:
    articles: list[RawArticle] = field(default_factory=list)
    sources_polled: int = 0
    sources_ok: int = 0
    errors: list[str] = field(default_factory=list)
    # name -> {"items": n, "newest": iso-or-None, "error": str-or-None}
    source_stats: dict[str, dict] = field(default_factory=dict)


def build_sources() -> list:
    """Instantiate every configured source."""
    sources: list = []

    if settings.use_mock_source:
        sources.append(MockSource())
        log.warning("USE_MOCK_SOURCE=true - synthetic news only")

    for spec in parse_source_config(settings.news_sources):
        try:
            if spec.kind in ("rss", "atom"):
                sources.append(RssSource(spec))
            elif spec.kind in ("html", "scrape"):
                sources.append(WebsiteSource(spec))
            elif spec.kind == "kitco":
                sources.append(KitcoSource(spec))
            elif spec.kind == "mock":
                sources.append(MockSource(spec.url))
            elif spec.kind == "fj":
                from app.ingestion.financialjuice import FinancialJuiceSource
                sources.append(FinancialJuiceSource(spec))
            else:
                log.warning("Unknown source kind %r for %s - skipped", spec.kind, spec.name)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to build source %s: %s", spec.name, exc)

    if not sources:
        log.warning(
            "No sources configured. Set NEWS_SOURCES in .env "
            "(see .env.example) or enable USE_MOCK_SOURCE."
        )
    return sources


async def _fetch_one(source) -> tuple[list[RawArticle] | None, str | None]:
    try:
        return await source.fetch(), None
    except asyncio.TimeoutError:
        return None, f"{source.name}: timeout"
    except httpx.HTTPStatusError as exc:
        return None, f"{source.name}: HTTP {exc.response.status_code}"
    except Exception as exc:
        log.exception("Source %s failed", getattr(source, "name", "?"))
        return None, f"{source.name}: {type(exc).__name__}: {exc}"


async def fetch_all_news() -> FetchReport:
    """Poll every source once, concurrently, with a global timeout guard."""
    sources = build_sources()
    report = FetchReport(sources_polled=len(sources))

    if not sources:
        return report

    results = await asyncio.gather(
        *(_fetch_one(s) for s in sources), return_exceptions=False
    )

    seen_guids: set[str] = set()
    for (articles, error), source in zip(results, sources):
        name = getattr(source, "name", "?")
        newest = None
        if articles:
            ts = [a.published_raw for a in articles if a.published_raw]
            if ts:
                newest = max(ts)
        report.source_stats[name] = {
            "items": len(articles) if articles else 0,
            "newest": newest,
            "error": error,
        }
        if error:
            report.errors.append(error)
            continue
        report.sources_ok += 1
        if not articles:
            continue
        for art in articles:
            guid = art.compute_guid()
            if guid in seen_guids:
                continue
            seen_guids.add(guid)
            art.guid = guid
            report.articles.append(art)

    log.info(
        "Fetched %d articles from %d/%d sources (%d errors)",
        len(report.articles), report.sources_ok, report.sources_polled,
        len(report.errors),
    )
    return report


def quality_multiplier(source_quality: str) -> float:
    return QUALITY_MULTIPLIER.get((source_quality or "").upper(), 0.82)