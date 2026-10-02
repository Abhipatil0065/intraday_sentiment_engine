"""
FinancialJuice Session Analysis - the eight validation cases from the spec.

These are pure unit tests: no network, no database, no Playwright. Timestamps
are injected explicitly so the IST date rules can be exercised deterministically
(including the midnight rollover) without waiting for a real clock change.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.analysis import fj_sessions as F
from app.models.enums import Asset

# A reference instant used throughout: 2026-10-02 09:00 UTC == 14:30 IST,
# so "today" in India is 2026-10-02.
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
TODAY_IN = date(2026, 10, 2)


def row(title, published_utc, *, source="Financial Juice", guid=None, body=""):
    return SimpleNamespace(
        guid=guid or title.lower()[:40],
        source=source,
        title=title,
        body=body,
        published_at_utc=published_utc,
    )


# ---------------------------------------------------------------------------
# Test 1 - midnight rollover in India switches the section to the new date
# ---------------------------------------------------------------------------
def test_india_date_rolls_over_at_ist_midnight():
    # 18:29 UTC on 1 Oct == 23:59 IST on 1 Oct (still the 1st in India).
    before = datetime(2026, 10, 1, 18, 29, tzinfo=timezone.utc)
    # 18:31 UTC on 1 Oct == 00:01 IST on 2 Oct (already the 2nd in India).
    after = datetime(2026, 10, 1, 18, 31, tzinfo=timezone.utc)

    assert F.india_today(before) == date(2026, 10, 1)
    assert F.india_today(after) == date(2026, 10, 2)
    # The UTC date never changes across that boundary - IST is what moves.
    assert before.date() == after.date()


def test_section_recomputes_india_date_for_each_request():
    payload = F.build_session_analysis([], now=NOW)
    assert payload["india_date"] == "2026-10-02"
    assert payload["timezone"] == "Asia/Kolkata"
    # Five boxes are always present, even with no data at all.
    assert len(payload["boxes"]) == 5


# ---------------------------------------------------------------------------
# Test 2 - per-box date policy
#   Box 1 (US Market Wrap) may show yesterday; boxes 2-5 are strictly today.
# ---------------------------------------------------------------------------
def test_us_market_wrap_falls_back_to_yesterday():
    """Box 1 is the one session that legitimately lags a day in IST."""
    rows = [row("US Stocks End Higher - US Market Wrap",
                datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}["US_MARKET_WRAP"]
    assert box["found"] is True
    assert box["is_today"] is False
    assert box["age_days"] == 1
    assert box["age_label"] == "YESTERDAY'S REPORT"
    assert box["report_date"] == "2026-10-01"
    # Its own IST timestamp is shown so the user can verify the date.
    assert box["published_ist"].startswith("Published: 01 Oct 2026")


def test_us_market_wrap_prefers_todays_report():
    rows = [
        row("US Stocks End Higher - US Market Wrap",
            datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc), guid="old"),
        row("US Stocks End Lower - US Market Wrap",
            datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc), guid="new"),
    ]
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}["US_MARKET_WRAP"]
    assert box["is_today"] is True
    assert box["age_label"] == "TODAY'S REPORT"
    assert "End Lower" in box["report_title"]


@pytest.mark.parametrize("key,title", [
    ("ASIAN_MARKET_WRAP", "Asia Market Wrap: Nikkei Rises"),
    ("EUROPE_MARKET_PREP", "Europe Market Prep - European Futures"),
    ("EUROPE_MARKET_WRAP", "European Stocks End Higher - Europe Market Wrap"),
    ("US_SESSION_PREP", "Morning Juice - US Session Prep"),
])
def test_other_sessions_never_show_yesterdays_report(key, title):
    """Boxes 2-5 are strictly today's; a prior-day report is stale news."""
    rows = [row(title, datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}[key]
    assert box["found"] is False
    assert box["age_label"] == "NO REPORT"
    # Still a neutral 50/50 rather than a fabricated signal.
    for a in box["assets"]:
        assert (a["bull_pct"], a["bear_pct"]) == (50.0, 50.0)


@pytest.mark.parametrize("key,title", [
    ("ASIAN_MARKET_WRAP", "Asia Market Wrap: Nikkei Rises"),
    ("EUROPE_MARKET_PREP", "Europe Market Prep - European Futures"),
    ("EUROPE_MARKET_WRAP", "European Stocks End Higher - Europe Market Wrap"),
    ("US_SESSION_PREP", "Morning Juice - US Session Prep"),
    ("US_MARKET_WRAP", "US Stocks End Higher - US Market Wrap"),
])
def test_every_session_still_shows_todays_report(key, title):
    rows = [row(title, datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}[key]
    assert box["found"] is True
    assert box["is_today"] is True
    assert box["age_label"] == "TODAY'S REPORT"
    assert box["report_date"] == "2026-10-02"


def test_reports_older_than_the_lookback_are_dropped():
    """Even box 1 refuses a report two days old."""
    rows = [row("US Stocks End Higher - US Market Wrap",
                datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}["US_MARKET_WRAP"]
    assert box["found"] is False
    assert box["age_label"] == "NO REPORT"


def test_future_dated_report_is_never_shown():
    rows = [row("US Stocks End Higher - US Market Wrap",
                datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}["US_MARKET_WRAP"]
    assert box["found"] is False


def test_lookback_window_is_configurable():
    """The override applies uniformly to every box."""
    rows = [row("US Stocks End Higher - US Market Wrap",
                datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW, lookback_days=3)
    box = {b["session_key"]: b for b in payload["boxes"]}["US_MARKET_WRAP"]
    assert box["found"] is True
    assert box["age_days"] == 2
    assert "2 DAYS AGO" in box["age_label"]


def test_lookback_override_applies_to_every_box():
    rows = [row("Europe Market Prep - European Futures",
                datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW, lookback_days=1)
    box = {b["session_key"]: b for b in payload["boxes"]}["EUROPE_MARKET_PREP"]
    assert box["found"] is True
    assert box["age_label"] == "YESTERDAY'S REPORT"


# ---------------------------------------------------------------------------
# External context: high-impact Section B news + economic calendar
# ---------------------------------------------------------------------------
class _Impact:
    def __init__(self, asset, direction, strength="HIGH", relevance=0.8,
                 reason=""):
        self.asset = asset
        self.direction = direction
        self.strength = strength
        self.relevance = relevance
        self.reason = reason


class _Article:
    def __init__(self, importance="HIGH", impacts=(), title="headline"):
        self.importance = importance
        self.impacts = list(impacts)
        self.title = title


def _gold_split(text, context=None):
    return F.analyse_report_text(text, context)[Asset.GOLD]


def test_context_is_none_when_no_external_evidence():
    ctx = F.merge_contexts(F.context_from_news([]), F.context_from_calendar([]))
    assert ctx is None


def test_high_importance_news_pushes_the_split():
    base = _gold_split("Gold rallies as yields fall.")
    ctx = F.context_from_news(
        [_Article(impacts=[_Impact("XAUUSD", "BEARISH", reason="hawkish")])])
    assert ctx.news_count == 1
    enriched = _gold_split("Gold rallies as yields fall.", ctx)
    assert enriched["bear_pct"] > base["bear_pct"]
    assert enriched["bull_pct"] + enriched["bear_pct"] == 100.0


def test_low_importance_news_is_ignored():
    rows = [_Article(importance="LOW", impacts=[_Impact("XAUUSD", "BEARISH")])]
    assert F.context_from_news(rows).news_count == 0


def test_very_high_importance_news_is_included():
    rows = [_Article(importance="VERY_HIGH",
                     impacts=[_Impact("XAUUSD", "BEARISH")])]
    assert F.context_from_news(rows).news_count == 1


def test_neutral_impact_contributes_nothing():
    """A NEUTRAL/MIXED verdict must never be coerced into a lean."""
    rows = [_Article(impacts=[_Impact("XAUUSD", "NEUTRAL")])]
    ctx = F.context_from_news(rows)
    assert ctx.news_count == 1          # the article was seen...
    assert Asset.GOLD not in ctx.news    # ...but it asserts nothing


def test_bullish_and_bearish_news_offset_each_other():
    rows = [
        _Article(impacts=[_Impact("XAUUSD", "BULLISH")]),
        _Article(impacts=[_Impact("XAUUSD", "BEARISH")]),
    ]
    entry = F.context_from_news(rows).news[Asset.GOLD]
    assert entry.bull == pytest.approx(entry.bear)


def test_external_evidence_cannot_break_the_100_percent_invariant():
    rows = [
        _Article(impacts=[_Impact("XAUUSD", "BULLISH"),
                          _Impact("XAGUSD", "BULLISH"),
                          _Impact("BTCUSD", "BULLISH")]),
        _Article(impacts=[_Impact("XAUUSD", "BEARISH"),
                          _Impact("XAGUSD", "BEARISH"),
                          _Impact("BTCUSD", "BEARISH")]),
    ]
    ctx = F.context_from_news(rows)
    scored = F.analyse_report_text("Fed holds rates steady. Gold rises.", ctx)
    for data in scored.values():
        assert data["bull_pct"] + data["bear_pct"] == 100.0
        assert 0.0 <= data["bull_pct"] <= 100.0


class _Sig:
    """Minimal stand-in for an economic_impact.CalendarSignal."""

    def __init__(self, mode="RELEASED"):
        self.event = type("E", (), {
            "title": "US CPI",
            "impact": "HIGH",
            "when_utc": datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        })()
        self.asset = "XAUUSD"
        self.direction = "BEARISH"
        self.strength = "VERY_HIGH"
        self.importance = "VERY_HIGH"
        self.relevance = 0.9
        self.mode = mode
        self.reason = "hot print"


def test_calendar_expected_event_is_damped_versus_released():
    released = F.context_from_calendar([_Sig("RELEASED")]).calendar[Asset.GOLD]
    expected = F.context_from_calendar([_Sig("EXPECTED")]).calendar[Asset.GOLD]
    assert expected.bear < released.bear


def test_calendar_signal_shifts_the_split():
    base = _gold_split("Gold rallies as yields fall.")
    ctx = F.context_from_calendar([_Sig()])
    assert _gold_split("Gold rallies as yields fall.", ctx)["bear_pct"] \
        > base["bear_pct"]


def test_merge_contexts_sums_news_and_calendar():
    rows = [_Article(impacts=[_Impact("XAUUSD", "BULLISH")])]
    merged = F.merge_contexts(
        F.context_from_news(rows), F.context_from_calendar([_Sig()]))
    assert merged.news_count == 1
    assert merged.calendar_count == 1
    assert Asset.GOLD in merged.news
    assert Asset.GOLD in merged.calendar


def test_payload_reports_the_context_it_used():
    rows = [row("Stocks Rebound - US Market Wrap",
                datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc))]
    ctx = F.context_from_news(
        [_Article(impacts=[_Impact("XAUUSD", "BULLISH")])])
    payload = F.build_session_analysis(rows, now=NOW, context=ctx)
    assert payload["context"]["news_items"] == 1
    assert payload["context"]["calendar_items"] == 0


def test_no_context_key_leak_when_context_is_none():
    rows = [row("Stocks Rebound - US Market Wrap",
                datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc))]
    payload = F.build_session_analysis(rows, now=NOW)
    assert payload["context"] == {"news_items": 0, "calendar_items": 0}
    box = {b["session_key"]: b for b in payload["boxes"]}["US_MARKET_WRAP"]
    for a in box["assets"]:
        assert a["bull_pct"] + a["bear_pct"] == 100.0


def test_empty_report_with_real_news_still_scores():
    """A report with no asset text is still informed by genuine evidence."""
    ctx = F.context_from_news(
        [_Article(impacts=[_Impact("XAUUSD", "BEARISH",
                                   strength="VERY_HIGH")])])
    scored = F.analyse_report_text("", ctx)
    assert scored[Asset.GOLD]["bear_pct"] > 50.0
    assert scored[Asset.GOLD]["bull_pct"] + scored[Asset.GOLD]["bear_pct"] == 100.0


# ---------------------------------------------------------------------------
# Test 3 - yesterday in UTC but TODAY in India MUST appear
# ---------------------------------------------------------------------------
def test_utc_yesterday_but_india_today_is_included():
    # 19:30 UTC on 1 Oct == 01:00 IST on 2 Oct.
    stamp = datetime(2026, 10, 1, 19, 30, tzinfo=timezone.utc)
    assert F.india_date_of(stamp) == date(2026, 10, 2)
    assert stamp.date() == date(2026, 10, 1)  # genuinely "yesterday" in UTC

    rows = [row("US Stocks End Higher - US Market Wrap", stamp)]
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}["US_MARKET_WRAP"]
    assert box["found"] is True



# ---------------------------------------------------------------------------
# Test 4 - duplicate reports appear only once
# ---------------------------------------------------------------------------
def test_duplicate_reports_are_deduplicated():
    stamp = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    same = "Asia Market Wrap: Nikkei Rises"
    rows = [
        row(same, stamp, guid="guid-a"),
        row(same, stamp, guid="guid-a"),        # exact repeat (same guid)
        row(same, stamp, guid="guid-b"),        # re-published, new guid
    ]
    reports = F.filter_reports_for_today(rows, today=TODAY_IN)
    assert len(reports) == 1
    assert reports[0].session_key == "ASIAN_MARKET_WRAP"

    payload = F.build_session_analysis(rows, now=NOW)
    assert len(payload["boxes"]) == 5  # still exactly five boxes


# ---------------------------------------------------------------------------
# Test 5 - a report must not leak into another session's box
# ---------------------------------------------------------------------------
def test_reports_do_not_cross_into_other_boxes():
    rows = [
        row("Morning Juice - US Session Prep (2nd October)",
            datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)),
        row("US Stocks Rise as AI Strength Offsets Bond Volatility "
            "- Europe Market Wrap",
            datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)),
        row("Europe Market Prep: DAX Futures Edge Higher",
            datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)),
        row("Asia Market Wrap: Nikkei Rises",
            datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)),
        row("Wall Street Ends Mixed - US Market Wrap",
            datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)),
    ]
    payload = F.build_session_analysis(rows, now=NOW)
    found = {b["session_key"]: b["found"] for b in payload["boxes"]}
    assert all(found.values()), found

    # The Europe wrap headline starts with "US" but must land in Europe only.
    by_key = {b["session_key"]: b for b in payload["boxes"]}
    assert "Europe Market Wrap" in by_key["EUROPE_MARKET_WRAP"]["report_title"]
    assert "Europe Market Wrap" not in by_key["US_MARKET_WRAP"]["report_title"]


