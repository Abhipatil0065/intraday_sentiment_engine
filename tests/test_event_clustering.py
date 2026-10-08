"""
EVENT CLUSTERING + CAPPED CORROBORATION BOOST (improvement item 4).

The defect these tests protect: five outlets covering one FOMC decision used
to cast five votes into the aggregate. A cluster must count as ONE event, with
only a small capped boost for independent corroboration.

Method: token-set similarity on normalised titles plus an entity/event overlap
term, restricted to a rolling time window. No external service required.
"""

import pytest
from datetime import datetime, timedelta, timezone

from app.analysis.event_clustering import (
    ClusterMatch,
    EventClusterer,
    corroboration_multiplier,
)

NOW = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Corroboration boost arithmetic (diminishing returns: 1/log2(n+1))
# --------------------------------------------------------------------------
def test_single_source_gets_no_boost():
    assert corroboration_multiplier(1) == 1.0


def test_two_independent_sources_diminishing_boost():
    # 2 sources: per=0.10 * 1/log2(3) = 0.10 * 0.6309 = 0.06309 -> 1.0631
    assert corroboration_multiplier(2) == pytest.approx(1.0631, abs=1e-4)


def test_boost_is_capped_at_thirty_percent():
    assert corroboration_multiplier(99) == 1.30


def test_boost_is_monotonic_non_decreasing():
    values = [corroboration_multiplier(n) for n in range(1, 12)]
    assert values == sorted(values)


def test_zero_or_negative_sources_is_identity():
    assert corroboration_multiplier(0) == 1.0
    assert corroboration_multiplier(-3) == 1.0


def test_boost_parameters_come_from_config(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "corroboration_per_source", 0.20, raising=False)
    # 2 sources with per=0.20: 0.20 * 1/log2(3) = 0.20 * 0.6309 = 0.1262 -> 1.1262
    assert corroboration_multiplier(2) == pytest.approx(1.1262, abs=1e-4)


# --------------------------------------------------------------------------
# Clustering behaviour
# --------------------------------------------------------------------------
def test_near_duplicate_headlines_join_one_cluster():
    c = EventClusterer()
    a = c.match("Fed holds interest rates steady, signals one cut in 2027",
                NOW, source="Reuters")
    b = c.match("Fed holds rates steady and signals a single cut in 2027",
                NOW + timedelta(minutes=5), source="Bloomberg")
    assert a.is_new_cluster is True
    assert b.is_new_cluster is False
    assert b.cluster_id == a.cluster_id


def test_unrelated_headlines_stay_in_separate_clusters():
    c = EventClusterer()
    a = c.match("Fed holds rates steady", NOW, source="Reuters")
    b = c.match("China factory output beats forecasts", NOW, source="Reuters")
    assert b.cluster_id != a.cluster_id


def test_same_source_repeats_do_not_inflate_corroboration():
    c = EventClusterer()
    c.match("Fed holds rates steady", NOW, source="Reuters")
    m = c.match("Fed holds rates steady again", NOW, source="Reuters")
    assert m.independent_sources == 1
    assert m.corroboration == 1.0


def test_independent_sources_raise_corroboration():
    c = EventClusterer()
    c.match("Fed holds rates steady", NOW, source="Reuters")
    m = c.match("Fed holds rates steady", NOW + timedelta(minutes=2), source="CNBC")
    assert m.independent_sources == 2
    assert m.corroboration == pytest.approx(1.0631, abs=1e-4)


def test_cluster_outside_the_time_window_is_a_new_cluster():
    c = EventClusterer(window_hours=2)
    a = c.match("Fed holds rates steady", NOW, source="Reuters")
    b = c.match("Fed holds rates steady", NOW + timedelta(hours=5), source="CNBC")
    assert b.cluster_id != a.cluster_id
    assert b.is_new_cluster is True


def test_numbers_are_ignored_when_clustering():
    c = EventClusterer()
    a = c.match("Gold ETF inflows reach 12 billion", NOW, source="Reuters")
    b = c.match("Gold ETF inflows reach 87 billion", NOW, source="CNBC")
    assert b.cluster_id == a.cluster_id


