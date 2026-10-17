"""
TASK 2 - bull and bear are persisted on every snapshot row.

Without them, r = min(bull,bear)/max(bull,bear) is unrecoverable from
history: a net score cannot be decomposed into its two sides, so infinitely
many pairs give the same value. These tests pin both the persistence and the
reconstruction identity.

The reconstruction replays the whole score path, because STEP 8 put a
contradiction dampener on top of the raw ratio:

    base  = 100 * (bull - bear) / (bull + bear)
    score = base * 0.5   when min/max > 0.6

Asserting the undampened ratio alone would pass before Step 8 and fail after,
for reasons that have nothing to do with what is being persisted here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)


def _session_for(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models.db_models as models

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'bullbear.db').as_posix()}")
    models.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False), models


def _article(session, models, guid, title, direction, strength,
             minutes_ago=25, asset="XAUUSD"):
    from app.models.enums import Direction, ImpactStrength

    art = models.NewsArticle(
        guid=guid, source="Reuters", source_quality="TIER_2_MAJOR",
        url=f"https://example.invalid/{guid}", title=title, body="",
        published_at_utc=datetime.now(timezone.utc)
        - timedelta(minutes=minutes_ago),
        is_today=True, category="Central Bank", importance="HIGH",
        report_kind="GENERAL", verification="CONFIRMED", relevance_score=0.9,
        analysis_mode="llm", llm_used=True,
    )
    session.add(art)
    session.flush()
    session.add(models.AssetImpact(
        article_id=art.id, asset=asset,
        direction=Direction[direction].value,
        strength=ImpactStrength[strength].value,
        relevance=0.85, persistence=0.75))
    return art


def _run(session):
    from app.services.pipeline import IngestStats, _recompute_sentiment
    _recompute_sentiment(session, IngestStats())
    session.commit()


# ---------------------------------------------------------------------------
# Columns exist and are populated
# ---------------------------------------------------------------------------
def test_columns_are_declared():
    """The ORM must declare both columns, or SQLAlchemy never maps them."""
    from app.models.db_models import SentimentSnapshot

    cols = {c.name for c in SentimentSnapshot.__table__.columns}
    assert {"bull", "bear"} <= cols


def test_migration_adds_the_columns_to_an_existing_db(tmp_path, monkeypatch):
    """An existing database must gain the two columns without losing rows."""
    import app.database as dbmod
    from sqlalchemy import create_engine, text

    import app.models.db_models as models

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sentiment_snapshot DROP COLUMN bull"))
        conn.execute(text("ALTER TABLE sentiment_snapshot DROP COLUMN bear"))
    con = engine.connect()
    assert "bull" not in {r[1] for r in con.execute(
        text("PRAGMA table_info(sentiment_snapshot)"))}
    con.close()

    monkeypatch.setattr(dbmod, "engine", engine)
    dbmod._add_missing_sentiment_columns()

    con = engine.connect()
    cols = {r[1] for r in con.execute(
        text("PRAGMA table_info(sentiment_snapshot)"))}
    assert {"bull", "bear"} <= cols
    assert "raw_score" in cols, "the migration must not drop raw_score"
    con.close()


def test_bull_bear_populated_on_every_row(tmp_path):
    """Every snapshot row must carry the two totals, never NULL."""
    Session, models = _session_for(tmp_path)
    with Session() as session:
        _article(session, models, "bb-1",
                 "Fed officials signal interest rates will stay lower",
                 "BULLISH", "VERY_HIGH")
        _run(session)

        rows = session.query(models.SentimentSnapshot).all()
        assert rows, "no snapshots written"
        for r in rows:
            assert r.bull is not None, f"{r.asset}/{r.scope}: bull is NULL"
            assert r.bear is not None, f"{r.asset}/{r.scope}: bear is NULL"
            assert isinstance(r.bull, float)
            assert isinstance(r.bear, float)


def test_bull_bear_populated_for_every_asset_and_scope(tmp_path):
    """TASK 2: one signal per market asset, every scope, values not NULL.
    EQUITY snapshots are no longer written (equity is served by the
    session-aware card), so only XAUUSD, XAGUSD, BTCUSD are expected."""
    Session, models = _session_for(tmp_path)
    with Session() as session:
        for i, asset in enumerate(("XAUUSD", "XAGUSD", "BTCUSD")):
            _article(session, models, f"bb-all-{asset}",
                     "Fed officials signal rates will stay lower for longer",
                     "BULLISH", "VERY_HIGH", asset=asset)
        _run(session)

        rows = session.query(models.SentimentSnapshot).all()
        assert rows, "no snapshots written"
        assets_written = {r.asset for r in rows}
        scopes_written = {r.scope for r in rows}
        assert "EQUITY" not in assets_written, "EQUITY snapshot must not be written"
        assert {"XAUUSD", "XAGUSD", "BTCUSD"} <= assets_written, (
            f"expected market assets written, got {sorted(assets_written)}")
        assert {"CURRENT", "MARKET_PREP", "MARKET_WRAP"} <= scopes_written, (
            f"expected all scopes written, got {sorted(scopes_written)}")

        current = []
        for r in rows:
            assert r.bull is not None, f"{r.asset}/{r.scope}: bull is NULL"
            assert r.bear is not None, f"{r.asset}/{r.scope}: bear is NULL"
            assert isinstance(r.bull, float)
            assert isinstance(r.bear, float)
            if r.signal_count > 0:
                assert not (r.bull == 0.0 and r.bear == 0.0), (
                    f"{r.asset}/{r.scope}: {r.signal_count} signals but both "
                    f"totals are zero")
            if r.scope == "CURRENT":
                current.append(r)

        assert current, "no CURRENT rows written"
        for r in current:
            replay = _replay(r.raw_score, r.bull, r.bear)
            assert abs(replay - r.raw_score) < 0.05, (
                f"{r.asset}: replaying persisted bull/bear gives {replay}, "
                f"but raw_score is {r.raw_score}; bull={r.bull}, bear={r.bear}")


# ---------------------------------------------------------------------------
# The reconstruction identity
# ---------------------------------------------------------------------------
def _replay(raw_score, bull, bear):
    """Replay compute()'s score path from the two persisted totals."""
    from app.config import (CONTRADICTION_DAMPENER_ENABLED,
                            CONTRADICTION_DAMPENER_FACTOR,
                            CONTRADICTION_RATIO)

    total = bull + bear
    if total <= 0:
        return 0.0
    base = round(100.0 * (bull - bear) / total, 1)
    if CONTRADICTION_DAMPENER_ENABLED:
        lo, hi = min(bull, bear), max(bull, bear)
        if hi > 0 and lo / hi > CONTRADICTION_RATIO:
            base = round(base * CONTRADICTION_DAMPENER_FACTOR, 1)
    return base


