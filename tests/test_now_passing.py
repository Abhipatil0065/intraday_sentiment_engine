"""
NOW PASSING TESTS

Verifies the live pipeline passes a tz-aware ``now`` into
``sentiment_engine.compute`` so the hold gate, cooling gate, Tier-1 bypass
and ``delta_60m`` actually run instead of being skipped.

Nothing here touches cluster_conflict logic, thresholds, clustering, scoring
formulas or the DB schema.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.sentiment_engine import (
    Asset,
    Bias,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
    clear_bias_history,
    compute,
)
from app.analysis.signal_factory import build_signal


def _signal(direction, strength=ImpactStrength.VERY_HIGH,
            importance=EconomicImportance.VERY_HIGH, published=None,
            report_kind=ReportKind.GENERAL, source_quality="TIER_1_OFFICIAL"):
    if published is None:
        published = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    return build_signal(
        asset=Asset.GOLD,
        title=f"now-test {direction.value}",
        direction=direction,
        strength=strength,
        importance=importance,
        published_at=published,
        relevance=1.0,
        persistence=1.0,
        source_quality=source_quality,
        source_name="now-test",
        category=NewsCategory.CENTRAL_BANK,
        independent_sources=1,
        report_kind=report_kind,
        event_key=f"now-{direction.value}",
        mode="heuristic",
        surprise_weight=1.0,
    )


# ---------------------------------------------------------------------------
# Step 3a: the pipeline path calls compute with a tz-aware `now`.
# ---------------------------------------------------------------------------
def test_pipeline_passes_tz_aware_now(monkeypatch):
    captured: dict = {}

    real_compute = compute

    def fake_compute(signals, asset, now=None, **kwargs):
        captured["now"] = now
        captured["kwargs"] = dict(kwargs)
        return real_compute(signals, asset, now=now, **kwargs)

    monkeypatch.setattr("app.analysis.sentiment_engine.compute", fake_compute)

    from app.database import session_scope
    from app.services.pipeline import IngestStats, _recompute_sentiment

    with session_scope() as session:
        _recompute_sentiment(session, IngestStats())

    assert captured["now"] is not None, "pipeline did not pass now= to compute()"
    now = captured["now"]
    assert now.tzinfo is not None, f"now={now!r} is not tz-aware"
    assert now.tzinfo is timezone.utc, f"now={now!r} is not UTC"


# ---------------------------------------------------------------------------
# Step 3b: explicit-now gate behaviour.
# ---------------------------------------------------------------------------
def test_hold_gate_blocks_flip_within_window():
    clear_bias_history()
    now = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)

    # MEDIUM importance -> event tier TIER_2, so the Tier-1 bypass does not
    # apply and the hold gate is the relevant gate.
    r1 = compute([_signal(Direction.BULLISH, importance=EconomicImportance.MEDIUM)],
                 Asset.GOLD, now=now)
    assert r1.bias is Bias.BULLISH

    r2 = compute([_signal(Direction.BEARISH, ImpactStrength.VERY_HIGH,
                          importance=EconomicImportance.MEDIUM)],
                 Asset.GOLD, now=now)
    assert r2.bias is Bias.BEARISH  # flip, flip time recorded

    # Same instant: hold_elapsed = 0 < FLIP_HOLD_MINUTES -> flip blocked.
    r3 = compute([_signal(Direction.BULLISH, importance=EconomicImportance.MEDIUM)],
                 Asset.GOLD, now=now)
    assert r3.bias is Bias.BEARISH
    assert "held" in r3.note


def test_flip_allowed_after_hold_and_cooling():
    clear_bias_history()
    t0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    # hold = 5 min, cooling = 10 min -> wait past both.
    t1 = t0 + timedelta(minutes=16, seconds=1)

    r1 = compute([_signal(Direction.BULLISH)], Asset.GOLD, now=t0)
    assert r1.bias is Bias.BULLISH

    r2 = compute([_signal(Direction.BEARISH, ImpactStrength.VERY_HIGH)],
                 Asset.GOLD, now=t0)
    assert r2.bias is Bias.BEARISH

    # 16+ minutes later: hold and cooling both expired; margin gate allows.
    r3 = compute([_signal(Direction.BULLISH, ImpactStrength.HIGH)],
                 Asset.GOLD, now=t1)
    assert r3.bias is Bias.BULLISH


def test_fresh_tier1_opposite_flips_immediately():
    clear_bias_history()
    t0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=30)  # inside hold + cooling windows

    r1 = compute([_signal(Direction.BULLISH)], Asset.GOLD, now=t0)
    assert r1.bias is Bias.BULLISH

    r2 = compute([_signal(Direction.BEARISH, ImpactStrength.VERY_HIGH)],
                 Asset.GOLD, now=t0)
    assert r2.bias is Bias.BEARISH  # flip time recorded

    # Fresh Tier-1 opposite signal published 30s later bypasses hold/cooling.
    tier1 = _signal(Direction.BULLISH, ImpactStrength.HIGH,
                    importance=EconomicImportance.VERY_HIGH,
                    published=t1)
    r3 = compute([tier1], Asset.GOLD, now=t1)
    assert r3.bias is Bias.BULLISH


def test_now_none_still_runs_but_skips_gates():
    """compute() default now=None behaviour is unchanged (Step 2c)."""
    clear_bias_history()
    r1 = compute([_signal(Direction.BULLISH)], Asset.GOLD, now=None)
    assert r1.bias is Bias.BULLISH

    # Hold gate is skipped when now is None (no flip time can be compared),
    # so the flip goes through on the margin gate alone.
    r2 = compute([_signal(Direction.BEARISH, ImpactStrength.VERY_HIGH)],
                 Asset.GOLD, now=None)
    assert r2.bias is Bias.BEARISH


def test_flip_time_naive_vs_aware_hold_gate_identical():
    """A naive-UTC ``now`` and the equivalent aware ``now`` record the same
    flip time (via ``_record_flip_time``/``_recall_flip_time``), so the hold
    gate behaves identically either way."""
    from app.analysis.sentiment_engine import (
        _record_flip_time,
        _recall_flip_time,
    )

    clear_bias_history()
    now_naive = datetime(2026, 10, 15, 12, 0, 0)          # naive (treated as UTC)
    now_aware = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
    key = "naive-aware-equiv"

    # 1. naive and aware record identical flip times
    _record_flip_time(key, now_naive)
    assert _recall_flip_time(key) == now_aware
    _record_flip_time(key, now_aware)
    assert _recall_flip_time(key) == now_aware

    # 2. hold gate behaves the same: flip BULLISH->BEARISH (stores aware flip
    #    time), then at the same instant a BULLISH flip is blocked.
    s_bull = _signal(Direction.BULLISH, importance=EconomicImportance.MEDIUM)
    r_bull = compute([s_bull], Asset.GOLD, now=now_aware)
    assert r_bull.bias is Bias.BULLISH
    s_bear = _signal(Direction.BEARISH, ImpactStrength.VERY_HIGH,
                     importance=EconomicImportance.MEDIUM)
    r_bear = compute([s_bear], Asset.GOLD, now=now_aware)
    assert r_bear.bias is Bias.BEARISH  # flip time recorded
    # same instant -> hold gate blocks
    r_hold = compute([s_bull], Asset.GOLD, now=now_aware)
    assert r_hold.bias is Bias.BEARISH
    assert "held" in r_hold.note


def test_flip_time_ist_utc_equivalent_store_same():
    """An aware IST datetime (UTC+05:30) and its UTC equivalent record the
    same flip time via ``_record_flip_time``/``_recall_flip_time``."""
    from app.analysis.sentiment_engine import (
        _record_flip_time,
        _recall_flip_time,
    )

    clear_bias_history()
    ist_now = datetime(2026, 10, 15, 12, 0, 0,
                       tzinfo=timezone(timedelta(hours=5, minutes=30)))
    utc_now = datetime(2026, 10, 15, 6, 30, 0, tzinfo=timezone.utc)
    key = "ist-utc-equiv"

    _record_flip_time(key, ist_now)
    assert _recall_flip_time(key) == utc_now
    _record_flip_time(key, utc_now)
    assert _recall_flip_time(key) == utc_now
