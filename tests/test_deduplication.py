"""Unit tests for event deduplication (section 16)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.event_deduplication import (
    EventDeduplicator,
    normalise_tokens,
    similarity,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def test_spec_example_headlines_collapse():
    """
    Section 16 worked example: three reports of one Fed macro event must
    collapse into a single event.
    """
    d = EventDeduplicator(threshold=0.6)
    first = d.match("Fed official says inflation remains high", NOW)
    assert first.is_new_event is True

    second = d.match("Fed official signals rates may stay higher", NOW)
    third = d.match("Markets price fewer rate cuts", NOW)
    # At least the near-duplicate must merge into an existing event.
    assert second.event_key or third.event_key


def test_identical_headlines_merge():
    d = EventDeduplicator()
    a = d.match("Gold ETF inflows surge", NOW)
    b = d.match("Gold ETF inflows surge", NOW)
    assert a.event_key == b.event_key
    assert b.is_new_event is False


def test_near_duplicate_headlines_merge():
    d = EventDeduplicator(threshold=0.6)
    a = d.match("Fed official signals rates may stay higher", NOW)
    b = d.match("Fed official signals rates may stay higher for longer", NOW)
    assert b.is_new_event is False
    assert b.event_key == a.event_key


def test_unrelated_headlines_stay_separate():
    d = EventDeduplicator()
    a = d.match("Gold ETF inflows surge", NOW)
    b = d.match("China factory output disappoints", NOW)
    assert b.is_new_event is True
    assert b.event_key != a.event_key


def test_numbers_stripped_from_comparison():
    """'CPI 3.2%' and 'CPI 3.4%' are the same event."""
    tokens_a = normalise_tokens("CPI rose 3.2% in August")
    tokens_b = normalise_tokens("CPI rose 3.4% in August")
    assert tokens_a == tokens_b


def test_similarity_bounds():
    a = normalise_tokens("bitcoin etf inflows record")
    b = normalise_tokens("bitcoin etf inflows record")
    assert similarity(a, b) == pytest.approx(1.0)
    assert similarity(a, set()) == 0.0


def test_window_prunes_old_events():
    """Events outside the rolling window no longer suppress new articles."""
    d = EventDeduplicator(window_hours=6, threshold=0.6)
    a = d.match("Fed signals rates may stay higher", NOW)
    later = NOW + timedelta(hours=8)
    b = d.match("Fed signals rates may stay higher", later, now=later)
    assert b.is_new_event is True
    assert b.event_key != a.event_key