"""
STANDARDISED SURPRISE + EVENT_RISK (improvement item 8).

The defect these tests protect: a flat 2% "material surprise" threshold treats
a 0.2% CPI miss as nothing and a 120k payroll miss as huge. Surprise must be
measured in standard deviations of the indicator itself.
"""

from datetime import datetime, timedelta, timezone

from app.analysis.calendar_risk import (
    event_risk_check,
    standardised_surprise,
    std_for,
    surprise_band,
)

NOW = datetime(2026, 10, 4, 14, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Standardised surprise
# --------------------------------------------------------------------------
def test_cpi_surprise_is_measured_in_sigma():
    # CPI std is 0.3: a 0.3pt miss is exactly one sigma.
    z = standardised_surprise(actual=3.5, forecast=3.2, title="CPI y/y")
    assert abs(z - 1.0) < 0.01


def test_payroll_surprise_is_measured_in_sigma():
    # Payroll std is 120k: a 120k miss is one sigma, same as the CPI case.
    z = standardised_surprise(actual=240, forecast=120, title="Nonfarm Payrolls")
    assert abs(z - 1.0) < 0.01


def test_small_cpi_miss_is_not_dismissed_as_flat_percent():
    """A 0.1% CPI miss is under the old flat 2% bar but is a real signal."""
    z = standardised_surprise(actual=3.3, forecast=3.2, title="CPI y/y")
    assert z is not None and abs(z) > 0.3


def test_no_forecast_means_no_surprise_is_invented():
    assert standardised_surprise(actual=3.5, forecast=None, title="CPI") is None


def test_std_lookup_prefers_the_longest_matching_indicator(monkeypatch):
    """
    "core pce" must beat "pce" when both are present.

    Proximity of match is what is being tested, so the two stds are set
    deliberately different here rather than relying on the shipped table.
    """
    from app.config import settings
    monkeypatch.setattr(
        settings, "surprise_std_table", "core pce=0.1,pce=0.5", raising=False)
    assert std_for("Core PCE Price Index") == 0.1
    assert std_for("PCE Price Index") == 0.5
    # Same absolute miss, larger standardised surprise on the tighter series.
    z_core = standardised_surprise(3.5, 3.2, "Core PCE Price Index")
    z_head = standardised_surprise(3.5, 3.2, "PCE Price Index")
    assert z_core > z_head


def test_unknown_indicator_falls_back_to_the_default():
    from app.config import settings
    assert std_for("Some Unlisted Indicator") == float(settings.surprise_std_default)


def test_surprise_band_is_neutral_for_a_small_miss():
    assert surprise_band(standardised_surprise(3.21, 3.2, "CPI")) == "NEUTRAL"


def test_surprise_band_escalates_with_size():
    assert surprise_band(0.1) == "NEUTRAL"
    assert surprise_band(0.5) == "MODERATE"
    assert surprise_band(1.5) == "STRONG"
    assert surprise_band(4.0) == "EXTREME"


def test_surprise_band_of_none_is_neutral():
    assert surprise_band(None) == "NEUTRAL"


def test_band_thresholds_are_configurable(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "surprise_neutral_band", 2.0, raising=False)
    assert surprise_band(1.5) == "NEUTRAL"


# --------------------------------------------------------------------------
# EVENT_RISK
# --------------------------------------------------------------------------
class _Event:
    def __init__(self, title, country, when, impact):
        self.title = title
        self.country = country
        self.when_utc = when
        self.impact = impact


def test_imminent_high_impact_release_flags_event_risk():
    events = [_Event("CPI y/y", "USD", NOW + timedelta(minutes=20), "High")]
    at_risk, note = event_risk_check(events, now=NOW)
    assert at_risk is True
    assert "CPI y/y" in note
    assert "EVENT RISK" in note


def test_no_release_nearby_is_not_event_risk():
    events = [_Event("CPI y/y", "USD", NOW + timedelta(hours=5), "High")]
    at_risk, note = event_risk_check(events, now=NOW)
    assert at_risk is False
    assert note == ""


def test_low_impact_release_does_not_flag_event_risk():
    events = [_Event("Some Minor Print", "USD", NOW + timedelta(minutes=10),
                     "Low")]
    at_risk, _ = event_risk_check(events, now=NOW)
    assert at_risk is False


def test_already_released_event_is_not_a_future_risk():
    events = [_Event("CPI y/y", "USD", NOW - timedelta(minutes=5), "High")]
    at_risk, _ = event_risk_check(events, now=NOW)
    assert at_risk is False


def test_empty_calendar_is_not_event_risk():
    assert event_risk_check([], now=NOW) == (False, "")


def test_lookahead_window_is_configurable():
    events = [_Event("CPI y/y", "USD", NOW + timedelta(minutes=30), "High")]
    assert event_risk_check(events, now=NOW, minutes=45)[0] is True
    assert event_risk_check(events, now=NOW, minutes=10)[0] is False


def test_soonest_release_is_named():
    events = [
        _Event("Later Print", "USD", NOW + timedelta(minutes=40), "High"),
        _Event("Sooner Print", "USD", NOW + timedelta(minutes=5), "High"),
    ]
    _, note = event_risk_check(events, now=NOW)
    assert "Sooner Print" in note