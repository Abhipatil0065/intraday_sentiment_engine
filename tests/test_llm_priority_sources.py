"""Tests for the LLM priority-source reservation (added 2026-10-21).

The per-tick LLM budget is small (2-5 verdicts at the provider's real
latency) and is spent strictly in the order the batch is sorted, so a source
whose headlines sit behind the freshest wire copy never gets a model verdict
at all. These tests pin the behaviour of the reservation that fixes that:

  * a configured source is promoted ahead of fresher non-priority items;
  * the promotion is capped by llm_priority_slots;
  * a priority item older than llm_priority_max_age_hours is NOT promoted;
  * with nothing configured the sort is byte-for-byte the old behaviour.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.ingestion.base import RawArticle
from app.services.pipeline import (
    _llm_priority_indices,
    _priority_sort_articles,
)


def _article(source: str, minutes_ago: int, title: str = "x",
             quality: str = "TIER_2_MAJOR") -> RawArticle:
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return RawArticle(
        guid="", source=source, source_quality=quality,
        # The guid is hashed from the url (or the title), so it must differ
        # per article or every row in a test shares one identity.
        url=f"https://example.test/{title}",
        title=title, body="", summary="", published_raw=stamp.isoformat(),
    ).ensure_guid()


class _Settings:
    """Minimal stand-in for the settings object."""

    def __init__(self, values: dict):
        self._values = values

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(name) from exc


def test_priority_source_is_promoted_ahead_of_fresher_items():
    """A listed source wins a model slot even when it is older."""
    old_tv = _article("TradingView API News", minutes_ago=25, title="tv")
    fresh_wire = _article("Reuters", minutes_ago=1, title="wire")

    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "TradingView API News",
        "llm_priority_slots": 2,
        "llm_priority_max_age_hours": 1.0,
    })):
        ordered = _priority_sort_articles([fresh_wire, old_tv])

    assert ordered[0].title == "tv"
    assert ordered[1].title == "wire"


def test_reservation_is_capped_by_slot_count():
    """Only llm_priority_slots items of a listed source are promoted."""
    tv = [_article("TradingView API News", minutes_ago=30 - i, title=f"tv{i}")
          for i in range(4)]
    wires = [_article("Reuters", minutes_ago=1 + i, title=f"wire{i}")
             for i in range(4)]

    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "TradingView API News",
        "llm_priority_slots": 2,
        "llm_priority_max_age_hours": 1.0,
    })):
        ordered = _priority_sort_articles(wires + tv)

    promoted = [a for a in ordered[:2] if a.source == "TradingView API News"]
    assert len(promoted) == 2
    # The two promoted ones are the FRESHEST of the four (tv0 is the oldest:
    # minutes_ago=30-i, so tv3 is the newest).
    assert {a.title for a in promoted} == {"tv2", "tv3"}


def test_stale_priority_item_is_not_promoted():
    """A priority item older than the age band keeps its normal position."""
    stale_tv = _article("TradingView API News", minutes_ago=180, title="stale")
    fresh_wire = _article("Reuters", minutes_ago=1, title="wire")

    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "TradingView API News",
        "llm_priority_slots": 2,
        "llm_priority_max_age_hours": 1.0,
    })):
        ordered = _priority_sort_articles([stale_tv, fresh_wire])

    assert ordered[0].title == "wire"


def test_no_priority_sources_keeps_the_old_order():
    """With nothing configured the sort is unchanged (regression guard)."""
    older_tier2 = _article("Reuters", minutes_ago=20, title="older")
    newer_tier2 = _article("FXStreet", minutes_ago=5, title="newer")
    tier1 = _article("Fed Press Releases", minutes_ago=60, title="fed",
                     quality="TIER_1_OFFICIAL")

    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "",
        "llm_priority_slots": 2,
        "llm_priority_max_age_hours": 1.0,
    })):
        ordered = _priority_sort_articles([older_tier2, newer_tier2, tier1])

    assert [a.title for a in ordered] == ["fed", "newer", "older"]


def test_indices_helper_is_inert_without_configuration():
    items = [("Reuters", time.time(), "a"), ("TradingView API News", time.time(), "b")]
    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "",
        "llm_priority_slots": 2,
        "llm_priority_max_age_hours": 1.0,
    })):
        assert _llm_priority_indices(items) == set()


def test_indices_helper_ignores_zero_slots():
    items = [("TradingView API News", time.time(), "a")]
    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "TradingView API News",
        "llm_priority_slots": 0,
        "llm_priority_max_age_hours": 1.0,
    })):
        assert _llm_priority_indices(items) == set()


def test_already_stored_priority_item_is_not_promoted():
    """A re-served headline must not eat the reservation.

    TradingView returns the SAME 30 items on every poll, so without this the
    freshest reserved items are always ones the tick skips before the LLM
    phase - the reservation would be spent on nothing.
    """
    stored_tv = _article("TradingView API News", minutes_ago=5, title="stored")
    fresh_tv = _article("TradingView API News", minutes_ago=25, title="fresh")
    wire = _article("Reuters", minutes_ago=1, title="wire")

    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "TradingView API News",
        "llm_priority_slots": 1,
        "llm_priority_max_age_hours": 1.0,
    })):
        ordered = _priority_sort_articles(
            [stored_tv, fresh_tv, wire],
            known_guids={stored_tv.guid},
        )

    assert ordered[0].title == "fresh"


def test_malformed_settings_do_not_raise():
    """A broken settings object must never break a tick."""
    class Broken:
        llm_priority_sources = object()
        llm_priority_slots = "not-a-number"
        llm_priority_max_age_hours = None

    with patch("app.services.pipeline.settings", Broken()):
        assert _llm_priority_indices(
            [("TradingView API News", time.time(), "a")]) == set()
        ordered = _priority_sort_articles(
            [_article("Reuters", minutes_ago=1, title="wire")])
        assert ordered[0].title == "wire"


def test_overnight_uses_the_same_reservation():
    """The morning catch-up pass promotes the same rows the live tick does."""
    from app.services.overnight import _ordered_for_llm

    class Row:
        def __init__(self, source, minutes_ago, title):
            self.source = source
            self.title = title
            self.source_quality = "TIER_2_MAJOR"
            self.published_at_utc = datetime.now(timezone.utc) - timedelta(
                minutes=minutes_ago)

    rows = [Row("Reuters", 1, "wire"), Row("TradingView API News", 25, "tv")]

    with patch("app.services.pipeline.settings", _Settings({
        "llm_priority_sources": "TradingView API News",
        "llm_priority_slots": 2,
        "llm_priority_max_age_hours": 1.0,
    })):
        ordered = _ordered_for_llm(rows)

    assert [r.title for r in ordered] == ["tv", "wire"]