def test_non_session_headlines_are_ignored():
    rows = [
        row("Fed's Jefferson: inflation has resulted from a cascade of shocks",
            datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)),
        row("China CPI slows in August",
            datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)),
    ]
    payload = F.build_session_analysis(rows, now=NOW)
    assert all(b["found"] is False for b in payload["boxes"])


# ---------------------------------------------------------------------------
# Test 6 - Bullish % + Bearish % must equal 100 for EVERY asset
# ---------------------------------------------------------------------------
def test_every_asset_sums_to_exactly_100():
    rows = [
        row("US Stocks End Higher as Yields Fall - US Market Wrap",
            datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc),
            body="The Fed is expected to cut rates as inflation cools. "
                 "Treasury yields fell and the dollar weakened. Gold surged."),
    ]
    payload = F.build_session_analysis(rows, now=NOW)
    for box in payload["boxes"]:
        for a in box["assets"]:
            total = round(a["bull_pct"] + a["bear_pct"], 6)
            assert total == 100.0, (box["session_key"], a["asset"], total)
            assert 0.0 <= a["bull_pct"] <= 100.0
            assert 0.0 <= a["bear_pct"] <= 100.0


def test_empty_box_still_sums_to_100():
    payload = F.build_session_analysis([], now=NOW)
    for box in payload["boxes"]:
        for a in box["assets"]:
            assert a["bull_pct"] + a["bear_pct"] == 100.0


