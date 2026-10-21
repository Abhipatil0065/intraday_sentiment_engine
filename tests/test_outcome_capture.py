"""
AUTOMATIC OUTCOME CAPTURE - the measurement loop.

The defect these tests protect: ``bias_outcome`` could only ever be filled by
hand (one click per call through ``POST /api/calibration/labels``), so it had
zero rows for the whole life of the project. With no rows there was nothing
for ``tools/calibration_report.py`` to report, ``calibrate_confidence()`` was
an identity by design, and every tuning decision in the repo was an
unfalsifiable opinion.

The loop now records a call when the published bias CHANGES, stores the price
at the call, and resolves the call to WORKED / FAILED / Mixed once the horizon
has elapsed.

Contract:
  * A CHANGE is a new call. A standing verdict is the same call.
  * A call is only resolved when a price exists at BOTH ends of the window.
  * A move inside the dead band is MIXED, not a hit and not a miss.
  * Nothing here feeds back into scoring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.sentiment_engine import Bias
from app.database import session_scope
from app.models.enums import Asset
from app.services.bias_snapshots import AssetVerdict
from app.services.outcomes import (
    FAILED,
    MIXED,
    PENDING,
    WORKED,
    classify_move,
    outcome_status,
    record_new_calls,
    record_price_point,
    resolve_pending,
    trim_price_points,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _write_snapshot(session, asset: Asset = Asset.BITCOIN,
                    bias: str = "BULLISH", confidence: float = 0.7,
                    score: float = 20.0) -> int:
    from app.models.db_models import BiasSnapshot

    row = BiasSnapshot(
        timestamp_utc=_now(),
        timestamp_ist="12:00:00 PM IST",
        asset=asset.value,
        bias=bias,
        score=score,
        confidence=confidence,
        raw_score=score,
        baseline_score=None,
        adjusted=False,
        bullish_pct=60.0,
        bearish_pct=40.0,
        article_count=3,
        last_signal_age_min=1.0,
        bull_drivers=[],
        bear_drivers=[],
    )
    session.add(row)
    session.flush()
    return row.id


def _verdict(asset: Asset = Asset.BITCOIN, bias: str = "BULLISH",
             confidence: float = 0.7, score: float = 20.0) -> AssetVerdict:
    return AssetVerdict(asset=asset, score=score, confidence=confidence,
                        bias=bias, article_count=3)


def _outcomes(session, asset: str = "BTCUSD"):
    from sqlalchemy import select

    from app.models.db_models import BiasOutcome

    return session.scalars(
        select(BiasOutcome).where(BiasOutcome.asset == asset)
        .order_by(BiasOutcome.id)
    ).all()


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def test_a_published_bias_change_records_a_pending_call():
    with session_scope() as session:
        _write_snapshot(session)
        written = record_new_calls(session, [_verdict()])
        assert written == 1
        rows = _outcomes(session)
        assert len(rows) == 1
        assert rows[0].outcome == PENDING
        assert rows[0].bias == "BULLISH"
        assert rows[0].confidence == pytest.approx(0.7)
        assert rows[0].score == pytest.approx(20.0)


def test_a_standing_verdict_is_not_recorded_again():
    """The same call, re-published every 180s, is ONE observation."""
    with session_scope() as session:
        _write_snapshot(session)
        assert record_new_calls(session, [_verdict()]) == 1
        # Second tick, same bias -> no new row.
        assert record_new_calls(session, [_verdict()]) == 1 - 1
        assert len(_outcomes(session)) == 1


def test_a_flip_records_a_second_call():
    with session_scope() as session:
        _write_snapshot(session)
        record_new_calls(session, [_verdict(bias="BULLISH")])
        assert record_new_calls(session, [_verdict(bias="BEARISH")]) == 1
        rows = _outcomes(session)
        assert [r.bias for r in rows] == ["BULLISH", "BEARISH"]


def test_no_snapshot_means_no_outcome_row():
    """The outcome row references a bias_snapshots row; with none, none is
    written rather than writing a broken foreign key."""
    with session_scope() as session:
        assert record_new_calls(session, [_verdict()]) == 0
        assert _outcomes(session) == []


def test_capture_can_be_switched_off():
    from app.config import settings

    with session_scope() as session:
        _write_snapshot(session)
        previous = settings.outcome_capture_enabled
        settings.outcome_capture_enabled = False
        try:
            assert record_new_calls(session, [_verdict()]) == 0
        finally:
            settings.outcome_capture_enabled = previous


# ---------------------------------------------------------------------------
# Price points
# ---------------------------------------------------------------------------
def test_price_points_are_recorded_and_rejected_when_unusable():
    from app.models.db_models import PricePoint
    from sqlalchemy import select

    with session_scope() as session:
        assert record_price_point(session, Asset.BITCOIN, 65000.0, "Coinbase")
        assert not record_price_point(session, Asset.BITCOIN, None, "x")
        assert not record_price_point(session, Asset.BITCOIN, -1.0, "x")
        assert not record_price_point(session, Asset.BITCOIN, 0.0, "x")
        rows = session.scalars(select(PricePoint)).all()
        assert len(rows) == 1
        assert rows[0].price == pytest.approx(65000.0)


def test_old_price_points_are_trimmed():
    from app.models.db_models import PricePoint
    from sqlalchemy import select

    with session_scope() as session:
        old = _now() - timedelta(hours=200)
        session.add(PricePoint(asset="BTCUSD", ts_utc=old, price=1.0,
                               source="t"))
        session.add(PricePoint(asset="BTCUSD", ts_utc=_now(), price=2.0,
                               source="t"))
        session.flush()
        assert trim_price_points(session, _now()) == 1
        rows = session.scalars(select(PricePoint)).all()
        assert len(rows) == 1
        assert rows[0].price == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _seed_call_and_prices(session, bias: str, entry: float, exit_: float,
                          horizon_minutes: int = 30):
    """A recorded call plus a price series spanning the horizon."""
    from app.models.db_models import BiasOutcome

    _write_snapshot(session)
    record_new_calls(session, [_verdict(bias=bias)])
    row = _outcomes(session)[-1]
    decided = _now() - timedelta(minutes=horizon_minutes + 1)
    row.date_utc = decided
    session.flush()

    record_price_point(session, Asset.BITCOIN, entry, "test",
                       ts=decided - timedelta(seconds=30))
    record_price_point(session, Asset.BITCOIN, exit_, "test",
                       ts=_now() + timedelta(seconds=30))
    session.flush()
    return row


def test_a_call_that_worked_is_resolved():
    with session_scope() as session:
        row = _seed_call_and_prices(session, "BULLISH", 100.0, 101.0)
        assert resolve_pending(session, _now()) == 1
        assert row.outcome == WORKED
        assert "over 30m" in row.note


def test_a_call_that_failed_is_resolved():
    with session_scope() as session:
        row = _seed_call_and_prices(session, "BULLISH", 100.0, 98.0)
        assert resolve_pending(session, _now()) == 1
        assert row.outcome == FAILED


def test_a_bearish_call_that_worked_is_resolved():
    with session_scope() as session:
        row = _seed_call_and_prices(session, "BEARISH", 100.0, 98.0)
        assert resolve_pending(session, _now()) == 1
        assert row.outcome == WORKED


def test_a_move_inside_the_dead_band_is_mixed():
    with session_scope() as session:
        row = _seed_call_and_prices(session, "BULLISH", 100.0, 100.01)
        assert resolve_pending(session, _now()) == 1
        assert row.outcome == MIXED


def test_a_call_without_a_price_stays_pending():
    with session_scope() as session:
        _write_snapshot(session)
        record_new_calls(session, [_verdict()])
        row = _outcomes(session)[-1]
        row.date_utc = _now() - timedelta(minutes=45)
        session.flush()
        assert resolve_pending(session, _now()) == 0
        assert row.outcome == PENDING


def test_a_call_whose_entry_price_is_too_old_stays_pending():
    """A call must not be scored against a price from a different part of
    the session."""
    with session_scope() as session:
        from app.models.db_models import BiasOutcome

        _write_snapshot(session)
        record_new_calls(session, [_verdict()])
        row = _outcomes(session)[-1]
        decided = _now() - timedelta(minutes=31)
        row.date_utc = decided
        session.flush()
        # Entry price is an hour old - far outside the tolerance.
        record_price_point(session, Asset.BITCOIN, 100.0, "test",
                           ts=decided - timedelta(hours=1))
        record_price_point(session, Asset.BITCOIN, 101.0, "test",
                           ts=_now() + timedelta(seconds=30))
        session.flush()
        assert resolve_pending(session, _now()) == 0
        assert row.outcome == PENDING


def test_a_call_inside_the_horizon_is_not_resolved_yet():
    with session_scope() as session:
        _write_snapshot(session)
        record_new_calls(session, [_verdict()])
        row = _outcomes(session)[-1]
        row.date_utc = _now() - timedelta(minutes=5)
        session.flush()
        record_price_point(session, Asset.BITCOIN, 100.0, "test", ts=_now())
        record_price_point(session, Asset.BITCOIN, 101.0, "test",
                           ts=_now() + timedelta(seconds=30))
        session.flush()
        assert resolve_pending(session, _now()) == 0
        assert row.outcome == PENDING


# ---------------------------------------------------------------------------
# Downstream contract
# ---------------------------------------------------------------------------
def test_pending_rows_are_not_evidence_for_the_calibration_fitter():
    """The fitter maps only worked/failed/mixed; PENDING must be ignored, or
    undecided calls would be scored as failures."""
    from tools.calibration_report import _OUTCOME_WEIGHT

    assert _OUTCOME_WEIGHT.get(PENDING.lower()) is None
    assert _OUTCOME_WEIGHT["worked"] == 1.0
    assert _OUTCOME_WEIGHT["mixed"] == 0.5


def test_the_manual_label_endpoint_still_rejects_pending():
    """A human label may not be PENDING."""
    from app.main import _normalize_outcome

    assert _normalize_outcome("PENDING") == ""
    assert _normalize_outcome("worked") == "WORKED"


def test_classify_move_respects_the_dead_band():
    assert classify_move(1.0, True, 0.05) == WORKED
    assert classify_move(1.0, False, 0.05) == FAILED
    assert classify_move(-1.0, False, 0.05) == WORKED
    assert classify_move(-1.0, True, 0.05) == FAILED
    assert classify_move(0.01, True, 0.05) == MIXED
    assert classify_move(-0.01, False, 0.05) == MIXED


def test_outcome_status_counts_pending_and_resolved():
    with session_scope() as session:
        assert outcome_status(session)["recorded"] == 0
        _write_snapshot(session)
        record_new_calls(session, [_verdict()])
        status = outcome_status(session)
        assert status["recorded"] == 1
        assert status["pending"] == 1
        assert status["resolved"] == 0


# ---------------------------------------------------------------------------
# End to end through the real pipeline
# ---------------------------------------------------------------------------
def test_the_pipeline_records_the_calls_it_publishes():
    """
    A full ``_recompute_sentiment`` pass must leave a PENDING outcome row
    behind for every asset it published a verdict for.

    This is the loop that was missing: without it the engine could publish
    for months and nobody would ever know whether it was right.
    """
    from app.analysis import sentiment_engine as se
    from app.models import db_models as models
    from app.models.enums import Direction, ImpactStrength
    from app.services.pipeline import IngestStats, _recompute_sentiment

    se.clear_bias_history()
    with session_scope() as session:
        article = models.NewsArticle(
            guid="outcome-test-0001", source="Reuters",
            source_quality="TIER_2_MAJOR",
            url="https://example.invalid/outcome",
            title="Fed officials signal interest rates will stay higher for longer",
            body="", published_at_utc=_now() - timedelta(minutes=25),
            is_today=True, category="Central Bank", importance="HIGH",
            report_kind="GENERAL", verification="CONFIRMED",
            relevance_score=0.9, analysis_mode="heuristic", llm_used=False,
        )
        session.add(article)
        session.flush()
        for asset in Asset:
            session.add(models.AssetImpact(
                article_id=article.id, asset=asset.value,
                direction=Direction.BULLISH.value,
                strength=ImpactStrength.VERY_HIGH.value,
                relevance=0.85, persistence=0.75,
            ))
        session.flush()

        _recompute_sentiment(session, IngestStats())
        session.flush()

        rows = _outcomes(session)
        assert rows, "the pipeline must record the calls it published"
        assert {r.asset for r in rows} <= {"BTCUSD", "XAUUSD", "XAGUSD", "EQUITY"}
        assert all(r.outcome == PENDING for r in rows)
        assert all(r.bias in ("BULLISH", "BEARISH") for r in rows)
        assert all(r.confidence > 0.0 for r in rows)
        # Every outcome row must point at a real bias_snapshots row.
        for row in rows:
            assert row.bias_snapshot_id is not None


def test_the_pipeline_does_not_record_a_second_call_for_a_standing_verdict():
    """Two consecutive ticks with the same verdict are ONE call."""
    from app.analysis import sentiment_engine as se
    from app.models import db_models as models
    from app.models.enums import Direction, ImpactStrength
    from app.services.pipeline import IngestStats, _recompute_sentiment

    se.clear_bias_history()
    with session_scope() as session:
        article = models.NewsArticle(
            guid="outcome-test-0002", source="Reuters",
            source_quality="TIER_2_MAJOR",
            url="https://example.invalid/outcome2",
            title="Fed officials signal interest rates will stay higher for longer",
            body="", published_at_utc=_now() - timedelta(minutes=25),
            is_today=True, category="Central Bank", importance="HIGH",
            report_kind="GENERAL", verification="CONFIRMED",
            relevance_score=0.9, analysis_mode="heuristic", llm_used=False,
        )
        session.add(article)
        session.flush()
        for asset in Asset:
            session.add(models.AssetImpact(
                article_id=article.id, asset=asset.value,
                direction=Direction.BULLISH.value,
                strength=ImpactStrength.VERY_HIGH.value,
                relevance=0.85, persistence=0.75,
            ))
        session.flush()

        _recompute_sentiment(session, IngestStats())
        session.flush()
        first = len(_outcomes(session))
        assert first > 0

        _recompute_sentiment(session, IngestStats())
        session.flush()
        assert len(_outcomes(session)) == first, (
            "a standing verdict is the same call, not a new one")
