"""
PHASE 7: A SCHEDULED PRINT IS A RISK, NOT EVIDENCE.

Before this change an UPCOMING calendar row (no ``actual`` yet) was scored
into the cards exactly like a released one, on the CONSENSUS direction, with
FULL recency weight (its timestamp is in the future, and a future timestamp
decays to 1.0). A card could therefore move on a number nobody had published
yet - and, for a growth survey, with the sign inverted.

The rules this file locks in:

  1. An UPCOMING print does NOT reach the card: no bull/bear weight, no
     driver row, no score change. It is still LISTED in the calendar panel
     and it still raises the EVENT RISK note.
  2. The same row WITH an actual DOES reach the card, through the RELEASED
     branch, on the realised surprise.
  3. The old behaviour is one config flag away
     (``calendar_upcoming_contributes``).
  4. A regional Reserve Bank SURVEY ("Kansas City Fed Composite Index") is
     growth data, not a policy event - the "fed " needle must not classify
     it as CENTRAL_BANK, which inverted the sign of the read.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.economic_impact import (
    CalendarEvent,
    classify_event,
    signals_for_event,
    to_calendar_summary,
)
from app.config import settings
from app.models.db_models import Base
from app.models.enums import Asset, Direction, NewsCategory
from app.services.pipeline import IngestStats, _recompute_sentiment

NOW = datetime.now(timezone.utc)


def _event(*, actual=None, when=None):
    return CalendarEvent(
        title="US CPI MoM",
        country="USD",
        when_utc=when or (NOW - timedelta(minutes=10) if actual is not None
                          else NOW + timedelta(hours=2)),
        impact="High",
        forecast=0.3,
        previous=0.2,
        actual=actual,
        provider="financialjuice",
    )


def _btc_signal(event):
    sigs = [s for s in signals_for_event(event) if s.asset is Asset.BITCOIN]
    assert sigs, "the fixture must produce a BTC signal"
    return sigs[0]


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _recompute_with_calendar(signal, SessionLocal):
    """Run the real recompute with one calendar signal patched in."""
    import app.services.pipeline as p

    original = p._calendar_signals
    p._calendar_signals = [signal]
    try:
        stats = IngestStats()
        with SessionLocal() as session:
            signals, _regime = _recompute_sentiment(session, stats)
        return signals
    finally:
        p._calendar_signals = original


# --------------------------------------------------------------------------
# 1. the gate
# --------------------------------------------------------------------------
def test_an_upcoming_print_does_not_reach_the_card(tmp_path):
    """A scheduled print with no actual adds nothing to the card."""
    signal = _btc_signal(_event(actual=None))
    assert signal.mode == "UPCOMING"

    signals = _recompute_with_calendar(signal, _db(tmp_path))

    assert [s for s in signals if "US CPI MoM" in s.title] == [], (
        "an UPCOMING print must not enter the aggregation")


def test_a_released_print_does_reach_the_card(tmp_path):
    """The same row, once the real number is published, scores normally."""
    signal = _btc_signal(_event(actual=0.4))
    assert signal.mode == "RELEASED"

    signals = _recompute_with_calendar(signal, _db(tmp_path))

    scored = [s for s in signals if "US CPI MoM" in s.title]
    assert len(scored) == 1, "a RELEASED print must reach the card"
    assert scored[0].asset is Asset.BITCOIN
    assert scored[0].direction is Direction.BEARISH, (
        "a hot CPI print is bearish for BTC")


def test_the_old_behaviour_is_one_flag_away(tmp_path, monkeypatch):
    """``calendar_upcoming_contributes`` restores consensus-as-evidence."""
    monkeypatch.setattr(settings, "calendar_upcoming_contributes", True)
    signal = _btc_signal(_event(actual=None))

    signals = _recompute_with_calendar(signal, _db(tmp_path))

    assert [s for s in signals if "US CPI MoM" in s.title], (
        "the flag must restore the previous behaviour")


def test_the_flag_defaults_to_off():
    assert settings.calendar_upcoming_contributes is False


# --------------------------------------------------------------------------
# 2. the panel still shows it
# --------------------------------------------------------------------------
def test_the_calendar_panel_still_lists_an_upcoming_print():
    """Visible as a scheduled risk, absent from the score."""
    event = _event(actual=None)
    signal = _btc_signal(event)

    rows = to_calendar_summary([signal], Asset.BITCOIN)

    assert rows, "the upcoming print must still be listed"
    assert rows[0]["mode"] == "UPCOMING"
    assert rows[0]["actual"] is None
    assert "not an outcome" in rows[0]["reason"]


# --------------------------------------------------------------------------
# 3. the session card agrees
# --------------------------------------------------------------------------
def test_the_session_card_ignores_an_upcoming_print():
    from app.analysis.fj_sessions import context_from_calendar

    signal = _btc_signal(_event(actual=None))
    ctx = context_from_calendar([signal])

    assert ctx.calendar == {}, "an upcoming print must not score the session box"
    assert ctx.calendar_count == 0


def test_the_session_card_counts_a_released_print():
    from app.analysis.fj_sessions import context_from_calendar

    signal = _btc_signal(_event(actual=0.4))
    ctx = context_from_calendar([signal])

    assert ctx.calendar_count == 1
    assert Asset.BITCOIN in ctx.calendar


# --------------------------------------------------------------------------
# 4. the regional-Fed survey is not a policy event
# --------------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "Kansas City Fed Composite Index",
    "Kansas City Fed Manufacturing",
    "Philly Fed Manufacturing Survey",
    "Chicago Fed National Activity Index",
    "Richmond Fed Manufacturing",
])
def test_a_regional_fed_survey_is_not_a_central_bank_event(title):
    profile = classify_event(CalendarEvent(
        title=title, country="USD", when_utc=NOW, impact="High"))
    assert profile is not None
    assert profile.category is not NewsCategory.CENTRAL_BANK, (
        f"{title!r} is growth data, not a policy decision")
    assert profile.hot_bias[Asset.BITCOIN] is Direction.MIXED, (
        "growth data has no fixed direction for BTC")


@pytest.mark.parametrize("title", [
    "FOMC Press Conference",
    "Fed Chair Powell Speaks",
    "Fed Interest Rate Decision",
    "Federal Reserve Beige Book",
])
def test_a_genuine_fed_policy_event_is_still_central_bank(title):
    profile = classify_event(CalendarEvent(
        title=title, country="USD", when_utc=NOW, impact="High"))
    assert profile is not None
    assert profile.category is NewsCategory.CENTRAL_BANK


def test_a_released_regional_survey_carries_no_lean():
    """A released Kansas City Fed print is NEUTRAL, not an inverted lean."""
    event = CalendarEvent(
        title="Kansas City Fed Composite Index", country="USD",
        when_utc=NOW - timedelta(minutes=10), impact="High",
        forecast=12.0, previous=14.0, actual=5.0, provider="financialjuice")

    sigs = [s for s in signals_for_event(event) if s.asset is Asset.BITCOIN]

    assert sigs, "the print must still be resolved"
    assert sigs[0].direction is Direction.NEUTRAL
    assert sigs[0].mode == "RELEASED"


# --------------------------------------------------------------------------
# 5. the "fed's" needle gap
# --------------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "Fed's Williams Speaks",
    "Fed’s Williams Speaks",
    "Fed Chair Powell Speaks",
    "Bank of England's Bailey Speaks",
    "Bank of Japan Ueda Speaks",
    "Bundesbank's Schnabel Speaks",
])
def test_a_central_bank_row_written_with_a_possessive_is_classified(title):
    """The "fed " needle needs a space, so "Fed's ..." used to match NOTHING."""
    profile = classify_event(CalendarEvent(
        title=title, country="USD", when_utc=NOW, impact="Medium"))
    assert profile is not None, f"{title!r} must not be classified as unknown"
    assert profile.category is NewsCategory.CENTRAL_BANK


def test_a_scheduled_speaker_is_listed_as_a_catalyst():
    """A row with no numbers at all is a catalyst - visible, never a lean."""
    event = CalendarEvent(
        title="Fed's Williams Speaks", country="USD",
        when_utc=NOW + timedelta(hours=2), impact="Medium",
        forecast=None, previous=None, actual=None, provider="financialjuice")

    sigs = [s for s in signals_for_event(event) if s.asset is Asset.BITCOIN]

    assert sigs, "a scheduled speaker must reach the calendar panel"
    assert sigs[0].direction is Direction.NEUTRAL
    assert sigs[0].mode == "UPCOMING"
    assert sigs[0].surprise is None
    assert "catalyst" in sigs[0].reason

    rows = to_calendar_summary(sigs, Asset.BITCOIN)
    assert rows and rows[0]["title"] == "Fed's Williams Speaks"
    assert rows[0]["mode"] == "UPCOMING"


def test_a_catalyst_never_moves_a_card(tmp_path):
    """A listed catalyst adds no bull/bear weight, released or not."""
    event = CalendarEvent(
        title="Fed's Williams Speaks", country="USD",
        when_utc=NOW + timedelta(hours=2), impact="Medium",
        forecast=None, previous=None, actual=None, provider="financialjuice")
    signal = _btc_signal(event)

    signals = _recompute_with_calendar(signal, _db(tmp_path))

    assert [s for s in signals if "Williams" in s.title] == [], (
        "a catalyst with no numbers must not enter the aggregation")
