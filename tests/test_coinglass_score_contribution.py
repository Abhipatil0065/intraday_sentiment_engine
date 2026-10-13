"""
The CoinGlass number must reach the BTC card's bias score.

Incident (2026-10-13, engine.log): every tick logged
    "CoinGlass scrape_all failed: asyncio.run() cannot be called from a
     running event loop"
The scrape was started from ``_recompute_sentiment``, which runs INSIDE the
pipeline coroutine - and ``asyncio.run`` refuses to start a loop in a thread
that already has one. No CVD / footprint signal was ever injected, so the
scraped readings never moved a bullish/bearish badge.

The footprint reading had a SECOND blocker on top: it is built as a MEDIUM
impact signal, which the impact gate (``only_high_impact_counts``) drops at
the weight choke point. Even a working injection would not have reached a
card.

After the fix:
  1. the scrape is awaited in ``run_once`` and handed to the sync recompute,
     which only injects what it is given;
  2. a scraped flow reading passes the impact gate - its own band still
     scales its magnitude, so it contributes to the totals, nothing else.

These tests pin both halves, and that the reading moves the score.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.sentiment_engine import (
    Bias,
    WeightedSignal,
    _signal_weight,
    clear_bias_history,
    compute,
)
from app.config import settings
from app.ingestion.coinglass_scraper import (
    CoinGlassSnapshot,
    CVDSeries,
    FootprintData,
    HeatmapCluster,
    coinglass_signals_to_weighted,
)
from app.models.db_models import AssetImpact, Base, NewsArticle
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
)
from app.services.pipeline import IngestStats, _recompute_sentiment

from tests.fixed_clock import FIXED_NOW as NOW


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _btc_snapshot(cvd: str = "CVD_BULLISH",
                  footprint: str = "BUY_IMBALANCE") -> CoinGlassSnapshot:
    """One realistic BTC scraping: spot, a nearby cluster, CVD + footprint."""
    return CoinGlassSnapshot(
        asset="BTCUSD",
        spot_price=95_000.0,
        fetched_at=NOW.isoformat(),
        heatmap_clusters=[
            HeatmapCluster(price_level=94_000.0, intensity="high", side="long"),
        ],
        cluster_below=HeatmapCluster(price_level=94_000.0, intensity="high",
                                     side="long"),
        cluster_above=HeatmapCluster(price_level=96_000.0, intensity="high",
                                     side="short"),
        caution_below=True,
        caution_above=False,
        cvd=CVDSeries(points=[(0.0, 1.0), (60.0, 3.0)], divergence=cvd,
                      divergence_note="price up, delta up", slope=0.4),
        footprint=FootprintData(rows=[], imbalance=footprint,
                                imbalance_pct=2.5),
        source_url="https://www.coinglass.com/pro/futures/LiquidationHeatMap",
        degraded=False,
    )


def _news_signal(direction: Direction, title: str) -> WeightedSignal:
    """One fresh HIGH-impact BTC headline, the shape the news feed produces."""
    return WeightedSignal(
        asset=Asset.BITCOIN,
        title=title,
        direction=direction,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.HIGH,
        published_at=NOW - timedelta(minutes=5),
        relevance=0.80,
        persistence=0.70,
        category=NewsCategory.CRYPTO_REGULATION,
        corroboration=1.0,
        cluster_id=f"news-{title[:12]}",
        source_quality="TIER_2_MAJOR",
    )


def _btc_db(tmp_path):
    """Throwaway DB with ONE bearish BTC news impact to score against.

    published_at uses the REAL clock, not FIXED_NOW: the recompute filters
    rows through pipeline.news_day_predicate(), which is anchored to the
    actual market day, so a fixed-clock row is correctly "not today".
    """
    from datetime import datetime, timezone

    engine = create_engine(f"sqlite:///{tmp_path / 'btc.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        art = NewsArticle(
            guid="btc-1", source="test", source_quality="TIER_2_MAJOR",
            url="http://test.example/btc", title="ETF outflows hit bitcoin",
            body="body",
            published_at_utc=datetime.now(timezone.utc).replace(tzinfo=None),
            is_today=True,
        )
        session.add(art)
        session.flush()
        session.add(AssetImpact(
            article_id=art.id, asset="BTCUSD", direction="BEARISH",
            strength="HIGH", relevance=0.80, persistence=0.70,
            transmission="BTC_ETF_FLOW", reason="test row",
        ))
        session.commit()
    return Session


# --------------------------------------------------------------------------
# 1. Injection wiring
# --------------------------------------------------------------------------
def test_recompute_injects_snapshots_it_is_handed(tmp_path):
    """The recompute must score the scraped readings, not scrape them.

    Regression for the nested-event-loop bug: the scrape is the async
    caller's job, the recompute only injects what it is GIVEN - so it works
    from sync test code with no browser at all.
    """
    import app.services.pipeline as p

    p._calendar_signals = []
    Session = _btc_db(tmp_path)

    stats = IngestStats()
    with Session() as session:
        signals, _regime = _recompute_sentiment(
            session, stats, coinglass_snapshots={"BTCUSD": _btc_snapshot()})

    cg = [s for s in signals if s.mode == "coinglass"]
    assert {s.title for s in cg} == {
        "CoinGlass CVD CVD_BULLISH (BTCUSD)",
        "CoinGlass Footprint BUY_IMBALANCE (BTCUSD)",
    }
    assert {s.direction for s in cg} == {Direction.BULLISH}
    # Also present: the news row from the DB.
    assert any(s.title == "ETF outflows hit bitcoin" for s in signals)


def test_recompute_without_snapshots_injects_nothing(tmp_path):
    """No snapshot passed (poll skipped) -> no CoinGlass signal, no crash."""
    import app.services.pipeline as p

    p._calendar_signals = []
    Session = _btc_db(tmp_path)

    stats = IngestStats()
    with Session() as session:
        signals, _regime = _recompute_sentiment(session, stats)

    assert [s for s in signals if s.mode == "coinglass"] == []


# --------------------------------------------------------------------------
# 2. The impact gate must not drop a scraped reading
# --------------------------------------------------------------------------
def test_coinglass_readings_pass_the_impact_gate_when_on(monkeypatch):
    """With the gate ON, both scraped readings still carry weight.

    The footprint is a MEDIUM band by construction; news in that band is
    correctly dropped, but a measured flow reading is not classified news and
    must keep its contribution.
    """
    monkeypatch.setattr(settings, "only_high_impact_counts", True)

    signals = coinglass_signals_to_weighted({"BTCUSD": _btc_snapshot()})
    assert len(signals) == 2

    weights = {s.title: _signal_weight(s, NOW) for s in signals}
    cvd = weights["CoinGlass CVD CVD_BULLISH (BTCUSD)"]
    footprint = weights["CoinGlass Footprint BUY_IMBALANCE (BTCUSD)"]

    assert cvd > 0, "CVD divergence must carry weight with the gate on"
    assert footprint > 0, "footprint imbalance must carry weight too"
    # The bands still scale the magnitude - admitting the reading does not
    # inflate it past a HIGH-band reading.
    assert footprint < cvd


def test_gate_still_drops_medium_news(monkeypatch):
    """The exemption is for scraped readings only - MEDIUM news stays out."""
    monkeypatch.setattr(settings, "only_high_impact_counts", True)
    from app.analysis.sentiment_engine import _passes_impact_gate

    medium_news = _news_signal(Direction.BULLISH, "analyst note")
    medium_news.strength = ImpactStrength.MEDIUM
    assert _passes_impact_gate(medium_news) is False

    scraped = coinglass_signals_to_weighted(
        {"BTCUSD": _btc_snapshot(footprint="SELL_IMBALANCE")})
    assert all(_passes_impact_gate(s) for s in scraped)


# --------------------------------------------------------------------------
# 3. The number moves the BTC bullish/bearish verdict
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(settings, "only_high_impact_counts", True)
    clear_bias_history()
    yield
    clear_bias_history()


def test_bullish_coinglass_flips_the_btc_bias():
    """A bearish news day plus a bullish CVD/footprint read must flip BTC."""
    news = [_news_signal(Direction.BEARISH, "ETF outflows hit bitcoin")]
    scraped = coinglass_signals_to_weighted({"BTCUSD": _btc_snapshot()})

    without = compute(news, Asset.BITCOIN, now=NOW)
    with_cg = compute(news + scraped, Asset.BITCOIN, now=NOW)

    assert without.bias is Bias.BEARISH
    assert with_cg.bias is Bias.BULLISH, (
        "the CoinGlass number must be able to decide the bullish/bearish "
        "bias on the card")
    assert with_cg.score > without.score


def test_bearish_coinglass_deepens_the_bearish_read():
    """The reading works in both directions - it is a number, not a nudge.

    Note the score is already saturated (-100) with only bearish evidence in
    the pool, so "deeper" shows up where extra weight actually lands: the
    damped score, the confidence and the burst counts - not the raw score.
    """
    news = [_news_signal(Direction.BEARISH, "ETF outflows hit bitcoin")]
    bearish_scrape = coinglass_signals_to_weighted(
        {"BTCUSD": _btc_snapshot(cvd="CVD_BEARISH",
                                 footprint="SELL_IMBALANCE")})
    assert {s.direction for s in bearish_scrape} == {Direction.BEARISH}

    without = compute(news, Asset.BITCOIN, now=NOW)
    with_cg = compute(news + bearish_scrape, Asset.BITCOIN, now=NOW)

    assert with_cg.bias is Bias.BEARISH
    titles = [d.title for d in with_cg.bear_drivers]
    assert "CoinGlass CVD CVD_BEARISH (BTCUSD)" in titles
    assert "CoinGlass Footprint SELL_IMBALANCE (BTCUSD)" in titles
    # More weight behind the same verdict: damped score leans further out and
    # the burst window counts the fresh same-direction cluster.
    assert with_cg.damped_score < without.damped_score
    assert with_cg.confidence > without.confidence
    assert with_cg.burst_bear > without.burst_bear


def test_coinglass_signal_lands_in_driver_boxes():
    """A contributing signal is named as a driver, like any other evidence."""
    news = [_news_signal(Direction.BULLISH, "ETF inflows return")]
    scraped = coinglass_signals_to_weighted({"BTCUSD": _btc_snapshot()})

    result = compute(news + scraped, Asset.BITCOIN, now=NOW)
    titles = [d.title for d in result.bull_drivers]

    assert "CoinGlass CVD CVD_BULLISH (BTCUSD)" in titles
    assert "CoinGlass Footprint BUY_IMBALANCE (BTCUSD)" in titles
