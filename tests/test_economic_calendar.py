"""
Unit tests for the economic-calendar impact engine.

The governing rule these tests protect: the engine must never invent a print.
Direction comes from a realised surprise when an ``actual`` exists, and from
published consensus only when it does not - never from a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.economic_impact import (
    PROFILES,
    TITLE_RULES,
    CalendarEvent,
    classify_event,
    evaluate,
    importance_from_impact,
    signals_for_event,
    signals_for_events,
    to_calendar_summary,
    to_float,
)
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
)

NOW = datetime.now(timezone.utc)


def _event(**kwargs) -> CalendarEvent:
    base = dict(
        title="CPI y/y",
        country="USD",
        when_utc=NOW + timedelta(hours=6),
        impact="High",
    )
    base.update(kwargs)
    return CalendarEvent(**base)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("3.5%", 3.5), ("-0.2%", -0.2), ("2.4", 2.4),
        ("1.2K", 1200.0), ("250B", 250_000_000_000.0), ("1,200", 1200.0),
        ("", None), (None, None), ("n/a", None),
    ],
)
def test_to_float(raw, expected):
    assert to_float(raw) == expected


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
@pytest.mark.parametrize("title,category", [
    ("CPI y/y", NewsCategory.INFLATION),
    ("Core PCE Price Index m/m", NewsCategory.INFLATION),
    ("Non-Farm Employment Change", NewsCategory.EMPLOYMENT),
    ("Unemployment Rate", NewsCategory.EMPLOYMENT),
    ("FOMC Interest Rate Decision", NewsCategory.CENTRAL_BANK),
    ("Retail Sales m/m", NewsCategory.GDP),
])
def test_known_titles_classify(title, category):
    profile = classify_event(_event(title=title))
    assert profile is not None
    assert profile.category is category


def test_unknown_title_is_not_classified():
    """An unrecognised release must be skipped, never force-fitted."""
    assert classify_event(_event(title="Boat Registration Count")) is None


def test_every_profile_index_is_valid():
    """A TITLE_RULES entry pointing past PROFILES would silently drop."""
    assert all(0 <= idx < len(PROFILES) for _, idx in TITLE_RULES)

# --------------------------------------------------------------------------
# Released prints: direction from the realised surprise
# --------------------------------------------------------------------------
def test_hot_cpi_is_bearish_for_metals_and_crypto():
    event = _event(forecast=3.2, previous=2.9, actual=3.6)
    signals = {s.asset: s for s in signals_for_event(event)}
    for asset in (Asset.GOLD, Asset.SILVER, Asset.BITCOIN):
        assert signals[asset].direction is Direction.BEARISH
        assert signals[asset].mode == "RELEASED"


def test_cool_cpi_is_bullish():
    event = _event(forecast=3.2, previous=2.9, actual=2.7)
    for sig in signals_for_event(event):
        assert sig.direction is Direction.BULLISH


def test_inline_print_is_neutral():
    """A print landing on forecast is not a directional signal."""
    event = _event(forecast=3.2, previous=3.2, actual=3.2)
    for sig in signals_for_event(event):
        assert sig.direction is Direction.NEUTRAL


def test_big_surprise_is_stronger_than_small():
    rank = {ImpactStrength.LOW: 0, ImpactStrength.MEDIUM: 1,
            ImpactStrength.HIGH: 2, ImpactStrength.VERY_HIGH: 3}
    small = evaluate(_event(forecast=3.0, previous=3.0, actual=3.05), Asset.GOLD)
    large = evaluate(_event(forecast=3.0, previous=3.0, actual=4.5), Asset.GOLD)
    assert rank[large.strength] > rank[small.strength]


# --------------------------------------------------------------------------
# Expected prints: consensus only, never presented as an outcome
# --------------------------------------------------------------------------
def test_expected_event_is_labelled_and_not_released():
    event = _event(forecast=5.5, previous=4.75, actual=None)
    signals = signals_for_event(event)
    assert signals
    for sig in signals:
        assert sig.mode == "EXPECTED"
        assert sig.event.is_released is False
        assert "not an outcome" in sig.reason


def test_expected_hike_is_bearish_via_consensus():
    event = _event(title="FOMC Interest Rate Decision",
                   forecast=5.5, previous=4.75, actual=None)
    for sig in signals_for_event(event):
        assert sig.direction is Direction.BEARISH


def test_unchanged_consensus_is_neutral():
    event = _event(forecast=2.0, previous=2.0, actual=None)
    for sig in signals_for_event(event):
        assert sig.direction is Direction.NEUTRAL


def test_no_forecast_and_no_previous_yields_nothing():
    """Nothing to reason from -> no signal, rather than a fabricated one."""
    assert evaluate(_event(forecast=None, previous=None, actual=None),
                    Asset.GOLD) is None


# --------------------------------------------------------------------------
# Relevance gating
# --------------------------------------------------------------------------
def test_zero_relevance_asset_is_skipped():
    """A profile that does not transmit to an asset must emit nothing for it."""
    from app.analysis.economic_impact import EventProfile

    crypto_only = EventProfile(
        category=NewsCategory.CRYPTO_REGULATION,
        relevance={Asset.GOLD: 0.0, Asset.SILVER: 0.0, Asset.BITCOIN: 0.9},
        hot_bias={Asset.GOLD: Direction.MIXED, Asset.SILVER: Direction.MIXED,
                  Asset.BITCOIN: Direction.BULLISH},
        base_importance=EconomicImportance.HIGH,
        transmission="Crypto-only catalyst.",
    )
    event = _event(forecast=2.0, previous=1.0)
    assert evaluate(event, Asset.GOLD, crypto_only) is None
    assert evaluate(event, Asset.BITCOIN, crypto_only) is not None


def test_industrial_demand_is_silver_led_not_gold_led():
    """Factory data moves silver industrial demand far more than gold."""
    event = _event(title="Factory Orders", forecast=2.0, previous=1.0)
    signals = {s.asset: s for s in signals_for_event(event)}
    assert signals[Asset.SILVER].relevance > signals[Asset.GOLD].relevance


def test_every_signal_carries_an_explanation():
    for sig in signals_for_event(_event(forecast=3.2, previous=2.9, actual=3.6)):
        assert sig.reason
        assert sig.transmission


# --------------------------------------------------------------------------
# Importance
# --------------------------------------------------------------------------
def _inflation_profile():
    return next(p for p in PROFILES if p.category is NewsCategory.INFLATION)


def test_high_impact_cpi_is_very_high_importance():
    assert importance_from_impact("High", _inflation_profile()) is (
        EconomicImportance.VERY_HIGH)


def test_low_impact_never_exceeds_ceiling():
    """A LOW provider tag must not promote a print to the class ceiling."""
    assert importance_from_impact("Low", _inflation_profile()) is not (
        EconomicImportance.VERY_HIGH)


# --------------------------------------------------------------------------
# Horizon + presentation
# --------------------------------------------------------------------------
def test_stale_events_are_excluded():
    old = _event(when_utc=NOW - timedelta(days=4), forecast=3.2, previous=2.9)
    assert signals_for_events([old]) == []


def test_events_beyond_horizon_are_excluded():
    far = _event(when_utc=NOW + timedelta(hours=200), forecast=3.2, previous=2.9)
    assert signals_for_events([far], horizon_hours=48) == []


def test_summary_puts_released_before_expected():
    released = _event(title="CPI y/y", forecast=3.0, previous=2.9, actual=3.6)
    expected = _event(title="FOMC Interest Rate Decision",
                      forecast=5.5, previous=4.75, actual=None)
    rows = to_calendar_summary(signals_for_events([released, expected]),
                               Asset.GOLD)
    assert rows
    assert rows[0]["mode"] == "RELEASED"


def test_event_key_is_stable():
    assert _event().event_key == _event().event_key
