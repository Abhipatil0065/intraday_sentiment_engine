"""
REGION WHITELIST FOR THE ECONOMIC CALENDAR.

The whitelist decides which scheduled releases may reach the sentiment engine.
It is two-stage and strict:

  1. the event's country/currency must belong to a whitelisted region;
  2. the event title must match that region's event list.

Both stages are covered here, together with the fail-safe rule: a filter that
cannot be applied must leave the previous behaviour untouched rather than
silently change what a published verdict is made of.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.calendar_config import (
    CalendarConfigError,
    classify_region,
    filter_events,
    load_calendar_config,
    normalise_title,
    title_matches,
)
from app.analysis.economic_impact import CalendarEvent

WHEN = datetime(2026, 10, 22, 12, 30, tzinfo=timezone.utc)


def _event(title: str, country: str, impact: str = "High") -> CalendarEvent:
    return CalendarEvent(title=title, country=country, when_utc=WHEN,
                         impact=impact, forecast=1.0, previous=1.0,
                         actual=1.0, provider="financialjuice")


# --------------------------------------------------------------------------
# The config file itself
# --------------------------------------------------------------------------
def test_config_declares_the_five_whitelisted_regions():
    cfg = load_calendar_config()
    codes = {r.code for r in cfg.regions}
    assert codes == {"USA", "EUR", "CNY", "JPY", "GBP"}


def test_region_weights_are_the_configured_defaults():
    cfg = load_calendar_config()
    weights = {r.code: r.weight for r in cfg.regions}
    assert weights["USA"] == 1.0
    assert weights["EUR"] == 0.6
    assert weights["CNY"] == 0.5
    assert weights["JPY"] == 0.6
    assert weights["GBP"] == 0.4


def test_region_weights_are_editable_in_the_config_file(monkeypatch, tmp_path):
    """A region weight is data: the file decides it, not the code."""
    import json

    from app.analysis import calendar_config as cc

    cfg = load_calendar_config()
    raw = json.loads(cc.CONFIG_FILE.read_text(encoding="utf-8"))
    for region in raw["regions"]:
        if region["code"] == "USA":
            region["weight"] = 2.5
    target = tmp_path / "calendar_bias.json"
    target.write_text(json.dumps(raw), encoding="utf-8")

    monkeypatch.setattr(cc, "CONFIG_FILE", target)
    cc.clear_config_cache()
    try:
        assert cc.load_calendar_config().region_by_code("USA").weight == 2.5
    finally:
        cc.clear_config_cache()


# --------------------------------------------------------------------------
# Stage 1: country / currency
# --------------------------------------------------------------------------
def test_country_outside_the_whitelist_is_dropped():
    kept, dropped = filter_events([_event("CPI y/y", "CHF")])
    assert kept == []
    assert len(dropped) == 1
    assert "not in the whitelist" in dropped[0][1]


def test_event_with_no_country_is_dropped():
    kept, dropped = filter_events([_event("CPI y/y", "")])
    assert kept == []
    assert dropped


def test_country_alias_resolves_to_the_region():
    """FJ renders no country column, so a few spellings must still resolve."""
    cfg = load_calendar_config()
    assert cfg.region_for_country("USD").code == "USA"
    assert cfg.region_for_country("US").code == "USA"
    assert cfg.region_for_country("eurozone").code == "EUR"
    assert cfg.region_for_country("UK").code == "GBP"
    assert cfg.region_for_country("JPY").code == "JPY"
    assert cfg.region_for_country("CNY").code == "CNY"


# --------------------------------------------------------------------------
# Stage 2: the title keyword list
# --------------------------------------------------------------------------
def test_us_event_on_the_list_is_kept():
    kept, dropped = filter_events([_event("CPI y/y", "USD")])
    assert len(kept) == 1
    assert dropped == []


def test_us_event_off_the_list_is_dropped():
    """An unlisted US release is not part of the whitelist."""
    kept, dropped = filter_events([_event("Housing Starts", "USD")])
    assert kept == []
    assert "event list" in dropped[0][1]


def test_keyword_match_ignores_case_and_possessives():
    assert title_matches(("fed",), "Fed's Williams Speaks")
    assert title_matches(("fed",), "FED SPEAK")
    assert not title_matches(("fed",), "Feeding frenzy")


def test_core_cpi_matches_the_cpi_keyword():
    """A longer title must still match its family keyword."""
    kept, _ = filter_events([_event("Core CPI m/m", "USD")])
    assert len(kept) == 1


def test_a_region_can_accept_every_one_of_its_events():
    """require_keyword=false switches the title stage off for that region."""
    import dataclasses

    from app.analysis import calendar_config as cc

    cfg = load_calendar_config()
    usa = next(r for r in cfg.regions if r.code == "USA")
    relaxed = dataclasses.replace(usa, require_keyword=False)
    patched = dataclasses.replace(cfg, regions=(relaxed,) + tuple(
        r for r in cfg.regions if r.code != "USA"))

    kept, dropped = filter_events([_event("Housing Starts", "USD")], patched)
    assert len(kept) == 1
    assert dropped == []


def test_exclude_keyword_wins_over_include_keyword():
    import dataclasses

    from app.analysis import calendar_config as cc

    cfg = load_calendar_config()
    usa = next(r for r in cfg.regions if r.code == "USA")
    patched_region = dataclasses.replace(
        usa, include_keywords=usa.include_keywords + ("holiday",),
        exclude_keywords=("holiday",))
    patched = dataclasses.replace(cfg, regions=(patched_region,) + tuple(
        r for r in cfg.regions if r.code != "USA"))

    kept, dropped = filter_events([_event("CPI y/y holiday", "USD")], patched)
    assert kept == []
    assert "exclude" in dropped[0][1]


# --------------------------------------------------------------------------
# The five regions end to end
# --------------------------------------------------------------------------
def test_one_event_per_whitelisted_region_survives():
    events = [
        _event("FOMC Statement", "USD"),
        _event("ECB Rate Decision", "EUR"),
        _event("China Caixin Manufacturing PMI", "CNY"),
        _event("BOJ Policy Rate", "JPY"),
        _event("Bank of England Rate Decision", "GBP"),
        _event("RBA Cash Rate Decision", "AUD"),
        _event("BoC Overnight Rate", "CAD"),
    ]
    kept, dropped = filter_events(events)
    assert [e.country for e in kept] == ["USD", "EUR", "CNY", "JPY", "GBP"]
    assert len(dropped) == 2


def test_region_membership_is_reported_for_a_kept_event():
    region, reason = classify_region(_event("Nonfarm Payrolls", "USD"))
    assert region is not None and region.code == "USA"
    assert reason == ""


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------
def test_filter_off_returns_everything(monkeypatch):
    """With the flag off the calendar must be exactly what it was."""
    from app.config import settings
    from app.ingestion.economic_calendar import apply_region_filter

    monkeypatch.setattr(settings, "calendar_region_filter_enabled", False,
                        raising=False)
    events = [_event("CPI y/y", "CHF"), _event("Housing Starts", "USD")]
    assert apply_region_filter(events) == events


def test_filter_on_drops_the_non_whitelisted_events(monkeypatch):
    from app.config import settings
    from app.ingestion.economic_calendar import apply_region_filter

    monkeypatch.setattr(settings, "calendar_region_filter_enabled", True,
                        raising=False)
    events = [_event("CPI y/y", "CHF"), _event("CPI y/y", "USD")]
    kept = apply_region_filter(events)
    assert [e.country for e in kept] == ["USD"]


def test_a_broken_config_never_silently_changes_the_calendar(monkeypatch):
    """Fail-safe: the previous behaviour stands rather than an empty calendar."""
    from app.config import settings
    from app.ingestion import economic_calendar as ec

    monkeypatch.setattr(settings, "calendar_region_filter_enabled", True,
                        raising=False)

    def _boom(*_args, **_kwargs):
        raise CalendarConfigError("config unreadable")

    monkeypatch.setattr("app.analysis.calendar_config.filter_events", _boom)
    events = [_event("CPI y/y", "CHF")]
    assert ec.apply_region_filter(events) == events


# --------------------------------------------------------------------------
# A dropped event is still a MARKET-MOVING EVENT - it just cannot score
# --------------------------------------------------------------------------
def test_the_filter_keeps_the_dropped_events_for_the_event_risk_note():
    """The dropped events are set aside, not thrown away."""
    from app.config import settings
    from app.ingestion.economic_calendar import (
        apply_region_filter,
        filtered_out_events,
    )

    original = settings.calendar_region_filter_enabled
    settings.calendar_region_filter_enabled = True
    try:
        apply_region_filter([_event("CPI y/y", "CHF"),
                             _event("CPI y/y", "USD")])
        dropped = filtered_out_events()
        assert len(dropped) == 1
        assert dropped[0][0].country == "CHF"
        assert "not in the whitelist" in dropped[0][1]
    finally:
        settings.calendar_region_filter_enabled = original


def test_an_unwhitelisted_high_impact_release_still_raises_the_event_risk():
    """A Swiss CPI moves the market even though it may not score."""
    from app.analysis.calendar_risk import event_risk_check

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    swiss = _event("CPI y/y", "CHF")
    swiss.when_utc = now + timedelta(minutes=20)

    at_risk, note = event_risk_check([], now=now, other_events=[swiss])
    assert at_risk is True
    assert "other region" in note
    assert "CPI y/y" in note


def test_the_event_risk_note_for_a_whitelisted_release_is_not_labelled():
    from app.analysis.calendar_risk import event_risk_check

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    us = _event("CPI y/y", "USD")
    us.when_utc = now + timedelta(minutes=20)

    at_risk, note = event_risk_check([us], now=now)
    assert at_risk is True
    assert "other region" not in note
    assert note.startswith("EVENT RISK:")


def test_an_unwhitelisted_release_contributes_zero_to_the_score():
    """The whitelist is a scoring decision, and it is a real one."""
    from app.analysis.regional_bias import compute_regional_bias
    from app.models.enums import Asset

    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    swiss = _event("CPI y/y", "CHF")
    swiss.when_utc = now - timedelta(hours=1)
    swiss.actual = 3.5
    swiss.forecast = 3.2

    result = compute_regional_bias([swiss], now=now)
    assert result.asset(Asset.GOLD).score == 0.0
    assert result.asset(Asset.GOLD).contributions == ()


# --------------------------------------------------------------------------
# The UK's price and retail releases
# --------------------------------------------------------------------------
def test_uk_price_and_retail_releases_are_on_the_list():
    """UK PPI, RPI and retail sales are UK releases, not filler."""
    for title in ("UK PPI Output Prices YoY", "UK RPI MoM",
                  "UK Core Retail Sales YoY"):
        kept, _dropped = filter_events([_event(title, "GBP")])
        assert len(kept) == 1, title


def test_the_treasury_auction_rows_are_off_the_us_list():
    """A bill/bond/TIPS auction row is not a macro release."""
    for title in ("US 4-Week Bill High Yield", "US 20-Year Bond High Yield",
                  "US 5-Year TIPS High Yield", "US 4-Week Bill Bid-to-Cover",
                  "US 20-Year Bond Bid-to-Cover", "US Treasury Bond Auction"):
        kept, dropped = filter_events([_event(title, "USD")])
        assert kept == [], title
        assert "exclude" in dropped[0][1], title


def test_a_genuine_us_yield_release_stays_on_the_list():
    """Removing the auction rows must not remove the yield itself."""
    kept, _dropped = filter_events([_event("US 10-Year Treasury Yield", "USD")])
    assert len(kept) == 1


def test_a_missing_config_file_raises_a_clear_error(tmp_path):
    from app.analysis import calendar_config as cc

    cc.clear_config_cache()
    try:
        with pytest.raises(CalendarConfigError):
            cc.load_calendar_config(tmp_path / "nope.json")
    finally:
        cc.clear_config_cache()


# --------------------------------------------------------------------------
# Title normalisation helper
# --------------------------------------------------------------------------
def test_normalise_title_folds_the_possessive():
    assert normalise_title("Fed's Williams Speaks") == "fed williams speaks"
