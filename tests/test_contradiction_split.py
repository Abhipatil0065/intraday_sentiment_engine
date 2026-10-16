"""
CONTRADICTION SPLIT - the two renamed flags.

``cluster_conflict`` is the cluster-level override: two readings of one event
were collapsed into one vote after disagreeing.

``evidence_split`` is the aggregate-ratio meaning: the bull/bear totals are
balanced above the placeholder threshold. This module locks each flag to its
documented downstream consumer and to the payload.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.analysis.sentiment_engine import (
    FLIP_MARGIN,
    SentimentResult,
    TIEBREAK_MAX_CONFIDENCE,
    cap_confidence_for_evidence_split,
    compute,
    effective_flip_margin,
    evidence_split_from_totals,
)
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
)

NOW = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)


def _signal(title, direction, mins_ago=0, cluster_conflict=False):
    from app.analysis.signal_factory import build_signal
    from dataclasses import replace

    signal = build_signal(
        asset=Asset.GOLD,
        title=title,
        direction=direction,
        strength=ImpactStrength.HIGH,
        importance=EconomicImportance.HIGH,
        published_at=NOW - timedelta(minutes=mins_ago),
        relevance=0.9,
        persistence=0.9,
        source_quality="TIER_2_MAJOR",
        source_name="Reuters",
        category=NewsCategory.CENTRAL_BANK,
        independent_sources=1,
        report_kind=ReportKind.GENERAL,
        session_multiplier=1.0,
        event_key=f"split-{title}",
        cluster_id=f"split-{title}",
    )
    if cluster_conflict:
        signal = replace(signal, cluster_conflict=True)
    return signal


def _force_aggregate_totals(monkeypatch, bull, bear):
    """
    Force compute()'s aggregate totals so evidence_split is tested on the exact
    ratio requested, independently of signal weighting.
    """
    import app.analysis.sentiment_engine as se

    real_aggregate = se._aggregate

    def fake_aggregate(signals, now=None):
        agg = real_aggregate(signals, now)
        agg["bull"] = bull
        agg["bear"] = bear
        return agg

    monkeypatch.setattr(se, "_aggregate", fake_aggregate)
    return fake_aggregate


def test_evidence_split_fires_for_bull_100_bear_100():
    """
    Regression guard for the renamed ratio condition: the old unreachable
    bull>80 AND bear>80 semantics must still be split evidence.
    """
    assert evidence_split_from_totals(100.0, 100.0) is True


def test_evidence_split_true_for_ratio_above_threshold(monkeypatch):
    """bull=3.0, bear=2.5 -> ratio 0.833 -> evidence_split, no cluster conflict."""
    _force_aggregate_totals(monkeypatch, 3.0, 2.5)
    result = compute(
        [_signal("Fed signals patience", Direction.BULLISH)],
        Asset.GOLD,
        now=NOW,
    )

    assert result.evidence_split is True
    assert result.cluster_conflict is False


def test_evidence_split_false_for_ratio_below_threshold(monkeypatch):
    """bull=1.0, bear=0.2 -> ratio 0.2 -> not evidence_split."""
    _force_aggregate_totals(monkeypatch, 1.0, 0.2)
    result = compute(
        [_signal("Fed signals patience", Direction.BULLISH)],
        Asset.GOLD,
        now=NOW,
    )

    assert result.evidence_split is False
    assert result.cluster_conflict is False


def test_evidence_split_and_cluster_conflict_both_true(monkeypatch):
    """A balanced aggregate plus a meaningful cluster conflict sets both flags."""
    import app.analysis.sentiment_engine as se

    _force_aggregate_totals(monkeypatch, 3.0, 2.5)
    monkeypatch.setattr(se, "_signal_weight", lambda s, now: 0.8)
    result = compute(
        [replace(
            _signal(
                "Fed signals patience",
                Direction.BULLISH,
                cluster_conflict=True,
            ),
            # One fully-conflicted signal: its magnitude is the conflicted
            # share (0.8 x HIGH strength mult = 0.576 >= 0.5 floor).
            conflict_weight=0.8 * se.STRENGTH_MULT[ImpactStrength.HIGH],
        )],
        Asset.GOLD,
        now=NOW,
    )

    assert result.evidence_split is True
    assert result.cluster_conflict is True


def test_cluster_conflict_with_balanced_totals(monkeypatch):
    """
    A manually cluster-flagged balanced pair must set cluster_conflict; the
    aggregate ratio then decides evidence_split (balanced means split here).
    """
    import app.analysis.sentiment_engine as se

    signals = [
        replace(
            _signal("Fed signals patience", Direction.BULLISH),
            cluster_conflict=True,
            conflict_weight=0.4 * se.STRENGTH_MULT[ImpactStrength.HIGH],
        ),
        replace(
            _signal("Fed signals patience", Direction.BEARISH, mins_ago=2),
            cluster_conflict=True,
            conflict_weight=0.4 * se.STRENGTH_MULT[ImpactStrength.HIGH],
        ),
    ]
    # Keep score-magnitude total above the absolute floor while preserving the
    # balanced bullish/bearish ratio.
    monkeypatch.setattr(se, "_signal_weight", lambda s, now: 0.4)

    result = compute(signals, Asset.GOLD, now=NOW)

    assert result.cluster_conflict is True
    assert result.evidence_split is True
    assert result.bull_weight > 0 and result.bear_weight > 0


def test_cluster_conflict_does_not_fire_on_low_share(monkeypatch):
    """One conflicted signal out of 100 is a long-tail sliver, not a flag."""
    import app.analysis.sentiment_engine as se

    high_mult = se.STRENGTH_MULT[ImpactStrength.HIGH]
    signals = [
        replace(
            _signal(f"Neutral note {i}", Direction.BULLISH),
            cluster_conflict=(i == 0),
            conflict_weight=(high_mult if i == 0 else 0.0),
        )
        for i in range(100)
    ]
    monkeypatch.setattr(se, "_signal_weight", lambda s, now: 1.0)

    result = compute(signals, Asset.GOLD, now=NOW)

    assert result.cluster_conflict is False


def test_cluster_conflict_fires_on_meaningful_share(monkeypatch):
    """Two conflicted signals out of 20 is a 10% share, so the gate fires."""
    import app.analysis.sentiment_engine as se

    high_mult = se.STRENGTH_MULT[ImpactStrength.HIGH]
    signals = [
        replace(
            _signal(f"Neutral note {i}", Direction.BULLISH),
            cluster_conflict=(i < 2),
            conflict_weight=(high_mult if i < 2 else 0.0),
        )
        for i in range(20)
    ]
    monkeypatch.setattr(se, "_signal_weight", lambda s, now: 1.0)

    result = compute(signals, Asset.GOLD, now=NOW)

    assert result.cluster_conflict is True


def test_cluster_conflict_does_not_fire_below_absolute_floor(monkeypatch):
    """A 10% share is irrelevant when the total evidence is below the floor."""
    import app.analysis.sentiment_engine as se

    signals = [
        _signal(f"Thin note {i}", Direction.BULLISH,
                cluster_conflict=(i < 2))
        for i in range(20)
    ]
    monkeypatch.setattr(se, "_signal_weight", lambda s, now: 0.02)

    result = compute(signals, Asset.GOLD, now=NOW)

    assert result.cluster_conflict is False


def test_cluster_conflict_fires_at_threshold_boundary(monkeypatch):
    """
    Boundary test: score-magnitude share exactly 5% and total exactly 0.5.

    The comparison is ``>=`` for both share and absolute floor, so this fires.
    """
    import app.analysis.sentiment_engine as se

    # 20 signals x 0.025 signal weight pass through _signal_weight and are
    # scaled by STRENGTH_MULT to produce 0.5 score magnitude; 1 conflicted
    # signal is 0.025 score magnitude / 0.5 total = 5%.
    strength_mult = se.STRENGTH_MULT[ImpactStrength.HIGH]
    signal_weight = 0.5 / (20 * strength_mult)

    signals = [
        replace(
            _signal(f"Boundary note {i}", Direction.BULLISH),
            cluster_conflict=(i == 0),
            conflict_weight=(signal_weight * strength_mult if i == 0 else 0.0),
        )
        for i in range(20)
    ]
    monkeypatch.setattr(se, "_signal_weight", lambda s, now: signal_weight)

    result = compute(signals, Asset.GOLD, now=NOW)

    assert result.cluster_conflict is True


def test_flip_margin_widening_reads_cluster_conflict_only():
    """The flip-margin widening must use cluster_conflict, not evidence_split."""
    assert effective_flip_margin(False) == FLIP_MARGIN
    assert effective_flip_margin(True) == FLIP_MARGIN + 10.0

    # An evidence-split-only verdict has no cluster conflict, so it must not
    # widen the flip margin.
    assert evidence_split_from_totals(100.0, 100.0) is True
    assert effective_flip_margin(False) == FLIP_MARGIN


def test_confidence_cap_reads_evidence_split_only():
    """The confidence cap must use evidence_split, not cluster_conflict."""
    cluster_only = SentimentResult(
        asset=Asset.GOLD,
        cluster_conflict=True,
        evidence_split=False,
    )
    split_only = SentimentResult(
        asset=Asset.GOLD,
        cluster_conflict=False,
        evidence_split=True,
    )

    assert cap_confidence_for_evidence_split(0.90) == TIEBREAK_MAX_CONFIDENCE
    assert cap_confidence_for_evidence_split(0.10) == 0.10

    # The flag pair itself encodes the routing: only evidence_split carries
    # the cap semantics; cluster_conflict carries the flip-margin semantics.
    assert cluster_only.cluster_conflict is True
    assert cluster_only.evidence_split is False
    assert split_only.cluster_conflict is False
    assert split_only.evidence_split is True


def test_payload_exposes_both_new_flags(tmp_path):
    """The dashboard asset card must expose both flags and the deprecated alias."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.main import _block
    from app.models.db_models import Base, SentimentSnapshot

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'split_payload.db').as_posix()}"
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as session:
        session.add(SentimentSnapshot(
            asset="XAUUSD",
            scope="CURRENT",
            score=2.0,
            raw_score=2.0,
            bull=1.7,
            bear=1.7,
            confidence=0.25,
            bias="BULLISH",
            mode="mixed",
            cluster_conflict=True,
            evidence_split=True,
            contradiction_detected=True,
        ))
        session.commit()

        block = _block(session, "XAUUSD", "CURRENT")

    assert block["cluster_conflict"] is True
    assert block["evidence_split"] is True
    assert block["contradiction_detected"] is True
    for existing in ("score", "raw_score", "confidence", "bias", "display"):
        assert existing in block, f"_block lost {existing}"


