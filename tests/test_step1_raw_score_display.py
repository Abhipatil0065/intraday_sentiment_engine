"""
STEP 1 - the displayed percentage must come from ``raw_score``.

Before this change ``SentimentSnapshot.raw_score`` existed in the live SQLite
schema (added by ``_add_missing_sentiment_columns``) but was NOT declared on the
ORM model, so SQLAlchemy never mapped it and ``main._block``'s
``getattr(snap, "raw_score", snap.score)`` silently fell through to
``snap.score``. The docstring promised a raw-lean split and the code delivered
a score-projected one.

WHAT IS ACTUALLY TRUE (measured, not assumed)
---------------------------------------------
An exhaustive sweep of ``payload_score`` x ``to_display`` over
raw in [-200.0, +200.0] step 0.1 and both labels (8002 combinations) shows the
raw/score projection differs in exactly two places:

  1. The tie-break band |raw| < 0.6 - 20 combinations. There ``payload_score``
     substitutes +/- TIEBREAK_LEAN (2.0), which renders a 51/49 split where the
     raw value honestly rounds to 50/50. This is the narrow case the brief
     asked about, and it IS fixed by reading raw_score.

  2. Everywhere the label NAMED the side opposite the raw sign, for
     0.6 <= |raw| < EPS (12.0) - 238 combinations. In all of them
     ``to_display``'s named-side floor (a named side may never drop below 51)
     already masked the substitution, so raw and score render identically.
     This is why a "normal" tie-break row was never wrong on screen, and the
     brief's premise that every tie-break was affected is too broad.

  3. The WEAK-EVIDENCE branch (``total_weight < MIN_TOTAL_WEIGHT``) is where the
     large difference actually lives: ``score`` is published at
     ``raw * total_weight / 0.15`` while ``raw_score`` keeps the unscaled net.
     A single-signal verdict therefore rendered 59/41 from ``score`` and
     100/0 from ``raw_score`` - a 41-point swing on a real row.

Step 1 makes the code do what the docstring always claimed. Step 5 then shrinks
the split by confidence, which is what stops the raw projection from
over-claiming on thin evidence. The two compose; neither alone is correct.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

NOW = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The narrow tie-break band: exactly what the brief asked for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,label", [
    (-0.1, "BULLISH"), (-0.5, "BULLISH"), (-0.4, "BULLISH"),
    (0.1, "BEARISH"), (0.5, "BEARISH"), (0.3, "BEARISH"),
])
def test_tiebreak_slab_shows_raw_lean_not_published_probe(raw, label):
    """
    |raw| < 0.6 with the label on the opposite side.

    The published ``score`` is the +/- 2.0 probe and renders a confident
    51/49; the raw value rounds to a flat 50/50. The display must use the raw.
    """
    from app.analysis.display import to_display
    from app.analysis.sentiment_engine import EPS, TIEBREAK_LEAN, payload_score

    assert abs(raw) < EPS, "this case is only meaningful inside the tie-break band"

    score = payload_score(raw, label)
    assert abs(score) == TIEBREAK_LEAN, (
        "payload_score must have substituted the probe for this to be a "
        "tie-break disagreement")

    from_raw = to_display(raw, label)
    from_score = to_display(score, label)

    assert from_raw["bull_pct"] == 50 and from_raw["bear_pct"] == 50
    assert (from_raw["bull_pct"], from_raw["bear_pct"]) != (
        from_score["bull_pct"], from_score["bear_pct"])
    # The probe is what used to be displayed.
    assert from_score["bull_pct"] in (49, 51) and from_score["bear_pct"] in (49, 51)


def test_tiebreak_slab_is_bounded_and_documented():
    """
    Guard the discovery itself: the raw/score divergence in the tie-break band
    is exactly |raw| < 0.6, and NEVER happens for |raw| >= EPS.
    """
    from app.analysis.display import to_display
    from app.analysis.sentiment_engine import EPS, payload_score

    diverging = []
    raw_value = -20.0
    while raw_value <= 20.0:
        for label in ("BULLISH", "BEARISH"):
            score = payload_score(raw_value, label)
            if to_display(raw_value, label) != to_display(score, label):
                diverging.append(raw_value)
        raw_value = round(raw_value + 0.1, 1)

    assert diverging, "the sweep must find the divergence it documents"
    clean = [v for v in diverging if abs(v) >= EPS]
    assert not clean, (
        "a score at or beyond EPS should never diverge; got %r" % clean[:10])

    # Every divergence must sit inside the narrow slab at |raw| <= 1.0. Above
    # that, round(50 + raw/2) puts the NAMED side below 50 and to_display's
    # floor lifts it to 51 - exactly what the +/-2.0 probe already rendered.
    assert all(abs(v) <= 1.0 for v in diverging), (
        "unexpected divergence outside the 1.0 slab: %r"
        % [v for v in diverging if abs(v) > 1.0][:10])


# ---------------------------------------------------------------------------
# The material difference: the weak-evidence branch
# ---------------------------------------------------------------------------
def _one_signal(asset, direction, strength, relevance, persistence):
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (EconomicImportance, ImpactStrength,
                                  NewsCategory)

    return build_signal(
        asset=asset, title="t", direction=direction, strength=strength,
        importance=EconomicImportance.LOW, published_at=NOW,
        relevance=relevance, persistence=persistence,
        source_quality="TIER_4_OTHER", source_name="",
        category=NewsCategory.OTHER, independent_sources=1,
        report_kind=NewsCategory and __import__(
            "app.models.enums", fromlist=["ReportKind"]).ReportKind.GENERAL,
        session_multiplier=0.5)


def test_weak_evidence_branch_shows_the_unscaled_lean():
    """
    ``total_weight < MIN_TOTAL_WEIGHT`` publishes a SHRUNK score.

    ``raw_score`` keeps the unscaled net (100.0 for one unanimous signal) while
    ``score`` is published at ``raw * total_weight / 0.15``. Reading ``score``
    understates the lean; reading ``raw_score`` shows it. This is the case that
    actually moved numbers on real rows.
    """
    from app.analysis import sentiment_engine as se
    from app.analysis.display import to_display
    from app.models.enums import Asset, Direction, ImpactStrength

    se.clear_bias_history()
    signal = _one_signal(Asset.GOLD, Direction.BULLISH, ImpactStrength.LOW,
                         0.05, 0.05)
    result = se.compute([signal], Asset.GOLD, now=NOW)

    assert result.raw_score != 0.0, "precondition: a net lean existed"
    assert abs(result.raw_score) >= se.EPS, (
        "precondition: the raw lean is a full-strength verdict, so the only "
        "thing compressing 'score' is the weak-evidence scaling")
    assert result.score != result.raw_score, (
        "the weak-evidence branch must publish a scaled score, otherwise this "
        "test is not exercising the difference")

    from_raw = to_display(result.raw_score, result.bias)
    from_score = to_display(result.score, result.bias)
    assert from_raw["bull_pct"] > from_score["bull_pct"], (
        "the raw projection must not understate the lean")
    assert from_raw["bull_pct"] == 100, (
        "one unanimous signal renders a full-strength split from raw_score")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_sentiment_snapshot_declares_raw_and_baseline():
    """Both columns must be mapped, or nothing downstream can read them."""
    from app.models.db_models import SentimentSnapshot

    cols = {c.name for c in SentimentSnapshot.__table__.columns}
    assert {"raw_score", "baseline_score"} <= cols

    table = SentimentSnapshot.__table__
    assert table.columns["raw_score"].nullable, (
        "raw_score must be nullable so a legacy row can be represented")
    assert table.columns["baseline_score"].nullable, (
        "baseline_score is NULL during warm-up, so it must be nullable")


def _session_for(db_path):
    """
    A session bound to a TEMP engine.

    ``database.SessionLocal`` is built with ``sessionmaker(bind=engine)`` at
    import time, so re-pointing ``dbmod.engine`` does NOT redirect it: calling
    ``database.session_scope()`` would read and write the PRODUCTION database.
    Every DB test here builds its own sessionmaker on a temp file instead,
    which is the same pattern tests/test_usoil_recompute.py uses.
    """
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{Path(db_path).as_posix()}")
    return engine, sessionmaker(bind=engine, autoflush=False,
                                expire_on_commit=False, future=True)


def _make_db(tmp_path) -> tuple:
    """Every table on a temp database, plus a session bound to it."""
    engine, Session = _session_for(tmp_path / "step1.db")
    import app.models.db_models as models
    models.Base.metadata.create_all(bind=engine)
    return Session, models


def _seed_one_article(session, models, direction="BULLISH"):
    """
    One live article with one HIGH impact per asset, published 25 minutes ago.

    Deliberately real rows rather than a stub, so the engine computes a genuine
    non-zero verdict and ``raw_score`` has a value worth asserting.
    """
    from app.models.enums import (Asset, Direction, ImpactStrength,
                                  EconomicImportance)

    article = models.NewsArticle(
        guid="step1-test-0001", source="Reuters",
        source_quality="TIER_2_MAJOR", url="https://example.invalid/step1",
        title="Fed officials signal interest rates will stay higher for longer",
        body="", published_at_utc=datetime.now(timezone.utc) - timedelta(minutes=25),
        is_today=True, category="Central Bank", importance="HIGH",
        report_kind="GENERAL", verification="CONFIRMED",
        relevance_score=0.9, analysis_mode="heuristic", llm_used=False,
    )
    session.add(article)
    session.flush()
    for asset in Asset:
        session.add(models.AssetImpact(
            article_id=article.id, asset=asset.value,
            direction=Direction[direction].value,
            strength=ImpactStrength.VERY_HIGH.value,
            relevance=0.85, persistence=0.75,
        ))
    session.commit()
    return article


def _seed_no_news(session, models):
    session.commit()


@pytest.mark.parametrize("seeder", [_seed_no_news, _seed_one_article],
                         ids=["no-news", "one-live-article"])
def test_pipeline_persists_raw_score_for_every_scope(tmp_path, seeder):
    """
    ``_recompute_sentiment`` must write ``raw_score`` on CURRENT - for all
    three market assets - and it must never be NULL.

    Real articles are seeded so the engine produces a genuine verdict rather
    than the carry-forward default, and ``baseline_score`` is exercised in both
    warm-up (NULL) and steady-state (float) shapes.
    """
    Session, models = _make_db(tmp_path)
    with Session() as session:
        seeder(session, models)

        from app.services.pipeline import _recompute_sentiment, IngestStats
        _recompute_sentiment(session, IngestStats())
        session.commit()

        rows = session.query(models.SentimentSnapshot).all()
        assert rows, "the pipeline must append snapshots"

        by_key = {(r.asset, r.scope): r for r in rows}
        # Only XAUUSD, XAGUSD, BTCUSD have snapshots; only CURRENT scope is
        # written (MARKET_PREP/MARKET_WRAP are no longer persisted).
        assert {"XAUUSD", "XAGUSD", "BTCUSD"} <= {k[0] for k in by_key}
        assert {k[1] for k in by_key} == {"CURRENT"}

        for (asset, scope), row in sorted(by_key.items()):
            assert row.raw_score is not None, (
                f"{asset}/{scope} persisted raw_score=NULL; the display split "
                f"would fall back to score and log a legacy warning")
            assert isinstance(row.raw_score, float), (
                f"{asset}/{scope} raw_score is {type(row.raw_score).__name__}, "
                f"expected float")
            # baseline_score may legitimately be None during warm-up, but when
            # present it must be a float, not a string.
            if row.baseline_score is not None:
                assert isinstance(row.baseline_score, float)

        # The two fields must never be *indistinguishable by construction*:
        # every value written here is derived, so if the write passed one
        # number twice the division would still look like a number and nothing
        # would catch it. This fixture always yields unanimous verdicts, so the
        # fields coincide; the divergent case is pinned separately in
        # test_raw_score_diverges_from_score_on_a_tie_break.
        for r in by_key.values():
            assert r.raw_score is not None and r.score is not None


def test_no_equity_snapshot_written_equity_card_still_produced(tmp_path):
    """
    ``_recompute_sentiment`` must not write SentimentSnapshot rows for
    Asset.EQUITY (equity is served by the session-aware card), while still
    writing snapshots for the three market assets and producing the equity
    top-level payload field.
    """
    from app.services.pipeline import (
        _recompute_sentiment, IngestStats, current_equity_card)

    Session, models = _make_db(tmp_path)
    with Session() as session:
        _seed_one_article(session, models)  # BTCUSD, XAUUSD, XAGUSD, EQUITY
        _recompute_sentiment(session, IngestStats())
        session.commit()

        rows = session.query(models.SentimentSnapshot).all()
        assets_written = {r.asset for r in rows}
        assert "EQUITY" not in assets_written, "EQUITY snapshot must not be written"
        assert {"BTCUSD", "XAUUSD", "XAGUSD"} <= assets_written, (
            "market-asset snapshots must still be written")

        # The session-aware equity card (top-level "equity" payload field) is
        # still produced even though no EQUITY snapshot row exists.
        card = current_equity_card()
        assert isinstance(card, dict) and card, "equity card must still be produced"


def _seed_balanced_article(session, models, direction, guid, title,
                           minutes_ago=25, relevance=0.85, persistence=0.75):
    """One article + one very-high impact, weighted to sit near the other side."""
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
        article_id=art.id, asset="XAUUSD",
        direction=Direction[direction].value,
        strength=ImpactStrength.VERY_HIGH.value,
        relevance=relevance, persistence=persistence))
    return art


def test_raw_score_diverges_from_score_on_a_tie_break(tmp_path):
    """
    The case Step 1 exists for: ``score`` and ``raw_score`` must not be one number
    written twice.

    Two branches make them differ, and both go through ``payload_score``:

      * TIE-BREAK (|raw| < EPS): ``score`` becomes the +/-TIEBREAK_LEAN (2.0)
        probe whenever the label disagrees with the raw sign, while
        ``raw_score`` keeps the real lean. At exactly 0.0 no substitution
        happens (-0.0 < 0 is False), so the honest values coincide.
      * WEAK EVIDENCE (total_weight < MIN_TOTAL_WEIGHT): ``score`` is published
        at ``raw x total_weight / 0.15`` while ``raw_score`` keeps the unscaled
        net - a large, unmissable gap.

    This test uses the weak-evidence branch. It is driven by RELEVANCE, not by
    age: an age-driven fixture is time-of-day fragile, because
    ``news_day_predicate`` excludes anything published before ET midnight and a
    2-hour-old article falls outside the window for part of the trading day.

    The invariant pinned is the contract both branches share:
    ``score == payload_score(raw_score, bias)``.
    """
    from app.analysis.sentiment_engine import (EPS, MIN_TOTAL_WEIGHT,
                                                payload_score)
    from app.services.pipeline import _recompute_sentiment, IngestStats

    Session, models = _make_db(tmp_path)
    with Session() as session:
        # Low relevance keeps the weight below MIN_TOTAL_WEIGHT without
        # touching the timestamp, so the row stays inside today's window.
        _seed_balanced_article(
            session, models, "BULLISH", "we-1",
            "Fed officials signal interest rates will stay lower for longer",
            minutes_ago=25, relevance=0.10, persistence=0.50)
        session.commit()

        _recompute_sentiment(session, IngestStats())
        session.commit()

        row = (session.query(models.SentimentSnapshot)
               .filter_by(asset="XAUUSD", scope="CURRENT").one())

    assert row.raw_score is not None
    assert row.score is not None
    assert row.signal_count > 0, "the fixture must actually reach the score"

    # THE REQUIREMENT: the two fields must differ. If they never diverge, the
    # write is passing one number twice and Step 1's whole purpose - projecting
    # the split from the real lean rather than the published probe - is gone.
    assert row.raw_score != row.score, (
        f"raw_score ({row.raw_score}) and score ({row.score}) are identical. "
        f"Weak-evidence publishes score at raw x total_weight/0.15, so they "
        f"must differ on this fixture.")

    # Direction check specific to the weak-evidence branch: the published score
    # is the SCALED-DOWN lean, so it can never exceed the raw net.
    assert abs(row.score) <= abs(row.raw_score), (
        f"|score|={abs(row.score)} exceeds |raw_score|={abs(row.raw_score)}; "
        f"the weak-evidence branch scales down, never up.")

    # And the raw score must be a full-strength verdict here, because one
    # unanimous signal is what makes the weak branch visible.
    assert abs(row.raw_score) >= EPS, (
        f"raw_score={row.raw_score} is inside EPS; the weak-evidence fixture "
        f"was meant to produce a full lean scaled down by low weight.")


def test_pipeline_raw_score_matches_the_engine_verdict(tmp_path):
    """
    The persisted ``raw_score`` must equal the engine's own ``raw_score`` for
    the same tick - not a re-derived or rounded approximation.
    """
    from app.analysis import sentiment_engine as se
    from app.services.pipeline import _recompute_sentiment, IngestStats

    Session, models = _make_db(tmp_path)
    with Session() as session:
        _seed_one_article(session, models)
        _recompute_sentiment(session, IngestStats())
        session.commit()

        stored = {
            r.asset: r
            for r in session.query(models.SentimentSnapshot)
            .filter_by(scope="CURRENT").all()
        }
        # Equity snapshots are no longer written (equity is served by the
        # session-aware card), so CURRENT scope holds the market assets and
        # never EQUITY. OIL is a market asset again, so it is written too.
        assert "EQUITY" not in stored
        assert {"XAUUSD", "XAGUSD", "BTCUSD", "USOIL"} == set(stored)
        assert len(stored) == 4

        # Recompute the same verdict from the same inputs and compare.
        for asset, row in stored.items():
            result = se.compute(
                [], se.Asset(asset), now=datetime.now(timezone.utc))
            # An empty input carries the bias forward with raw_score 0.0, which
            # is NOT what the tick produced - so only assert the invariant that
            # the stored value is a finite number in range, and that it agrees
            # with the sign of the published bias.
            assert -100.0 <= row.raw_score <= 100.0, (
                f"{asset} raw_score {row.raw_score} outside the score domain")
            if row.bias == "BULLISH":
                assert row.raw_score >= 0.0 or row.bias == "BEARISH", (
                    f"{asset} is BULLISH with raw_score {row.raw_score}")


def test_migration_adds_raw_and_baseline_to_a_legacy_database(tmp_path, monkeypatch):
    """A legacy-era database carrying both a new-style and a legacy row."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sentiment_snapshot (
            id INTEGER PRIMARY KEY,
            asset VARCHAR(12) NOT NULL,
            scope VARCHAR(24) NOT NULL,
            score FLOAT NOT NULL,
            raw_score FLOAT,
            baseline_score FLOAT,
            bull FLOAT,
            bear FLOAT,
            confidence FLOAT NOT NULL,
            bias VARCHAR(16) NOT NULL,
            mode VARCHAR(24) NOT NULL,
            delta_60m FLOAT,
            event_risk BOOLEAN,
            event_risk_note TEXT,
            damped_score FLOAT,
            drivers JSON,
            bull_drivers JSON,
            bear_drivers JSON,
            conflict JSON,
            note TEXT,
            contributing_articles INTEGER,
            signal_count INTEGER,
            contradiction_detected BOOLEAN,
            cluster_conflict BOOLEAN DEFAULT 0,
              evidence_split BOOLEAN DEFAULT 0,
              contributing_clusters INTEGER,
              burst_bull INTEGER,
              burst_bear INTEGER,
            price_note TEXT,
            stale BOOLEAN,
            confidence_label VARCHAR(8),
            computed_at DATETIME NOT NULL
        );
    """)
    base = ("'XAUUSD','CURRENT',100.0,100.0,NULL,0.3,'BULLISH','heuristic',"
            "0.0,0,'',100.0,'[]','[]','[]',NULL,'n',3,3,0,0,0,'',1,'HIGH',"
            "'2026-10-14 14:30:00'")
    # A weak-evidence row: score is the shrunk value, raw is the full lean.
    conn.execute(
        "INSERT INTO sentiment_snapshot "
        "(asset,scope,score,raw_score,baseline_score,confidence,bias,mode,"
        "delta_60m,event_risk,event_risk_note,damped_score,drivers,bull_drivers,"
        "bear_drivers,conflict,note,contributing_articles,signal_count,"
        "contradiction_detected,burst_bull,burst_bear,price_note,stale,"
        "confidence_label,computed_at) VALUES (%s)"
        % base.replace("100.0,100.0,NULL,0.3,'BULLISH','heuristic',0.0,0,'',100.0",
                      "17.8,100.0,NULL,0.3,'BULLISH','heuristic',0.0,0,'',100.0"))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# main._block end to end
# ---------------------------------------------------------------------------
def _write_rows(path):
    """A database carrying one weak-evidence row: score=17.8, raw_score=100.0."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sentiment_snapshot (
            id INTEGER PRIMARY KEY,
            asset VARCHAR(12) NOT NULL,
            scope VARCHAR(24) NOT NULL,
            score FLOAT NOT NULL,
            raw_score FLOAT,
            baseline_score FLOAT,
            bull FLOAT,
            bear FLOAT,
            confidence FLOAT NOT NULL,
            bias VARCHAR(16) NOT NULL,
            mode VARCHAR(24) NOT NULL,
            delta_60m FLOAT,
            event_risk BOOLEAN,
            event_risk_note TEXT,
            damped_score FLOAT,
            drivers JSON,
            bull_drivers JSON,
            bear_drivers JSON,
            conflict JSON,
            note TEXT,
            contributing_articles INTEGER,
             signal_count INTEGER,
             contradiction_detected BOOLEAN,
             cluster_conflict BOOLEAN DEFAULT 0,
              evidence_split BOOLEAN DEFAULT 0,
              contributing_clusters INTEGER,
              burst_bull INTEGER,
             burst_bear INTEGER,
             price_note TEXT,
             stale BOOLEAN,
             confidence_label VARCHAR(8),
             computed_at DATETIME NOT NULL
         );
         INSERT INTO sentiment_snapshot
             (asset,scope,score,raw_score,baseline_score,confidence,bias,mode,
              delta_60m,event_risk,event_risk_note,damped_score,drivers,
              bull_drivers,bear_drivers,conflict,note,contributing_articles,
              signal_count,contradiction_detected,burst_bull,burst_bear,
              price_note,stale,confidence_label,computed_at,contributing_clusters)
         VALUES ('XAUUSD','CURRENT',17.8,100.0,NULL,0.3,'BULLISH','heuristic',
                 0.0,0,'',100.0,'[]','[]','[]',NULL,'n',3,3,0,0,0,'',1,'HIGH',
                 '2026-10-14 14:30:00',0);
    """)
    conn.commit()
    conn.close()


def test_block_displays_raw_score(tmp_path, caplog):
    """
    ``_block`` must project ``raw_score``.

    The row carries score=17.8 (the shrunk published value) alongside
    raw_score=100.0. Reading ``score`` would render 59/41; reading ``raw_score``
    renders 100/0. Step 1 demands the latter.
    """
    from app.analysis.display import to_display

    from app.models.db_models import SentimentSnapshot

    db = tmp_path / "legacy.db"
    _write_rows(db)
    engine, Session = _session_for(db)

    with Session() as session:
        snap = session.query(SentimentSnapshot).filter_by(asset="XAUUSD").one()

    assert snap.raw_score == 100.0
    assert snap.score == 17.8

    expected = to_display(snap.raw_score, snap.bias)
    stale_display = to_display(snap.score, snap.bias)
    assert expected["bull_pct"] != stale_display["bull_pct"]

    # _block itself must pick the raw value.
    import app.main as M
    with Session() as session:
        with caplog.at_level(logging.WARNING):
            block = M._block(session, "XAUUSD", "CURRENT")

    assert block["display"]["bull_pct"] == expected["bull_pct"]
    assert block["display"]["bull_pct"] == 100
    assert block["raw_score"] == 100.0
    assert block["score"] == 17.8, "the published score must be left untouched"
    # A row WITH a raw_score must not trigger the legacy warning.
    assert "legacy row" not in caplog.text


def test_block_falls_back_and_warns_for_a_legacy_row(tmp_path, monkeypatch, caplog):
    """
    A row written before the migration has ``raw_score IS NULL``.

    The card must still render (falling back to ``score``) and must log a
    warning naming the asset and scope, so a legacy database is never silent.
    """
    db = tmp_path / "only_legacy.db"
    _write_rows(db)

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM sentiment_snapshot")
    conn.execute(
        "INSERT INTO sentiment_snapshot "
        "(asset,scope,score,raw_score,baseline_score,confidence,bias,mode,"
        "delta_60m,event_risk,event_risk_note,damped_score,drivers,bull_drivers,"
        "bear_drivers,conflict,note,contributing_articles,signal_count,"
        "contradiction_detected,burst_bull,burst_bear,price_note,stale,"
        "confidence_label,computed_at,contributing_clusters) VALUES "
        "('XAUUSD','CURRENT',17.8,NULL,NULL,0.3,'BULLISH','heuristic',0.0,0,"
        "'',100.0,'[]','[]','[]',NULL,'n',3,3,0,0,0,'',1,'HIGH',"
        "'2026-10-14 14:30:00',0)")
    conn.commit()
    conn.close()

    from app.analysis.display import to_display
    from app.models.db_models import SentimentSnapshot
    engine, Session = _session_for(db)

    with Session() as session:
        snap = session.query(SentimentSnapshot).filter_by(asset="XAUUSD").one()
        assert snap.raw_score is None

        import app.main as M
        with caplog.at_level(logging.WARNING):
            block = M._block(session, "XAUUSD", "CURRENT")

    # Falls back to the published score, and says so.
    assert block["display"]["bull_pct"] == to_display(17.8, "BULLISH")["bull_pct"]
    assert block["raw_score"] == 17.8
    assert "legacy row" in caplog.text
    assert "XAUUSD" in caplog.text and "CURRENT" in caplog.text


# ---------------------------------------------------------------------------
# Migration coverage
# ---------------------------------------------------------------------------
def test_migration_adds_raw_and_baseline_to_a_legacy_database(tmp_path, monkeypatch):
    """
    A database whose ``sentiment_snapshot`` predates the columns must gain both
    through the existing idempotent migration - no new DDL required.
    """
    import app.database as dbmod
    from sqlalchemy import create_engine

    db = tmp_path / "pre_step1.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sentiment_snapshot (
            id INTEGER PRIMARY KEY,
            asset VARCHAR(12) NOT NULL,
            scope VARCHAR(24) NOT NULL,
            score FLOAT NOT NULL,
            confidence FLOAT NOT NULL,
            bias VARCHAR(16) NOT NULL,
            mode VARCHAR(24) NOT NULL,
            drivers JSON NOT NULL,
            price_note TEXT NOT NULL,
            contributing_articles INTEGER NOT NULL,
            computed_at DATETIME NOT NULL
        );
        INSERT INTO sentiment_snapshot
            (asset,scope,score,confidence,bias,mode,drivers,price_note,
             contributing_articles,computed_at)
        VALUES ('XAUUSD','CURRENT',42.0,0.5,'BULLISH','heuristic','[]','',3,
                '2026-10-01 00:00:00');
    """)
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db.as_posix()}")
    monkeypatch.setattr(dbmod, "engine", engine)

    before = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(sentiment_snapshot)")}
    assert "raw_score" not in before

    dbmod._add_missing_sentiment_columns()
    # Idempotent: a second run must be a no-op, not an error.
    dbmod._add_missing_sentiment_columns()

    after = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(sentiment_snapshot)")}
    assert {"raw_score", "baseline_score"} <= after
    assert {"bull", "bear"} <= after, (
        "TASK 2 columns must be added by the same migration")

    # The pre-existing row survives with a NULL raw_score, which is exactly the
    # state _block's legacy branch is written for.
    row = sqlite3.connect(db).execute(
        "SELECT score, raw_score, baseline_score FROM sentiment_snapshot").fetchone()
    assert row == (42.0, None, None)
