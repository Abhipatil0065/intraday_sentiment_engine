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