@pytest.mark.parametrize("bull,bear", [
    (0, 0), (1, 0), (0, 1), (5, 5), (10, 0), (0, 10), (3, 7), (99, 1),
])
def test_score_conversion_always_sums_to_100(bull, bear):
    b, s = F.bullish_bearish_from_score(bull, bear)
    assert round(b + s, 6) == 100.0


def test_no_evidence_returns_exactly_even_split():
    assert F.bullish_bearish_from_score(0, 0) == (50.0, 50.0)


def test_weak_evidence_stays_near_even():
    b, s = F.bullish_bearish_from_score(1, 0, evidence=0.1)
    assert 50.0 < b < 60.0
    assert round(b + s, 6) == 100.0


def test_conviction_is_capped_so_signal_is_never_absurd():
    b, s = F.bullish_bearish_from_score(1000, 0, evidence=1.0)
    assert b <= 50.0 + F.CONVICTION_CAP * 100.0 + 0.1
    assert round(b + s, 6) == 100.0


def test_dovish_report_leans_bullish_for_gold():
    from app.models.enums import Asset
    scored = F.analyse_report_text(
        "Fed signals rate cut as inflation cools; Treasury yields fell and "
        "the dollar weakened; gold surged on safe haven demand.")
    gold = scored[Asset.GOLD]
    assert gold["bull_pct"] > gold["bear_pct"]


