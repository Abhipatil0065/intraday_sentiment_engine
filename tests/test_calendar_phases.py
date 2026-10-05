"""Phase 3: per-indicator polarity, core weighting, and MoM/YoY handling."""
from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.economic_impact import (
    CalendarEvent,
    _frequency_of,
    dedupe_frequency_variants,
    evaluate,
    indicator_polarity,
    is_core_release,
    release_family,
    signals_for_event,
)
from app.models.enums import Asset, Direction

T0 = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)


def ev(title, forecast=None, previous=None, actual=None,
       country="USD", impact="High", when=T0):
    return CalendarEvent(title=title, country=country, when_utc=when,
                         impact=impact, forecast=forecast, previous=previous,
                         actual=actual)


# --------------------------------------------------------------------------
# Polarity
# --------------------------------------------------------------------------
def test_payrolls_beat_is_bearish_gold():
    """More jobs than expected = hawkish = higher yields = weaker gold."""
    sig = evaluate(ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0), Asset.GOLD)
    assert sig.direction is Direction.BEARISH


def test_rising_unemployment_is_bullish_gold():
    """
    The sign error this phase exists to fix. A HIGHER unemployment rate is a
    WEAKER economy, so it is bullish gold - but "above forecast = hot" pointed
    gold the wrong way, on the prints carrying the most labour-market
    information.
    """
    assert indicator_polarity(ev("US Unemployment Rate")) == -1
    sig = evaluate(ev("US Unemployment Rate", 4.4, 4.3, 4.6), Asset.GOLD)
    assert sig.direction is Direction.BULLISH


def test_rising_jobless_claims_are_bullish_gold():
    """More claiming = weaker labour market = dovish = bullish gold."""
    assert indicator_polarity(ev("US Initial Jobless Claims")) == -1
    sig = evaluate(ev("US Initial Jobless Claims", 250.0, 240.0, 260.0),
                   Asset.GOLD)
    assert sig.direction is Direction.BULLISH


def test_falling_unemployment_is_bearish_gold():
    """The other half of the rule, so this is a real inversion not a flip."""
    sig = evaluate(ev("US Unemployment Rate", 4.4, 4.6, 4.2), Asset.GOLD)
    assert sig.direction is Direction.BEARISH


def test_an_unrecognised_indicator_keeps_the_ordinary_reading():
    """Polarity is driven by configuration, never by guesswork."""
    assert indicator_polarity(ev("US Some Unheard-Of Indicator")) == 1


def test_polarity_can_be_turned_off(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "calendar_polarity_enabled", False)
    assert indicator_polarity(ev("US Unemployment Rate")) == 1


def test_a_longer_inverse_keyword_wins_over_a_shorter_one():
    """Both needles mean the same thing; the longer must be found first."""
    assert indicator_polarity(ev("US Initial Jobless Claims")) == -1
    assert indicator_polarity(ev("US Jobless Claims")) == -1


# --------------------------------------------------------------------------
# Core vs headline
# --------------------------------------------------------------------------
@pytest.mark.parametrize("title,expected", [
    ("US Core CPI MoM", True),
    ("Core PCE Price Index", True),
    ("CPI Excl. Food & Energy", True),
    ("US CPI MoM", False),
    ("US CPI YoY", False),
])
def test_core_releases_are_identified(title, expected):
    assert is_core_release(ev(title)) is expected


def test_a_core_release_carries_more_relevance_than_the_headline():
    core = evaluate(ev("US Core CPI MoM", 0.2, 0.2, 0.3), Asset.GOLD)
    headline = evaluate(ev("US CPI MoM", 0.2, 0.2, 0.3), Asset.GOLD)
    assert core.relevance > headline.relevance


def test_the_core_bonus_is_bounded_at_one():
    """Relevance is a 0..1 factor; the bonus must never push it past 1."""
    sig = evaluate(ev("US Core CPI MoM", 0.2, 0.2, 0.3), Asset.GOLD)
    assert sig.relevance <= 1.0


# --------------------------------------------------------------------------
# m/m vs y/y
# --------------------------------------------------------------------------
def test_mom_and_yoy_reduce_to_one_release_family():
    assert release_family(ev("US CPI MoM")) == release_family(ev("US CPI YoY"))


@pytest.mark.parametrize("title,expected", [
    ("US CPI MoM", "mom"), ("US CPI m/m", "mom"),
    ("US CPI YoY", "yoy"), ("US CPI y/y", "yoy"),
    ("US Nonfarm Payrolls", ""),
])
def test_the_frequency_variant_is_recognised(title, expected):
    assert _frequency_of(title) == expected


def test_one_release_is_not_counted_twice():
    """
    A single CPI print publishes both a monthly and a yearly figure. Counting
    both would enter the same macro moment twice and double the implied move.
    """
    mom = signals_for_event(ev("US CPI MoM", 0.3, 0.2, 0.4))
    yoy = signals_for_event(ev("US CPI YoY", 2.9, 2.8, 3.1))
    combined = dedupe_frequency_variants(mom + yoy)
    gold = [s for s in combined if s.asset is Asset.GOLD]
    assert len(gold) == 1, "one CPI release must yield one gold signal"


def test_core_and_headline_are_separate_releases():
    """
    Core CPI and headline CPI are DIFFERENT measures published together, so
    they must not be collapsed - that is what the core BONUS is for, not a
    dedupe. Collapsing them would discard one of the two prints.
    """
    headline = signals_for_event(ev("US CPI MoM", 0.3, 0.2, 0.4))
    core = signals_for_event(ev("US Core CPI MoM", 0.3, 0.2, 0.4))
    kept = dedupe_frequency_variants(headline + core)
    gold = [s for s in kept if s.asset is Asset.GOLD]
    assert len(gold) == 2
    assert sorted(is_core_release(s.event) for s in gold) == [False, True]


def test_two_different_releases_are_not_collapsed():
    """CPI and PPI are separate prints and must both survive."""
    cpi = signals_for_event(ev("US CPI MoM", 0.3, 0.2, 0.4))
    ppi = signals_for_event(ev("US PPI MoM", 0.2, 0.1, 0.3))
    kept = dedupe_frequency_variants(cpi + ppi)
    assert len([s for s in kept if s.asset is Asset.GOLD]) == 2


def test_the_same_release_on_a_different_day_is_not_collapsed():
    cpi = signals_for_event(ev("US CPI MoM", 0.3, 0.2, 0.4))
    yoy = signals_for_event(ev("US CPI YoY", 2.9, 2.8, 3.1,
                              when=T0 + timedelta(days=30)))
    kept = dedupe_frequency_variants(cpi + yoy)
    assert len([s for s in kept if s.asset is Asset.GOLD]) == 2