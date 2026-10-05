"""
DETERMINISTIC SENTIMENT HARNESS (phases 0-7).

WHY THIS EXISTS
---------------
Comparing "before" and "after" a backend change needs the SAME input every time.
The live engine reads its signals out of SQLite and its clock out of the wall
clock, so simply running the pipeline twice is not a comparison: the articles,
their ages and therefore their decay would differ between the two runs.

This module builds the aggregator's inputs from a FIXED corpus of headlines at
FIXED ages, against a FIXED ``now``, so the only thing that can move a number is
a change in the engine. That is what makes ``baseline_before.json`` a real
baseline rather than a sample.

IT MIRRORS PRODUCTION ON PURPOSE
--------------------------------
``build_signals`` reproduces ``pipeline._recompute_sentiment`` step for step. If
it did not, a pipeline fix would not show up in the comparison at all - which is
the entire point of the exercise.

    legacy=True   frozen reference of the PRE-fix wiring (no ``category``,
                  no corroboration). Kept only so "before" stays reproducible.
    legacy=False  the CURRENT wiring, via ``signal_factory.build_signal``.

Read-only: it imports the engine, it never writes to the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.analysis.event_clustering import EventClusterer
from app.analysis.news_classifier import heuristic_analyze
from app.analysis.sentiment_engine import WeightedSignal, compute
from app.analysis.display import to_display
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
    SessionName,
)
from app.market.session_detector import session_relevance

# The one instant every harness run is anchored to. Fixed so decay is a pure
# function of the corpus, never of the hour the script happened to be run.
FIXED_NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)

# (title, source, source_quality, minutes_ago, session)
#
# Chosen to exercise every mechanism the engine has, and - importantly - to
# include OLD Crypto Regulation and ETF Flows items. Those are the categories
# configured with long half-lives (72h / 24h), so an item aged 10 hours should
# barely have decayed. Under the pre-fix wiring they decayed on the 6h "Other"
# default instead, and that difference is precisely what phase 1 repairs.
CORPUS: tuple[tuple[str, str, str, int, SessionName], ...] = (
    ("Fed signals hawkish rate hike as inflation stays sticky",
     "Reuters", "TIER_2_MAJOR", 12, SessionName.US),
    ("Fed raises rates by 50 basis points in surprise move",
     "Federal Reserve", "TIER_1_OFFICIAL", 25, SessionName.US),
    ("FOMC officials warn inflation may stay elevated for longer",
     "Bloomberg", "TIER_2_MAJOR", 90, SessionName.US),
    ("SEC approves spot bitcoin ETF in landmark ruling",
     "Reuters", "TIER_2_MAJOR", 200, SessionName.US),
    ("Congress weighs new crypto market structure bill",
     "Congressional Wire", "TIER_2_MAJOR", 600, SessionName.US),
    ("Spot bitcoin ETF inflows surge to record high",
     "CoinDesk", "TIER_3_CRYPTO", 600, SessionName.US),
    ("Bitcoin ETF sees heavy outflows as risk aversion returns",
     "CoinDesk", "TIER_3_CRYPTO", 45, SessionName.US),
    ("OPEC+ extends output cuts to tighten supply",
     "Reuters", "TIER_2_MAJOR", 30, SessionName.EUROPE),
    ("OPEC+ raises output target as supply glut emerges",
     "OPEC", "TIER_1_OFFICIAL", 300, SessionName.EUROPE),
    ("Middle East escalation closes Strait of Hormuz to oil tankers",
     "Reuters", "TIER_2_MAJOR", 15, SessionName.EUROPE),
    ("Banking crisis fears hit Wall Street as credit markets freeze",
     "Reuters", "TIER_2_MAJOR", 20, SessionName.US),
    ("China factory output beats forecasts, copper demand soars",
     "Reuters", "TIER_2_MAJOR", 75, SessionName.ASIA),
    ("US dollar climbs to fresh high against the euro",
     "Reuters", "TIER_2_MAJOR", 40, SessionName.US),
    ("Treasury yields rise sharply on renewed inflation concerns",
     "Bloomberg", "TIER_2_MAJOR", 35, SessionName.US),
    ("Nonfarm payrolls beat expectations strongly in August",
     "Bureau of Labor Statistics", "TIER_1_OFFICIAL", 55, SessionName.US),
    ("Gold ETF inflows hit highest level since 2020",
     "Kitco", "TIER_3_CRYPTO", 500, SessionName.US),
    ("Risk appetite improves as equity markets rally broadly",
     "Reuters", "TIER_2_MAJOR", 10, SessionName.US),
    ("Crypto liquidations top one billion as bitcoin slides",
     "CoinDesk", "TIER_3_CRYPTO", 25, SessionName.US),
    ("Reports suggest Federal Reserve may cut rates in September",
     "SomeBlog", "TIER_4_OTHER", 120, SessionName.US),
    ("Federal Reserve denies plans for emergency rate cut",
     "Federal Reserve", "TIER_1_OFFICIAL", 65, SessionName.US),
    # --- SYNDICATION GROUP -----------------------------------------------
    # The same wire story as reported by three different outlets, worded
    # differently each time. These are what the clusterer must collapse into
    # ONE event, and they are the only reason corroboration is observable in
    # this corpus at all.
    # Wording is deliberately near-identical, the way a wire story actually
    # syndicates, so the clusterer collapses all three into ONE event and the
    # corroboration boost is observable in the comparison.
    ("Fed holds rates steady at 4.75 percent, signals no change to policy stance",
     "Reuters", "TIER_2_MAJOR", 18, SessionName.US),
    ("Fed holds rates steady at 4.75 percent, signals no change to policy",
     "CNBC", "TIER_2_MAJOR", 19, SessionName.US),
    ("Fed holds rates steady at 4.75 percent, no change to policy stance",
     "MarketWatch", "TIER_2_MAJOR", 21, SessionName.US),
)


def _corpus_rows(now: datetime):
    """Classify every corpus headline exactly once, yielding row-like tuples."""
    for title, source, quality, minutes_ago, session in CORPUS:
        analysis = heuristic_analyze(title)
        published = now - timedelta(minutes=minutes_ago)
        yield {
            "title": title,
            "source": source,
            "source_quality": quality,
            "published_at_utc": published,
            "analysis": analysis,
            "session": session,
        }


def build_signals(now: datetime = FIXED_NOW, *, legacy: bool = False
                  ) -> list[WeightedSignal]:
    """
    Turn the corpus into ``WeightedSignal`` rows, one per (article, asset).

    Clustering runs over the real ``EventClusterer``, so syndicated headlines
    share a cluster id and their independent-source count is what feeds
    corroboration. ``legacy=True`` reproduces the PRE-phase-1 wiring verbatim -
    no category, no cluster, no corroboration - so the "before" column of the
    comparison can always be regenerated.
    """
    signals: list[WeightedSignal] = []
    clusterer = EventClusterer()
    for row in _corpus_rows(now):
        analysis = row["analysis"]
        try:
            category = NewsCategory(analysis.category.value)
        except ValueError:
            category = NewsCategory.OTHER

        match = clusterer.match(row["title"], row["published_at_utc"],
                                source=row["source"], now=now)

        for impact in analysis.assets:
            if legacy:
                # --- PRE-FIX: category / cluster / corroboration all omitted ---
                signals.append(WeightedSignal(
                    asset=impact.asset,
                    title=row["title"],
                    direction=impact.direction,
                    strength=impact.strength,
                    importance=analysis.importance,
                    published_at=row["published_at_utc"],
                    relevance=impact.relevance,
                    persistence=impact.persistence,
                    source_quality=row["source_quality"],
                    reason=impact.reason,
                    transmission=impact.transmission,
                    report_kind=ReportKind(analysis.report_kind.value),
                    session_multiplier=session_relevance(
                        row["session"], category.value),
                ))
            else:
                from app.analysis.signal_factory import build_signal

                signals.append(build_signal(
                    asset=impact.asset,
                    title=row["title"],
                    direction=impact.direction,
                    strength=impact.strength,
                    importance=analysis.importance,
                    published_at=row["published_at_utc"],
                    relevance=impact.relevance,
                    persistence=impact.persistence,
                    source_quality=row["source_quality"],
                    source_name=row["source"],
                    category=category,
                    cluster_id=match.cluster_id,
                    independent_sources=match.independent_sources,
                    reason=impact.reason,
                    transmission=impact.transmission,
                    report_kind=ReportKind(analysis.report_kind.value),
                    session_multiplier=session_relevance(
                        row["session"], category.value),
                ))
    return signals


def snapshot(now: datetime = FIXED_NOW, *, legacy: bool = False) -> dict:
    """
    Every asset's full published verdict, in dashboard terms.

    ``bull_pct`` / ``bear_pct`` come from ``display.to_display`` - the very same
    projection the dashboard renders - so the baseline records the numbers a
    user actually sees, not an internal intermediate.
    """
    signals = build_signals(now, legacy=legacy)
    out: dict = {
        "generated_for_now_utc": now.isoformat(),
        "wiring": "legacy" if legacy else "current",
        "corpus_size": len(CORPUS),
        "assets": {},
    }
    for asset in Asset:
        result = compute([s for s in signals if s.asset is asset], asset, now=now)
        shown = to_display(result.score, result.bias)
        out["assets"][asset.value] = {
            "asset": asset.value,
            "label": asset.label,
            "score": result.score,
            "confidence": result.confidence,
            "bias": result.bias.value,
            "bull_pct": shown["bull_pct"],
            "bear_pct": shown["bear_pct"],
            "label_shown": shown["label"],
            "delta_60m": result.delta_60m,
            "contributing_articles": result.contributing_articles,
            "contributing_clusters": result.contributing_clusters,
            "drivers": [
                {"title": d.title, "direction": d.direction,
                 "contribution": d.contribution, "weight": d.weight}
                for d in result.bull_drivers
            ],
            "bear_drivers": [
                {"title": d.title, "direction": d.direction,
                 "contribution": d.contribution, "weight": d.weight}
                for d in result.bear_drivers
            ],
            "note": result.note,
        }
    return out