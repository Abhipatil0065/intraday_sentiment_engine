"""
REGIONAL MACRO BIAS ENGINE.

The engine turns the FinancialJuice calendar into one signed score per asset
(Gold, Silver, Bitcoin, Equities) from five regions only. What is protected
here is the ARITHMETIC and the HONESTY of the result:

  * a print from a region outside the whitelist contributes NOTHING;
  * an inverted indicator (unemployment, jobless claims) flips its sign;
  * an upcoming print contributes NOTHING but is flagged as event risk;
  * a speech with no direction tag contributes NOTHING;
  * a missing actual or forecast contributes NOTHING - the event is skipped,
    never guessed at;
  * the score is normalised to -100 .. +100 and labelled from the configured
    bands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.calendar_config import load_calendar_config
from app.analysis.economic_impact import CalendarEvent
from app.analysis.regional_bias import (
    PANEL_ASSETS,
    compute_regional_bias,
    label_for,
    normalise_score,
    resolve_stance,
    speech_stance,
    surprise_for,
    time_decay,
)
from app.models.enums import Asset

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _event(title: str, country: str, *, when: datetime | None = None,
           impact: str = "High", actual: float | None = None,
           forecast: float | None = None, previous: float | None = None
           ) -> CalendarEvent:
    return CalendarEvent(
        title=title,
        country=country,
        when_utc=when or (NOW - timedelta(hours=1)),
        impact=impact,
        forecast=forecast,
        previous=previous,
        actual=actual,
        provider="financialjuice",
    )


def _cfg():
    return load_calendar_config()


def _score(title, country, **kwargs) -> float:
    """The gold score produced by a single event."""
    result = compute_regional_bias([_event(title, country, **kwargs)], now=NOW)
    return result.asset(Asset.GOLD).score


def _contribution(title, country, asset, **kwargs) -> float:
    result = compute_regional_bias([_event(title, country, **kwargs)], now=NOW)
    row = result.asset(asset)
    return sum(c.contribution for c in row.contributions)


# --------------------------------------------------------------------------
# Region filtering
# --------------------------------------------------------------------------
def test_event_from_a_region_outside_the_whitelist_scores_nothing():
    result = compute_regional_bias(
        [_event("CPI y/y", "CHF", actual=3.5, forecast=3.2)], now=NOW)
    assert result.asset(Asset.GOLD).score == 0.0
    assert result.asset(Asset.GOLD).contributions == ()


def test_us_event_is_scored():
    result = compute_regional_bias(
        [_event("CPI y/y", "USD", actual=3.5, forecast=3.2)], now=NOW)
    assert result.asset(Asset.GOLD).contributions


def test_low_impact_events_are_ignored_by_default():
    """The impact floor is config, and the default drops Low."""
    cfg = _cfg()
    assert cfg.impact_min == "Medium"
    result = compute_regional_bias(
        [_event("Retail Sales m/m", "USD", impact="Low", actual=0.8,
                forecast=0.2)],
        now=NOW)
    assert result.asset(Asset.GOLD).contributions == ()


def test_an_overridden_event_escapes_the_impact_floor():
    """A forced High print is used even when the provider tagged it Low."""
    result = compute_regional_bias(
        [_event("Core CPI y/y", "USD", impact="Low", actual=3.5, forecast=3.2)],
        now=NOW)
    assert result.asset(Asset.GOLD).contributions


# --------------------------------------------------------------------------
# Surprise
# --------------------------------------------------------------------------
def test_surprise_is_the_standardised_miss():
    """CPI std is 0.3: a 0.3pt miss is exactly one sigma."""
    z, pct, source = surprise_for(
        _event("CPI y/y", "USD", actual=3.5, forecast=3.2), _cfg())
    assert abs(z - 1.0) < 0.01
    assert source == "standardised"
    assert pct is not None and abs(pct - 9.375) < 0.01


def test_surprise_falls_back_to_a_percent_deviation_when_there_is_no_std():
    """An indicator with no std of its own still produces a comparable miss."""
    cfg = _cfg()
    z, pct, source = surprise_for(
        _event("Zebra Widget Output", "USD", actual=110.0, forecast=100.0), cfg)
    assert source == "percent"
    assert abs(pct - 10.0) < 0.01
    assert abs(z - 1.0) < 0.01      # 10% relative miss / percent_divisor 10


def test_missing_actual_or_forecast_skips_the_event():
    cfg = _cfg()
    assert surprise_for(_event("CPI y/y", "USD", forecast=3.2), cfg) \
        == (None, None, "none")
    assert surprise_for(_event("CPI y/y", "USD", actual=3.5), cfg) \
        == (None, None, "none")


def test_one_release_cannot_swamp_the_panel_with_an_oversized_miss():
    """The std table is a rule of thumb, so the surprise is capped."""
    cfg = _cfg()
    assert cfg.surprise_max_sigma == 3.0
    # ISM std is 1.5: a 5.0 point miss is 3.3 sigma before the cap -> 3.0.
    z, _pct, _source = surprise_for(
        _event("ISM Manufacturing PMI", "USD", actual=55.0, forecast=50.0),
        cfg)
    assert abs(z - 3.0) < 0.001


def test_an_event_without_numbers_does_not_crash_the_engine():
    result = compute_regional_bias(
        [_event("CPI y/y", "USD"), _event("Fed Speak", "USD")], now=NOW)
    assert result.asset(Asset.GOLD).score == 0.0
    assert result.error == ""


# --------------------------------------------------------------------------
# Direction, including the inverted indicators
# --------------------------------------------------------------------------
def test_hot_us_cpi_is_bearish_for_gold():
    assert _contribution("CPI y/y", "USD", Asset.GOLD,
                         actual=3.5, forecast=3.2) < 0


def test_soft_us_cpi_is_bullish_for_gold():
    assert _contribution("CPI y/y", "USD", Asset.GOLD,
                         actual=3.0, forecast=3.2) > 0


def test_unemployment_beat_is_inverted_and_bullish_for_gold():
    """A higher unemployment rate is a WEAK economy, so gold goes UP."""
    contribution = _contribution("Unemployment Rate", "USD", Asset.GOLD,
                                 actual=4.3, forecast=4.2)
    assert contribution > 0


def test_jobless_claims_beat_is_inverted_and_bullish_for_gold():
    contribution = _contribution("Initial Jobless Claims", "USD", Asset.GOLD,
                                 actual=250.0, forecast=240.0)
    assert contribution > 0


def test_inverted_event_stance_is_reported_explicitly():
    cfg = _cfg()
    region = cfg.region_by_code("USA")
    stance, reason, inverted, is_speech, is_decision = resolve_stance(
        _event("Unemployment Rate", "USD", actual=4.3, forecast=4.2),
        cfg, region, 0.5)
    assert stance == "WEAK"
    assert inverted is True
    assert is_speech is False and is_decision is False
    assert "inverted" in reason


def test_non_inverted_event_stance_is_hot_on_a_beat():
    cfg = _cfg()
    region = cfg.region_by_code("USA")
    stance, _reason, inverted, _s, _d = resolve_stance(
        _event("CPI y/y", "USD", actual=3.5, forecast=3.2), cfg, region, 1.0)
    assert stance == "STRONG"
    assert inverted is False


# --------------------------------------------------------------------------
# Central banks
# --------------------------------------------------------------------------
def test_hawkish_ecb_decision_is_bullish_for_gold_and_bearish_for_equities():
    gold = _contribution("ECB Rate Decision", "EUR", Asset.GOLD,
                         actual=2.25, forecast=2.0)
    equities = _contribution("ECB Rate Decision", "EUR", Asset.EQUITY,
                             actual=2.25, forecast=2.0)
    assert gold > 0
    assert equities < 0


def test_hawkish_boj_decision_is_bearish_for_bitcoin_and_bullish_for_gold():
    btc = _contribution("BOJ Policy Rate", "JPY", Asset.BITCOIN,
                        actual=0.5, forecast=0.25)
    gold = _contribution("BOJ Policy Rate", "JPY", Asset.GOLD,
                         actual=0.5, forecast=0.25)
    assert btc < 0
    assert gold > 0


def test_dovish_fed_decision_is_bullish_for_gold():
    assert _contribution("FOMC Rate Decision", "USD", Asset.GOLD,
                         actual=4.25, forecast=4.5) > 0


def test_a_speech_with_no_direction_tag_scores_nothing():
    assert speech_stance("Fed Chair Speaks on the Economy", _cfg())[0] is None
    result = compute_regional_bias(
        [_event("Fed Chair Speaks on the Economy", "USD")], now=NOW)
    assert result.asset(Asset.GOLD).contributions == ()


def test_a_tagged_speech_scores():
    stance = speech_stance("Fed Chair Speaks, remains hawkish on inflation",
                           _cfg())
    assert stance[0] == "HAWKISH"


def test_a_speech_matching_both_tags_is_not_a_direction():
    assert speech_stance("Fed Chair Speaks: hawkish but dovish", _cfg())[0] is None


# --------------------------------------------------------------------------
# The speech tagger: config-driven, and it says what it matched
# --------------------------------------------------------------------------
def test_the_speech_tagger_reports_the_matched_keywords():
    stance, hits = speech_stance(
        "Fed Chair Speaks, remains hawkish on inflation", _cfg())
    assert stance == "HAWKISH"
    assert "hawkish" in hits


def test_a_dovish_speech_is_tagged_and_says_so():
    stance, hits = speech_stance("ECB's Lagarde Speaks: rate cut on the table",
                                  _cfg())
    assert stance == "DOVISH"
    assert hits


def test_the_speech_tagger_is_config_driven(monkeypatch):
    """The keywords live in the config, not in the code."""
    import dataclasses

    from app.analysis.calendar_config import load_calendar_config

    cfg = load_calendar_config()
    patched = dataclasses.replace(
        cfg, speech_tags=(("HAWKISH", ("banana split",)),))
    stance, hits = speech_stance("The Chair Speaks on the banana split", patched)
    assert stance == "HAWKISH"
    assert hits == ("banana split",)


def test_a_press_conference_is_tagged_like_a_speech():
    """A press conference carries a direction in its title too."""
    stance, hits = speech_stance(
        "FOMC Press Conference: Fed remains hawkish", _cfg())
    assert stance == "HAWKISH"
    assert "hawkish" in hits


def test_an_untagged_press_conference_scores_zero():
    result = compute_regional_bias(
        [_event("FOMC Press Conference", "USD")], now=NOW)
    assert result.asset(Asset.GOLD).contributions == ()
    assert result.asset(Asset.GOLD).score == 0.0


def test_the_matched_keywords_are_logged(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="app.analysis.regional_bias"):
        compute_regional_bias(
            [_event("Fed Chair Speaks, remains hawkish on inflation", "USD")],
            now=NOW)
    text = caplog.text
    assert "speech tag" in text
    assert "hawkish" in text
    assert "HAWKISH" in text


def test_the_reason_names_the_matched_keyword():
    result = compute_regional_bias(
        [_event("Fed Chair Speaks, remains hawkish on inflation", "USD")],
        now=NOW)
    drivers = result.asset(Asset.GOLD).top_drivers
    assert drivers
    assert "hawkish" in drivers[0].reason


# --------------------------------------------------------------------------
# China
# --------------------------------------------------------------------------
def test_strong_china_data_is_bullish_for_silver_and_equities():
    silver = _contribution("China Caixin Manufacturing PMI", "CNY",
                           Asset.SILVER, actual=51.0, forecast=50.5)
    equities = _contribution("China Caixin Manufacturing PMI", "CNY",
                             Asset.EQUITY, actual=51.0, forecast=50.5)
    assert silver > 0
    assert equities > 0


def test_weak_china_data_is_bearish_for_silver_and_equities():
    silver = _contribution("China Caixin Manufacturing PMI", "CNY",
                           Asset.SILVER, actual=49.0, forecast=50.5)
    equities = _contribution("China Caixin Manufacturing PMI", "CNY",
                             Asset.EQUITY, actual=49.0, forecast=50.5)
    assert silver < 0
    assert equities < 0


# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------
def test_region_weight_scales_the_contribution():
    """The same miss is worth less in the Eurozone than in the USA."""
    cfg = _cfg()
    assert cfg.region_by_code("USA").weight == 1.0
    assert cfg.region_by_code("EUR").weight == 0.6
    us = abs(_contribution("CPI y/y", "USD", Asset.GOLD,
                           actual=3.5, forecast=3.2))
    eu = abs(_contribution("CPI y/y", "EUR", Asset.GOLD,
                           actual=3.5, forecast=3.2))
    assert us > eu


def test_impact_weight_scales_the_contribution():
    high = abs(_contribution("Retail Sales m/m", "USD", Asset.GOLD,
                              impact="High", actual=0.8, forecast=0.2))
    medium = abs(_contribution("Retail Sales m/m", "USD", Asset.GOLD,
                                impact="Medium", actual=0.8, forecast=0.2))
    assert high > medium


# --------------------------------------------------------------------------
# Time decay
# --------------------------------------------------------------------------
def test_time_decay_is_full_for_the_first_hours():
    cfg = _cfg()
    assert time_decay(NOW - timedelta(minutes=5), NOW, cfg) == 1.0
    assert time_decay(NOW - timedelta(hours=4), NOW, cfg) == 1.0


def test_time_decay_is_linear_to_zero():
    cfg = _cfg()
    half = time_decay(NOW - timedelta(hours=26), NOW, cfg)
    assert abs(half - 0.5) < 0.01
    assert time_decay(NOW - timedelta(hours=48), NOW, cfg) == 0.0
    assert time_decay(NOW - timedelta(hours=72), NOW, cfg) == 0.0


def test_an_upcoming_event_has_no_decay_weight():
    """A print that has not happened yet carries no influence."""
    cfg = _cfg()
    assert time_decay(NOW + timedelta(minutes=10), NOW, cfg) == 0.0


def test_an_old_event_scores_nothing():
    result = compute_regional_bias(
        [_event("CPI y/y", "USD", when=NOW - timedelta(hours=72),
                actual=3.5, forecast=3.2)], now=NOW)
    assert result.asset(Asset.GOLD).contributions == ()


def test_a_recent_event_scores_more_than_an_old_one():
    fresh = abs(_contribution("CPI y/y", "USD", Asset.GOLD,
                              when=NOW - timedelta(hours=1),
                              actual=3.5, forecast=3.2))
    old = abs(_contribution("CPI y/y", "USD", Asset.GOLD,
                            when=NOW - timedelta(hours=30),
                            actual=3.5, forecast=3.2))
    assert fresh > old


# --------------------------------------------------------------------------
# Score, normalisation and labels
# --------------------------------------------------------------------------
def test_score_is_normalised_to_the_hundred_point_range():
    cfg = _cfg()
    assert normalise_score(0.0, cfg) == 0.0
    assert normalise_score(1000.0, cfg) == 100.0
    assert normalise_score(-1000.0, cfg) == -100.0
    assert abs(normalise_score(5.0, cfg) - 50.0) < 0.01


def test_labels_follow_the_configured_bands():
    cfg = _cfg()
    assert label_for(-70.0, cfg) == "Strong Bearish"
    assert label_for(-30.0, cfg) == "Bearish"
    assert label_for(0.0, cfg) == "Neutral"
    assert label_for(30.0, cfg) == "Bullish"
    assert label_for(70.0, cfg) == "Strong Bullish"


def test_a_hot_cpi_beat_reads_bearish_for_gold():
    """End to end: one hot US CPI print is a bearish gold reading."""
    result = compute_regional_bias(
        [_event("CPI y/y", "USD", actual=3.5, forecast=3.2)], now=NOW)
    gold = result.asset(Asset.GOLD)
    assert gold.score < 0
    assert gold.bias in ("Bearish", "Strong Bearish")


def test_the_score_stays_inside_the_range_for_a_crowded_calendar():
    events = [
        _event("CPI y/y", "USD", actual=3.9, forecast=3.2),
        _event("Nonfarm Payrolls", "USD", actual=350.0, forecast=180.0),
        _event("ISM Manufacturing PMI", "USD", actual=58.0, forecast=50.0),
        _event("Retail Sales", "USD", actual=0.8, forecast=0.2),
        _event("Unemployment Rate", "USD", actual=4.5, forecast=4.1),
    ]
    result = compute_regional_bias(events, now=NOW)
    for asset in PANEL_ASSETS:
        assert -100.0 <= result.asset(asset).score <= 100.0


def test_the_region_breakdown_adds_up_to_the_raw_score():
    result = compute_regional_bias(
        [_event("CPI y/y", "USD", actual=3.5, forecast=3.2),
         _event("ECB Rate Decision", "EUR", actual=2.25, forecast=2.0)],
        now=NOW)
    gold = result.asset(Asset.GOLD)
    breakdown_total = sum(row[2] for row in gold.region_breakdown)
    assert abs(breakdown_total - gold.raw_score) < 1e-6
    assert {row[0] for row in gold.region_breakdown} == {"USA", "EUR"}


def test_the_top_drivers_are_the_largest_contributions():
    result = compute_regional_bias(
        [_event("CPI y/y", "USD", actual=3.5, forecast=3.2),
         _event("Unemployment Rate", "USD", actual=4.3, forecast=4.2),
         _event("Retail Sales", "USD", actual=0.8, forecast=0.2)],
        now=NOW)
    gold = result.asset(Asset.GOLD)
    assert len(gold.top_drivers) <= 3
    magnitudes = [abs(d.contribution) for d in gold.top_drivers]
    assert magnitudes == sorted(magnitudes, reverse=True)


# --------------------------------------------------------------------------
# Event risk
# --------------------------------------------------------------------------
def test_an_upcoming_high_impact_event_is_listed_but_scores_nothing():
    result = compute_regional_bias(
        [_event("FOMC Rate Decision", "USD", when=NOW + timedelta(hours=3))],
        now=NOW)
    assert result.asset(Asset.GOLD).score == 0.0
    assert len(result.upcoming) == 1
    assert result.upcoming[0].title == "FOMC Rate Decision"
    assert result.upcoming[0].minutes_away == 180


def test_an_upcoming_low_impact_event_is_not_listed():
    result = compute_regional_bias(
        [_event("CPI y/y", "USD", impact="Low", when=NOW + timedelta(hours=3))],
        now=NOW)
    assert result.upcoming == ()


def test_an_upcoming_event_from_an_unlisted_region_is_not_listed():
    result = compute_regional_bias(
        [_event("CPI y/y", "CHF", when=NOW + timedelta(hours=3))], now=NOW)
    assert result.upcoming == ()


def test_the_neutral_zone_is_widened_around_an_imminent_release():
    """A thin score is not presented as a call while a print is imminent."""
    events = [
        _event("CPI y/y", "USD", actual=3.5, forecast=3.2),    # a clear beat
        _event("FOMC Rate Decision", "USD", when=NOW + timedelta(minutes=10)),
    ]
    result = compute_regional_bias(events, now=NOW)
    gold = result.asset(Asset.GOLD)
    assert gold.event_risk is not None
    assert gold.bias == "Neutral"

    # Without the imminent print the same score is a call.
    calm = compute_regional_bias(events[:1], now=NOW).asset(Asset.GOLD)
    assert calm.bias != "Neutral"


def test_the_event_risk_window_expires():
    """The window is 30 min before the release until 15 min after it."""
    soon = compute_regional_bias(
        [_event("FOMC Rate Decision", "USD", when=NOW + timedelta(minutes=20))],
        now=NOW)
    assert soon.asset(Asset.GOLD).event_risk is not None

    later = compute_regional_bias(
        [_event("FOMC Rate Decision", "USD", when=NOW + timedelta(hours=2))],
        now=NOW)
    assert later.asset(Asset.GOLD).event_risk is None


# --------------------------------------------------------------------------
# The m/m + y/y dedupe
# --------------------------------------------------------------------------
def test_an_mom_and_a_yoy_row_of_one_release_count_once():
    result = compute_regional_bias(
        [_event("CPI m/m", "USD", actual=0.4, forecast=0.3),
         _event("CPI y/y", "USD", actual=3.5, forecast=3.2)], now=NOW)
    gold = result.asset(Asset.GOLD)
    assert len(gold.contributions) == 1


# --------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------
def test_the_payload_carries_scores_regions_and_upcoming_events():
    from app.analysis.regional_bias import to_payload

    result = compute_regional_bias(
        [_event("CPI y/y", "USD", actual=3.5, forecast=3.2),
         _event("FOMC Rate Decision", "USD", when=NOW + timedelta(hours=3))],
        now=NOW)
    payload = to_payload(result)
    assert len(payload["assets"]) == 5
    assert [a["asset"] for a in payload["assets"]] == [
        "XAUUSD", "XAGUSD", "BTCUSD", "EQUITY", "USOIL"]
    assert payload["upcoming"][0]["countdown"].startswith("in ")
    assert payload["display_timezone"]
    assert payload["generated_at_utc"].endswith("+00:00")


def test_a_broken_config_produces_an_error_payload_not_a_crash():
    from app.analysis.calendar_config import CalendarConfigError
    from app.analysis.regional_bias import to_payload

    def _boom():
        raise CalendarConfigError("unreadable")

    import app.analysis.regional_bias as rb
    original = rb.load_calendar_config
    rb.load_calendar_config = _boom
    try:
        result = rb.compute_regional_bias([], now=NOW)
    finally:
        rb.load_calendar_config = original

    payload = to_payload(result)
    assert payload["error"]
    assert payload["assets"] == []


# --------------------------------------------------------------------------
# The pipeline accessor
# --------------------------------------------------------------------------
def test_the_pipeline_exposes_the_calendar_events_as_a_copy():
    """A caller must not be able to mutate the scheduler's state."""
    from app.services import pipeline

    events = pipeline.current_calendar_events()
    before = len(events)
    events.append("not an event")
    assert len(pipeline.current_calendar_events()) == before


