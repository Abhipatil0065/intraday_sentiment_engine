"""
IMPACT GATE (config.only_high_impact_counts).

The product rule: a card's bullish/bearish split AND its driver boxes are
built from VERY_HIGH / HIGH impact news alone. MEDIUM and LOW impact items
must never reach either.

Everything the dashboard renders on a card - bull_pct / bear_pct, the
BULLISH DRIVERS and BEARISH DRIVERS boxes - is derived from the same two
totals (bull, bear) that `compute()` aggregates, so these tests assert on the
verdict, the driver lists and the counts, never on a re-derived split.

The autouse fixture in conftest.py turns the gate OFF for the rest of the
suite (those tests predate it and use MEDIUM signals as generic evidence); this
file runs with the gate ON and asserts both sides of the flag.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.analysis.sentiment_engine import (
    Bias,
    WeightedSignal,
    _aggregate,
    clear_bias_history,
    compute,
)
from app.analysis.display import to_display
from app.config import settings
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
    """Pin the clock, reset carry-forward history, force the gate ON."""
    freeze_now(monkeypatch)
    monkeypatch.setattr(settings, "only_high_impact_counts", True)
    clear_bias_history()
    yield
    clear_bias_history()


def make(direction=Direction.BULLISH, strength=ImpactStrength.HIGH,
         importance=EconomicImportance.HIGH, minutes_ago=5,
         title="test headline", **kw) -> WeightedSignal:
    """One fresh, high-relevance GOLD signal - straight from the make() shape
    the rest of the suite uses, so only the fields under test differ."""
    return WeightedSignal(
        asset=Asset.GOLD, title=title, direction=direction, strength=strength,
        importance=importance,
        published_at=NOW - timedelta(minutes=minutes_ago),
        relevance=kw.get("relevance", 0.8),
        persistence=kw.get("persistence", 0.7),
        category=kw.get("category", NewsCategory.CENTRAL_BANK),
        corroboration=kw.get("corroboration", 1.0),
        cluster_id=kw.get("cluster_id", ""),
    )


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------
def test_medium_and_low_news_never_reach_the_card():
    """A LOW and a MEDIUM bearish headline beside one HIGH bullish one must
    appear in neither the totals, nor the driver boxes, nor the conflict."""
    signals = [
        make(Direction.BULLISH, ImpactStrength.VERY_HIGH,
             EconomicImportance.VERY_HIGH, title="Fed pauses, yields drop"),
        make(Direction.BEARISH, ImpactStrength.MEDIUM,
             EconomicImportance.MEDIUM, title="analyst note trims metals",
             cluster_id="c-medium"),
        make(Direction.BEARISH, ImpactStrength.LOW,
             EconomicImportance.LOW, title="minor ETF outflow",
             cluster_id="c-low"),
    ]
    r = compute(signals, Asset.GOLD, now=NOW)

    assert r.contributing_articles == 1, "only the VERY_HIGH signal is evidence"
    assert [d.title for d in r.bull_drivers] == ["Fed pauses, yields drop"]
    assert r.bear_drivers == [], "no bearish driver may reach the card"
    assert r.conflict is None, "a gated headline cannot be named as the conflict"
    assert r.bias is Bias.BULLISH


def test_display_split_is_unchanged_by_medium_and_low_news():
    """bull_pct / bear_pct come from the same totals, so adding or removing
    low-impact headlines must not move them."""
    high_only = [make(Direction.BULLISH, ImpactStrength.HIGH,
                      EconomicImportance.HIGH, title="soft CPI")]
    noisy = high_only + [
        make(Direction.BEARISH, ImpactStrength.MEDIUM,
             EconomicImportance.MEDIUM, title="analyst note trims metals"),
        make(Direction.BEARISH, ImpactStrength.LOW,
             EconomicImportance.LOW, title="minor ETF outflow"),
    ]

    a = to_display(compute(high_only, Asset.GOLD, now=NOW).score,
                   compute(high_only, Asset.GOLD, now=NOW).bias)
    b = to_display(compute(noisy, Asset.GOLD, now=NOW).score,
                   compute(noisy, Asset.GOLD, now=NOW).bias)

    assert (a["bull_pct"], a["bear_pct"]) == (b["bull_pct"], b["bear_pct"])


def test_gate_uses_impact_strength_not_importance():
    """The gate reads the per-asset IMPACT band (AssetImpact.strength), which
    is what actually scales the card. A VERY_HIGH-importance article whose
    relevance-capped impact band is MEDIUM still may not reach the card."""
    signals = [make(Direction.BULLISH, ImpactStrength.MEDIUM,
                    EconomicImportance.VERY_HIGH, title="relevance-capped")]
    r = compute(signals, Asset.GOLD, now=NOW)

    assert r.bull_drivers == []
    assert r.bear_drivers == []
    assert r.contributing_articles == 0


def test_only_low_impact_news_leaves_an_empty_card():
    """Nothing admissible: no drivers, no evidence, thin branch - the card
    carries a label instead of inventing weight for noise."""
    signals = [
        make(Direction.BULLISH, ImpactStrength.MEDIUM,
             EconomicImportance.MEDIUM, title="m1", cluster_id="c1"),
        make(Direction.BEARISH, ImpactStrength.LOW,
             EconomicImportance.LOW, title="m2", cluster_id="c2"),
    ]
    r = compute(signals, Asset.GOLD, now=NOW)

    assert r.drivers == [] and r.bull_drivers == [] and r.bear_drivers == []
    assert r.conflict is None
    assert r.contributing_articles == 0
    assert r.signal_count == 2, "the items existed, they simply did not count"
    assert r.score == 0.0
    assert r.stale is True


def test_burst_bonus_ignores_gated_signals():
    """The news-velocity bonus reads the raw signal list, so three fresh
    MEDIUM headlines must not boost their side by 1.2x."""
    fresh_medium = [
        make(Direction.BULLISH, ImpactStrength.MEDIUM,
             EconomicImportance.MEDIUM, minutes_ago=5, title=f"m{i}",
             cluster_id=f"c{i}")
        for i in range(3)
    ]
    agg = _aggregate(fresh_medium, NOW)

    assert agg["bull"] == 0.0
    assert agg["burst_bull"] == 0
    assert agg["used"] == 0


def test_flag_off_restores_the_old_scaled_behaviour():
    """Same inputs, flag off: MEDIUM/LOW contribute again, reduced but real."""
    signals = [
        make(Direction.BULLISH, ImpactStrength.MEDIUM,
             EconomicImportance.MEDIUM, title="analyst note lifts metals"),
        make(Direction.BEARISH, ImpactStrength.HIGH,
             EconomicImportance.HIGH, title="hot CPI"),
    ]
    settings.only_high_impact_counts = False
    try:
        off = compute(signals, Asset.GOLD, now=NOW)
    finally:
        settings.only_high_impact_counts = True
    on = compute(signals, Asset.GOLD, now=NOW)

    # Gate off: the MEDIUM bullish headline is a real driver and the bear side
    # has to fight it. Gate on: only "hot CPI" exists.
    assert any(d.title == "analyst note lifts metals" for d in off.bull_drivers)
    assert [d.title for d in on.bull_drivers] == []
    assert [d.title for d in on.bear_drivers] == ["hot CPI"]
    assert on.score < 0.0
