"""Unit tests for classification, relevance and session detection."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.analysis.asset_relevance import relevant_assets, relevance_for
from app.analysis.cue_rules import (
    detect_category,
    detect_direction,
    detect_importance,
    detect_metal_character,
    detect_report_kind,
    detect_verification,
)
from app.analysis.news_classifier import heuristic_analyze
from app.market.session_detector import (
    detect_session, next_session, previous_session, session_relevance,
)
from app.models.enums import (
    Asset, Direction, EconomicImportance, NewsCategory, ReportKind,
    SessionName, Verification,
)

NY = ZoneInfo("America/New_York")


# ---------------------------- classification ----------------------------
def test_hawkish_fed_is_bearish_gold():
    """Section 6: the canonical transmission must work."""
    r = heuristic_analyze(
        "Fed officials signal interest rates will stay higher for longer")
    gold = next(a for a in r.assets if a.asset is Asset.GOLD)
    assert gold.direction is Direction.BEARISH
    assert gold.strength.value in ("HIGH", "VERY_HIGH")
    assert gold.transmission


def test_dovish_signal_is_bullish_gold():
    r = heuristic_analyze("Fed announces surprise rate cut as inflation cools")
    gold = next(a for a in r.assets if a.asset is Asset.GOLD)
    assert gold.direction is Direction.BULLISH


def test_rising_yields_not_read_as_bullish():
    """Subject-aware: 'yields climb' is bearish despite an 'up' verb."""
    assert detect_direction(
        "treasury yields climb as traders price fewer cuts") is Direction.BEARISH


def test_falling_gold_is_bearish():
    assert detect_direction("gold falls to a three-month low") is Direction.BEARISH


def test_bitcoin_plunge_is_bearish():
    r = heuristic_analyze("Bitcoin plunges below key support after liquidations")
    btc = next(a for a in r.assets if a.asset is Asset.BITCOIN)
    assert btc.direction is Direction.BEARISH


def test_bitcoin_etf_inflow_is_bullish_btc():
    r = heuristic_analyze(
        "Spot Bitcoin ETFs record largest daily inflows since launch")
    btc = next(a for a in r.assets if a.asset is Asset.BITCOIN)
    assert btc.direction is Direction.BULLISH


def test_crypto_regulation_ignored_for_gold():
    """A crypto-regulatory story must not be scored as precious-metals news."""
    rel = relevance_for(NewsCategory.CRYPTO_REGULATION, Asset.GOLD,
                        "SEC approves new crypto custody rules")
    assert rel < 0.20


def test_china_story_matters_more_for_silver_than_gold():
    text = "China factory output misses forecasts as demand stays soft"
    assert (relevance_for(NewsCategory.CHINA, Asset.SILVER, text)
            > relevance_for(NewsCategory.CHINA, Asset.GOLD, text))


def test_silver_dual_character_detected():
    assert detect_metal_character("Fed yields and China factory data") == "BOTH"
    assert detect_metal_character("China output and copper demand") == "INDUSTRIAL"
    assert detect_metal_character("Fed rate decision") == "PRECIOUS"


def test_relevant_assets_routing():
    r = relevant_assets(NewsCategory.BITCOIN_ETF_FLOWS, "spot bitcoin etf flows")
    assert Asset.BITCOIN in r
    assert Asset.GOLD not in r


def test_report_kind_detection():
    assert detect_report_kind("Market wrap: gold pares losses") is ReportKind.WRAP
    assert detect_report_kind("Market prep: what to watch before the open") \
        is ReportKind.PREP
    assert detect_report_kind("Fed holds rates steady") is ReportKind.GENERAL


def test_verification_labels_rumours():
    assert detect_verification("Report: Fed to announce emergency cut") \
        is Verification.UNVERIFIED
    assert detect_verification("Fed announced emergency cut") \
        is Verification.CONFIRMED


def test_importance_escalation():
    assert detect_importance("FOMC decision announcement",
                             NewsCategory.CENTRAL_BANK) \
        is EconomicImportance.VERY_HIGH
    assert detect_importance("Single store sales tick",
                             NewsCategory.OTHER) is EconomicImportance.LOW


def test_category_leads_with_specific_driver():
    assert detect_category("China factory output disappoints") is NewsCategory.CHINA


def test_analysis_always_returns_reason_and_transmission():
    r = heuristic_analyze("Fed holds rates steady amid slowing growth")
    for a in r.assets:
        assert a.reason and a.transmission
        assert a.relevance > 0.0


# ----------------------------- sessions --------------------------------
@pytest.mark.parametrize("hour,expected", [
    (2, SessionName.ASIA), (9, SessionName.EUROPE),
    (14, SessionName.US), (19, SessionName.OFF_HOURS),
])
def test_session_detection(monkeypatch, hour, expected):
    monkeypatch.setattr("app.config.settings.market_timezone",
                        "America/New_York")
    when = datetime(2026, 6, 1, hour, 0, tzinfo=NY)
    assert detect_session(when) is expected


def test_us_session_boosts_fed_relevance(monkeypatch):
    monkeypatch.setattr("app.config.settings.market_timezone",
                        "America/New_York")
    assert (session_relevance(SessionName.US, NewsCategory.CENTRAL_BANK.value)
            > session_relevance(SessionName.ASIA, NewsCategory.CENTRAL_BANK.value))


def test_asia_session_boosts_china_relevance(monkeypatch):
    monkeypatch.setattr("app.config.settings.market_timezone",
                        "America/New_York")
    assert (session_relevance(SessionName.ASIA, NewsCategory.CHINA.value)
            > session_relevance(SessionName.US, NewsCategory.CHINA.value))


def test_session_cycling():
    now = datetime(2026, 6, 1, 14, 0, tzinfo=NY)
    assert previous_session(now) in SessionName
    assert next_session(now) in SessionName