def test_cluster_conflict_true_does_not_cap_confidence(monkeypatch):
    """Semantics A regression guard: cluster_conflict widens the flip margin
    only; it must never cap confidence.

    One-sided bullish evidence (bear = 0) is not evidence_split, so the
    confidence cap cannot fire from that side either. A conflicted cluster
    flag set on one signal must leave the formula confidence untouched
    (> TIEBREAK_MAX_CONFIDENCE), not force it down to 0.30."""
    import app.analysis.sentiment_engine as se

    strength_mult = se.STRENGTH_MULT[ImpactStrength.HIGH]
    signal_weight = 0.5 / (20 * strength_mult)  # total magnitude exactly 0.5

    signals = [
        replace(
            _signal(f"One-sided note {i}", Direction.BULLISH),
            cluster_conflict=(i == 0),
            conflict_weight=(signal_weight * strength_mult if i == 0 else 0.0),
        )
        for i in range(20)
    ]
    monkeypatch.setattr(se, "_signal_weight", lambda s, now: signal_weight)

    result = compute(signals, Asset.GOLD, now=NOW)

    assert result.cluster_conflict is True
    assert result.evidence_split is False
    assert result.confidence > TIEBREAK_MAX_CONFIDENCE, (
        "cluster_conflict must not cap confidence under Semantics A"
    )