def test_corroboration_is_reported_on_every_match():
    c = EventClusterer()
    m = c.match("Fed holds rates steady", NOW, source="Reuters")
    assert m.corroboration >= 1.0
    assert m.canonical_title


def test_five_outlets_do_not_produce_five_votes():
    c = EventClusterer()
    m = None
    for i, src in enumerate(["Reuters", "CNBC", "FT", "Bloomberg", "WSJ"]):
        m = c.match("Fed holds rates steady", NOW + timedelta(minutes=i), source=src)
    assert m.independent_sources == 5
    # Diminishing returns: sum_{i=2..5} 1/log2(i+1) = 1.9485 -> 0.19485 -> 1.1948
    # NOT 5x weight, and capped at 1.30
    assert m.corroboration == pytest.approx(1.1948, abs=1e-4)


def test_blank_title_does_not_crash():
    c = EventClusterer()
    m = c.match("", NOW, source="Reuters")
    assert isinstance(m, ClusterMatch)


def test_seeding_from_titles_rebuilds_the_index():
    c = EventClusterer()
    c.seed_from_titles(["Fed holds rates steady"], NOW)
    m = c.match("Fed holds rates steady", NOW, source="CNBC")
    assert m.is_new_cluster is False


# --------------------------------------------------------------------------
# Clustering edge cases (explicitly requested)
# --------------------------------------------------------------------------
# Similarity method: Jaccard on normalized tokens (stopwords removed, numbers
# stripped) + entity boost (0.15 per shared macro entity: fed, fomc, cpi, nfp,
# gold, silver, bitcoin, etc.). Threshold: 0.72 (from settings.dedup_similarity_threshold).
# Time window: 30 minutes (0.5 hours) for cluster_signals, 12 hours for EventClusterer.

def test_differently_worded_same_event_clusters_together():
    """Fed holds rates - different wording, same event, same entities."""
    c = EventClusterer()
    a = c.match("Fed holds rates steady signals one cut in 2027", NOW, source="Reuters")
    b = c.match("Fed holds rates steady and signals a single cut in 2027", NOW + timedelta(minutes=10), source="Bloomberg")
    # Both contain 'fed' entity and share 'holds rates steady signals cut' tokens
    assert b.is_new_cluster is False, f"Expected cluster match, got new cluster. Similarity: {b.similarity}"
    assert b.cluster_id == a.cluster_id


def test_different_events_within_30_min_do_not_merge():
    """NFP and Unemployment Rate at same time - different events, should NOT merge."""
    c = EventClusterer()
    a = c.match("US Nonfarm Payrolls Rise 250K in September", NOW, source="Reuters")
    b = c.match("Unemployment Rate Falls to 3.8 Percent", NOW + timedelta(minutes=15), source="Bloomberg")
    # Both have employment entities but different core events (payrolls vs unemployment)
    # Token overlap is low, entities differ (nonfarm/payrolls vs unemployment/rate)
    assert b.is_new_cluster is True, f"Expected separate cluster, got match. Similarity: {b.similarity}"
    assert b.cluster_id != a.cluster_id


def test_same_title_over_30_min_apart_stays_separate():
    """Identical headline 31 minutes apart - should be separate clusters (time window)."""
    c = EventClusterer(window_hours=0.5)  # 30 min window
    a = c.match("Fed Holds Rates Steady", NOW, source="Reuters")
    b = c.match("Fed Holds Rates Steady", NOW + timedelta(minutes=31), source="CNBC")
    # Outside the 30-minute window, even identical titles should not cluster
    assert b.is_new_cluster is True
    assert b.cluster_id != a.cluster_id


