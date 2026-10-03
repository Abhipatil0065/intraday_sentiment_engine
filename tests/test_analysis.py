"""Unit tests for classification, relevance and session detection."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.analysis import heuristics as app_heuristics
from app.analysis.asset_relevance import relevant_assets, relevance_for
from app.analysis.text_match import keyword_position
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


# ------------------ word-boundary matching regressions -------------------
def test_keywords_do_not_match_inside_other_words():
    """
    Regression: "Novig CREDITS Sydney Sweeney..." was classified as
    BANKING_FINANCIAL_STRESS because the keyword "credit" was substring-matched
    inside "credits". The headline then inherited HIGH importance and a
    0.78/0.66/0.74 relevance to gold/silver/bitcoin, and a bullish read from
    the word "surge" - surfacing a sports-betting marketing story as a top
    driver for all three assets.
    """
    title = ("Novig credits Sydney Sweeney-backed campaign for platform's "
             "surge in growth")
    r = heuristic_analyze(title)

    assert r.category is NewsCategory.OTHER
    # Routed to NO asset: there is no transmission path to any of the three.
    assert r.assets == []
    assert r.relevance_score == 0.0


def test_unclassified_item_never_forced_onto_an_asset():
    """relevant_assets must be allowed to return an empty list."""
    assert relevant_assets(NewsCategory.OTHER, "a local sports story") == []


def test_other_category_stays_below_min_relevance():
    """
    OTHER must sit below MIN_RELEVANCE so an unclassifiable item cannot reach
    the assets on category weight alone. An explicit asset mention still lifts
    it back over the line.
    """
    assert (relevance_for(NewsCategory.OTHER, Asset.GOLD, "unrelated item")
            < 0.20)
    # ...but naming the asset explicitly is still enough.
    assert (relevance_for(NewsCategory.OTHER, Asset.GOLD, "gold spikes sharply")
            >= 0.20)


@pytest.mark.parametrize("text,bad_keyword", [
    ("Novig credits Sydney Sweeney", "credit"),
    ("Analysts warn of a slowdown", "war"),
    ("Shares of the bank rose", "ban"),
    ("A downtown opening drew crowds", "downtown"),
    ("Undermining confidence in the Fed", "mine"),
])
def test_category_keywords_require_whole_words(text, bad_keyword):
    """No category may be triggered by a fragment of an unrelated word."""
    from app.analysis.cue_rules import detect_category as _dc
    from app.analysis.heuristics import CATEGORY_KEYWORDS

    hits = [c.value for c, kws in CATEGORY_KEYWORDS.items()
            if c is not NewsCategory.OTHER and bad_keyword in kws]
    if hits:
        assert _dc(text) not in hits


def test_cues_still_match_inflections():
    """Word-boundary matching must not break ordinary verb inflections."""
    from app.analysis.cue_rules import score_cues, BULLISH_CUES, BEARISH_CUES
    assert score_cues("bitcoin surges past resistance", BULLISH_CUES) > 0
    assert score_cues("bitcoin plunges below support", BEARISH_CUES) > 0
    assert score_cues("etf inflows accelerate", BULLISH_CUES) > 0


def test_repeated_cue_cannot_dominate():
    """A cue repeated many times must be capped, not multiplied without bound."""
    from app.analysis.cue_rules import score_cues, BULLISH_CUES
    once = score_cues("gold rally", BULLISH_CUES)
    many = score_cues("gold rally " * 20, BULLISH_CUES)
    assert many <= once * 2 + 0.001


def test_stem_keywords_still_match_inflections():
    """Deliberate stems (liquidat/escalat/treasur) keep working."""
    assert detect_category("Bitcoin slides after fresh liquidations") \
        is NewsCategory.CRYPTO_MARKET_FLOWS
    assert detect_category("Escalating tension in the region") \
        is NewsCategory.GEOPOLITICS


# --------------------- keyword matching matrix --------------------------
# Guards the three-way policy: declared stem -> prefix match,
# plural-exempt -> whole word only, otherwise -> whole word or plural.
KEYWORD_POLICY = dict(
    stems=app_heuristics.STEM_KEYWORDS,
    plural_exempt=app_heuristics.PLURAL_EXEMPT_KEYWORDS,
)


@pytest.mark.parametrize("text,keyword,expected", [
    # the original defect
    ("Novig credits Sydney Sweeney", "credit", False),
    ("credit crunch hits lenders", "credit", True),
    # plural-exempt: whole word only
    ("toward record highs", "war", False),
    ("warrant exercise", "war", False),
    ("war erupts in the region", "war", True),
    ("wars break out", "war", False),
    ("bank stress", "ban", False),
    ("banks cut rates", "ban", False),
    ("regulators ban bitcoin", "ban", True),
    ("second quarter", "sec", False),
    ("sec approves spot etf", "sec", True),
    ("federated funds", "fed", False),
    ("fed officials speak", "fed", True),
    ("usdt peg slips", "usd", False),
    ("usd rises sharply", "usd", True),
    # plural tolerated - these must keep matching
    ("Russia launches massive strikes", "strike", True),
    ("Iran sanctions tighten", "sanction", True),
    ("Treasury yields climb", "yield", True),
    ("Gold ETFs record inflows", "etf", True),
    ("oil prices surge", "oil", True),
    ("banks brace for defaults", "bank", True),
    # declared stems
    ("fresh liquidations hit crypto", "liquidat", True),
    ("escalating tensions", "escalat", True),
    ("treasury auction", "treasur", True),
    # substring traps that must not fire
    ("undermining confidence", "mine", False),
    ("software stocks rally", "war", False),
])
def test_keyword_matching_policy(text, keyword, expected):
    got = keyword_position(text.lower(), keyword, **KEYWORD_POLICY) != -1
    assert got is expected


@pytest.mark.parametrize("text,expected", [
    ("Novig credits Sydney Sweeney campaign surge growth", NewsCategory.OTHER),
    ("Russia launches massive strikes on Ukraine energy grid", NewsCategory.GEOPOLITICS),
    ("Iran sanctions tighten on oil exports", NewsCategory.GEOPOLITICS),
    ("Gold ETFs record inflows", NewsCategory.ETF_FLOWS),
    ("Analysts warn of slowdown", NewsCategory.OTHER),
    ("Wells Fargo raises Micron price target", NewsCategory.OTHER),
    ("Fed officials hawkish on inflation", NewsCategory.CENTRAL_BANK),
    ("China factory output disappoints", NewsCategory.CHINA),
    ("Bitcoin slides after fresh liquidations", NewsCategory.CRYPTO_MARKET_FLOWS),
    ("Treasury yields climb to 5 percent", NewsCategory.TREASURY_YIELDS),
])
def test_category_detection_end_to_end(text, expected):
    assert detect_category(text) is expected


def test_genuine_macro_events_still_reach_very_high():
    """
    The fix must not silence real signal. Every one of these was previously
    promoted to VERY_HIGH by the substring "war" matching inside "toward",
    "warrant", "warm" and "warn" - 51 false promotions in one day's corpus.
    Real macro events must still reach the top importance band.
    """
    for text in ("FOMC announces rate decision",
                 "US inflation CPI comes in hotter than expected",
                 "Israel launches missile strike, ceasefire collapses",
                 "Major bank defaults, liquidity crisis hits markets"):
        assert detect_importance(text, detect_category(text)) \
            is EconomicImportance.VERY_HIGH


def test_filler_items_are_still_low_importance():
    """LOW triggers must survive the whole-word policy."""
    for text in ("Best shopping guide for holiday deals",
                 "Weekly horoscope and recipe roundup",
                 "Limited time giveaway this holiday"):
        assert detect_importance(text, NewsCategory.OTHER) \
            is EconomicImportance.LOW


# ---------------------------- retention --------------------------------
def _mk_article(session, *, title: str, published, is_today: bool = True):
    from app.models.db_models import NewsArticle
    row = NewsArticle(
        guid=f"g-{title[:24]}-{published.isoformat()}",
        source="TestWire", source_quality="TIER_4_OTHER",
        url=f"https://example.invalid/{title[:12]}",
        title=title, body="", summary="",
        published_at_utc=published, is_today=is_today,
    )
    session.add(row)
    session.flush()
    return row


def _mk_impact(session, article_id, asset="XAUUSD", direction="BULLISH"):
    from app.models.db_models import AssetImpact
    row = AssetImpact(article_id=article_id, event_id=None, asset=asset,
                      direction=direction, strength="LOW", relevance=0.5)
    session.add(row)
    session.flush()
    return row


@pytest.fixture()
def db(tmp_path):
    """
    Throwaway SQLite DB. Usage: ``session = db()``.

    ``PRAGMA foreign_keys=ON`` is set because SQLite ignores ``ON DELETE
    CASCADE`` by default - without it the cascade test would pass or fail for
    the wrong reason. app/database.py enables the same pragma in production.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from app.models.db_models import Base

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False,
                           future=True)

    def _factory():
        return Session()

    return _factory


