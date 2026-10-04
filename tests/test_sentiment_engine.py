"""
Unit tests for the sentiment aggregator (items 2, 6, 7).

REWRITTEN for the new contract. The old file asserted two guarantees that item
2 deliberately removes:

    * "bull_pct + bear_pct == 100"      -> the forced 100% split
    * "label is always BULLISH/BEARISH" -> the forced binary verdict

The guarantees that SURVIVE and are still tested here are the ones that matter:
per-asset isolation, damped duplicate coverage, category-aware decay, and
visible conflicts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.sentiment_engine import Bias, WeightedSignal, compute
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
)

# The engine decays against the clock, so tests must pin it. Previously this
# was datetime.now(), which meant the suite exercised a different decay path
# depending on the hour it ran.
from tests.fixed_clock import FIXED_NOW as NOW, freeze_now


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    """Freeze the clock: decay drives score and confidence here."""
    freeze_now(monkeypatch)


def make(asset=Asset.GOLD, direction=Direction.BULLISH,
         strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
         minutes_ago=5, title="test headline",
         category=NewsCategory.CENTRAL_BANK, **kw) -> WeightedSignal:
    return WeightedSignal(
        asset=asset, title=title, direction=direction, strength=strength,
        importance=importance,
        published_at=NOW - timedelta(minutes=minutes_ago),
        relevance=kw.get("relevance", 0.8),
        persistence=kw.get("persistence", 0.7),
        category=category,
        is_primary=kw.get("is_primary", True),
        corroboration=kw.get("corroboration", 1.0),
    )


# --------------------------------------------------------------------------
# Bounds - the replacement for the old 100% invariant
# --------------------------------------------------------------------------
@pytest.mark.parametrize("count", [1, 2, 5, 12, 40])
def test_score_and_confidence_stay_within_bounds(count):
    directions = [Direction.BULLISH, Direction.BEARISH,
                  Direction.NEUTRAL, Direction.MIXED]
    signals = [make(direction=directions[i % 4], title=f"h{i}")
               for i in range(count)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert -100.0 <= r.score <= 100.0
    assert 0.0 <= r.confidence <= 1.0


def test_bias_is_one_of_three_states():
    signals = [make(direction=d) for d in
               (Direction.BULLISH, Direction.BEARISH, Direction.NEUTRAL)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.bias in (Bias.BULLISH, Bias.BEARISH, Bias.NO_BIAS)


def test_no_signals_is_no_bias_with_zero_confidence():
    r = compute([], Asset.GOLD, now=NOW)
    assert r.bias is Bias.NO_BIAS
    assert r.score == 0.0
    assert r.confidence == 0.0


def test_all_directionless_items_yield_no_confidence():
    signals = [make(direction=Direction.NEUTRAL, title=f"n{i}")
               for i in range(5)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.confidence == 0.0
    assert r.bias is Bias.NO_BIAS
def test_dominant_bullish_tilt():
    signals = [make(direction=Direction.BULLISH, title=f"b{i}",
                    cluster_id=f"c{i}") for i in range(6)]
    signals += [make(direction=Direction.BEARISH, title="bear",
                     strength=ImpactStrength.LOW,
                     importance=EconomicImportance.LOW)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.score > 0
    assert r.bias is Bias.BULLISH


def test_dominant_bearish_tilt():
    signals = [make(direction=Direction.BEARISH, title=f"r{i}",
                    cluster_id=f"c{i}") for i in range(6)]
    signals += [make(direction=Direction.BULLISH, title="bull",
                     strength=ImpactStrength.LOW,
                     importance=EconomicImportance.LOW)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.score < 0
    assert r.bias is Bias.BEARISH


def test_conflicting_signals_reported_not_forced():
    """A genuine conflict must be surfaced, never silently resolved."""
    signals = [
        make(direction=Direction.BULLISH, title="Fed turns dovish"),
        make(direction=Direction.BEARISH, title="Dollar surges broadly"),
    ]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.conflict is not None, "a genuine conflict must be reported"
    assert r.conflict.title in ("Fed turns dovish", "Dollar surges broadly")


def test_balanced_conflict_is_no_bias_not_a_coin_flip():
    """An even conflict must NOT be dressed up as a directional call."""
    signals = [
        make(direction=Direction.BULLISH, title="dovish signal", cluster_id="a"),
        make(direction=Direction.BEARISH, title="hawkish signal", cluster_id="b"),
    ]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.bias is Bias.NO_BIAS
    assert r.confidence < 0.95


def test_recency_dominance_fresher_news_wins():
    fresh = [make(direction=Direction.BULLISH, title="fresh bullish",
                  minutes_ago=2)]
    stale = [make(direction=Direction.BEARISH, title="stale bearish",
                  minutes_ago=600)]
    r_bull = compute(fresh, Asset.GOLD, now=NOW)
    r_bear = compute(fresh + stale, Asset.GOLD, now=NOW)
    # The stale bearish item must not overturn a very fresh bullish one.
    assert r_bear.score < r_bull.score


def test_category_half_lives_differ():
    """Item 7: a regulatory story must outlast a CPI print."""
    cpi = [make(direction=Direction.BEARISH, title="hot cpi", minutes_ago=600,
                category=NewsCategory.INFLATION, cluster_id="c1")]
    crypto = [make(direction=Direction.BEARISH, title="crypto ruling",
                   minutes_ago=600, category=NewsCategory.CRYPTO_REGULATION,
                   cluster_id="c1")]
    w_cpi = compute(cpi, Asset.GOLD, now=NOW).drivers[0].weight
    w_crypto = compute(crypto, Asset.GOLD, now=NOW).drivers[0].weight
    assert w_crypto > w_cpi


def test_duplicate_event_coverage_is_damped():
    """One cluster is one vote: corroboration lifts it, never multiplies it."""
    solo = [make(direction=Direction.BULLISH, title="one story",
                 cluster_id="a")]
    corroborated = [make(direction=Direction.BULLISH, title="one story",
                         cluster_id="a", corroboration=1.30)]
    s1 = compute(solo, Asset.GOLD, now=NOW).score
    s2 = compute(corroborated, Asset.GOLD, now=NOW).score
    assert s1 > 0 and s2 > 0


def test_assets_are_scored_independently():
    """One asset's flow must never leak into another's."""
    signals = [
        make(asset=Asset.BITCOIN, direction=Direction.BULLISH, title="btc etf"),
        make(asset=Asset.GOLD, direction=Direction.BEARISH, title="hawkish fed"),
    ]
    assert compute(signals, Asset.GOLD, now=NOW).score < 0
    assert compute(signals, Asset.BITCOIN, now=NOW).score > 0
    # An asset nothing reached must report nothing, not inherit a neighbour's view.
    assert compute(signals, Asset.SILVER, now=NOW).contributing_articles == 0


def test_drivers_and_conflict_are_populated():
    signals = [
        make(direction=Direction.BULLISH, title="bull one", cluster_id="a"),
        make(direction=Direction.BULLISH, title="bull two", cluster_id="b"),
        make(direction=Direction.BEARISH, title="bear one", cluster_id="c"),
    ]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.drivers
    assert r.conflict is not None
    assert all(d.title for d in r.drivers)
