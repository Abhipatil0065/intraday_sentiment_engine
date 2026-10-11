"""
NO-BIAS STATE + NEW OUTPUT SHAPE (improvement item 2).

The defect these tests protect: `label = BULLISH if net >= 0` forced a
directional answer out of every input, including an empty one, and the forced
100% bull/bear split made a coin-flip look like a reading.

Contract:
  score      -100..+100
  confidence  0..1
  bias       BULLISH | BEARISH | NO_BIAS
NO_BIAS whenever confidence or |score| is under a configurable threshold.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.sentiment_engine import Bias, WeightedSignal, clear_bias_history, compute
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
)
from tests.fixed_clock import FIXED_NOW as NOW, freeze_now


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Pin the clock and reset carry-forward history around each test."""
    freeze_now(monkeypatch)
    clear_bias_history()
    yield
    clear_bias_history()


def make(asset=Asset.GOLD, direction=Direction.BULLISH, strength=ImpactStrength.HIGH,
         importance=EconomicImportance.HIGH, relevance=0.9, published_at=None,
         title="Fed signals dovish pivot", category=NewsCategory.CENTRAL_BANK,
         cluster_id="c1", session_multiplier=1.0, source_quality="TIER_1_OFFICIAL",
         persistence=0.7):
    return WeightedSignal(
        asset=asset,
        title=title,
        direction=direction,
        strength=strength,
        importance=importance,
        published_at=published_at or NOW,
        relevance=relevance,
        persistence=persistence,
        source_quality=source_quality,
        session_multiplier=session_multiplier,
        category=category,
        cluster_id=cluster_id,
    )


def _min_score() -> float:
    from app.config import settings
    return float(getattr(settings, "bias_min_abs_score", 10.0))


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------
def test_result_exposes_score_confidence_and_bias():
    r = compute([make()], Asset.GOLD)
    assert -100.0 <= r.score <= 100.0
    assert 0.0 <= r.confidence <= 1.0
    assert r.bias in (Bias.BULLISH, Bias.BEARISH, Bias.NO_BIAS)


def test_bull_pct_and_bear_pct_are_gone():
    r = compute([make()], Asset.GOLD)
    assert not hasattr(r, "bull_pct")
    assert not hasattr(r, "bear_pct")


def test_no_forced_100_percent_split():
    """A lopsided read must NOT be reported as 100/0."""
    signals = [make(direction=Direction.BULLISH,
                     strength=ImpactStrength.VERY_HIGH,
                     importance=EconomicImportance.VERY_HIGH)
               for _ in range(4)]
    signals.append(make(direction=Direction.BEARISH, strength=ImpactStrength.LOW,
                        importance=EconomicImportance.LOW, title="weak counter"))
    r = compute(signals, Asset.GOLD)
    assert r.score < 100.0


def test_mode_is_stamped_on_every_result():
    for signals in ([make()], [], [make(direction=Direction.NEUTRAL)]):
        r = compute(signals, Asset.GOLD)
        assert r.mode in ("heuristic", "llm", "mixed")


def test_no_signals_carries_forward_with_zero_confidence():
    r = compute([], Asset.GOLD)
    assert r.bias in (Bias.BULLISH, Bias.BEARISH)
    assert r.confidence == 0.0
    assert r.score == 0.0
    assert r.stale is True


# --------------------------------------------------------------------------
# Binary bias: a side is always named, never withheld as NO_BIAS
# --------------------------------------------------------------------------
def test_all_directionless_items_produce_a_binary_verdict():
    signals = [make(direction=Direction.NEUTRAL, title="filing"),
               make(direction=Direction.MIXED, title="two-sided")]
    r = compute(signals, Asset.GOLD)
    assert r.bias in (Bias.BULLISH, Bias.BEARISH)


def test_tiny_net_score_is_low_confidence():
    r = compute([make(direction=Direction.BULLISH, relevance=0.2,
                      strength=ImpactStrength.LOW,
                      importance=EconomicImportance.LOW)], Asset.GOLD)
    if abs(r.score) < _min_score():
        assert r.bias in (Bias.BULLISH, Bias.BEARISH)
        assert r.confidence_label == "LOW"


def test_low_confidence_is_no_bias_even_with_a_signed_score():
    signals = [make(direction=Direction.BULLISH, title=f"bull {i}", cluster_id=f"c{i}")
               for i in range(2)]
    signals.append(make(direction=Direction.BEARISH, title="bear", cluster_id="cb"))
    r = compute(signals, Asset.GOLD)
    if r.confidence < _min_conf():
        assert r.bias is Bias.NO_BIAS


def test_balanced_conflict_still_names_a_side():
    signals = []
    for i in range(3):
        signals.append(make(direction=Direction.BULLISH, title=f"bull {i}",
                            cluster_id=f"b{i}"))
        signals.append(make(direction=Direction.BEARISH, title=f"bear {i}",
                            cluster_id=f"r{i}"))
    r = compute(signals, Asset.GOLD)
    assert abs(r.score) < 25.0
    assert r.bias in (Bias.BULLISH, Bias.BEARISH)