def test_retention_keeps_today_and_yesterday_only(db):
    """keep_days=2 must drop anything published before yesterday's midnight."""
    from datetime import datetime, timedelta, timezone
    from app.services.pipeline import purge_old_news
    from app.models.db_models import NewsArticle
    from app.utils import timeutils as T

    now = T.now_utc()
    session = db()
    # 1: today, 2: yesterday, 3: two days ago, 4: five days ago
    keep_ids = [
        _mk_article(session, title=f"today{i}",
                    published=now - timedelta(hours=i)).id
        for i in (1, 2)
    ]
    yday = _mk_article(session, title="yesterday",
                       published=now - timedelta(days=1)).id
    drop_ids = [
        _mk_article(session, title=f"old{i}",
                    published=now - timedelta(days=2 + i)).id
        for i in range(3)
    ]

    res = purge_old_news(session, keep_days=2)
    session.commit()

    surviving = {a.id for a in session.query(NewsArticle).all()}
    assert keep_ids[0] in surviving
    assert yday in surviving
    for gone in drop_ids:
        assert gone not in surviving
    assert res["articles"] == 3


def test_retention_cascades_to_child_rows(db):
    """Deleting an article must remove its impacts, not orphan them."""
    from datetime import timedelta
    from app.services.pipeline import purge_old_news
    from app.models.db_models import AssetImpact
    from app.utils import timeutils as T

    now = T.now_utc()
    session = db()
    old = _mk_article(session, title="old-with-impact",
                      published=now - timedelta(days=4))
    new = _mk_article(session, title="new-with-impact", published=now)
    _mk_impact(session, old.id)
    _mk_impact(session, new.id)

    purge_old_news(session, keep_days=2)
    session.commit()

    ids = {a.id for a in session.query(AssetImpact).all()}
    assert new.id in ids
    assert old.id not in ids


