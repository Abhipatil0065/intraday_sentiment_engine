"""
Pluggable news sources (DATA INGESTION LAYER).

Every source implements ``NewsSource``. Adding or replacing the monitored
website is a configuration change, not a code change.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol

from app.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class RawArticle:
    """Normalised pre-analysis record produced by every source."""

    guid: str
    source: str
    source_quality: str
    url: str
    title: str
    body: str = ""
    summary: str = ""
    published_raw: str | None = None
    updated_raw: str | None = None
    # The story's own TradingView tags (relatedSymbols). Audit only.
    symbols: list[str] = field(default_factory=list)
    # The cards whose CHART listed this story (asset values: XAUUSD,
    # XAGUSD, BTCUSD, EQUITY). Empty for every source without a chart, and
    # then the classifier's routing decides the cards exactly as before.
    # See app/analysis/chart_scope.py.
    chart_assets: list[str] = field(default_factory=list)

    def compute_guid(self) -> str:
        key = (self.url or self.title).strip().lower()
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def ensure_guid(self) -> RawArticle:
        if not self.guid:
            self.guid = self.compute_guid()
        return self


class NewsSource(Protocol):
    """Any ingestion backend - RSS, HTML scraper, API, or mock."""

    name: str
    quality: str
    url: str
    kind: str

    async def fetch(self) -> list[RawArticle]:
        ...


# --------------------------------------------------------------------------
# Source registry - parsed from NEWS_SOURCES in .env
# --------------------------------------------------------------------------
@dataclass
class SourceSpec:
    name: str
    kind: str
    url: str
    weight: float = 1.0
    quality: str = "TIER_4_OTHER"


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    if not text:
        return ""
    cleaned = _TAG_RE.sub(" ", text)
    for entity, repl in (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'"), ("&ndash;", "-"),
        ("&mdash;", "-"), ("&hellip;", "..."),
    ):
        cleaned = cleaned.replace(entity, repl)
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_source_config(raw: str | None) -> list[SourceSpec]:
    """
    Parse ``Name|type|url|weight|quality`` entries.

    Newlines and commas both separate entries, so a multi-line .env block and
    a single-line list are equivalent.
    """
    if not raw or not raw.strip():
        return []

    specs: list[SourceSpec] = []
    tokens = [t.strip() for t in re.split(r"[,\n]", raw) if t.strip()]
    for token in tokens:
        parts = [p.strip() for p in token.split("|")]
        if len(parts) < 3 or not parts[2]:
            log.warning("Skipping malformed source entry: %r", token)
            continue
        try:
            weight = float(parts[3]) if len(parts) > 3 and parts[3] else 1.0
        except ValueError:
            weight = 1.0
        quality = parts[4] if len(parts) > 4 and parts[4] else "TIER_4_OTHER"
        specs.append(
            SourceSpec(name=parts[0], kind=parts[1].lower(), url=parts[2],
                       weight=weight, quality=quality.upper())
        )
    return specs


QUALITY_MULTIPLIER: dict[str, float] = {
    "TIER_1_OFFICIAL": 1.15,   # central banks, government agencies
    "TIER_2_MAJOR": 1.00,      # major financial news organisations
    "TIER_3_CRYPTO": 0.92,     # reputable crypto press
    "TIER_4_OTHER": 0.82,      # everything else - down-weighted
}