# --------------------------------------------------------------------------
# cluster_signals function tests (scoring-time clustering)
# --------------------------------------------------------------------------
def test_cluster_signals_collapses_duplicates():
    """8 near-duplicate articles should collapse to 1 cluster with corroboration boost."""
    from app.analysis.event_clustering import cluster_signals
    from app.analysis.sentiment_engine import WeightedSignal
    from app.models.enums import Asset, Direction, ImpactStrength, EconomicImportance, NewsCategory
    from datetime import datetime, timezone, timedelta

    base_time = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)

    # Create 8 near-duplicate signals (different sources, same time window)
    signals = []
    source_qualities = ["TIER_1_OFFICIAL", "TIER_2_MAJOR", "TIER_2_MAJOR", "TIER_2_MAJOR", "TIER_2_MAJOR", "TIER_3_CRYPTO", "TIER_4_OTHER", "TIER_4_OTHER"]
    for i, sq in enumerate(source_qualities):
        signals.append(WeightedSignal(
            asset=Asset.GOLD,
            title=f"Fed minutes show policymakers see another rate hike by year end {i}",
            direction=Direction.BEARISH,
            strength=ImpactStrength.HIGH,
            importance=EconomicImportance.VERY_HIGH,
            published_at=base_time + timedelta(minutes=i),
            relevance=0.8,
            persistence=0.5,
            source_quality=sq,
            category=NewsCategory.CENTRAL_BANK,
            final_weight=1.0,  # Will be computed in real usage
        ))

    clustered = cluster_signals(signals, window_hours=0.5)

    # Should collapse to 1 cluster
    assert len(clustered) == 1
    # Should have corroboration boost for multiple independent sources
    # corroboration_multiplier(8) = 1.0 + 0.10 * sum(1/log2(i+1) for i=2..8)
    # = 1.0 + 0.10 * (1/log2(3) + 1/log2(4) + ... + 1/log2(9))
    # ??? 1.0 + 0.10 * 2.5 = 1.25 (capped at 1.30)
    assert clustered[0].corroboration >= 1.15  # At least some boost
    assert clustered[0].corroboration <= 1.30  # Capped


def test_cluster_signals_keeps_unrelated():
    """Unrelated headlines should not be clustered together."""
    from app.analysis.event_clustering import cluster_signals
    from app.analysis.sentiment_engine import WeightedSignal
    from app.models.enums import Asset, Direction, ImpactStrength, EconomicImportance, NewsCategory
    from datetime import datetime, timezone

    base_time = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)

    signals = [
        WeightedSignal(
            asset=Asset.GOLD,
            title="Fed holds rates steady",
            direction=Direction.BEARISH,
            strength=ImpactStrength.HIGH,
            importance=EconomicImportance.VERY_HIGH,
            published_at=base_time,
            relevance=0.8,
            persistence=0.5,
            source_quality="TIER_2_MAJOR",
            category=NewsCategory.CENTRAL_BANK,
        ),
        WeightedSignal(
            asset=Asset.GOLD,
            title="China factory output beats forecasts",
            direction=Direction.BULLISH,
            strength=ImpactStrength.MEDIUM,
            importance=EconomicImportance.HIGH,
            published_at=base_time,
            relevance=0.6,
            persistence=0.5,
            source_quality="TIER_2_MAJOR",
            category=NewsCategory.CHINA,
        ),
    ]

    clustered = cluster_signals(signals, window_hours=0.5)
    assert len(clustered) == 2  # Two separate clusters


def test_cluster_signals_respects_time_window():
    """Articles outside the time window should not be clustered."""
    from app.analysis.event_clustering import cluster_signals
    from app.analysis.sentiment_engine import WeightedSignal
    from app.models.enums import Asset, Direction, ImpactStrength, EconomicImportance, NewsCategory
    from datetime import datetime, timezone, timedelta

    base_time = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)

    signals = [
        WeightedSignal(
            asset=Asset.GOLD,
            title="Fed holds rates steady",
            direction=Direction.BEARISH,
            strength=ImpactStrength.HIGH,
            importance=EconomicImportance.VERY_HIGH,
            published_at=base_time,
            relevance=0.8,
            persistence=0.5,
            source_quality="TIER_2_MAJOR",
            category=NewsCategory.CENTRAL_BANK,
        ),
        WeightedSignal(
            asset=Asset.GOLD,
            title="Fed holds rates steady",
            direction=Direction.BEARISH,
            strength=ImpactStrength.HIGH,
            importance=EconomicImportance.VERY_HIGH,
            published_at=base_time + timedelta(hours=2),  # Outside 30-min window
            relevance=0.8,
            persistence=0.5,
            source_quality="TIER_2_MAJOR",
            category=NewsCategory.CENTRAL_BANK,
        ),
    ]

    clustered = cluster_signals(signals, window_hours=0.5)
    assert len(clustered) == 2  # Two separate clusters due to time window

