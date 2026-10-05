"""
PHASE 1: THE TWO WIRING BUGS.

Before this phase the production path built ``WeightedSignal`` inline and never
passed ``category`` or ``corroboration``. Both fell back to dataclass defaults,
so every article decayed on the 6h "Other" half-life regardless of what kind of
event it was, and multi-source confirmation never boosted anything.

These tests pin the two behaviours that fix must produce. They are written
against the FACTORY (the production builder), not against a hand-rolled signal,
so they fail if the wiring regresses.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.sentiment_engine import (
    Bias,
    WeightedSignal,
    compute,
    decay_weight,
)
from app.analysis.signal_factory import build_signal, resolve_corroboration
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
)
NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def _weight_of(signal, asset=Asset.GOLD) -> float:
    return compute([signal], asset, now=NOW).drivers[0].weight


# --------------------------------------------------------------------------
# (a) A Crypto Regulation signal decays SLOWER than an Other signal
# --------------------------------------------------------------------------
def test_crypto_regulation_category_has_a_longer_half_life_than_other():
    """The headline case for the fix: 72h vs the 6h default."""
    from app.config import settings

    assert settings.half_lives["crypto regulation"] > settings.half_lives["other"]

    old = NOW - timedelta(hours=10)
    crypto = decay_weight(old, NewsCategory.CRYPTO_REGULATION, NOW)
    other = decay_weight(old, NewsCategory.OTHER, NOW)
    assert crypto > other


def test_category_survives_the_factory():
    """The factory must not silently drop category back to the default."""
    s = build_signal(
        asset=Asset.BITCOIN, title="SEC approves spot bitcoin ETF",
        direction=Direction.BULLISH, category=NewsCategory.CRYPTO_REGULATION,
        published_at=NOW - timedelta(hours=10), relevance=0.9,
        source_name="Reuters", event_key="evt-1",
    )
    assert s.category is NewsCategory.CRYPTO_REGULATION


def test_old_crypto_ruling_outweighs_an_old_other_headline():
    """End-to-end: same age and everything else, long-lived category wins."""
    common = dict(
        asset=Asset.BITCOIN, direction=Direction.BEARISH,
        strength=ImpactStrength.HIGH, relevance=0.88, persistence=0.95,
        source_quality="TIER_2_MAJOR", published_at=NOW - timedelta(hours=10),
        event_key="evt-x", title="Crypto regulator finalises custody rules",
    )
    crypto = build_signal(category=NewsCategory.CRYPTO_REGULATION, **common)
    other = build_signal(category=NewsCategory.OTHER, **common)

    assert _weight_of(crypto, Asset.BITCOIN) > _weight_of(other, Asset.BITCOIN)
# --------------------------------------------------------------------------
# (b) 3 independent sources outweigh 1 source
# --------------------------------------------------------------------------
def test_three_independent_sources_outweigh_one():
    """The headline case for corroboration."""
    common = dict(
        asset=Asset.GOLD, title="Fed signals dovish pivot",
        direction=Direction.BULLISH, strength=ImpactStrength.HIGH,
        relevance=0.9, persistence=0.7, published_at=NOW,
        category=NewsCategory.CENTRAL_BANK, event_key="evt-1",
    )
    solo = build_signal(independent_sources=1, **common)
    trio = build_signal(independent_sources=3, **common)

    assert trio.corroboration > solo.corroboration
    assert _weight_of(trio) > _weight_of(solo)


def test_corroboration_boost_is_capped():
    """20 outlets must not out-vote 3."""
    assert resolve_corroboration(3) == pytest.approx(1.20)
    assert resolve_corroboration(20) == pytest.approx(1.30)


def test_single_source_gets_no_boost():
    assert resolve_corroboration(1) == 1.0
    assert resolve_corroboration(0) == 1.0


def test_corroboration_can_be_switched_off_from_config(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "corroboration_enabled", False,
                        raising=False)
    assert resolve_corroboration(9) == 1.0


# --------------------------------------------------------------------------
# contributing_clusters is populated (it used to always be 0)
# --------------------------------------------------------------------------
def _clustered(n: int, key_for) -> list[WeightedSignal]:
    return [
        build_signal(
            asset=Asset.GOLD, title=f"story {i}", direction=Direction.BULLISH,
            strength=ImpactStrength.HIGH, relevance=0.8, published_at=NOW,
            category=NewsCategory.CENTRAL_BANK, event_key=key_for(i))
        for i in range(n)
    ]


def test_event_key_populates_contributing_clusters():
    """The cluster id is what makes the count non-zero."""
    r = compute(_clustered(3, lambda i: f"evt-{i}"), Asset.GOLD, now=NOW)
    assert r.contributing_clusters == 3


def test_one_cluster_is_one_vote_not_many():
    """Three articles on ONE event collapse to a single cluster."""
    r = compute(_clustered(3, lambda i: "evt-shared"), Asset.GOLD, now=NOW)
    assert r.contributing_clusters == 1


def test_cluster_id_falls_back_to_event_key():
    s = build_signal(
        asset=Asset.GOLD, title="t", direction=Direction.BULLISH,
        event_key="evt-9", category=NewsCategory.CENTRAL_BANK, published_at=NOW)
    assert s.cluster_id == "evt-9"


def test_factory_preserves_every_value_it_is_given():
    from app.models.enums import ReportKind

    s = build_signal(
        asset=Asset.SILVER, title="headline",
        direction=Direction.BEARISH, strength=ImpactStrength.VERY_HIGH,
        importance=EconomicImportance.VERY_HIGH,
        published_at=NOW, relevance=0.77, persistence=0.66,
        source_quality="TIER_3_CRYPTO", source_name="CoinDesk",
        category=NewsCategory.CHINA, session_multiplier=1.2,
        is_primary=False, event_key="evt-c", cluster_id="clu-c",
        independent_sources=2, report_kind=ReportKind.PREP,
        reason="why", transmission="chain", mode="llm",
    )
    assert s.asset is Asset.SILVER
    assert s.direction is Direction.BEARISH
    assert s.strength is ImpactStrength.VERY_HIGH
    assert s.importance is EconomicImportance.VERY_HIGH
    assert s.relevance == 0.77
    assert s.persistence == 0.66
    assert s.source_quality == "TIER_3_CRYPTO"
    assert s.category is NewsCategory.CHINA
    assert s.session_multiplier == 1.2
    assert s.is_primary is False
    assert s.cluster_id == "clu-c"
    assert s.report_kind is ReportKind.PREP
    assert s.reason == "why"
    assert s.transmission == "chain"
    assert s.mode == "llm"


def test_build_signal_still_produces_a_normal_verdict():
    s = build_signal(
        asset=Asset.GOLD, title="Fed signals dovish pivot",
        direction=Direction.BULLISH, strength=ImpactStrength.HIGH,
        relevance=0.9, persistence=0.7, published_at=NOW,
        category=NewsCategory.CENTRAL_BANK, event_key="evt-1")
    r = compute([s], Asset.GOLD, now=NOW)
    assert r.bias is Bias.BULLISH
    assert -100.0 <= r.score <= 100.0