def test_hawkish_report_leans_bearish_for_gold():
    from app.models.enums import Asset
    scored = F.analyse_report_text(
        "Fed signals higher for longer as inflation stays hot; Treasury yields "
        "rose and the dollar climbed; gold slumped on rate hike fears.")
    gold = scored[Asset.GOLD]
    assert gold["bear_pct"] > gold["bull_pct"]


def test_report_with_no_asset_content_stays_even():
    from app.models.enums import Asset
    scored = F.analyse_report_text("A short note about the weather today.")
    for asset in (Asset.GOLD, Asset.SILVER, Asset.BITCOIN):
        assert scored[asset]["bull_pct"] == 50.0
        assert scored[asset]["bear_pct"] == 50.0





# ---------------------------------------------------------------------------
# Test 7 - no Neutral / Mixed category may exist in the payload
# ---------------------------------------------------------------------------
FORBIDDEN = ("neutral", "mixed", "sideways", "unclear", "no bias")


def test_payload_contains_no_third_sentiment_category():
    rows = [
        row("Asia Market Wrap: Stocks Mixed on Rate Uncertainty",
            datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
            body="Equities traded mixed while the Fed weighed rate cuts."),
    ]
    payload = F.build_session_analysis(rows, now=NOW)
    for box in payload["boxes"]:
        for a in box["assets"]:
            # Only these keys may exist on an asset row.
            assert set(a) == {"asset", "label", "bull_pct", "bear_pct",
                              "drivers"}
            for bad in FORBIDDEN:
                assert bad not in a["label"].lower()