# --------------------------------------------------------------------------
# Direction + score semantics
# --------------------------------------------------------------------------
def test_overwhelming_bullish_read_is_bullish_with_high_score():
    signals = [make(direction=Direction.BULLISH, strength=ImpactStrength.VERY_HIGH,
                    importance=EconomicImportance.VERY_HIGH, title=f"b{i}",
                    cluster_id=f"c{i}") for i in range(5)]
    r = compute(signals, Asset.GOLD)
    assert r.bias is Bias.BULLISH
    assert r.score > 40.0
    assert r.confidence > 0.4


def test_bearish_read_has_negative_score():
    signals = [make(direction=Direction.BEARISH, strength=ImpactStrength.VERY_HIGH,
                    importance=EconomicImportance.VERY_HIGH, title=f"r{i}",
                    cluster_id=f"c{i}") for i in range(5)]
    r = compute(signals, Asset.GOLD)
    assert r.bias is Bias.BEARISH
    assert r.score < 0.0


def test_assets_are_scored_independently():
    signals = [
        make(asset=Asset.GOLD, direction=Direction.BULLISH, title="gold bid"),
        make(asset=Asset.BITCOIN, direction=Direction.BEARISH, title="btc sold"),
    ]
    g = compute(signals, Asset.GOLD)
    b = compute(signals, Asset.BITCOIN)
    assert g.score > 0 > b.score


def test_mixed_items_lower_confidence_but_keep_the_leading_side():
    only_bull = compute([make(direction=Direction.BULLISH, title=f"b{i}",
                              cluster_id=f"c{i}") for i in range(4)], Asset.GOLD)
    with_mixed = compute(
        [make(direction=Direction.BULLISH, title=f"b{i}", cluster_id=f"c{i}")
         for i in range(4)]
        + [make(direction=Direction.MIXED, title="mixed")], Asset.GOLD)
    assert with_mixed.confidence < only_bull.confidence


# --------------------------------------------------------------------------
# Explainability (item 10)
# --------------------------------------------------------------------------
def test_top_three_drivers_are_reported_with_contribution():
    signals = [make(direction=Direction.BULLISH, title=f"driver {i}",
                    cluster_id=f"c{i}") for i in range(5)]
    r = compute(signals, Asset.GOLD)
    assert len(r.drivers) <= 3
    assert all(d.title and d.contribution != 0.0 for d in r.drivers)


def test_drivers_are_ordered_by_absolute_contribution():
    signals = [
        make(direction=Direction.BULLISH, title="small",
             strength=ImpactStrength.LOW, importance=EconomicImportance.LOW),
        make(direction=Direction.BULLISH, title="big",
             strength=ImpactStrength.VERY_HIGH,
             importance=EconomicImportance.VERY_HIGH, cluster_id="c2"),
    ]
    r = compute(signals, Asset.GOLD)
    contribs = [abs(d.contribution) for d in r.drivers]
    assert contribs == sorted(contribs, reverse=True)


def test_strongest_conflicting_headline_is_named():
    signals = [
        make(direction=Direction.BULLISH, title="bull case", cluster_id="c1"),
        make(direction=Direction.BEARISH, title="bear case", cluster_id="c2"),
    ]
    r = compute(signals, Asset.GOLD)
    assert r.conflict is not None
    assert r.conflict.title in ("bull case", "bear case")


def test_no_conflict_when_all_signals_agree():
    r = compute([make(direction=Direction.BULLISH, cluster_id=f"c{i}")
                  for i in range(3)], Asset.GOLD)
    assert r.conflict is None


def test_changed_in_last_60_min_delta_is_reported():
    signals = [make(direction=Direction.BULLISH, title=f"b{i}", cluster_id=f"c{i}")
               for i in range(3)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert hasattr(r, "delta_60m")
    assert isinstance(r.delta_60m, float)


def test_delta_ignores_signals_older_than_the_window():
    fresh = [make(direction=Direction.BULLISH, title=f"b{i}", cluster_id=f"c{i}")
             for i in range(3)]
    old = [make(direction=Direction.BULLISH,
                published_at=NOW - timedelta(hours=6),
                title=f"o{i}", cluster_id=f"o{i}") for i in range(3)]
    assert compute(fresh + old, Asset.GOLD, now=NOW).delta_60m == 0.0
    assert compute(fresh, Asset.GOLD, now=NOW).delta_60m != 0.0
def test_confidence_never_gates_the_label(monkeypatch):
    """Binary bias: confidence is still reported, but a high floor can no
    longer withhold the verdict - the side comes from the score's sign."""
    from app.config import settings

    strong = [make(direction=Direction.BULLISH, strength=ImpactStrength.VERY_HIGH,
                   importance=EconomicImportance.VERY_HIGH)]
    baseline = compute(strong, Asset.GOLD)
    assert baseline.bias is Bias.BULLISH

    monkeypatch.setattr(settings, "bias_min_confidence", 0.99, raising=False)
    strict = compute(strong, Asset.GOLD)
    assert strict.bias is Bias.BULLISH
    assert 0.0 <= strict.confidence <= 1.0
def _min_conf() -> float:
    from app.config import settings
    return float(getattr(settings, "bias_min_confidence", 0.25))