# --------------------------------------------------------------------------
# Direction-split representative selection (bug fix 2026-10-08)
# --------------------------------------------------------------------------
# The old selector ranked cluster members by ``final_weight``, which is
# 0.0 for every signal at clustering time (it is assigned later by
# ``_aggregate``), so the survivor was picked by arbitrary tuple order
# and a cluster whose members disagreed in direction collapsed to one
# side. These tests pin the fix: clusters split by direction, and the
# representative is the member with the highest ``_signal_weight``.
#
# NOTE: these tests stamp headlines at the REAL wall clock (not the
# fixed ``NOW`` above) because ``cluster_signals`` selects the
# representative with its own wall-clock "now" (``_scoring_now``).
# Fresh timestamps keep that internal selection meaningful.

def test_hawkish_and_dovish_same_event_both_survive():
    """A hawkish and a dovish headline about ONE event are two readings.

    The titles differ by a single word ("hike" vs "cut"), so the
    scoring-time similarity puts them in ONE title cluster inside the
    30-minute window: jaccard 7/9 = 0.778 plus 0.15 for the shared
    "fed" entity = 0.928 >= 0.72. Before the fix the cluster collapsed
    to a single signal and one side's vote silently vanished; both
    readings must survive now.
    """
    from datetime import timedelta

    from app.analysis.event_clustering import cluster_signals
    from app.analysis.sentiment_engine import WeightedSignal
    from app.models.enums import (
        Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
    )
    from app.utils import timeutils as T

    base = T.now_utc().replace(microsecond=0)
    common = dict(
        asset=Asset.GOLD,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.VERY_HIGH,
        relevance=0.90,
        persistence=0.90,
        source_quality="TIER_2_MAJOR",
        category=NewsCategory.CENTRAL_BANK,
    )
    hawkish = WeightedSignal(
        title="Fed minutes show policymakers back another rate hike",
        direction=Direction.BEARISH,
        published_at=base,
        **common,
    )
    dovish = WeightedSignal(
        title="Fed minutes show policymakers back another rate cut",
        direction=Direction.BULLISH,
        published_at=base + timedelta(minutes=10),
        **common,
    )

    clustered = cluster_signals([hawkish, dovish], window_hours=0.5)

    assert len(clustered) == 2, (
        f"hawkish and dovish readings collapsed to {len(clustered)} signal(s)"
    )
    assert {s.direction for s in clustered} == {
        Direction.BEARISH, Direction.BULLISH}
    titles = {s.title for s in clustered}
    assert "Fed minutes show policymakers back another rate hike" in titles
    assert "Fed minutes show policymakers back another rate cut" in titles


