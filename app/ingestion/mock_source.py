"""
Offline synthetic source.

Lets the entire pipeline - classification, weighting, dedup, sentiment,
prep/wrap, dashboard - be demonstrated and tested with no network and no
LLM key. Clearly flagged in the UI so synthetic news is never confused
with real news.
"""

from __future__ import annotations

import random
from datetime import timedelta

from app.ingestion.base import RawArticle
from app.logging_conf import get_logger
from app.utils import timeutils as T

log = get_logger(__name__)

_TEMPLATES: list[dict] = [
    {
        "title": "Fed officials signal interest rates will stay higher for longer",
        "category": "Central Bank",
        "importance": "VERY_HIGH",
        "minutes_ago": 12,
    },
    {
        "title": "US consumer prices rise faster than economists expected in latest reading",
        "category": "Inflation",
        "importance": "VERY_HIGH",
        "minutes_ago": 35,
    },
    {
        "title": "Dollar index slips as traders trim rate-cut bets",
        "category": "USD/DXY",
        "importance": "HIGH",
        "minutes_ago": 48,
    },
    {
        "title": "Gold ETF holdings post largest weekly inflow since March",
        "category": "ETF Flows",
        "importance": "HIGH",
        "minutes_ago": 70,
    },
    {
        "title": "China factory output misses forecasts as demand stays soft",
        "category": "China",
        "importance": "HIGH",
        "minutes_ago": 95,
    },
    {
        "title": "Spot Bitcoin ETFs record second day of steady inflows",
        "category": "Bitcoin ETF Flows",
        "importance": "HIGH",
        "minutes_ago": 25,
    },
    {
        "title": "Treasury yields climb as traders price fewer rate cuts",
        "category": "Treasury Yields",
        "importance": "MEDIUM",
        "minutes_ago": 58,
    },
    {
        "title": "Copper extends losses as global manufacturing concerns grow",
        "category": "Industrial Demand",
        "importance": "MEDIUM",
        "minutes_ago": 140,
    },
    {
        "title": "Geopolitical tensions escalate as two nations trade new sanctions",
        "category": "Geopolitics",
        "importance": "HIGH",
        "minutes_ago": 180,
    },
    {
        "title": "Market prep: what traders are watching before the US session opens",
        "category": "Other",
        "importance": "MEDIUM",
        "minutes_ago": 240,
        "report_kind": "MARKET_PREP",
    },
    {
        "title": "Market wrap: gold pares losses as dollar volatility eases",
        "category": "Other",
        "importance": "MEDIUM",
        "minutes_ago": 300,
        "report_kind": "MARKET_WRAP",
    },
    {
        "title": "Minor single-store sales figure shows no clear trend",
        "category": "Other",
        "importance": "LOW",
        "minutes_ago": 420,
    },
]

_BODY = (
    "Synthetic demonstration article generated locally for offline testing. "
    "It exists to exercise the classification, weighting and sentiment "
    "pipeline end-to-end. It is not real market news and carries no market "
    "information whatsoever."
)


class MockSource:
    """Deterministic synthetic feed."""

    kind = "mock"
    name = "Synthetic Demo Feed"
    quality = "TIER_4_OTHER"

    def __init__(self, url: str = "mock://demo", seed: int | None = None) -> None:
        self.url = url
        self._rng = random.Random(seed)

    async def fetch(self) -> list[RawArticle]:
        now = T.now_utc()
        out: list[RawArticle] = []
        for tpl in _TEMPLATES:
            published = now - timedelta(minutes=tpl["minutes_ago"])
            art = RawArticle(
                guid="",
                source=self.name,
                source_quality=self.quality,
                url=f"mock://demo/{abs(hash(tpl['title']))}",
                title=tpl["title"],
                body=_BODY,
                summary=_BODY[:200],
                published_raw=published.isoformat(),
            )
            # Stash hints the heuristic classifier can use.
            art.ensure_guid()
            out.append(art)
        self._rng.shuffle(out)
        log.info("Mock source produced %d synthetic articles", len(out))
        return out
