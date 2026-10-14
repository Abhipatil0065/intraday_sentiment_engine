"""
Regression tests: FinancialJuice session reports may only shape the cards on
``fj_sessions.SESSION_REPORT_ALLOWED_ASSETS`` (today: EQUITY).

The 2026-10-14 incident: "Morning Juice - Europe Session Prep" was turned into
a WeightedSignal for GOLD (contribution +0.802) and landed in gold's TOP
DRIVERS box and the binary tie-break, even though a session report is a SUMMARY
of a session, not a trade signal. Its home is the five FJ boxes; the only
multi-source card it may legitimately move is the equity card, because that
report IS its region's equity tape.

No network, no Playwright: rows are written straight into an in-memory SQLite
database, exactly like ``test_usoil_recompute`` does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.services.pipeline as pipeline
from app.analysis import fj_sessions as F
from app.analysis.sentiment_engine import compute as compute_sentiment
from app.models.db_models import AssetImpact, Base, NewsArticle
from app.models.enums import Asset, ImpactStrength
from app.services.pipeline import IngestStats, _recompute_sentiment

FJ_SOURCE = "Financial Juice"
WIRE_SOURCE = "NewsQuawk"
SESSION_TITLE = "Morning Juice - Europe Session Prep (14th October)"
# Sorted so it matches ``_signal_assets`` below.
ALL_ASSETS = ["BTCUSD", "EQUITY", "XAGUSD", "XAUUSD"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fj_filter.db'}")
    Base.metadata.create_all(engine)
    pipeline._calendar_signals = []
    return engine


def _wire_pubtime(minutes_ago=15):
    """A publication time ``news_day_predicate`` accepts for a NON-FJ row.

    The predicate tests ``published_at_utc >= market_day_start_utc()``; in the
    few minutes right after market-local midnight a plain "now - 15min" would
    fall outside today, so clamp up to just after the day start. Only used for
    ordinary sources - FJ session reports are admitted by source, not by time.
    """
    from app.utils import timeutils as T

    now = datetime.now(timezone.utc)
    day_start = T.market_day_start_utc().replace(tzinfo=timezone.utc)
    pub = now - timedelta(minutes=minutes_ago)
    return max(pub, day_start + timedelta(minutes=2))


def _add_article(session, *, guid, title, source, minutes_ago=15,
                 importance="VERY_HIGH", body=""):
    published = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
                 if "juice" in (source or "").lower()
                 else _wire_pubtime(minutes_ago))
    art = NewsArticle(
        guid=guid,
        source=source,
        source_quality="TIER_2_MAJOR",
        url=f"https://example.com/{guid}",
        title=title,
        body=body,
        published_at_utc=published,
        is_today=True,
        importance=importance,
        category="Equities",
        report_kind="GENERAL",
    )
    session.add(art)
    session.flush()
    return art


def _add_impacts(session, art, *, assets=ALL_ASSETS, direction="BULLISH",
                 strength="VERY_HIGH", relevance=0.9):
    for asset in assets:
        session.add(AssetImpact(
            asset=asset,
            article_id=art.id,
            direction=direction,
            strength=strength,
            relevance=relevance,
            persistence=0.8,
            transmission="EQUITY_NEWS",
            reason="test impact",
        ))


def _recompute(engine):
    SessionLocal = sessionmaker(bind=engine)
    stats = IngestStats()
    with SessionLocal() as session:
        signals, _regime = _recompute_sentiment(session, stats)
        return signals, stats, session


def _signal_assets(signals):
    return sorted({s.asset.value for s in signals})


def _signal_titles(signals, asset):
    return [s.title for s in signals if s.asset is asset]


def _driver_titles(result):
    out = []
    for d in list(result.drivers or []) + list(result.bull_drivers or []) \
            + list(result.bear_drivers or []):
        out.append(d.title)
    if result.conflict is not None:
        out.append(result.conflict.title)
    return out


# ---------------------------------------------------------------------------
# the incident itself
# ---------------------------------------------------------------------------
def test_session_report_never_reaches_metals_or_bitcoin(tmp_path):
    """GOLD / SILVER / BITCOIN must see nothing of a session report."""
    engine = _setup_db(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        art = _add_article(
            session, guid="fj-prep-1", title=SESSION_TITLE, source=FJ_SOURCE)
        _add_impacts(session, art)
        session.commit()

    signals, stats, _ = _recompute(engine)

    # Only the equity card may still hold it.
    assert _signal_assets(signals) == ["EQUITY"]
    for blocked in (Asset.GOLD, Asset.SILVER, Asset.BITCOIN):
        assert _signal_titles(signals, blocked) == []
    assert SESSION_TITLE in _signal_titles(signals, Asset.EQUITY)

    # Three blocked impact rows were dropped at signal build.
    assert stats.skipped_fj_session == 3

    # And the verdict/driver boxes agree: gold computes with NO session-report
    # signal at all, so the report cannot be a top driver or break a tie.
    gold = compute_sentiment(signals, Asset.GOLD)
    assert SESSION_TITLE not in _driver_titles(gold)

    equity = compute_sentiment(signals, Asset.EQUITY)
    assert SESSION_TITLE in _driver_titles(equity)


def test_session_report_row_is_kept_for_the_five_boxes(tmp_path):
    """Filtering is a scoring decision, not a delete: the FJ boxes still read it."""
    engine = _setup_db(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        art = _add_article(
            session, guid="fj-prep-2", title=SESSION_TITLE, source=FJ_SOURCE)
        _add_impacts(session, art)
        session.commit()

    signals, _stats, session = _recompute(engine)

    kept = session.execute(
        text("SELECT COUNT(*) FROM news_article WHERE guid = 'fj-prep-2'")
    ).scalar()
    assert kept == 1
    assert session.execute(
        text("SELECT COUNT(*) FROM asset_impact WHERE asset = 'XAUUSD'")
    ).scalar() == 1

    # The five-box section reads rows directly and is unaffected.
    rows = session.execute(text(
        "SELECT source, title, body, published_at_utc FROM news_article"
    )).all()
    payload = F.build_session_analysis(_as_rows(rows))
    prep = {b["session_key"]: b for b in payload["boxes"]}["EUROPE_MARKET_PREP"]
    assert prep["found"] is True
    assert prep["report_title"] == SESSION_TITLE


def _as_rows(rows):
    """sqlalchemy Row -> the attribute shape fj_sessions expects."""
    from types import SimpleNamespace
    return [
        SimpleNamespace(
            source=r.source,
            title=r.title,
            body=r.body,
            published_at_utc=r.published_at_utc,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# nothing else changes
# ---------------------------------------------------------------------------
def test_ordinary_wire_headlines_reach_every_card(tmp_path):
    """A non-FJ source keeps its gold/silver/bitcoin/equity impacts."""
    engine = _setup_db(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        art = _add_article(
            session, guid="wire-1", title="ECB holds rates, equities rally",
            source=WIRE_SOURCE)
        _add_impacts(session, art)
        session.commit()

    signals, stats, _ = _recompute(engine)

    assert _signal_assets(signals) == ALL_ASSETS
    assert stats.skipped_fj_session == 0


def test_ordinary_financialjuice_headlines_reach_every_card(tmp_path):
    """Only the five session REPORTS are filtered, not the FJ wire."""
    engine = _setup_db(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        art = _add_article(
            session, guid="fj-wire-1",
            title="ECB's Lagarde: inflation is cooling in the euro area",
            source=FJ_SOURCE)
        _add_impacts(session, art)
        session.commit()

    signals, stats, _ = _recompute(engine)

    assert _signal_assets(signals) == ALL_ASSETS
    assert stats.skipped_fj_session == 0


def test_session_report_with_no_equity_impact_is_fully_excluded(tmp_path):
    """A session report the classifier did not mark equity-relevant reaches
    no multi-source card at all - it only lives in its five boxes."""
    engine = _setup_db(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        art = _add_article(
            session, guid="fj-prep-3", title=SESSION_TITLE, source=FJ_SOURCE)
        _add_impacts(session, art, assets=("XAUUSD", "XAGUSD", "BTCUSD"))
        session.commit()

    signals, stats, _ = _recompute(engine)

    assert signals == []
    assert stats.skipped_fj_session == 3


def test_asset_filter_is_independent_of_the_impact_gate(tmp_path):
    """The asset allowlist is a separate rule from the strength gate.

    The suite runs with ``only_high_impact_counts=False`` (see conftest), so a
    MEDIUM session report still becomes a signal - yet gold, silver and bitcoin
    are dropped exactly the same way, because the filter is on the ASSET, not
    on the strength. EQUITY survives and is judged on its own merits.
    """
    engine = _setup_db(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        art = _add_article(
            session, guid="fj-prep-4", title=SESSION_TITLE, source=FJ_SOURCE,
            importance="LOW")
        _add_impacts(session, art, strength="MEDIUM", relevance=0.2)
        session.commit()

    signals, stats, _ = _recompute(engine)

    assert _signal_assets(signals) == ["EQUITY"]
    assert stats.skipped_fj_session == 3
    assert _signal_titles(signals, Asset.EQUITY) == [SESSION_TITLE]


# ---------------------------------------------------------------------------
# the shared helpers (one definition, both callers)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("asset,allowed", [
    (Asset.EQUITY, True),
    (Asset.GOLD, False),
    (Asset.SILVER, False),
    (Asset.BITCOIN, False),
    ("EQUITY", True),
    ("XAUUSD", False),
    ("XAGUSD", False),
    ("BTCUSD", False),
])
def test_session_report_asset_allowed(asset, allowed):
    assert F.session_report_asset_allowed(asset) is allowed


@pytest.mark.parametrize("source,title,is_report", [
    (FJ_SOURCE, "Morning Juice - Europe Session Prep (14th October)", True),
    (FJ_SOURCE, "US Stocks End Higher - US Market Wrap", True),
    (FJ_SOURCE, "ECB's Lagarde: inflation is cooling", False),
    (WIRE_SOURCE, "Morning Juice - Europe Session Prep (14th October)", False),
    (None, "Morning Juice - Europe Session Prep", False),
    (FJ_SOURCE, "", False),
])
def test_is_fj_session_report_row(source, title, is_report):
    from types import SimpleNamespace
    row = SimpleNamespace(source=source, title=title, body="")
    assert F.is_fj_session_report_row(row) is is_report


def test_is_fj_session_report_row_reads_an_orm_row(tmp_path):
    engine = _setup_db(tmp_path)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        session.add(NewsArticle(
            guid="fj-prep-orm", source=FJ_SOURCE, source_quality="TIER_2_MAJOR",
            url="https://example.com/orm", title=SESSION_TITLE, body="",
            is_today=True,
        ))
        session.commit()
        art_id = session.execute(text(
            "SELECT id FROM news_article WHERE guid = 'fj-prep-orm'")
        ).scalar()
        art = session.get(NewsArticle, art_id)
        assert F.is_fj_session_report_row(art) is True
        # An ordinary FJ headline is NOT a session report.
        art.title = "ECB's Lagarde: inflation is cooling"
        assert F.is_fj_session_report_row(art) is False