def test_module_source_declares_no_neutral_output():
    import inspect
    src = inspect.getsource(F.analyse_report_text)
    for bad in ("neutral", "mixed"):
        assert bad not in src.lower()


# ---------------------------------------------------------------------------
# Test 8 - only FinancialJuice data may be used
# ---------------------------------------------------------------------------
def test_non_financialjuice_sources_are_rejected():
    rows = [
        row("US Stocks End Higher - US Market Wrap",
            datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc),
            source="CNBC Finance"),
        row("Asia Market Wrap: Nikkei Rises",
            datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc),
            source="CoinDesk"),
    ]
    payload = F.build_session_analysis(rows, now=NOW)
    assert all(b["found"] is False for b in payload["boxes"])


def test_source_matching_is_forgiving_of_spacing():
    for s in ("Financial Juice", "financialjuice", "FinancialJuice",
              "FINANCIAL JUICE"):
        assert F.is_financial_juice(s) is True
    for s in ("CNBC Finance", "Investing.com", "CoinDesk", "", None):
        assert F.is_financial_juice(s) is False


# ---------------------------------------------------------------------------
# Ordering - the five boxes must never reorder
# ---------------------------------------------------------------------------
def test_box_order_is_exactly_as_specified():
    assert list(F.SESSION_ORDER) == [
        "US_MARKET_WRAP",
        "ASIAN_MARKET_WRAP",
        "EUROPE_MARKET_PREP",
        "EUROPE_MARKET_WRAP",
        "US_SESSION_PREP",
    ]
    payload = F.build_session_analysis([], now=NOW)
    assert [b["session_key"] for b in payload["boxes"]] == list(F.SESSION_ORDER)
    assert [b["title"] for b in payload["boxes"]] == [
        "US Market Wrap", "Asian Market Wrap", "Europe Market Prep",
        "Europe Market Wrap", "US Session Prep",
    ]