def test_retention_sweeps_orphaned_events(db):
    """news_event does not cascade from the article, so it is swept separately."""
    from datetime import timedelta
    from sqlalchemy import select
    from app.services.pipeline import purge_old_news
    from app.models.db_models import ArticleEvent, NewsArticle, NewsEvent
    from app.utils import timeutils as T

    now = T.now_utc()
    session = db()
    old = _mk_article(session, title="old-event-holder",
                      published=now - timedelta(days=4))
    keep = _mk_article(session, title="keep-event-holder", published=now)

    for art, key in ((old, "ev-old"), (keep, "ev-keep")):
        session.add(NewsEvent(event_key=key, canonical_headline=art.title))
        session.flush()
        ev = session.execute(select(NewsEvent).where(
            NewsEvent.event_key == key)).scalar_one()
        session.add(ArticleEvent(article_id=art.id, event_id=ev.id,
                                similarity=1.0))
    session.flush()

    purge_old_news(session, keep_days=2)
    session.commit()

    keys = {e.event_key for e in session.query(NewsEvent).all()}
    assert "ev-keep" in keys
    assert "ev-old" not in keys


def test_retention_zero_disables_purging(db):
    """NEWS_RETENTION_DAYS=0 must keep everything."""
    from datetime import timedelta
    from app.services.pipeline import purge_old_news
    from app.models.db_models import NewsArticle
    from app.utils import timeutils as T

    now = T.now_utc()
    session = db()
    _mk_article(session, title="ancient", published=now - timedelta(days=400))
    before = session.query(NewsArticle).count()

    res = purge_old_news(session, keep_days=0)
    session.commit()
    assert res["articles"] == 0
    assert session.query(NewsArticle).count() == before


def test_retention_default_setting_is_two_days():
    from app.config import settings
    assert settings.news_retention_days == 2


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