def test_eight_same_direction_duplicates_score_like_one():
    """8 same-direction duplicates score the same as 1, boost alone excepted.

    The 8 articles span the 4 source tiers, so the cluster counts 4
    independent sources and its weight is multiplied by
    corroboration_multiplier(4) = 1.1562 (diminishing returns, capped
    at 1.30) - and by nothing else. Every duplicate points the same
    way, so the signed score (a ratio of the two sides) is unchanged:
    score_8 == score_1 exactly (tolerance 0.0). The bear-side TOTAL may
    differ only by that capped boost: tolerance abs=1e-6.
    """
    from datetime import timedelta

    from app.analysis.event_clustering import (
        cluster_signals, corroboration_multiplier,
    )
    from app.analysis.sentiment_engine import (
        WeightedSignal, _aggregate, clear_bias_history, compute,
    )
    from app.models.enums import (
        Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
    )
    from app.utils import timeutils as T

    base = T.now_utc().replace(microsecond=0)
    now = base
    tiers = [
        "TIER_1_OFFICIAL", "TIER_2_MAJOR", "TIER_2_MAJOR",
        "TIER_2_MAJOR", "TIER_2_MAJOR", "TIER_3_CRYPTO",
        "TIER_4_OTHER", "TIER_4_OTHER",
    ]
    duplicates = [
        WeightedSignal(
            asset=Asset.GOLD,
            title=(f"Fed minutes show policymakers see another rate "
                   f"hike by year end {i}"),
            direction=Direction.BEARISH,
            strength=ImpactStrength.VERY_HIGH,
            importance=EconomicImportance.VERY_HIGH,
            published_at=base + timedelta(minutes=i),
            relevance=0.80,
            persistence=0.90,
            source_quality=tier,
            category=NewsCategory.CENTRAL_BANK,
        )
        for i, tier in enumerate(tiers)
    ]
    # The cluster representative is the TIER_1_OFFICIAL member (quality
    # multiplier 1.00 and TIER_1 event tier - the heaviest member), so
    # the single-article baseline is that same article on its own.
    single = duplicates[0]

    clustered = cluster_signals(list(duplicates), window_hours=0.5)
    assert len(clustered) == 1

    clear_bias_history()
    agg_one = _aggregate([single], now)
    res_one = compute([single], Asset.GOLD, now=now)
    clear_bias_history()
    agg_eight = _aggregate(clustered, now)
    res_eight = compute(clustered, Asset.GOLD, now=now)

    # Same direction on every member: the score is a ratio of the two
    # sides, so scaling the bear side by the boost cannot move it.
    assert res_eight.score == res_one.score
    assert res_eight.score == -100.0

    # The bear total grows by exactly the capped corroboration boost
    # for 4 independent sources (1.1562) and by nothing else.
    expected_boost = corroboration_multiplier(4)
    assert clustered[0].corroboration == pytest.approx(expected_boost, abs=1e-9)
    assert agg_eight["bear"] == pytest.approx(
        agg_one["bear"] * expected_boost, abs=1e-6)
    assert agg_eight["bull"] == agg_one["bull"] == 0.0


def test_representative_is_the_highest_signal_weight_member():
    """The surviving signal is the member with the highest _signal_weight.

    Three same-direction near-duplicates (identical token sets after
    number stripping) with deliberately different weights: the
    TIER_1_OFFICIAL / high-relevance article outweighs the TIER_4 one
    ~10x and the TIER_3 one ~40x. The heaviest article is also the
    NEWEST, so picking the first (or any positional) member would
    survive a different headline. The cluster must keep the heaviest.
    """
    from datetime import timedelta

    from app.analysis.event_clustering import cluster_signals
    from app.analysis.sentiment_engine import WeightedSignal, _signal_weight
    from app.models.enums import (
        Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
    )
    from app.utils import timeutils as T

    base = T.now_utc().replace(microsecond=0)
    now = base

    light = WeightedSignal(
        asset=Asset.GOLD,
        title="Fed minutes show policymakers back another rate hike in 2027",
        direction=Direction.BEARISH,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.HIGH,
        published_at=base,
        relevance=0.40,
        persistence=0.50,
        source_quality="TIER_4_OTHER",
        category=NewsCategory.CENTRAL_BANK,
    )
    lighter = WeightedSignal(
        asset=Asset.GOLD,
        title="Fed minutes show policymakers back another rate hike in 2028",
        direction=Direction.BEARISH,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.LOW,
        published_at=base + timedelta(minutes=5),
        relevance=0.30,
        persistence=0.50,
        source_quality="TIER_3_CRYPTO",
        category=NewsCategory.CENTRAL_BANK,
    )
    heavy = WeightedSignal(
        asset=Asset.GOLD,
        title="Fed minutes show policymakers back another rate hike in 2026",
        direction=Direction.BEARISH,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.VERY_HIGH,
        published_at=base + timedelta(minutes=10),
        relevance=0.95,
        persistence=0.90,
        source_quality="TIER_1_OFFICIAL",
        category=NewsCategory.CENTRAL_BANK,
    )

    # Sanity: the weight ordering is unambiguous at the scoring moment.
    members = (light, lighter, heavy)
    weights = {s.title: _signal_weight(s, now) for s in members}
    assert weights[heavy.title] > weights[light.title] > weights[lighter.title]

    # Deliberately unordered input: selection must not depend on it.
    clustered = cluster_signals([light, heavy, lighter], window_hours=0.5)

    assert len(clustered) == 1
    expected = max(members, key=lambda s: _signal_weight(s, now))
    assert clustered[0].title == expected.title == heavy.title
    # Guards against positional (first-member) selection ...
    assert clustered[0].title != light.title
    # ... and against picking the lowest-weight member, which the old
    # final_weight-based selector could do arbitrarily.
    assert clustered[0].title != lighter.title


