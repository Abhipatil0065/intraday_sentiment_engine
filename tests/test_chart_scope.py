"""
CHART SCOPE: a story listed on the gold chart moves the gold card only.

WHAT IS BEING TESTED
====================
TradingView serves a per-symbol news feed, so the feed itself knows which
chart a story belongs to. The engine now uses that: the chart that listed the
story decides the cards the story may touch, and the classifier's own asset
list can no longer widen it.

    gold chart news   -> gold card only
    silver chart news -> silver card only
    bitcoin chart news-> bitcoin card only
    equity chart news -> equity card only

A story TWO charts list (TradingView lists a lot of stories on both the gold
and the silver chart) feeds BOTH cards - that is TradingView's own semantics,
and it is the default here. ``settings.chart_scope_single_owner`` turns it
into a strict one-card rule.

Sources without a chart (every RSS feed, FinancialJuice, Kitco) are
untouched: their rows keep the classifier's routing exactly as before.

The tests below drive the REAL ``_ingest_article`` with a stubbed classifier,
so the production branch order is exercised - the scope is applied before the
one-shot fate decision and before any impact row is written.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from app.analysis.chart_scope import (
    chart_assets_for_symbols,
    parse_scope,
    scope_label,
)
from app.analysis.news_classifier import (
    ArticleAnalysis,
    AssetAnalysis,
    apply_chart_scope,
    is_section_b_verdict,
)
from app.analysis.sentiment_engine import _passes_impact_gate
from app.analysis.signal_factory import build_bleed_signal, build_signal
from app.config import settings
from app.database import session_scope
from app.ingestion.base import RawArticle
from app.ingestion.tradingview_api import (
    TradingViewAPISource,
    fetch_tradingview_news_api,
    map_internal_to_tv,
)
from app.models.db_models import AssetImpact, NewsArticle
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
    Verification,
)
from app.services import pipeline


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _analysis(assets=(), importance=EconomicImportance.HIGH,
              category=NewsCategory.OTHER):
    return ArticleAnalysis(
        category=category,
        importance=importance,
        report_kind=ReportKind.GENERAL,
        verification=Verification.CONFIRMED,
        event_summary="Stub verdict",
        relevance_score=0.5,
        assets=list(assets),
        mode="llm",
        note="stub",
    )


def _asset(asset=Asset.GOLD, strength=ImpactStrength.HIGH,
           direction=Direction.BULLISH):
    return AssetAnalysis(
        asset=asset, direction=direction, strength=strength,
        relevance=0.9, persistence=0.7, reason="stub", transmission="stub",
    )


def _article(title="Fed holds rates steady, gold steadies", guid=None,
             chart_assets=(), symbols=()):
    now = datetime.now(timezone.utc)
    return RawArticle(
        guid=guid or f"cs-guid-{abs(hash(title)) % 10**8}",
        source="TradingView API News",
        source_quality="TIER_2_MAJOR",
        url=f"https://www.tradingview.com/news/{abs(hash(title)) % 10**8}",
        title=title,
        body="Body text for the article.",
        published_raw=(now - timedelta(minutes=15)).isoformat(),
        symbols=list(symbols),
        chart_assets=list(chart_assets),
    )


def _ingest(monkeypatch, analysis, art):
    """Drive the real ingest path with a stubbed classifier."""
    async def _analyze(title, body=""):
        return analysis, True, "ok"

    monkeypatch.setattr(pipeline, "analyze_article", _analyze)
    stats = pipeline.IngestStats()
    with session_scope() as session:
        row = asyncio.run(pipeline._ingest_article(art, session, stats, True))
    return row, stats


def _impact_assets():
    with session_scope() as session:
        rows = session.query(AssetImpact).all()
        return sorted((r.article_id, r.asset) for r in rows)


@pytest.fixture(autouse=True)
def _clear_tradingview_cache():
    """
    The TradingView cache is a module-level dict that survives across test
    files. A payload cached here would otherwise be read by another test
    file's fetch, which is how a cached payload ends up answering a query it
    was never fetched for.
    """
    from app.ingestion.tradingview_api import _cache

    _cache.clear()
    yield
    _cache.clear()


# --------------------------------------------------------------------------
# 1. The symbol -> card table
# --------------------------------------------------------------------------
def test_chart_symbols_map_to_cards():
    assert chart_assets_for_symbols(["OANDA:XAUUSD"]) == [Asset.GOLD]
    assert chart_assets_for_symbols(["OANDA:XAGUSD"]) == [Asset.SILVER]
    assert chart_assets_for_symbols(["BITSTAMP:BTCUSD"]) == [Asset.BITCOIN]


def test_unknown_symbol_has_no_card():
    assert chart_assets_for_symbols(["NASDAQ:XYZ"]) == []
    assert chart_assets_for_symbols([]) == []


def test_equity_feed_is_one_card():
    """Every ticker of the equity session belongs to the equity card."""
    assert chart_assets_for_symbols(["NASDAQ:TSLA"], equity=True) == [Asset.EQUITY]
    assert chart_assets_for_symbols(
        ["NASDAQ:TSLA", "NASDAQ:NVDA"], equity=True) == [Asset.EQUITY]


def test_multi_owner_is_the_default():
    """A story on two charts feeds both cards (TradingView's semantics)."""
    assets = chart_assets_for_symbols(["OANDA:XAUUSD", "OANDA:XAGUSD"])
    assert assets == [Asset.GOLD, Asset.SILVER]
    assert scope_label(assets) == "XAUUSD,XAGUSD"


def test_single_owner_mode_picks_gold_first(monkeypatch):
    monkeypatch.setattr(settings, "chart_scope_single_owner", True)
    assets = chart_assets_for_symbols(["OANDA:XAUUSD", "OANDA:XAGUSD"])
    assert assets == [Asset.GOLD]


def test_single_owner_mode_falls_through_to_the_next_card(monkeypatch):
    monkeypatch.setattr(settings, "chart_scope_single_owner", True)
    assert chart_assets_for_symbols(["OANDA:XAGUSD", "BITSTAMP:BTCUSD"]) == [
        Asset.SILVER]
    assert chart_assets_for_symbols(["BITSTAMP:BTCUSD"], equity=False) == [
        Asset.BITCOIN]


def test_scope_label_round_trip():
    assert parse_scope("XAUUSD,XAGUSD") == [Asset.GOLD, Asset.SILVER]
    assert parse_scope("") == []
    assert parse_scope("NOPE") == []


# --------------------------------------------------------------------------
# 2. The fetch layer records WHICH chart listed the story
# --------------------------------------------------------------------------
def _tv_item(news_id, title, tags):
    return {
        "id": news_id,
        "title": title,
        "published": 1792513758,
        "urgency": 2,
        "relatedSymbols": [{"symbol": s} for s in tags],
        "storyPath": f"/news/{news_id}/",
        "provider": {"name": "Reuters"},
    }


def test_story_on_two_charts_keeps_both_origins():
    """The same story returned by the gold and the silver query is on both."""
    from app.ingestion.tradingview_api import _cache

    _cache.clear()
    gold = {"items": [_tv_item("g1", "Gold steady", ["OANDA:XAUUSD"])],
            "pagination": {}}
    silver = {"items": [_tv_item("g1", "Gold steady", ["OANDA:XAUUSD"]),
                        _tv_item("s1", "Silver slips", ["OANDA:XAGUSD"])],
              "pagination": {}}

    with patch("app.ingestion.tradingview_api._get_session") as mock_get:
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value=gold)),
            Mock(status_code=200, json=Mock(return_value=silver)),
        ]
        mock_get.return_value = session

        items = fetch_tradingview_news_api(
            ["OANDA:XAUUSD", "OANDA:XAGUSD"], max_items=10)

    by_id = {i["news_id"]: i for i in items}
    assert set(by_id) == {"g1", "s1"}
    # The duplicate is MERGED, not dropped: it is on both charts.
    assert by_id["g1"]["origin_symbols"] == ["OANDA:XAUUSD", "OANDA:XAGUSD"]
    assert by_id["s1"]["origin_symbols"] == ["OANDA:XAGUSD"]
    # The story's own tags are still carried for the audit trail.
    assert by_id["g1"]["symbols"] == ["OANDA:XAUUSD"]