def test_the_panel_payload_is_well_formed_even_without_calendar_data():
    from app.services import pipeline

    payload = pipeline.regional_bias_payload()
    assert [a["asset"] for a in payload["assets"]] == [
        "XAUUSD", "XAGUSD", "BTCUSD", "EQUITY", "USOIL"]
    assert payload["upcoming"] == []
    assert payload["error"] == ""
    assert "stale_reason" in payload


# --------------------------------------------------------------------------
# The dashboard panel (source assertions - the panel renders in the browser)
# --------------------------------------------------------------------------
def _html() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "app" / "dashboard" / "web"
            / "index.html").read_text(encoding="utf-8")


def test_the_dashboard_has_a_regional_bias_panel():
    html = _html()
    assert 'id="regionalBias"' in html
    assert "function renderRegionalBias" in html
    assert "renderRegionalBias(d.regional_bias);" in html


def test_the_panel_shows_the_score_label_drivers_and_regions():
    html = _html()
    assert "TOP DRIVERS" in html
    assert "REGION CONTRIBUTION" in html
    assert "UPCOMING HIGH-IMPACT EVENTS" in html


def test_the_panel_reports_a_stale_or_missing_calendar():
    html = _html()
    assert "CALENDAR STALE" in html
    assert "REGIONAL BIAS UNAVAILABLE" in html
    assert "last updated" in html


# --------------------------------------------------------------------------
# The API payload
# --------------------------------------------------------------------------
def test_the_dashboard_endpoint_carries_the_regional_bias_section():
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).get("/api/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert "regional_bias" in body
    panel = body["regional_bias"]
    assert [a["asset"] for a in panel["assets"]] == [
        "XAUUSD", "XAGUSD", "BTCUSD", "EQUITY", "USOIL"]
    assert "upcoming" in panel
    assert "last_updated_local" in panel