def test_every_box_has_the_three_assets_in_order():
    payload = F.build_session_analysis([], now=NOW)
    for box in payload["boxes"]:
        assert [a["asset"] for a in box["assets"]] == [
            "XAUUSD", "XAGUSD", "BTCUSD"]


# ---------------------------------------------------------------------------
# Timestamp display - always IST
# ---------------------------------------------------------------------------
def test_published_timestamp_is_rendered_in_ist():
    rows = [row("Asia Market Wrap: Nikkei Rises",
                datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc))]  # 09:30 IST
    payload = F.build_session_analysis(rows, now=NOW)
    box = {b["session_key"]: b for b in payload["boxes"]}["ASIAN_MARKET_WRAP"]
    assert box["published_ist"] == "Published: 02 Oct 2026, 09:30 IST"


def test_india_stamp_handles_missing_timestamp():
    assert "unavailable" in F.india_stamp(None).lower()


# ---------------------------------------------------------------------------
# Deterministic classification of every required report name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("title,expected", [
    ("US Market Wrap", "US_MARKET_WRAP"),
    ("Morning Juice - US Market Wrap (2nd October)", "US_MARKET_WRAP"),
    ("Asian Market Wrap", "ASIAN_MARKET_WRAP"),
    ("Asia Market Wrap: Nikkei Rises Sharply", "ASIAN_MARKET_WRAP"),
    ("Europe Market Prep", "EUROPE_MARKET_PREP"),
    ("Europe Market Prep: What to Watch", "EUROPE_MARKET_PREP"),
    ("Europe Market Wrap", "EUROPE_MARKET_WRAP"),
    ("Europe Market Wrap: Stocks End Higher", "EUROPE_MARKET_WRAP"),
    ("US Session Prep", "US_SESSION_PREP"),
    ("Morning Juice - US Session Prep (2nd October)", "US_SESSION_PREP"),
])
def test_each_required_report_classifies_to_its_own_box(title, expected):
    assert F.classify_fj_session(title) == expected

def test_ordinary_wire_headlines_cannot_classify_as_a_session():
    assert F.classify_fj_session("Fed's Jefferson: inflation cools") is None
    assert F.classify_fj_session("") is None
# ---------------------------------------------------------------------------
# TOP DRIVERS & KEY RISKS - read off each box's own session report
# ---------------------------------------------------------------------------
US_WRAP_BODY = (
    "Gold surged as the dollar weakened and safe haven demand lifted "
    "Treasuries. Equities fell while oil climbed, with geopolitical "
    "tensions escalating around the Strait of Hormuz."
)


def test_every_box_always_exposes_drivers_and_risks():
    """The key must exist on all five boxes, found or not, so the UI never
    has to special-case a missing report."""
    payload = F.build_session_analysis([], now=NOW)
    for box in payload["boxes"]:
        assert "drivers" in box and "risks" in box
        assert box["drivers"] == [] and box["risks"] == []


def test_box_drivers_and_risks_are_extracted_from_the_report():
    rows = [row("Gold Surges as Dollar Weakens - US Market Wrap",
                datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc),
                body=US_WRAP_BODY)]
    box = {b["session_key"]: b for b in
           F.build_session_analysis(rows, now=NOW)["boxes"]}["US_MARKET_WRAP"]
    assert box["drivers"], "a report with supportive lines must yield drivers"
    assert box["risks"], "geopolitical escalation must be flagged as a risk"
    # Everything shown is the report's own wording, never invented text.
    joined = " ".join(box["drivers"] + box["risks"]).lower()
    for phrase in ("gold surged", "geopolitical", "hormuz"):
        assert phrase in joined