# --------------------------------------------------------------------------
# Event-anchored clustering (second rule, 2026-10-08)
# --------------------------------------------------------------------------
# Rule 2 joins two signals when ALL of these hold:
#   * the same direction,
#   * inside the 30-minute window (the same window as the title rule -
#     the anchor never extends it),
#   * the same specific event key from event_anchor() (Fed/FOMC
#     minutes, a named release such as NFP/CPI, or a central-bank
#     decision).
# The 0.72 title-similarity rule is unchanged and still fires first.

from app.models.enums import Direction  # module-level: used by the
# real-headline fixture below.

# The 7 real 2026-10-07 Fed/FOMC-minutes signals from the
# 17:30-19:30 UTC replay (exact titles, timestamps and XAUUSD
# directions as stored in the database).
FED_MINUTES_SET = [
    ("US Dollar Index Price Forecast: 101.80 remains key support zone "
     "ahead of FOMC minutes - FXStreet",
     "2026-10-07T18:00:00", Direction.MIXED),
    ("Fed's minutes show no appetite for a series of interest-rate hikes",
     "2026-10-07T18:10:13", Direction.BEARISH),
    ("Fed officials see another hike coming, but no sign as to when, "
     "minutes show", "2026-10-07T18:42:36", Direction.BEARISH),
    ("FOMC minutes show the Fed ready to hike again in 2026 as AI "
     "buildout replaces tariffs as main driver of core prices - Kitco",
     "2026-10-07T18:45:37", Direction.MIXED),
    ("Fed policymakers divided over rate-hike logic in September, "
     "minutes show - Kitco", "2026-10-07T18:54:52", Direction.BEARISH),
    ("Fed Minutes: Officials saw inflation risks worsening before "
     "September hike - FXStreet", "2026-10-07T18:58:01", Direction.MIXED),
    ("Gold slides below $4,100 as FOMC Minutes signal further Fed "
     "hikes - FXStreet", "2026-10-07T19:29:05", Direction.BEARISH),
]


def _mk_signal(title, published_iso, direction):
    """A weighted signal with uniform, realistic scoring fields."""
    from app.analysis.sentiment_engine import WeightedSignal
    from app.models.enums import (
        Asset, EconomicImportance, ImpactStrength, NewsCategory,
    )
    return WeightedSignal(
        asset=Asset.GOLD,
        title=title,
        direction=direction,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.VERY_HIGH,
        published_at=datetime.fromisoformat(published_iso),
        relevance=0.90,
        persistence=0.90,
        source_quality="TIER_2_MAJOR",
        category=NewsCategory.CENTRAL_BANK,
    )


def test_event_anchor_is_specific_and_conservative():
    from app.analysis.event_clustering import event_anchor

    assert (event_anchor("Fed minutes show policymakers back "
                         "another rate hike") == "fed_minutes")
    assert (event_anchor("FOMC Minutes: Officials saw inflation "
                         "risks worsening") == "fed_minutes")
    assert (event_anchor("US Nonfarm Payrolls Rise 250K in September")
            == "release:nfp")
    assert (event_anchor("Unemployment Rate Falls to 3.8 Percent")
            == "release:unemployment")
    assert (event_anchor("US CPI Rises 0.3 Percent in September")
            == "release:cpi")
    assert event_anchor("Fed holds rates steady") == "fed_decision"
    assert event_anchor("ECB holds rates steady") == "ecb_decision"
    # Loose topics never anchor on their own - only specific events.
    assert event_anchor("Gold slides on profit taking") is None
    assert event_anchor("China factory output beats forecasts") is None
    assert (event_anchor("BOJ: many regions reported consumption "
                         "stayed resilient") is None)
    # A headline covering several releases anchors on its PRIMARY
    # release only (priority order), so it can never bridge a pure
    # payrolls headline to a pure unemployment headline.
    assert (event_anchor("Nonfarm Payrolls Rise as Unemployment "
                         "Holds at 4.1 Percent") == "release:nfp")