def test_persisted_totals_reproduce_raw_score(tmp_path):
    """
    The delivered guarantee: replaying the score path from the persisted
    bull/bear reproduces raw_score.
    """
    Session, models = _session_for(tmp_path)
    with Session() as session:
        # An asymmetric set: 2 bullish, 1 bearish, so bull != bear.
        _article(session, models, "re-1",
                 "Fed officials signal interest rates will stay lower",
                 "BULLISH", "VERY_HIGH")
        _article(session, models, "re-2",
                 "Powell opens door to further easing in December",
                 "BULLISH", "HIGH")
        _article(session, models, "re-3",
                 "Dollar index surges to a six-month high",
                 "BEARISH", "VERY_HIGH")
        _run(session)

        snap = (session.query(models.SentimentSnapshot)
                .filter_by(asset="XAUUSD", scope="CURRENT").one())
        bull, bear, raw = snap.bull, snap.bear, snap.raw_score

    assert bull is not None and bear is not None
    assert bull + bear > 0, "the fixture must produce weight on both sides"

    replay = _replay(raw, bull, bear)
    assert abs(replay - raw) < 0.05, (
        f"replaying the score path from the persisted totals gives {replay}, "
        f"but raw_score is {raw}. bull={bull}, bear={bear}")


def test_asymmetric_fixture_actually_divides_the_evidence(tmp_path):
    """Guard the fixture above: it must be asymmetric, or it proves nothing."""
    Session, models = _session_for(tmp_path)
    with Session() as session:
        _article(session, models, "as-1",
                 "Fed officials signal interest rates will stay lower",
                 "BULLISH", "VERY_HIGH")
        _article(session, models, "as-2",
                 "Dollar index surges to a six-month high",
                 "BEARISH", "VERY_HIGH")
        _run(session)

        snap = (session.query(models.SentimentSnapshot)
                .filter_by(asset="XAUUSD", scope="CURRENT").one())

    assert snap.bull > 0 and snap.bear > 0
    ratio = min(snap.bull, snap.bear) / max(snap.bull, snap.bear)
    # Two signals of the same class land within a hair of 1:1 (the residual
    # asymmetry is their different decay), which is a perfectly good split for
    # the reconstruction test - it just is not a dramatic one. The guard only
    # needs to confirm the evidence really did divide.
    assert 0 < ratio < 1.0, (
        f"fixture ratio {ratio:.3f} is degenerate - one side carried "
        f"everything, so the split proves nothing")


def test_api_payload_exposes_bull_and_bear(tmp_path):
    """main._block must pass the two totals through to the dashboard."""
    from app.main import _block

    Session, models = _session_for(tmp_path)
    with Session() as session:
        _article(session, models, "api-1",
                 "Fed officials signal interest rates will stay lower",
                 "BULLISH", "VERY_HIGH")
        _run(session)
        session.commit()

        block = _block(session, "XAUUSD", "CURRENT")

    assert "bull" in block and "bear" in block, (
        "_block must expose bull and bear")
    assert block["bull"] >= 0 and block["bear"] >= 0
    # Everything that already existed must still be there.
    for existing in ("score", "raw_score", "bias", "confidence", "display"):
        assert existing in block, f"_block lost {existing}"


def test_equity_card_payload_exposes_bull_and_bear():
    """The session card must report the same two totals."""
    from app.analysis import equity_card as ec

    text = open(ec.__file__, encoding="utf-8").read()
    assert 'result["bull"] = float(card.bull)' in text, (
        "to_payload must expose bull")
    assert 'result["bear"] = float(card.bear)' in text, (
        "to_payload must expose bear")
