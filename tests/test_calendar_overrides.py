"""
FORCED IMPACT, AND THE CONFIDENCE OF A DERIVED COUNTRY.

Two small rules that decide how much a release is worth:

  * ``impact_overrides`` force an event's impact BEFORE any weighting, so a
    FOMC press conference weighs like a rate decision even when the provider
    tagged it Medium.
  * a country derived from the GENERIC fallback list is LOW CONFIDENCE: the
    title named no country, only an indicator, so the attribution is a
    convention rather than a fact. It is marked, not hidden.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.analysis.calendar_config import (
    apply_impact_overrides,
    load_calendar_config,
    override_impact,
)
from app.analysis.economic_impact import CalendarEvent
from app.ingestion import fj_calendar

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _event(title: str, impact: str = "Medium", country: str = "USD",
           actual: float | None = None, forecast: float | None = None
           ) -> CalendarEvent:
    return CalendarEvent(title=title, country=country, when_utc=NOW,
                         impact=impact, forecast=forecast, previous=1.0,
                         actual=actual, provider="financialjuice")


# --------------------------------------------------------------------------
# Forced impact
# --------------------------------------------------------------------------
def test_a_fomc_press_conference_is_forced_to_high():
    cfg = load_calendar_config()
    forced, hits = override_impact(_event("FOMC Press Conference"), cfg)
    assert forced == "High"
    assert hits


def test_a_rate_decision_is_forced_to_high():
    cfg = load_calendar_config()
    for title in ("FOMC Rate Decision", "ECB Rate Decision",
                  "BOJ Policy Rate", "Bank of England Rate Decision"):
        forced, hits = override_impact(_event(title, country="EUR"), cfg)
        assert forced == "High", title
        assert hits


def test_the_named_speakers_are_forced_to_high():
    cfg = load_calendar_config()
    for title in ("Fed's Powell Speaks", "ECB's Lagarde Speaks",
                  "BOJ's Ueda Speaks", "BoE's Gov. Bailey Speaks"):
        forced, _hits = override_impact(_event(title, country="EUR"), cfg)
        assert forced == "High", title


def test_the_big_numbers_are_forced_to_high():
    cfg = load_calendar_config()
    for title in ("Core CPI m/m", "CPI y/y", "Nonfarm Payrolls", "PCE Price Index"):
        forced, _hits = override_impact(_event(title), cfg)
        assert forced == "High", title


def test_a_survey_is_not_a_print_and_stays_medium():
    """"CPI Expectations" is a survey, not a release."""
    cfg = load_calendar_config()
    forced, _hits = override_impact(_event("ECB CPI Expectations"), cfg)
    assert forced == "Medium"


def test_an_event_outside_the_rules_keeps_its_impact():
    cfg = load_calendar_config()
    forced, hits = override_impact(_event("Retail Sales m/m", impact="Medium"),
                                   cfg)
    assert forced == "Medium"
    assert hits == ()


def test_an_event_already_high_is_left_alone():
    cfg = load_calendar_config()
    forced, hits = override_impact(_event("Core CPI m/m", impact="High"), cfg)
    assert forced == "High"
    assert hits


def test_the_override_returns_a_copy_and_leaves_the_original_alone():
    cfg = load_calendar_config()
    original = _event("FOMC Press Conference", impact="Medium")
    events, changes = apply_impact_overrides([original], cfg)
    assert events[0].impact == "High"
    assert original.impact == "Medium"      # untouched
    assert len(changes) == 1
    changed, was, now, hits = changes[0]
    assert (was, now) == ("Medium", "High")
    assert hits


def test_the_override_is_logged_when_it_changes_an_event(caplog):
    from app.ingestion.economic_calendar import apply_impact_overrides_logged

    with caplog.at_level(logging.INFO, logger="app.ingestion.economic_calendar"):
        events = apply_impact_overrides_logged(
            [_event("FOMC Press Conference", impact="Medium")])
    assert events[0].impact == "High"
    text = caplog.text
    assert "impact override" in text
    assert "FOMC Press Conference" in text
    assert "Medium -> High" in text


def test_the_override_reaches_the_weight():
    """The forced impact must change the SCORE, not just the label."""
    from app.analysis.regional_bias import compute_regional_bias
    from app.models.enums import Asset

    common = dict(actual=3.5, forecast=3.2)

    def _contribution(title: str, impact: str) -> float:
        result = compute_regional_bias(
            [_event(title, impact=impact, **common)], now=NOW)
        return abs(sum(c.contribution
                       for c in result.asset(Asset.GOLD).contributions))

    # A release that is NOT overridden: High weighs more than Medium.
    assert _contribution("Retail Sales m/m", "High") > \
        _contribution("Retail Sales m/m", "Medium")

    # An OVERRIDDEN release weighs exactly as much as it would at High.
    assert abs(_contribution("Core CPI y/y", "Medium")
               - _contribution("Core CPI y/y", "High")) < 1e-9


# --------------------------------------------------------------------------
# The confidence of a derived country
# --------------------------------------------------------------------------
def test_a_generic_derivation_is_low_confidence():
    code, needle, is_generic = fj_calendar.guess_country_detail(
        "University Michigan 1 Yr Inflation Final")
    assert code == "USD"
    assert is_generic is True
    assert needle == "inflation"


def test_an_explicit_derivation_is_high_confidence():
    code, _needle, is_generic = fj_calendar.guess_country_detail(
        "Eurozone Composite PMI Flash")
    assert code == "EUR"
    assert is_generic is False


def test_the_country_answer_is_unchanged_by_the_confidence_work():
    """The confidence reporting must not change any derivation."""
    assert fj_calendar.guess_country("Swiss CPI YoY") == "CHF"
    assert fj_calendar.guess_country("UK Services CPI YoY") == "GBP"
    assert fj_calendar.guess_country("Japanese Mfg PMI Flash") == "JPY"
    assert fj_calendar.guess_country("US S&P Composite PMI Flash") == "USD"
    assert fj_calendar.guess_country("Zebra Widget Output") == ""


def test_a_low_confidence_derivation_is_logged(caplog):
    from app.ingestion.fj_calendar import parse_rows

    with caplog.at_level(logging.INFO, logger="app.ingestion.fj_calendar"):
        events, _skipped = parse_rows(
            [{"title": "University Michigan 1 Yr Inflation Final", "time": "14:00",
              "impact": "2", "date": NOW.date()}], reference=NOW)
    assert len(events) == 1
    assert events[0].country == "USD"
    assert "LOW-CONFIDENCE" in caplog.text
    assert "generic fallback" in caplog.text


def test_an_explicit_derivation_is_not_logged_as_low_confidence(caplog):
    from app.ingestion.fj_calendar import parse_rows

    with caplog.at_level(logging.INFO, logger="app.ingestion.fj_calendar"):
        parse_rows(
            [{"title": "Eurozone Composite PMI Flash", "time": "14:00",
              "impact": "2", "date": NOW.date()}], reference=NOW)
    assert "LOW-CONFIDENCE" not in caplog.text


def test_the_audit_tool_marks_the_low_confidence_rows():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "tools"
              / "audit_calendar.py").read_text(encoding="utf-8")
    assert "LOW-CONFIDENCE" in source
    assert "guess_country_detail" in source