def test_source_attaches_the_chart_scope_to_the_article():
    """The RawArticle carries the cards whose chart listed the story."""
    from app.ingestion.tradingview_api import _cache

    _cache.clear()
    gold = {"items": [_tv_item("g1", "Gold steady", ["OANDA:XAUUSD"])],
            "pagination": {}}
    silver = {"items": [_tv_item("s1", "Silver slips", ["OANDA:XAGUSD"])],
              "pagination": {}}
    bitcoin = {"items": [_tv_item("b1", "Bitcoin climbs", ["BITSTAMP:BTCUSD"])],
               "pagination": {}}

    with patch("app.ingestion.tradingview_api._get_session") as mock_get:
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value=gold)),
            Mock(status_code=200, json=Mock(return_value=silver)),
            Mock(status_code=200, json=Mock(return_value=bitcoin)),
        ]
        mock_get.return_value = session

        source = TradingViewAPISource()
        articles = asyncio.run(source.fetch())

    scopes = {a.title: a.chart_assets for a in articles}
    assert scopes["Gold steady"] == ["XAUUSD"]
    assert scopes["Silver slips"] == ["XAGUSD"]
    assert scopes["Bitcoin climbs"] == ["BTCUSD"]


def test_bitcoin_symbol_is_the_one_tradingview_tags():
    """BINANCE:BTCUSD matched nothing (0/30 measured); BITSTAMP:BTCUSD 29/30."""
    mapped = map_internal_to_tv(["BTCUSD"])
    assert mapped == ["BITSTAMP:BTCUSD"]