def test_geopolitical_risk_lands_in_risks_not_drivers():
    """'escalating the war' is a risk; it must never be shown as a driver."""
    drivers, risks = F.extract_drivers_and_risks(
        "Tensions escalated sharply and the Fed signaled a rate hike, with "
        "sanctions on crude exports raising the risk of a severe downturn.")
    assert risks
    assert not any("escalat" in d.lower() or "sanction" in d.lower()
                   for d in drivers)


def test_dovish_language_lands_in_drivers_not_risks():
    drivers, risks = F.extract_drivers_and_risks(
        "The Fed signaled a rate cut as inflation cooled, and safe haven "
        "demand surged with the dollar sliding lower on the news.")
    assert drivers
    assert not any("rate cut" in r.lower() for r in risks)


def test_scheduled_docket_entries_count_as_forward_looking_risks():
    """A print that has not happened yet is a risk, not a realised move."""
    body = ("Sentiment\nOil steadied after a two-day gain.\n"
            "Docket\n05:00 ET\nEurozone CPI\n"
            "YoY \u2013 Forecast: 3.7% | Prior: 3.2%")
    _drivers, risks = F.extract_drivers_and_risks(body)
    assert any("Eurozone CPI" in r for r in risks)
    # The bare clock line is formatting, not an event.
    assert not any(r.strip().startswith("05:00") for r in risks)


def test_speakers_section_is_treated_as_ahead_of_time_risk():
    _drivers, risks = F.extract_drivers_and_risks(
        "Speakers\nFed Vice Chair Philip Jefferson")
    assert any("Jefferson" in r for r in risks)


def test_report_with_no_directional_content_flags_nothing():
    """No evidence must render as empty, never as a fabricated driver."""
    drivers, risks = F.extract_drivers_and_risks(
        "Good Morning Traders. Here is what to look out for today.")
    assert drivers == [] and risks == []


def test_driver_and_risk_items_are_clipped_to_a_readable_width():
    drivers, risks = F.extract_drivers_and_risks(
        "Gold surged as the dollar slid and the Fed signalled a rate cut " * 12,
        title="Gold Surges - US Market Wrap")
    for item in drivers + risks:
        assert len(item) <= F.MAX_ITEM_CHARS


def test_title_is_scored_separately_from_the_first_body_sentence():
    """The headline must not be spliced onto the opening sentence."""
    drivers, _risks = F.extract_drivers_and_risks(
        "Equities fell as oil advanced on the day.",
        title="Stocks Surge as Yields Fall - US Market Wrap")
    assert drivers
    assert any("Stocks Surge as Yields Fall" in d for d in drivers)
    assert not any("US Market Wrap Equities fell" in d for d in drivers)


def test_context_drivers_do_not_leak_into_the_report_notes():
    """Section B / calendar evidence shapes the score, but TOP DRIVERS and
    KEY RISKS must stay FinancialJuice-report-only, matching the source rule."""
    from app.models.enums import Direction, EconomicImportance, ImpactStrength

    ctx = F.SessionContext()
    ctx.news_count = 1
    entry = ctx.news.setdefault(Asset.GOLD, F.AssetContext())
    entry.bull, entry.bear, entry.drivers = 6.0, 0.0, ["Some Section B headline"]

    rows = [row("US Stocks End Higher - US Market Wrap",
                datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc), body=US_WRAP_BODY)]
    box = {b["session_key"]: b for b in
           F.build_session_analysis(rows, now=NOW, context=ctx)["boxes"]
           }["US_MARKET_WRAP"]
    assert "Some Section B headline" not in " ".join(box["drivers"] + box["risks"])


def test_section_splitting_keeps_headings_as_boundaries():
    sections = F.split_report_sections(
        "Intro line.\nSentiment\nMarkets rallied broadly.\nDocket\n10:00 ET\nCPI")
    assert set(sections) == {"body", "sentiment", "docket"}
    assert "rallied" in sections["sentiment"]
    assert "rallied" not in sections["docket"]


def test_wraps_without_headings_land_entirely_in_body():
    """A MARKET WRAP is plain prose - nothing may be lost to a missing heading."""
    sections = F.split_report_sections(US_WRAP_BODY)
    assert list(sections) == ["body"]
    assert "Hormuz" in sections["body"]
    assert F.classify_fj_session("Unrelated: something happened") is None
