"""Session-aware global equity card: title mapping + region filtering."""
from datetime import datetime, timezone

from app.analysis.equity_card import (
    build_equity_card,
    neutral_card,
    region_signals,
)
from app.analysis.signal_factory import build_signal
from app.market.session_detector import (
    EQUITY_SESSION_MAP,
    equity_keywords,
    equity_session,
    equity_title,
)
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    SessionName,
)

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _eq_signal(title, direction=Direction.BULLISH):
    return build_signal(
        asset=Asset.BITCOIN, title=title, direction=direction,
        strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
        published_at=NOW, relevance=0.7, persistence=0.6,
        category=NewsCategory.EQUITIES)


def test_session_to_title_mapping():
    assert equity_title(SessionName.ASIA) == "ASIA EQUITY"
    assert equity_title(SessionName.EUROPE) == "EUROPE EQUITY"
    assert equity_title(SessionName.US) == "US EQUITY"
    assert EQUITY_SESSION_MAP[SessionName.ASIA]["symbol"] == "NIKKEI / HSI"
    assert EQUITY_SESSION_MAP[SessionName.EUROPE]["symbol"] == "DAX / STOXX50"
    assert EQUITY_SESSION_MAP[SessionName.US]["symbol"] == "SPX / NDX"


def test_each_region_has_keywords():
    for session in (SessionName.ASIA, SessionName.EUROPE, SessionName.US):
        assert len(equity_keywords(session)) >= 5, session

def test_asia_headline_does_not_count_in_us():
    asia = _eq_signal("Nikkei rallies as Bank of Japan holds rates")
    us = _eq_signal("S&P 500 hits record as Fed signals cuts")
    assert [s.title for s in region_signals([asia, us], SessionName.ASIA)] == [asia.title]
    assert [s.title for s in region_signals([asia, us], SessionName.US)] == [us.title]
    eu = _eq_signal("DAX climbs as ECB holds rates steady")
    assert [s.title for s in region_signals([asia, us, eu], SessionName.EUROPE)] == [eu.title]


def test_non_equities_category_never_counts():
    other = build_signal(
        asset=Asset.GOLD, title="Nikkei rallies on strong demand",
        direction=Direction.BULLISH, published_at=NOW, relevance=0.7,
        category=NewsCategory.CHINA)
    assert region_signals([other], SessionName.ASIA) == []


def test_empty_region_returns_neutral_card():
    us_only = [_eq_signal("Nasdaq climbs as Fed cuts rates")]
    card = build_equity_card(us_only, now=NOW, session=SessionName.ASIA)
    assert card.no_data is True
    assert card.region == "ASIA"
    assert card.title == "ASIA EQUITY"
    assert card.score == 0.0
    # NO_BIAS: an empty region with no history returns NO_BIAS (rendered NEUTRAL 50/50)
    assert card.bias == "NO_BIAS"
    assert card.stale is True


def test_empty_signal_list_returns_neutral_not_error():
    card = build_equity_card([], now=NOW, session=SessionName.US)
    assert card.no_data is True
    assert card.region == "US"
    assert card.title == "US EQUITY"


def test_matching_region_scores_directionally():
    sigs = [_eq_signal("S&P 500 rallies as Fed cuts rates"),
            _eq_signal("Nasdaq jumps on strong earnings")]
    card = build_equity_card(sigs, now=NOW, session=SessionName.US)
    assert card.no_data is False
    assert card.region == "US"
    assert card.title == "US EQUITY"
    assert card.score > 0
    assert card.bias == "BULLISH"
    assert card.contributing_articles > 0


def test_off_hours_falls_back_to_previous_session():
    # OFF_HOURS title resolves via previous_session(), never crashes.
    assert equity_title(SessionName.OFF_HOURS) in (
        "ASIA EQUITY", "EUROPE EQUITY", "US EQUITY")
    card = neutral_card(equity_session())
    assert card.region in ("ASIA", "EUROPE", "US")