# --------------------------------------------------------------------------
# 3. The classifier obeys the scope
# --------------------------------------------------------------------------
def test_apply_chart_scope_drops_assets_outside_the_scope():
    analysis = _analysis(assets=[_asset(Asset.GOLD), _asset(Asset.BITCOIN)])
    apply_chart_scope(analysis, [Asset.GOLD], "Gold steady", "")
    assert [a.asset for a in analysis.assets] == [Asset.GOLD]


def test_apply_chart_scope_builds_the_card_the_model_ignored():
    """A story the model attributed to another asset still reaches its chart."""
    analysis = _analysis(assets=[_asset(Asset.BITCOIN)])
    apply_chart_scope(analysis, [Asset.GOLD], "Gold steady as Fed waits", "")
    assets = [a.asset for a in analysis.assets]
    assert assets == [Asset.GOLD]


def test_apply_chart_scope_keeps_both_cards_of_a_two_chart_story():
    analysis = _analysis(assets=[_asset(Asset.GOLD)])
    apply_chart_scope(analysis, [Asset.GOLD, Asset.SILVER],
                      "Gold futures inch higher on oil price retreat", "")
    assert sorted(a.asset.value for a in analysis.assets) == [
        "XAGUSD", "XAUUSD"]


def test_apply_chart_scope_on_an_empty_verdict_still_fills_the_card():
    """'No material impact' must not mean 'no card' for a chart story."""
    analysis = _analysis(assets=[])
    apply_chart_scope(analysis, [Asset.GOLD], "Gold steady as Fed waits", "")
    assert [a.asset for a in analysis.assets] == [Asset.GOLD]


def test_apply_chart_scope_without_scope_changes_nothing():
    analysis = _analysis(assets=[_asset(Asset.GOLD), _asset(Asset.BITCOIN)])
    apply_chart_scope(analysis, [], "anything", "")
    assert len(analysis.assets) == 2


# --------------------------------------------------------------------------
# 4. The ingest path: impact rows are written for the scoped cards only
# --------------------------------------------------------------------------
def test_gold_chart_news_writes_only_a_gold_impact_row(monkeypatch):
    art = _article("Fed holds rates steady, gold steadies",
                   chart_assets=["XAUUSD"])
    analysis = _analysis(assets=[_asset(Asset.GOLD), _asset(Asset.SILVER),
                                  _asset(Asset.BITCOIN)])
    row, _stats = _ingest(monkeypatch, analysis, art)
    assert row is not None
    with session_scope() as session:
        assets = [i.asset for i in session.query(AssetImpact).all()]
    assert assets == ["XAUUSD"]


