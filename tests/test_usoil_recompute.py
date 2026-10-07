"""
Regression test: USOIL/Asset.OIL rows must not crash the sentiment recompute.

Reproduces the 2026-10-06 incident where 399 legacy asset_impact rows carrying
asset='USOIL' caused ``Asset(impact.asset)`` to raise ``ValueError`` inside
``_recompute_sentiment``, which killed every pipeline tick.

After the fix:
  1. ``_purge_unroutable_impacts`` deletes non-Asset rows before the recompute.
  2. ``_recompute_sentiment`` skips unroutable rows defensively (never raises).
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.db_models import AssetImpact, Base, NewsArticle
from app.models.enums import Asset
from app.services.pipeline import _recompute_sentiment, _purge_unroutable_impacts
from app.services.pipeline import IngestStats


def _setup_db(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def _insert_usoil_rows(engine):
    """Insert legacy USOIL impact rows (with parent articles) that should be purged."""
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        for i in range(5):
            art = NewsArticle(
                guid=f"usoil-legacy-{i}",
                source="test",
                source_quality="TIER_2_MAJOR",
                url=f"http://test.example/{i}",
                title=f"Oil price story {i}",
                body="Test body about oil prices.",
            )
            session.add(art)
            session.flush()
            session.add(
                AssetImpact(
                    asset="USOIL",
                    article_id=art.id,
                    direction="BEARISH",
                    strength="STRONG",
                    relevance=1.0,
                    persistence=1.0,
                    transmission="OIL_NEWS",
                    reason="legacy row - should be purged",
                    metal_character=None,
                )
            )
        session.commit()
    return SessionLocal


def test_purge_removes_unroutable_assets(tmp_path):
    """ _purge_unroutable_impacts must delete all rows whose asset is NOT XAUUSD/XAGUSD/BTCUSD."""
    engine = _setup_db(tmp_path)
    SessionLocal = _insert_usoil_rows(engine)

    with SessionLocal() as session:
        before = session.execute(
            text("SELECT COUNT(*) FROM asset_impact WHERE asset = 'USOIL'")
        ).scalar()
        assert before == 5

        purged = _purge_unroutable_impacts(session)
        session.commit()

        after = session.execute(
            text("SELECT COUNT(*) FROM asset_impact WHERE asset = 'USOIL'")
        ).scalar()
        assert after == 0
        assert purged == 5


def test_recompute_does_not_crash_on_unroutable(tmp_path):
    """
    Even if unroutable rows survive the purge, ``_recompute_sentiment`` must
    skip them instead of raising ``ValueError`` from ``Asset(impact.asset)``.
    """
    engine = _setup_db(tmp_path)

    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        art = NewsArticle(
            guid="test-usoil-1",
            source="test",
            source_quality="TIER_2_MAJOR",
            url="http://test.example/usoil",
            title="USOIL legacy crash test",
            body="Test body about oil prices.",
            published_at_utc=now,
            is_today=True,
        )
        session.add(art)
        session.flush()
        session.add(
            AssetImpact(
                asset="USOIL",
                article_id=art.id,
                direction="BEARISH",
                strength="STRONG",
                relevance=1.0,
                persistence=1.0,
                transmission="OIL_NEWS",
                reason="legacy row - should be skipped",
                metal_character=None,
            )
        )
        session.commit()

    # Patch calendar signals to empty
    import app.services.pipeline as p
    p._calendar_signals = []

    stats = IngestStats()
    with SessionLocal() as session:
        signals, regime = _recompute_sentiment(session, stats)
        # The USOIL row was skipped, not crashed on
        assert stats.skipped_unroutable == 1
        # The USOIL row should still exist (recompute skips, doesn't delete)
        usoil = session.execute(
            text("SELECT COUNT(*) FROM asset_impact WHERE asset = 'USOIL'")
        ).scalar()
        assert usoil == 1
        # Only the USOIL impact exists → no valid signals
        assert len(signals) == 0


def test_supported_assets_enum_has_no_oil():
    """Make sure Asset.OIL was removed and only XAUUSD/XAGUSD/BTCUSD remain."""
    assets = {a.value for a in Asset}
    assert "USOIL" not in assets
    assert "XAUUSD" in assets
    assert "XAGUSD" in assets
    assert "BTCUSD" in assets
