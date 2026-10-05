"""
PHASE 3 (subset): ROBUST VALUE PARSING + THE NON-NUMERIC CRASH FIX.

Two defects are pinned here.

1. A CRASH. ``evaluate()`` did ``event.actual - event.forecast`` with no type
   check, so any provider placeholder that reached it unparsed ("-", "<0.1%",
   "1.5|2.1") raised TypeError and could take a poll down.
2. A GUESS. ``to_float()`` would happily read a bound or a revision pair as if
   it were a published number.

Unparseable must mean NO SURPRISE, never a crash and never a guess.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.economic_impact import (
    CalendarEvent,
    evaluate,
    to_float,
)
from app.models.enums import Asset

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def ev(title="US CPI m/m", country="USD", forecast=None, previous=None,
       actual=None, impact="High", hours=1.0):
    return CalendarEvent(title=title, country=country,
                         when_utc=NOW + timedelta(hours=hours),
                         impact=impact, forecast=forecast, previous=previous,
                         actual=actual)


# --------------------------------------------------------------------------
# Values that MUST parse
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("3.4%", 3.4),
    ("-0.2%", -0.2),
    ("235K", 235_000.0),
    ("1.2M", 1_200_000.0),
    ("$74.2B", 74_200_000_000.0),
    ("1,250", 1250.0),
    ("0.3", 0.3),
    ("-1", -1.0),
    (5, 5.0),
    (5.5, 5.5),
])
def test_readable_values_parse(raw, expected):
    assert to_float(raw) == pytest.approx(expected)


# --------------------------------------------------------------------------
# Values that MUST NOT parse (a guess is worse than nothing)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    None, "", "   ", "-", "--", "---", "N/A", "n/a", "none", "null",
    "TBC", "?", "<0.1%", ">0.1%", "1.5|2.1", "unchanged", "~2.5", "≈2.5",
])
def test_unreadable_values_return_none(raw):
    assert to_float(raw) is None


def test_a_bound_is_not_read_as_a_number():
    """'<0.1%' is a limit, not a measurement of 0.1."""
    assert to_float("<0.1%") is None


def test_a_revision_pair_is_not_read_as_one_number():
    """'1.5|2.1' is revised|original; picking either would be a guess."""
    assert to_float("1.5|2.1") is None


def test_boolean_is_not_a_number():
    """True must not silently become 1.0."""
    assert to_float(True) is None


def test_nan_and_inf_are_rejected():
    assert to_float(float("nan")) is None
    assert to_float(float("inf")) is None


# --------------------------------------------------------------------------
# The crash fix
# --------------------------------------------------------------------------
@pytest.mark.parametrize("f,p,a", [
    ("<0.1%", "-", "1.5|2.1"),
    ("-", "-", "-"),
    ("N/A", "3.1", "N/A"),
    ("3.2", "TBC", "3.5"),
])
def test_unparseable_values_do_not_crash(f, p, a):
    """The original defect raised TypeError here."""
    for asset in Asset:
        out = evaluate(ev(forecast=f, previous=p, actual=a), asset)
        assert out is None or out.direction is not None


@pytest.mark.parametrize("f,p,a", [
    ("<0.1%", "-", "1.5|2.1"),
    ("-", "-", "-"),
])
def test_unparseable_yields_no_surprise(f, p, a):
    for asset in Asset:
        out = evaluate(ev(forecast=f, previous=p, actual=a), asset)
        assert out is None
        assert not hasattr(out, "surprise")


def test_a_bad_actual_never_becomes_a_signal():
    """No surprise, so no directional call - the engine says it does not know."""
    out = evaluate(ev(forecast="3.2", previous="3.1", actual="<0.1%"),
                   Asset.GOLD)
    assert out is None


def test_a_good_actual_still_produces_a_signal():
    """The guard must not suppress genuinely usable data."""
    out = evaluate(ev(forecast="3.2", previous="3.1", actual="3.5"), Asset.GOLD)
    assert out is not None
    assert out.mode == "RELEASED"
    assert out.surprise is not None


def test_numeric_strings_still_work_end_to_end():
    """Values arriving as strings from a provider are coerced, not rejected."""
    out = evaluate(ev(forecast="3.2", previous="3.1", actual="3.5"), Asset.GOLD)
    assert out is not None
    assert "3.5" in out.reason


def test_expected_path_guards_placeholders_too():
    """forecast/previous placeholders must not crash the EXPECTED branch."""
    out = evaluate(ev(forecast="-", previous="-", actual=None), Asset.GOLD)
    assert out is None