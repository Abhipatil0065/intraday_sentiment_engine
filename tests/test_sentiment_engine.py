"""Unit tests for the sentiment aggregator (sections 10, 11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.sentiment_engine import WeightedSignal, compute
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength,
)

# The engine decays against the real clock, so tests anchor to real "now"
# rather than a fixed date (otherwise every signal would age to the floor).
NOW = datetime.now(timezone.utc)


def make(asset=Asset.GOLD, direction=Direction.BULLISH,
         strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
         minutes_ago=5, title="test headline", **kw) -> WeightedSignal:
    return WeightedSignal(
        asset=asset, title=title, direction=direction, strength=strength,
        importance=importance,
        published_at=NOW - timedelta(minutes=minutes_ago),
        relevance=kw.get("relevance", 0.8),
        persistence=kw.get("persistence", 0.7),
        is_primary=kw.get("is_primary", True),
    )


def test_percentages_always_sum_to_100():
    """Section 11 normalisation invariant - now a strict two-way split."""
    signals = [make(direction=Direction.BULLISH),
               make(direction=Direction.BEARISH),
               make(direction=Direction.NEUTRAL)]
    r = compute(signals, Asset.GOLD)
    assert r.bull_pct + r.bear_pct == pytest.approx(100.0)


@pytest.mark.parametrize("count", [1, 2, 5, 12, 40])
def test_percentages_sum_to_100_across_sizes(count):
    signals = [
        make(direction=Direction.BULLISH if i % 3 == 0 else Direction.BEARISH,
             title=f"h{i}")
        for i in range(count)
    ]
    r = compute(signals, Asset.GOLD)
    assert r.bull_pct + r.bear_pct == pytest.approx(100.0)


@pytest.mark.parametrize("count", [1, 3, 7, 25])
def test_verdict_is_always_binary(count):
    """Only two views exist: BULLISH or BEARISH. Never anything else."""
    directions = [Direction.BULLISH, Direction.BEARISH,
                  Direction.NEUTRAL, Direction.MIXED]
    signals = [make(direction=directions[i % 4], title=f"h{i}")
               for i in range(count)]
    r = compute(signals, Asset.GOLD)
    assert r.label in (Direction.BULLISH, Direction.BEARISH)
    assert not hasattr(r, "neutral_pct")


def test_no_signals_reports_even_split_with_no_confidence():
    """No evidence must not produce a confident lean - 50/50, confidence 0."""
    r = compute([], Asset.GOLD)
    assert (r.bull_pct, r.bear_pct) == (50.0, 50.0)
    assert r.net_bias == 0.0
    assert r.confidence == 0.0
    assert r.label in (Direction.BULLISH, Direction.BEARISH)


def test_all_directionless_items_yield_no_confidence():
    """Items that never take a side must not be forced onto one."""
    signals = [make(direction=Direction.NEUTRAL, title=f"n{i}")
               for i in range(5)]
    r = compute(signals, Asset.GOLD)
    assert (r.bull_pct, r.bear_pct) == (50.0, 50.0)
    assert r.confidence == 0.0


def test_dominant_bullish_tilt():
    signals = [make(direction=Direction.BULLISH,
                    importance=EconomicImportance.VERY_HIGH,
                    strength=ImpactStrength.VERY_HIGH, title=f"bull {i}")
               for i in range(4)]
    signals += [make(direction=Direction.BEARISH, strength=ImpactStrength.LOW,
                     importance=EconomicImportance.LOW, title=f"bear {i}")
                for i in range(1)]
    r = compute(signals, Asset.GOLD)
    assert r.label is Direction.BULLISH
    assert r.bull_pct > r.bear_pct


def test_dominant_bearish_tilt():
    signals = [make(direction=Direction.BEARISH,
                    importance=EconomicImportance.VERY_HIGH,
                    strength=ImpactStrength.VERY_HIGH, title=f"bear {i}")
               for i in range(4)]
    signals += [make(direction=Direction.BULLISH, strength=ImpactStrength.LOW,
                     importance=EconomicImportance.LOW, title=f"bull {i}")
                for i in range(1)]
    r = compute(signals, Asset.GOLD)
    assert r.label is Direction.BEARISH
    assert r.bear_pct > r.bull_pct
def test_conflicting_signals_reported_not_forced():
    """Section 10: opposing signals must surface as a conflict."""
    signals = [
        make(direction=Direction.BULLISH,
             importance=EconomicImportance.VERY_HIGH,
             strength=ImpactStrength.HIGH, title="Fed turns dovish"),
        make(direction=Direction.BEARISH,
             importance=EconomicImportance.VERY_HIGH,
             strength=ImpactStrength.HIGH, title="Dollar surges broadly"),
    ]
    r = compute(signals, Asset.GOLD)
    assert r.conflicts, "a genuine conflict must be reported"
    c = r.conflicts[0]
    assert "bullish_driver" in c and "bearish_driver" in c
    assert "resolution" in c
    assert r.label in (Direction.BULLISH, Direction.BEARISH)


def test_balanced_conflict_still_binary_but_low_confidence():
    """An even conflict still names a side, but confidence must be low."""
    signals = [
        make(direction=Direction.BULLISH, importance=EconomicImportance.HIGH,
             strength=ImpactStrength.HIGH, title="dovish signal"),
        make(direction=Direction.BEARISH, importance=EconomicImportance.HIGH,
             strength=ImpactStrength.HIGH, title="hawkish signal"),
    ]
    r = compute(signals, Asset.GOLD)
    assert r.label in (Direction.BULLISH, Direction.BEARISH)
    assert (r.bull_pct, r.bear_pct) == (50.0, 50.0)
    assert r.conflicts, "the unresolved conflict must still be reported"
    assert r.confidence < 0.95


def test_recency_dominance_fresher_news_wins():
    """Section 4: recent bullish vs older bearish of similar importance."""
    signals = [
        make(direction=Direction.BULLISH, minutes_ago=5, title="fresh bullish"),
        make(direction=Direction.BEARISH, minutes_ago=600, title="stale bearish"),
    ]
    r = compute(signals, Asset.GOLD)
    assert r.bull_pct > r.bear_pct


def test_duplicate_event_coverage_is_damped():
    """Section 16: repeat coverage must not act as independent signal."""
    primary = [make(direction=Direction.BULLISH, title="original", is_primary=True)]
    with_dupes = primary + [
        make(direction=Direction.BULLISH, title="dup", is_primary=False)
        for _ in range(4)
    ]
    assert (compute(primary, Asset.GOLD).label
            == compute(with_dupes, Asset.GOLD).label == Direction.BULLISH)


def test_assets_are_scored_independently():
    """Section 9: one asset's flow must not leak into another's."""
    signals = [
        make(asset=Asset.BITCOIN, direction=Direction.BULLISH, title="btc etf"),
        make(asset=Asset.GOLD, direction=Direction.BEARISH, title="hawkish fed"),
    ]
    assert compute(signals, Asset.GOLD).label is Direction.BEARISH
    assert compute(signals, Asset.BITCOIN).label is Direction.BULLISH


def test_confidence_within_bounds():
    r = compute([make(title=f"h{i}") for i in range(6)], Asset.GOLD)
    assert 0.0 <= r.confidence <= 1.0


def test_drivers_and_risks_populated():
    r = compute([make(direction=Direction.BULLISH, title="bullish headline one"),
                 make(direction=Direction.BEARISH, title="bearish headline one")],
                Asset.GOLD)
    assert r.drivers and r.risks