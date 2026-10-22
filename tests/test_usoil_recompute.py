"""
Regression test: unroutable asset_impact rows must not crash the sentiment
recompute.

Reproduces the 2026-10-06 incident where 399 legacy asset_impact rows
carrying asset='USOIL' caused ``Asset(impact.asset)`` to raise ``ValueError``
inside ``_recompute_sentiment``, which killed every pipeline tick.

After the fix:
  1. ``_purge_unroutable_impacts`` deletes non-Asset rows before the recompute.
  2. ``_recompute_sentiment`` skips unroutable rows defensively (never raises).
  3. A row whose enum string is stale (e.g. strength='STRONG', which the
     ImpactStrength enum never had) is degraded, not raised on.

NOTE (2026-10-22): crude oil is a first-class asset again - ``Asset.OIL`` is
back as "USOIL" - so 'USOIL' rows are ROUTABLE now. The unroutable path is
therefore exercised with a symbol that is still not an asset ('XPTUSD').
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.db_models import AssetImpact, Base, NewsArticle
from app.models.enums import Asset
from app.services.pipeline import _recompute_sentiment, _purge_unroutable_impacts
from app.services.pipeline import IngestStats

#: A symbol that is not a member of the Asset enum, in any version.
UNROUTABLE = "XPTUSD"


def _setup_db(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


def _insert_rows(engine, asset: str, count: int = 5, strength: str = "STRONG"):
    """Insert legacy impact rows (with parent articles) for one asset string."""
    from datetime import datetime, timezone
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        for i in range(count):
            art = NewsArticle(
                guid=f"legacy-{asset}-{i}",
                source="test",
                source_quality="TIER_2_MAJOR",
                url=f"http://test.example/{i}",
                title=f"Oil price story {i}",
                body="Test body about oil prices.",
                published_at_utc=datetime.now(timezone.utc),
                is_today=True,
            )
            session.add(art)
            session.flush()
            session.add(
                AssetImpact(
                    asset=asset,
                    article_id=art.id,
                    direction="BEARISH",
                    strength=strength,
                    relevance=1.0,
                    persistence=1.0,
                    transmission="OIL_NEWS",
                    reason="legacy row",
                    metal_character=None,
                )
            )
        session.commit()
    return SessionLocal


def test_purge_removes_unroutable_assets(tmp_path):
    """ _purge_unroutable_impacts must delete all rows whose asset is NOT an Asset member."""
    engine = _setup_db(tmp_path)
    SessionLocal = _insert_rows(engine, UNROUTABLE)

    with SessionLocal() as session:
        before = session.execute(
            text(f"SELECT COUNT(*) FROM asset_impact WHERE asset = '{UNROUTABLE}'")
        ).scalar()
        assert before == 5

        purged = _purge_unroutable_impacts(session)
        session.commit()

        after = session.execute(
            text(f"SELECT COUNT(*) FROM asset_impact WHERE asset = '{UNROUTABLE}'")
        ).scalar()
        assert after == 0
        assert purged == 5


def test_purge_keeps_oil_rows(tmp_path):
    """USOIL is an Asset again, so oil rows must SURVIVE the purge."""
    engine = _setup_db(tmp_path)
    SessionLocal = _insert_rows(engine, "USOIL")

    with SessionLocal() as session:
        purged = _purge_unroutable_impacts(session)
        session.commit()

        after = session.execute(
            text("SELECT COUNT(*) FROM asset_impact WHERE asset = 'USOIL'")
        ).scalar()
        assert after == 5
        assert purged == 0


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
            guid="test-unroutable-1",
            source="test",
            source_quality="TIER_2_MAJOR",
            url="http://test.example/unroutable",
            title="Unroutable crash test",
            body="Test body about oil prices.",
            published_at_utc=now,
            is_today=True,
        )
        session.add(art)
        session.flush()
        session.add(
            AssetImpact(
                asset=UNROUTABLE,
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
        # The unroutable row was skipped, not crashed on
        assert stats.skipped_unroutable == 1
        # The row should still exist (recompute skips, doesn't delete)
        left = session.execute(
            text(f"SELECT COUNT(*) FROM asset_impact WHERE asset = '{UNROUTABLE}'")
        ).scalar()
        assert left == 1
        # Only the unroutable impact exists → no valid signals
        assert len(signals) == 0


def test_recompute_survives_a_stale_enum_string(tmp_path):
    """
    A ROUTABLE row whose stored enum string is stale (strength='STRONG', which
    ImpactStrength never had) must be degraded to a default, not raised on.
    """
    engine = _setup_db(tmp_path)
    SessionLocal = _insert_rows(engine, "USOIL", count=1, strength="STRONG")

    import app.services.pipeline as p
    p._calendar_signals = []

    stats = IngestStats()
    with SessionLocal() as session:
        signals, regime = _recompute_sentiment(session, stats)
        assert stats.skipped_unroutable == 0
        oil_signals = [s for s in signals if s.asset is Asset.OIL]
        assert len(oil_signals) == 1
        assert oil_signals[0].strength.value == "MEDIUM"


def test_supported_assets_enum_has_oil():
    """Crude oil is a first-class asset again, alongside the metals."""
    assets = {a.value for a in Asset}
    assert "USOIL" in assets
    assert "XAUUSD" in assets
    assert "XAGUSD" in assets
    assert "BTCUSD" in assets
    assert "EQUITY" in assets
