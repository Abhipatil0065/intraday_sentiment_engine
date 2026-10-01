"""Unit tests for the recency engine (section 4/5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.recency_engine import (
    IMPORTANCE_WEIGHT,
    compute_final_weight,
    recency_weight,
)
from app.models.enums import EconomicImportance, ImpactStrength


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (5, 1.00), (15, 1.00),
        (20, 0.90), (30, 0.90),
        (45, 0.80), (60, 0.80),
        (90, 0.65), (120, 0.65),
        (180, 0.50), (240, 0.50),
        (360, 0.30), (480, 0.30),
        (600, 0.15), (1500, 0.15),
    ],
)
def test_recency_buckets(minutes, expected):
    """Each documented bucket boundary maps to its specified weight."""
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    published = now - timedelta(minutes=minutes)
    assert recency_weight(published, now=now) == pytest.approx(expected)


def test_recency_missing_timestamp_gets_floor():
    assert recency_weight(None) == 0.15


def test_recency_monotonically_decreasing():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    weights = [recency_weight(now - timedelta(minutes=m), now=now)
               for m in (5, 25, 45, 90, 180, 360, 600)]
    assert weights == sorted(weights, reverse=True)


def test_importance_weight_ordering():
    """VERY_HIGH must outweigh HIGH outweigh MEDIUM outweigh LOW."""
    assert (IMPORTANCE_WEIGHT[EconomicImportance.VERY_HIGH]
            > IMPORTANCE_WEIGHT[EconomicImportance.HIGH]
            > IMPORTANCE_WEIGHT[EconomicImportance.MEDIUM]
            > IMPORTANCE_WEIGHT[EconomicImportance.LOW])


def test_final_weight_is_product_of_factors():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    published = now - timedelta(minutes=10)
    f = compute_final_weight(
        published_at=published,
        importance=EconomicImportance.VERY_HIGH,
        strength=ImpactStrength.VERY_HIGH,
        asset_relevance=0.8,
        persistence=0.5,
        source_quality="TIER_2_MAJOR",
        now=now,
    )
    expected = (f["recency"] * f["economic_importance"] * f["asset_relevance"]
                * f["persistence"] * f["source_quality"] * f["session_relevance"])
    assert f["final_weight"] == pytest.approx(expected)


def test_recency_alone_does_not_override_importance():
    """
    Section 4: a major central-bank announcement from hours ago can outweigh
    a minor headline from five minutes ago.
    """
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    old_major = compute_final_weight(
        published_at=now - timedelta(hours=3),
        importance=EconomicImportance.VERY_HIGH,
        strength=ImpactStrength.VERY_HIGH,
        asset_relevance=0.9, persistence=0.9, now=now,
    )["final_weight"]

    fresh_minor = compute_final_weight(
        published_at=now - timedelta(minutes=3),
        importance=EconomicImportance.LOW,
        strength=ImpactStrength.LOW,
        asset_relevance=0.3, persistence=0.3, now=now,
    )["final_weight"]

    assert old_major > fresh_minor


def test_source_quality_orders_official_above_other():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    common = dict(published_at=now, importance=EconomicImportance.HIGH,
                  strength=ImpactStrength.HIGH, asset_relevance=0.7,
                  persistence=0.6, now=now)
    official = compute_final_weight(source_quality="TIER_1_OFFICIAL", **common)
    other = compute_final_weight(source_quality="TIER_4_OTHER", **common)
    assert official["final_weight"] > other["final_weight"]