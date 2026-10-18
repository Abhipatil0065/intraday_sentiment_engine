"""
V6: New tests for contributing_clusters fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.event_clustering import cluster_signals
from app.analysis.sentiment_engine import _aggregate, compute
from app.analysis.signal_factory import build_signal
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength,
    NewsCategory, ReportKind,
)

NOW = datetime(2026, 10, 18, 13, 30, tzinfo=timezone.utc)


def _mk(title, mins=0, asset=Asset.GOLD, key="", direction=Direction.BULLISH):
    return build_signal(
        asset=asset, title=title, direction=direction,
        strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
        published_at=NOW - timedelta(minutes=mins),
        relevance=0.9, persistence=0.9,
        source_quality="TIER_2_MAJOR", source_name="Reuters",
        category=NewsCategory.GEOPOLITICS,
        event_key=key, cluster_id=key,
        report_kind=ReportKind.GENERAL, session_multiplier=1.0,
    )


def _unkeyed(title, mins=0, asset=Asset.GOLD):
    """Unkeyed signal: no event_key, no cluster_id."""
    return build_signal(
        asset=asset, title=title, direction=Direction.BULLISH,
        strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
        published_at=NOW - timedelta(minutes=mins),
        relevance=0.9, persistence=0.9,
        source_quality="TIER_2_MAJOR", source_name="Reuters",
        category=NewsCategory.GEOPOLITICS,
        event_key="", cluster_id="",
        report_kind=ReportKind.GENERAL, session_multiplier=1.0,
    )


def test_unkeyed_clusters_get_a_cluster_id():
    """Unkeyed clusters should be stamped with a generated cluster_id."""
    # 8 near-duplicate headlines, no event_key.
    headlines = [
        "Fed holds rates steady as inflation cools",
        "Fed holds rates steady as inflation eases",
        "Fed holds rates steady as inflation cools further",
        "Fed holds rates steady amid cooling inflation",
        "Fed holds rates steady while inflation cools",
        "Fed keeps rates steady as inflation cools",
        "Fed leaves rates steady as inflation cools",
        "Fed holds rates steady on cooling inflation",
    ]
    signals = [_unkeyed(t, mins=i) for i, t in enumerate(headlines)]

    clustered = cluster_signals(signals, window_hours=0.5)
    agg = _aggregate(clustered, now=NOW)

    assert agg["clusters"] == 1, f"expected contributing_clusters=1, got {agg['clusters']}"
    # The survivor must have a generated cluster_id
    assert all(s.cluster_id for s in clustered), "all survivors must have cluster_id"
    assert clustered[0].cluster_id.startswith("unkeyed-"), (
        f"cluster_id must start with 'unkeyed-': {clustered[0].cluster_id}")


def test_keyed_clusters_keep_event_key_as_cluster_id():
    """Keyed clusters should continue to use event_key as cluster_id."""
    # 8 near-duplicate headlines sharing event_key='evt-test-001'.
    headlines = [
        "Fed holds rates steady as inflation cools",
        "Fed holds rates steady as inflation eases",
        "Fed holds rates steady as inflation cools further",
        "Fed holds rates steady amid cooling inflation",
        "Fed holds rates steady while inflation cools",
        "Fed keeps rates steady as inflation cools",
        "Fed leaves rates steady as inflation cools",
        "Fed holds rates steady on cooling inflation",
    ]
    signals = [_mk(t, mins=i, key="evt-test-001") for i, t in enumerate(headlines)]

    clustered = cluster_signals(signals, window_hours=0.5)
    agg = _aggregate(clustered, now=NOW)

    assert agg["clusters"] == 1, f"expected contributing_clusters=1, got {agg['clusters']}"
    # The survivor must keep the original event_key as cluster_id
    assert clustered[0].cluster_id == "evt-test-001", (
        f"survivor.cluster_id must equal event_key: {clustered[0].cluster_id}")


def test_two_distinct_unkeyed_clusters_count_as_two():
    """Two distinct unkeyed clusters should count as 2."""
    # Cluster A: 4 near-duplicates about Fed (bullish)
    headlines_a = [
        "Fed holds rates steady as inflation cools",
        "Fed holds rates steady as inflation eases",
        "Fed holds rates steady as inflation cools further",
        "Fed holds rates steady amid cooling inflation",
    ]
    # Cluster B: 4 near-duplicates about ECB (different entities, bearish)
    headlines_b = [
        "ECB holds rates steady as inflation cools",
        "ECB holds rates steady as inflation eases",
        "ECB holds rates steady as inflation cools further",
        "ECB holds rates steady amid cooling inflation",
    ]
    signals_a = [_unkeyed(t, mins=i) for i, t in enumerate(headlines_a)]
    signals_b = [build_signal(
        asset=Asset.GOLD, title=t, direction=Direction.BEARISH,
        strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
        published_at=NOW - timedelta(minutes=i),
        relevance=0.9, persistence=0.9,
        source_quality="TIER_2_MAJOR", source_name="Reuters",
        category=NewsCategory.GEOPOLITICS,
        event_key="", cluster_id="",
        report_kind=ReportKind.GENERAL, session_multiplier=1.0,
    ) for i, t in enumerate(headlines_b)]
    signals = signals_a + signals_b

    clustered = cluster_signals(signals, window_hours=0.5)
    agg = _aggregate(clustered, now=NOW)

    assert agg["clusters"] == 2, f"expected contributing_clusters=2, got {agg['clusters']}"
    assert all(s.cluster_id for s in clustered), "all survivors must have cluster_id"
    # The two clusters must have different cluster_ids
    assert clustered[0].cluster_id != clustered[1].cluster_id, (
        "distinct clusters must have different generated IDs")


def test_unkeyed_cluster_id_is_deterministic():
    """Same input -> same generated cluster_id."""
    headlines = [
        "Fed holds rates steady as inflation cools",
        "Fed holds rates steady as inflation eases",
        "Fed holds rates steady as inflation cools further",
        "Fed holds rates steady amid cooling inflation",
        "Fed holds rates steady while inflation cools",
        "Fed keeps rates steady as inflation cools",
        "Fed leaves rates steady as inflation cools",
        "Fed holds rates steady on cooling inflation",
    ]
    signals1 = [_unkeyed(t, mins=i) for i, t in enumerate(headlines)]
    signals2 = [_unkeyed(t, mins=i) for i, t in enumerate(headlines)]

    clustered1 = cluster_signals(signals1, window_hours=0.5)
    clustered2 = cluster_signals(signals2, window_hours=0.5)

    ids1 = {s.cluster_id for s in clustered1 if s.cluster_id}
    ids2 = {s.cluster_id for s in clustered2 if s.cluster_id}

    assert ids1 == ids2, f"cluster_ids must be deterministic: {ids1} vs {ids2}"
    assert len(ids1) == 1, f"expected 1 cluster, got {len(ids1)}"


def test_calendar_signals_carry_event_key_as_cluster_id():
    """Calendar signals via _calendar_signal_to_weighted should carry event_key."""
    from app.services.pipeline import _calendar_signal_to_weighted
    from app.analysis.economic_impact import CalendarSignal, CalendarEvent, PROFILES
    from app.models.enums import Asset, Direction, ImpactStrength, EconomicImportance, NewsCategory

    # Construct a fake CalendarEvent with event_key
    event = CalendarEvent(
        title="US CPI YoY",
        country="USD",
        when_utc=NOW,
        impact="High",
        forecast=3.2,
        previous=3.1,
        actual=3.3,
        provider="financialjuice",
    )
    # Use an existing profile from PROFILES (INFLATION profile)
    profile = PROFILES[0]  # INFLATION profile
    cal_sig = CalendarSignal(
        event=event,
        profile=profile,
        asset=Asset.GOLD,
        direction=Direction.BULLISH,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.HIGH,
        relevance=0.9,
        persistence=0.9,
        mode="RELEASED",
        reason="CPI surprise",
        transmission="direct",
        surprise=0.1,
    )

    # Convert to WeightedSignal (what pipeline does)
    ws = _calendar_signal_to_weighted(cal_sig)

    # Cluster it (single signal -> its own cluster)
    clustered = cluster_signals([ws], window_hours=0.5)
    agg = _aggregate(clustered, now=NOW)

    assert agg["clusters"] == 1, f"expected contributing_clusters=1, got {agg['clusters']}"
    # The survivor must carry the event_key as cluster_id
    assert clustered[0].cluster_id == event.event_key, (
        f"calendar survivor.cluster_id must equal event_key "
        f"({clustered[0].cluster_id} != {event.event_key})")
def test_contributing_clusters_in_payload():
    """After a tick, the payload should expose contributing_clusters."""
    from app.database import get_session
    from app.main import build_payload

    with get_session() as s:
        payload = build_payload(s)

    assets = payload.get("assets", [])
    assert len(assets) > 0
    for asset in assets:
        cur = asset.get("current", {})
        assert "contributing_clusters" in cur, f"missing on {asset['asset']}"
        assert cur["contributing_clusters"] >= 0
