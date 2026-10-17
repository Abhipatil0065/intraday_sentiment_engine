"""Session-aware global equity card: title mapping + region filtering."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.analysis.equity_card import (
    _collapse_cross_asset,
    _matches_region,
    build_equity_card,
    GLOBAL_MACRO_WEIGHT,
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

NY = ZoneInfo("America/New_York")

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
    us = _eq_signal("Apple beats earnings estimates as revenue surges")
    eu = _eq_signal("DAX climbs as ECB holds steady")
    # Session-specific headlines match only their own session (no global-keyword
    # overlap), so they do not leak into other regions.
    assert [s.title for s in region_signals([asia, us, eu], SessionName.ASIA)] == [asia.title]
    assert [s.title for s in region_signals([asia, us, eu], SessionName.US)] == [us.title]
    assert [s.title for s in region_signals([asia, us, eu], SessionName.EUROPE)] == [eu.title]


def test_non_equities_category_never_counts():
    other = build_signal(
        asset=Asset.GOLD, title="Nikkei rallies on strong demand",
        direction=Direction.BULLISH, published_at=NOW, relevance=0.7,
        category=NewsCategory.CHINA)
    assert region_signals([other], SessionName.ASIA) == []


def test_empty_region_returns_neutral_card():
    # A headline that matches neither ASIA keywords nor global keywords ->
    # no signals for ASIA -> neutral carry-forward card.
    us_only = [_eq_signal("Apple beats earnings estimates as revenue surges")]
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


# ------------------------- end-to-end session switch -------------------------
# The user-visible contract: ONE 4th card whose region/title/symbol/bias follow
# the active session, switching the MOMENT the next session opens, even when
# the previous session's window has not expired (newest session wins).
def _all_regions_pool():
    return [
        _eq_signal("Nikkei rallies as Bank of Japan holds rates"),
        _eq_signal("DAX climbs as ECB holds steady"),
        _eq_signal("Apple beats earnings estimates as revenue surges"),
    ]


@pytest.mark.parametrize("ny_hour,expected_region,expected_title", [
    (21, "ASIA", "ASIA EQUITY"),   # Asia running -> Asia bias
    (2, "ASIA", "ASIA EQUITY"),    # still Asia (its window ends 04:00)
    (3, "EUROPE", "EUROPE EQUITY"),  # Europe opens -> Europe, Asia tail ignored
    (7, "EUROPE", "EUROPE EQUITY"),
    (8, "US", "US EQUITY"),        # US opens while Europe runs -> US wins
    (14, "US", "US EQUITY"),
])
def test_card_follows_active_session(ny_hour, expected_region, expected_title):
    when = datetime(2026, 10, 6, ny_hour, 0, tzinfo=NY)
    card = build_equity_card(_all_regions_pool(), now=when,
                             session=equity_session(when))
    assert card.region == expected_region
    assert card.title == expected_title
    assert EQUITY_SESSION_MAP[SessionName(expected_region)]["title"] == expected_title
    # The region actually decided the verdict: only its own region scored.
    assert card.no_data is False
    assert card.signal_count == 1


def test_session_switch_keeps_regions_independent():
    pool = _all_regions_pool()
    when_eu = datetime(2026, 10, 6, 6, 0, tzinfo=NY)
    when_us = datetime(2026, 10, 6, 10, 0, tzinfo=NY)
    eu = build_equity_card(pool, now=when_eu, session=equity_session(when_eu))
    us = build_equity_card(pool, now=when_us, session=equity_session(when_us))
    assert eu.region == "EUROPE" and us.region == "US"
    # Different signal sets per region proof: each card saw exactly one region.
    assert eu.signal_count == 1 and us.signal_count == 1


# ---------------- one story, one vote (cross-asset duplication) -------------
# Regression (2026-10-13): one article is classified against every asset it
# could touch, so the pipeline built one WeightedSignal PER IMPACT ROW, all
# sharing one title. cluster_signals groups by asset first, so the copies never
# merged, and the equity card re-tagged them all as Asset.EQUITY - the same
# (e.g. bullish) headline then appeared twice in the driver list and counted
# twice in the score.
_DUP_TITLE = "S&P 500 rallies as Fed signals cuts"


def _copy(asset, direction=Direction.BULLISH, event_key="evt-1",
          published_at=NOW, relevance=0.8):
    return build_signal(
        asset=asset, title=_DUP_TITLE, direction=direction,
        strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
        published_at=published_at, relevance=relevance, persistence=0.6,
        category=NewsCategory.EQUITIES, event_key=event_key,
        cluster_id=event_key)


def _same_story_pool():
    """One article, four impact rows (gold / silver / bitcoin / equity)."""
    return [_copy(a) for a in (Asset.GOLD, Asset.SILVER,
                               Asset.BITCOIN, Asset.EQUITY)]


def test_same_story_scores_once_across_assets():
    pool = _same_story_pool()
    card = build_equity_card(pool, now=NOW, session=SessionName.US)
    assert card.no_data is False
    assert card.bias == "BULLISH"
    # ONE vote, not one per impact row.
    assert card.signal_count == 1
    assert card.contributing_articles == 1
    assert [d["title"] for d in card.drivers] == [_DUP_TITLE]


def test_multi_asset_duplicate_matches_single_signal_card():
    # The collapsed card must be indistinguishable from the same story
    # sent in once: no double-counted score, no inflated confidence.
    pool = _same_story_pool()
    dup = build_equity_card(pool, now=NOW, session=SessionName.US)
    lone = build_equity_card([_copy(Asset.GOLD)], now=NOW,
                             session=SessionName.US)
    assert dup.score == lone.score
    assert dup.raw_score == lone.raw_score
    assert dup.confidence == lone.confidence
    assert dup.signal_count == lone.signal_count == 1


def test_opposing_readings_stay_separate_votes():
    # A hawkish and a dovish reading of one event are two readings (the same
    # rule cluster_signals applies), so both survive the collapse.
    bull = _copy(Asset.GOLD, direction=Direction.BULLISH)
    bear = _copy(Asset.EQUITY, direction=Direction.BEARISH)
    assert len(_collapse_cross_asset([bull, bear], now=NOW)) == 2


def test_duplicate_guid_same_headline_merges_inside_window():
    a = _copy(Asset.GOLD, event_key="evt-a", published_at=NOW)
    b = _copy(Asset.EQUITY, event_key="evt-b",
              published_at=datetime(2026, 10, 6, 14, 10, tzinfo=timezone.utc))
    assert len(_collapse_cross_asset([a, b], now=NOW)) == 1


def test_repeated_story_hours_later_stays_two_votes():
    a = _copy(Asset.GOLD, event_key="evt-a", published_at=NOW)
    b = _copy(Asset.EQUITY, event_key="evt-b",
              published_at=datetime(2026, 10, 6, 20, 10, tzinfo=timezone.utc))
    assert len(_collapse_cross_asset([a, b], now=NOW)) == 2


def test_collapse_keeps_highest_weight_copy():
    weak = _copy(Asset.GOLD, relevance=0.2)
    strong = _copy(Asset.EQUITY, relevance=0.95)
    out = _collapse_cross_asset([weak, strong], now=NOW)
    assert len(out) == 1
    assert out[0].relevance == 0.95


def test_collapse_does_not_mutate_inputs():
    src = _copy(Asset.GOLD, relevance=0.1)
    other = _copy(Asset.SILVER, relevance=0.9)
    before = src.relevance
    _collapse_cross_asset([src, other], now=NOW)
    assert src.relevance == before
    assert other.relevance == 0.9


# ---------------- global macro keyword weighting (M4 fix) ----------------


def test_asia_keyword_full_weight():
    """An ASIA-specific headline gets match weight 1.0 for the ASIA session."""
    sig = _eq_signal("Nikkei rallies as Bank of Japan holds rates")
    matched = region_signals([sig], SessionName.ASIA)
    assert len(matched) == 1
    # EQUITIES category has equity relevance 1.00, times match weight 1.0.
    assert matched[0].relevance == pytest.approx(1.0)


def test_global_macro_partial_weight():
    """A US CPI headline matches only global keywords, so it reaches every
    session at GLOBAL_MACRO_WEIGHT (0.65)."""
    sig = _eq_signal("US CPI accelerates as inflation prints above forecast")
    for session in (SessionName.ASIA, SessionName.EUROPE, SessionName.US):
        matched = region_signals([sig], session)
        assert len(matched) == 1, f"{session.value} should include the CPI headline"
        # EQUITIES category has equity relevance 1.00, times GLOBAL_MACRO_WEIGHT.
        assert matched[0].relevance == pytest.approx(GLOBAL_MACRO_WEIGHT), (
            f"{session.value} should weight global macro at {GLOBAL_MACRO_WEIGHT}")


def test_unrelated_headline_no_match():
    """A headline about a specific single stock matches no session keywords
    (session-specific or global) and is excluded from every card."""
    sig = _eq_signal("Walmart appoints new chief supply chain officer")
    for session in (SessionName.ASIA, SessionName.EUROPE, SessionName.US):
        assert region_signals([sig], session) == [], (
            f"{session.value} should not match a single-stock headline")


def test_equity_card_has_both_driver_sides():
    """The equity payload must expose both bull_drivers and bear_drivers
    (each a list, even when empty) so the frontend can render both sides."""
    from app.database import init_db, session_scope
    from app.main import build_payload

    init_db()
    with session_scope() as s:
        payload = build_payload(s)

    equity = payload.get("equity", {})
    assert "bull_drivers" in equity, "equity card must have bull_drivers"
    assert "bear_drivers" in equity, "equity card must have bear_drivers"
    assert isinstance(equity["bull_drivers"], list)
    assert isinstance(equity["bear_drivers"], list)

