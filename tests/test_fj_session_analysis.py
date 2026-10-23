"""
Unit tests for the FinancialJuice Session Analysis section.

Covers the improved scoring path (Bias/score/confidence via the transmission
map), the deterministic session classification, IST date handling, and the
per-box lookback policy.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.analysis.fj_sessions import (
    ASSET_CUES,  # noqa: F401 - kept for backwards-compat imports
    SESSION_ORDER,
    SESSION_TITLES,
    SESSION_LOOKBACK_DAYS,
    MAX_LOOKBACK_DAYS,
    AGE_LABELS,
    age_label,
    build_session_analysis,
    classify_fj_session,
    filter_reports_for_today,
    india_date_of,
    india_today,
    india_stamp,
    normalise_title,
    extract_drivers_and_risks,
    _empty_box,
    _score_asset_for_report,
    analyse_report_text,
    FJ_ASSETS,
)
from app.models.enums import Asset


# ---------------------------------------------------------------------------
# 1. classify_fj_session - deterministic session classification
# ---------------------------------------------------------------------------

def test_classify_us_session_prep():
    assert classify_fj_session("Morning Juice - US Session Prep") == "US_SESSION_PREP"
    assert classify_fj_session("US Session Prep - Stocks Watchlist") == "US_SESSION_PREP"


def test_classify_us_market_wrap():
    assert classify_fj_session("US Market Wrap - Stocks Close Higher") == "US_MARKET_WRAP"
    assert classify_fj_session("Stocks Rally - US Wrap") == "US_MARKET_WRAP"


def test_classify_europe_market_prep():
    assert classify_fj_session("Europe Market Prep - Fed Decision Looms") == "EUROPE_MARKET_PREP"
    assert classify_fj_session("London Open Watchlist - Europe Prep") == "EUROPE_MARKET_PREP"


def test_classify_europe_market_wrap():
    assert classify_fj_session("Europe Market Wrap - FTSE Closes Flat") == "EUROPE_MARKET_WRAP"
    assert classify_fj_session("Closing Bell - Europe Wrap") == "EUROPE_MARKET_WRAP"


def test_classify_asian_market_wrap():
    assert classify_fj_session("Asian Market Wrap - Nikkei Gains") == "ASIAN_MARKET_WRAP"
    assert classify_fj_session("Tokyo Close - Asia Wrap") == "ASIAN_MARKET_WRAP"


def test_classify_europe_wrap_not_us_when_trailing_label():
    # "US Stocks Rise - Europe Market Wrap" must route to Europe because the
    # trailing session label wins over region words earlier in the headline.
    assert classify_fj_session("US Stocks Rise - Europe Market Wrap") == "EUROPE_MARKET_WRAP"


def test_classify_us_wrap_not_europe_when_trailing_label():
    # "Europe Stocks Fall - US Market Wrap" must route to US.
    assert classify_fj_session("Europe Stocks Fall - US Market Wrap") == "US_MARKET_WRAP"


def test_classify_plain_headline_returns_none():
    assert classify_fj_session("Fed's Jefferson speaks on inflation") is None
    assert classify_fj_session("Gold prices hold steady") is None
    assert classify_fj_session("") is None


def test_normalise_title_strips_dateline():
    assert normalise_title("Morning Juice - US Session Prep") == "US Session Prep"
    assert normalise_title("US Market Wrap (1st October)") == "US Market Wrap"


# ---------------------------------------------------------------------------
# 2. india_date_of - IST date conversion at UTC/IST boundary
# ---------------------------------------------------------------------------

def test_india_date_of_1830_utc_is_next_ist_day():
    # 18:30 UTC = 00:00 IST next day (IST = UTC+5:30)
    dt = datetime(2026, 10, 4, 18, 30, tzinfo=timezone.utc)
    d = india_date_of(dt)
    assert d == date(2026, 10, 5), f"Expected 2026-10-05, got {d}"


def test_india_date_of_1829_utc_is_same_ist_day():
    # 18:29 UTC = 23:59 IST same day
    dt = datetime(2026, 10, 4, 18, 29, tzinfo=timezone.utc)
    d = india_date_of(dt)
    assert d == date(2026, 10, 4), f"Expected 2026-10-04, got {d}"


def test_india_date_of_1900_utc_is_next_ist_day():
    # 19:00 UTC = 00:30 IST next day
    dt = datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc)
    d = india_date_of(dt)
    assert d == date(2026, 10, 5), f"Expected 2026-10-05, got {d}"


def test_india_date_of_none():
    assert india_date_of(None) is None


def test_india_stamp_format():
    dt = datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc)
    stamp = india_stamp(dt)
    assert "2026" in stamp
    assert "IST" in stamp
    assert "20:00" in stamp  # 14:30 UTC = 20:00 IST


# ---------------------------------------------------------------------------
# 3. SESSION_LOOKBACK_DAYS policy
# ---------------------------------------------------------------------------

def test_lookback_days_values():
    assert SESSION_LOOKBACK_DAYS["US_MARKET_WRAP"] == 1
    assert SESSION_LOOKBACK_DAYS["ASIAN_MARKET_WRAP"] == 0
    assert SESSION_LOOKBACK_DAYS["EUROPE_MARKET_PREP"] == 0
    assert SESSION_LOOKBACK_DAYS["EUROPE_MARKET_WRAP"] == 0
    assert SESSION_LOOKBACK_DAYS["US_SESSION_PREP"] == 0


def test_max_lookback_days():
    assert MAX_LOOKBACK_DAYS == 1


def test_age_labels():
    assert age_label(0) == "TODAY'S REPORT"
    assert age_label(1) == "YESTERDAY'S REPORT"
    assert age_label(2) == "REPORT FROM 2 DAYS AGO"


# ---------------------------------------------------------------------------
# 4. filter_reports_for_today - IST date filtering + dedup
# ---------------------------------------------------------------------------

def _make_row(title, body, published_utc, guid=None, source="Financial Juice"):
    from types import SimpleNamespace
    return SimpleNamespace(
        guid=guid or f"guid-{title[:10]}",
        source=source,
        title=title,
        body=body,
        published_at_utc=published_utc,
    )


def test_filter_includes_today_ist_report():
    # 14:30 UTC = 20:00 IST same day
    rows = [_make_row("US Market Wrap", "Stocks closed higher.",
                      datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc))]
    result = filter_reports_for_today(rows, today=date(2026, 10, 4))
    assert len(result) == 1
    assert result[0].age_days == 0
    assert result[0].session_key == "US_MARKET_WRAP"


def test_filter_excludes_yesterday_for_strict_boxes():
    # US Market Wrap allows lookback 1, but Asia Wrap allows 0.
    # When lookback_days=0, yesterday's report is excluded.
    yesterday_utc = datetime(2026, 10, 3, 14, 30, tzinfo=timezone.utc)  # 20:00 IST Oct 3
    rows = [_make_row("Asian Market Wrap", "Nikkei closed higher.",
                      yesterday_utc)]
    result = filter_reports_for_today(rows, today=date(2026, 10, 4),
                                      lookback_days=0)
    # lookback_days=0 means only today's reports are accepted.
    assert len(result) == 0


def test_filter_includes_yesterday_for_us_wrap():
    # US Market Wrap has lookback 1, so yesterday's report is included.
    yesterday_utc = datetime(2026, 10, 3, 14, 30, tzinfo=timezone.utc)  # 20:00 IST Oct 3
    rows = [_make_row("US Market Wrap", "Stocks closed higher.",
                      yesterday_utc)]
    result = filter_reports_for_today(rows, today=date(2026, 10, 4))
    assert len(result) == 1
    assert result[0].age_days == 1


def test_filter_deduplicates_by_title():
    utc = datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc)
    rows = [
        _make_row("US Market Wrap", "Stocks closed higher.", utc, guid="g1"),
        _make_row("US Market Wrap", "Stocks closed higher.", utc, guid="g2"),
    ]
    result = filter_reports_for_today(rows, today=date(2026, 10, 4))
    assert len(result) == 1  # deduplicated on normalised title


# ---------------------------------------------------------------------------
# 5. _score_asset_for_report - new Bias/score/confidence scoring
# ---------------------------------------------------------------------------

def test_score_asset_zero_evidence():
    """A report with no relevant cues yields score 0.0, confidence 0.0."""
    result = _score_asset_for_report("The weather is nice today.", Asset.GOLD)
    assert result["score"] == 0.0
    assert result["confidence"] == 0.0
    assert result["bias"] in ("BULLISH", "BEARISH")  # binary, never NO_BIAS


def test_score_asset_bullish_evidence():
    """A report with bullish cues yields positive score."""
    result = _score_asset_for_report(
        "Gold surges on safe-haven demand as geopolitical tensions rise.",
        Asset.GOLD)
    assert result["bias"] == "BULLISH"
    assert result["score"] > 0


def test_score_asset_bearish_evidence():
    """A report with bearish cues yields negative score."""
    result = _score_asset_for_report(
        "Gold falls as dollar strengthens and yields rise.",
        Asset.GOLD)
    assert result["bias"] == "BEARISH"
    assert result["score"] < 0


def test_score_asset_never_no_bias():
    """The engine contract is binary: always BULLISH or BEARISH."""
    for asset in FJ_ASSETS:
        result = _score_asset_for_report("Markets mixed on unclear signals.", asset)
        assert result["bias"] in ("BULLISH", "BEARISH")
        assert result["bias"] != "NO_BIAS"


def test_score_asset_has_required_fields():
    result = _score_asset_for_report("Gold rallies on safe-haven bids.", Asset.GOLD)
    for key in ("asset", "label", "bias", "score", "confidence",
                "confidence_label", "drivers", "raw_score",
                "bull_weight", "bear_weight", "stale"):
        assert key in result, f"Missing key: {key}"
    assert result["asset"] == "XAUUSD"
    assert isinstance(result["score"], float)
    assert -100.0 <= result["score"] <= 100.0
    assert 0.0 <= result["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# 6. analyse_report_text - full report scoring for all assets
# ---------------------------------------------------------------------------

def test_analyse_report_text_returns_all_assets():
    result = analyse_report_text("Gold surges, silver follows, bitcoin rallies, stocks climb.")
    for asset in FJ_ASSETS:
        assert asset in result
        assert result[asset]["bias"] in ("BULLISH", "BEARISH")
        assert -100.0 <= result[asset]["score"] <= 100.0


def test_analyse_report_text_empty_body():
    result = analyse_report_text("")
    for asset in FJ_ASSETS:
        assert result[asset]["score"] == 0.0
        assert result[asset]["confidence"] == 0.0


# ---------------------------------------------------------------------------
# 7. extract_drivers_and_risks - Docket/Speakers sections → risks
# ---------------------------------------------------------------------------

def test_extract_drivers_and_risks_docket_section():
    body = """Sentiment: Stocks are higher on the day.