def test_a_two_chart_story_reaches_both_cards(monkeypatch):
    art = _article("Gold futures inch higher on oil price retreat",
                   chart_assets=["XAUUSD", "XAGUSD"])
    analysis = _analysis(assets=[_asset(Asset.GOLD)])
    row, _stats = _ingest(monkeypatch, analysis, art)
    assert row is not None
    with session_scope() as session:
        assets = sorted(i.asset for i in session.query(AssetImpact).all())
    assert assets == ["XAGUSD", "XAUUSD"]


def test_the_scope_is_stored_on_the_row(monkeypatch):
    art = _article("Gold futures inch higher", chart_assets=["XAUUSD", "XAGUSD"],
                   symbols=["OANDA:XAUUSD", "OANDA:XAGUSD"])
    analysis = _analysis(assets=[_asset(Asset.GOLD)])
    row, _stats = _ingest(monkeypatch, analysis, art)
    assert row is not None
    with session_scope() as session:
        stored = session.get(NewsArticle, row.id)
        assert stored.chart_scope == "XAUUSD,XAGUSD"
        assert "OANDA:XAUUSD" in stored.tv_symbols


def test_a_source_without_a_chart_is_untouched(monkeypatch):
    """An RSS story keeps the classifier's routing: no scope, no isolation."""
    art = _article("Fed holds rates steady", chart_assets=())
    art.source = "Reuters"
    analysis = _analysis(assets=[_asset(Asset.GOLD), _asset(Asset.BITCOIN)])
    row, _stats = _ingest(monkeypatch, analysis, art)
    assert row is not None
    with session_scope() as session:
        assets = sorted(i.asset for i in session.query(AssetImpact).all())
        stored = session.get(NewsArticle, row.id)
    assert assets == ["BTCUSD", "XAUUSD"]
    assert stored.chart_scope == ""


def test_chart_scope_isolation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "chart_scope_isolation", False)
    art = _article("Fed holds rates steady, gold steadies",
                   chart_assets=["XAUUSD"])
    analysis = _analysis(assets=[_asset(Asset.GOLD), _asset(Asset.SILVER)])
    row, _stats = _ingest(monkeypatch, analysis, art)
    assert row is not None
    with session_scope() as session:
        assets = sorted(i.asset for i in session.query(AssetImpact).all())
    assert assets == ["XAGUSD", "XAUUSD"]


# --------------------------------------------------------------------------
# 5. The one-shot policy must not delete a chart story
# --------------------------------------------------------------------------
def test_a_chart_story_is_not_deleted_by_the_one_shot_policy():
    """A story the chart listed is that card's news: it must survive."""
    analysis = _analysis(importance=EconomicImportance.LOW,
                          assets=[_asset(Asset.GOLD, strength=ImpactStrength.LOW)])
    assert is_section_b_verdict(analysis, llm_used=True) is False
    assert is_section_b_verdict(analysis, llm_used=True,
                                 chart_scoped=True) is True


def test_a_chart_story_survives_the_ingest_path(monkeypatch):
    art = _article("A routine filing nobody trades on", chart_assets=["XAUUSD"])
    analysis = _analysis(importance=EconomicImportance.LOW,
                          assets=[_asset(Asset.GOLD, strength=ImpactStrength.LOW)])
    row, _stats = _ingest(monkeypatch, analysis, art)
    assert row is not None


# --------------------------------------------------------------------------
# 6. The impact gate: a MEDIUM chart story still counts
# --------------------------------------------------------------------------
def test_medium_strength_chart_story_passes_the_gate(monkeypatch):
    monkeypatch.setattr(settings, "only_high_impact_counts", True)
    signal = build_signal(
        asset=Asset.GOLD, title="Gold steady", direction=Direction.BULLISH,
        strength=ImpactStrength.MEDIUM, chart_scope="XAUUSD")
    assert _passes_impact_gate(signal) is True