def test_real_fed_minutes_headlines_collapse_by_event():
    """(a) The 7 real Oct-7 Fed-minutes signals: 7 singletons -> 5.

    The event-anchored rule merges exactly the two pairs that share
    direction + event key + the 30-minute window:
      * 18:42:36 + 18:54:52 (both BEARISH, gap 12m16s, title
        similarity 0.40 - far below 0.72)
      * 18:45:37 + 18:58:01 (both MIXED, gap 12m24s, title
        similarity 0.28)

    NOT VERIFIED against a 1-2 cluster outcome: the seven headlines
    span 89 minutes and every remaining same-direction bridge is
    outside the 30-minute window - 18:10:13->18:42:36 is 32m23s,
    18:54:52->19:29:05 is 34m13s, 18:00:00->18:45:37 is 45m37s -
    and 18:58:01->19:29:05 (31m04s) also disagrees in direction.
    Collapsing this set to 1-2 clusters needs a ~46-minute event
    window; that is a threshold decision and was NOT made here.
    """
    from app.analysis.event_clustering import cluster_signals

    signals = [_mk_signal(t, ts, d) for t, ts, d in FED_MINUTES_SET]
    clustered = cluster_signals(signals, window_hours=0.5)

    assert len(clustered) == 5

    survivors = {s.title for s in clustered}
    # The three headlines with no same-direction neighbour inside
    # 30 minutes always survive untouched.
    for idx in (0, 1, 6):
        assert FED_MINUTES_SET[idx][0] in survivors
    # Each merged pair contributes exactly ONE representative.
    pair_bearish = {FED_MINUTES_SET[2][0], FED_MINUTES_SET[4][0]}
    pair_mixed = {FED_MINUTES_SET[3][0], FED_MINUTES_SET[5][0]}
    assert len(survivors & pair_bearish) == 1
    assert len(survivors & pair_mixed) == 1
    assert len(survivors) == 5

    # The two merges are legal under all three rule conditions ...
    from app.analysis.event_clustering import event_anchor
    for i, j in ((2, 4), (3, 5)):
        title_i, ts_i, dir_i = FED_MINUTES_SET[i]
        title_j, ts_j, dir_j = FED_MINUTES_SET[j]
        assert dir_i == dir_j
        assert event_anchor(title_i) == event_anchor(title_j) == "fed_minutes"
        gap = abs((datetime.fromisoformat(ts_j)
                   - datetime.fromisoformat(ts_i)).total_seconds())
        assert gap <= 1800
    # ... and the bridges that stayed separate are all outside it.
    for i, j in ((1, 2), (4, 6), (0, 3), (5, 6)):
        ts_i = FED_MINUTES_SET[i][1]
        ts_j = FED_MINUTES_SET[j][1]
        gap = abs((datetime.fromisoformat(ts_j)
                   - datetime.fromisoformat(ts_i)).total_seconds())
        assert gap > 1800


def test_nfp_and_unemployment_headlines_do_not_merge():
    """(b) NFP and the unemployment rate are different releases.

    Both point BEARISH five minutes apart, so only the event key
    keeps them apart: release:nfp vs release:unemployment. Title
    similarity is 0 (no shared content tokens, no shared entities).
    """
    from app.analysis.event_clustering import cluster_signals, event_anchor

    nfp = _mk_signal("US Nonfarm Payrolls Rise 250K in September",
                     "2026-10-07T12:30:00", Direction.BEARISH)
    unemployment = _mk_signal("Unemployment Rate Falls to 3.8 Percent",
                              "2026-10-07T12:35:00", Direction.BEARISH)
    assert event_anchor(nfp.title) == "release:nfp"
    assert event_anchor(unemployment.title) == "release:unemployment"

    clustered = cluster_signals([nfp, unemployment], window_hours=0.5)
    assert len(clustered) == 2