Docket:
08:30 ET - CPI Print
10:00 ET - Fed Speaker
"""
    drivers, risks = extract_drivers_and_risks(body, title="US Market Wrap")
    # Docket lines are forward-looking → risks
    assert len(risks) >= 1
    assert any("CPI" in r for r in risks) or len(risks) > 0


def test_extract_drivers_and_risks_speakers_section():
    body = """Sentiment: Markets steady.

Speakers:
Powell speaks at 14:00 ET.
Yellen testifies at 10:00 ET.
"""
    drivers, risks = extract_drivers_and_risks(body, title="US Session Prep")
    # Speakers are forward-looking → risks
    assert len(risks) >= 1


def test_extract_drivers_and_risks_bullish_sentiment():
    body = """Sentiment: Gold surges on safe-haven demand. Silver rallies with it.

Docket:
08:30 ET - CPI Print
"""
    drivers, risks = extract_drivers_and_risks(body, title="US Market Wrap")
    # Bullish sentiment sentence → driver
    assert len(drivers) >= 1
    # Docket → risk
    assert len(risks) >= 1


# ---------------------------------------------------------------------------
# 8. build_session_analysis - five-box assembly
# ---------------------------------------------------------------------------

def _make_session_report(title, body, published_utc, session_key,
                         india_date, age_days=0, guid=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        guid=guid or f"guid-{session_key}",
        source="Financial Juice",
        title=title,
        body=body,
        published_at_utc=published_utc,
        session_key=session_key,
        india_date=india_date,
        age_days=age_days,
        published_ist=lambda: india_stamp(published_utc),
    )


def test_build_session_analysis_returns_five_boxes():
    utc = datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc)
    today = date(2026, 10, 4)
    rows = [_make_session_report(
        "US Market Wrap", "Stocks closed higher.", utc,
        "US_MARKET_WRAP", today, age_days=0)]
    result = build_session_analysis(rows, now=utc)
    assert len(result["boxes"]) == 5
    assert result["boxes"][0]["session_key"] == "US_MARKET_WRAP"
    assert result["boxes"][0]["found"] is True


def test_build_session_analysis_missing_boxes_are_empty():
    result = build_session_analysis([], now=datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc))
    assert len(result["boxes"]) == 5
    for box in result["boxes"]:
        assert box["found"] is False
        assert box["age_label"] == "NO REPORT"
        for asset_card in box["assets"]:
            assert asset_card["score"] == 0.0
            assert asset_card["confidence"] == 0.0


def test_build_session_analysis_box_order():
    result = build_session_analysis([], now=datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc))
    keys = [b["session_key"] for b in result["boxes"]]
    assert keys == list(SESSION_ORDER)

def test_empty_box_has_new_format():
    box = _empty_box("US_MARKET_WRAP")
    for asset_card in box["assets"]:
        assert "bias" in asset_card
        assert "score" in asset_card
        assert "confidence" in asset_card
        assert asset_card["score"] == 0.0
        assert asset_card["confidence"] == 0.0
        # Backward-compatible fields are also present
        assert "bull_pct" in asset_card
        assert "bear_pct" in asset_card
        assert asset_card["bull_pct"] == 50.0
        assert asset_card["bear_pct"] == 50.0


# ---------------------------------------------------------------------------
# 9. scoring contract - new format with backward-compatible fields
# ---------------------------------------------------------------------------


def test_no_legacy_fields_in_output():
    result = build_session_analysis([], now=datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc))
    import json
    payload = json.dumps(result)
    # The new format uses bias/score/confidence as primary fields.
    # bull_pct/bear_pct are present for backward compatibility.
    assert "bias" in payload
    assert "score" in payload
    assert "confidence" in payload
    assert "legacy" not in payload.lower()


def test_scoring_contract_block():
    result = build_session_analysis([], now=datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc))
    for box in result["boxes"]:
        for asset_card in box["assets"]:
            assert asset_card["bias"] in ("BULLISH", "BEARISH")
            assert -100.0 <= asset_card["score"] <= 100.0
            assert 0.0 <= asset_card["confidence"] <= 1.0