def test_medium_strength_ordinary_story_is_still_dropped(monkeypatch):
    monkeypatch.setattr(settings, "only_high_impact_counts", True)
    signal = build_signal(
        asset=Asset.GOLD, title="Gold steady", direction=Direction.BULLISH,
        strength=ImpactStrength.MEDIUM)
    assert _passes_impact_gate(signal) is False


def test_low_strength_chart_story_is_still_dropped(monkeypatch):
    monkeypatch.setattr(settings, "only_high_impact_counts", True)
    signal = build_signal(
        asset=Asset.GOLD, title="Gold steady", direction=Direction.BULLISH,
        strength=ImpactStrength.LOW, chart_scope="XAUUSD")
    assert _passes_impact_gate(signal) is False


# --------------------------------------------------------------------------
# 7. The cross-asset bleed must not copy a chart story to the other metal
# --------------------------------------------------------------------------
def test_a_chart_scoped_story_does_not_bleed_to_the_other_metal():
    signal = build_signal(
        asset=Asset.GOLD, title="Fed holds rates steady", direction=Direction.BEARISH,
        strength=ImpactStrength.VERY_HIGH, importance=EconomicImportance.VERY_HIGH,
        relevance=0.95, category=NewsCategory.CENTRAL_BANK,
        chart_scope="XAUUSD")
    assert build_bleed_signal(signal) is None


def test_an_ordinary_story_still_bleeds():
    signal = build_signal(
        asset=Asset.GOLD, title="Fed holds rates steady", direction=Direction.BEARISH,
        strength=ImpactStrength.VERY_HIGH, importance=EconomicImportance.VERY_HIGH,
        relevance=0.95, category=NewsCategory.CENTRAL_BANK)
    bleed = build_bleed_signal(signal)
    assert bleed is not None
    assert bleed.asset is Asset.SILVER


def test_a_two_chart_story_still_bleeds_nowhere_but_reaches_both_cards():
    """It has its own impact rows for both cards, so it loses nothing."""
    signal = build_signal(
        asset=Asset.GOLD, title="Gold futures inch higher", direction=Direction.BULLISH,
        strength=ImpactStrength.VERY_HIGH, importance=EconomicImportance.VERY_HIGH,
        relevance=0.95, category=NewsCategory.CENTRAL_BANK,
        chart_scope="XAUUSD,XAGUSD")
    assert build_bleed_signal(signal) is None


# --------------------------------------------------------------------------
# 8. The equity card
# --------------------------------------------------------------------------
def test_a_gold_chart_story_is_not_re_tagged_into_the_equity_card():
    from app.analysis.equity_card import region_signals
    from app.market.session_detector import SessionName

    signal = build_signal(
        asset=Asset.GOLD, title="Nasdaq rallies on Fed signal", direction=Direction.BULLISH,
        strength=ImpactStrength.HIGH, category=NewsCategory.EQUITIES,
        relevance=0.9, chart_scope="XAUUSD")
    matched = region_signals([signal], SessionName.US)
    assert matched == []


def test_an_equity_chart_story_reaches_the_equity_card():
    from app.analysis.equity_card import region_signals
    from app.market.session_detector import SessionName

    signal = build_signal(
        asset=Asset.EQUITY, title="Tesla third-quarter operating profit falls 56%",
        direction=Direction.BEARISH, strength=ImpactStrength.HIGH,
        category=NewsCategory.EQUITIES, relevance=0.9, chart_scope="EQUITY")
    matched = region_signals([signal], SessionName.US)
    assert len(matched) == 1
    assert matched[0].asset is Asset.EQUITY


def test_an_ordinary_macro_story_still_reaches_the_equity_card():
    """The re-tagging path is unchanged for stories with no chart."""
    from app.analysis.equity_card import region_signals
    from app.market.session_detector import SessionName

    signal = build_signal(
        asset=Asset.GOLD, title="Nasdaq rallies on Fed signal", direction=Direction.BULLISH,
        strength=ImpactStrength.HIGH, category=NewsCategory.EQUITIES,
        relevance=0.9)
    matched = region_signals([signal], SessionName.US)
    assert len(matched) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