def test_same_release_different_wording_merges():
    """Two differently worded NFP headlines are ONE release event.

    Title similarity is only 0.63 (jaccard 2/6 + 0.30 for the two
    shared entities), so the 0.72 rule does NOT fire - the event
    anchor does the merge.
    """
    from app.analysis.event_clustering import cluster_signals

    a = _mk_signal("US Nonfarm Payrolls Rise 250K in September",
                   "2026-10-07T12:30:00", Direction.BEARISH)
    b = _mk_signal("Nonfarm payrolls top forecasts at 250K",
                   "2026-10-07T12:38:00", Direction.BEARISH)
    clustered = cluster_signals([a, b], window_hours=0.5)
    assert len(clustered) == 1


def test_event_rule_keeps_hawkish_and_dovish_minutes_apart():
    """(c) Hawkish and dovish minutes readings are two readings.

    Both carry the fed_minutes anchor, and their title similarity
    (jaccard 4/12 = 0.333 + 0.15 shared "fed" = 0.483) is below
    0.72, so ONLY the event rule could join them - and it must not,
    because the rule requires the same direction.
    """
    from app.analysis.event_clustering import cluster_signals, event_anchor

    hawkish = _mk_signal("Fed minutes show policymakers back "
                         "another rate hike",
                         "2026-10-07T18:42:00", Direction.BEARISH)
    dovish = _mk_signal("Fed minutes reveal policymakers leaned "
                        "against rate cut",
                        "2026-10-07T18:50:00", Direction.BULLISH)
    assert (event_anchor(hawkish.title)
            == event_anchor(dovish.title) == "fed_minutes")

    clustered = cluster_signals([hawkish, dovish], window_hours=0.5)
    assert len(clustered) == 2
    assert {s.direction for s in clustered} == {
        Direction.BEARISH, Direction.BULLISH}


def test_same_event_over_30_minutes_apart_stays_separate():
    """(d) The event anchor never extends the 30-minute window."""
    from app.analysis.event_clustering import cluster_signals

    a = _mk_signal("Fed minutes show policymakers back "
                   "another rate hike",
                   "2026-10-07T18:42:00", Direction.BEARISH)
    b = _mk_signal("Fed minutes show policymakers back "
                   "another rate hike",
                   "2026-10-07T19:13:00", Direction.BEARISH)  # +31 min
    clustered = cluster_signals([a, b], window_hours=0.5)
    assert len(clustered) == 2


def test_same_event_exactly_30_minutes_apart_merges():
    """The window boundary is inclusive: 30:00 exactly is inside."""
    from app.analysis.event_clustering import cluster_signals

    a = _mk_signal("Fed minutes show policymakers back "
                   "another rate hike",
                   "2026-10-07T18:42:00", Direction.BEARISH)
    b = _mk_signal("Fed minutes show policymakers back "
                   "another rate hike",
                   "2026-10-07T19:12:00", Direction.BEARISH)  # +30:00
    clustered = cluster_signals([a, b], window_hours=0.5)
    assert len(clustered) == 1


def test_different_event_keys_do_not_merge():
    """A Fed DECISION and Fed MINUTES are different events."""
    from app.analysis.event_clustering import cluster_signals, event_anchor

    decision = _mk_signal("Fed holds rates steady",
                          "2026-10-07T14:00:00", Direction.BEARISH)
    minutes = _mk_signal("Fed minutes show no appetite for hikes",
                         "2026-10-07T14:05:00", Direction.BEARISH)
    assert event_anchor(decision.title) == "fed_decision"
    assert event_anchor(minutes.title) == "fed_minutes"

    clustered = cluster_signals([decision, minutes], window_hours=0.5)
    assert len(clustered) == 2
