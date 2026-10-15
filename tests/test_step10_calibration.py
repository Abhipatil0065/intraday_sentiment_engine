"""
STEP 10 - confidence calibration loop.

The engine's 0..1 confidence used fixed hand-picked coefficients that were
never checked against what actually happened. This step wires a mapping from
the blend's output onto observed hit rates, so a confidence means what it says.

WHAT IS ACTUALLY DELIVERED
--------------------------
The FIXTURE IS EMPTY: ``bias_outcome`` has 0 rows - no calibration label has
ever been recorded. So there is no evidence to fit a map from, and any mapping
placed in config.py today would be an invented formula rather than a
measurement. The applier therefore ships as an IDENTITY with the flag off, and
``tools/calibration_report.py`` is the fitter that prints the block to paste
once enough labels exist.

The tests below pin both halves: that the fitter works when data exists, and
that the runtime is a no-op while it does not.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
EDGES = (0.0, 0.1, 0.2, 0.3, 0.45, 0.6, 0.8, 1.01)


def _load_fitter():
    spec = importlib.util.spec_from_file_location(
        "calibration_report", REPO / "tools" / "calibration_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["calibration_report"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The runtime applier: identity until fitted
# ---------------------------------------------------------------------------
def test_applier_is_identity_by_default():
    """No fitted map means no behaviour change - scoring is untouched."""
    import app.config as cfg
    from app.analysis.confidence_calibration import calibrate_confidence

    saved = (cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION)
    try:
        cfg.CONFIDENCE_CALIBRATION_ENABLED = False
        cfg.CONFIDENCE_CALIBRATION = ((0.0, 0.0), (1.0, 1.0))
        for c in (0.0, 0.05, 0.14, 0.30, 0.55, 0.88, 1.0):
            assert calibrate_confidence(c) == c, (
                f"confidence {c} must pass through unchanged while uncalibrated")
    finally:
        cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION = saved


def test_flag_is_off_and_table_is_identity_in_config():
    """The shipped config must not pretend to be calibrated."""
    import app.config as cfg

    assert cfg.CONFIDENCE_CALIBRATION_ENABLED is False
    assert [list(p) for p in cfg.CONFIDENCE_CALIBRATION] == [[0.0, 0.0], [1.0, 1.0]]


def test_applier_honours_a_fitted_map():
    """With the flag on, the map remaps - compressing over-confidence."""
    import app.config as cfg
    from app.analysis.confidence_calibration import calibrate_confidence

    saved = (cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION)
    try:
        cfg.CONFIDENCE_CALIBRATION_ENABLED = True
        cfg.CONFIDENCE_CALIBRATION = ((0.0, 0.0), (0.3, 0.12), (1.0, 0.7))
        assert calibrate_confidence(0.30) == pytest.approx(0.12)
        assert calibrate_confidence(0.0) == pytest.approx(0.0)
        assert calibrate_confidence(1.0) == pytest.approx(0.7)
        assert 0.0 < calibrate_confidence(0.15) < 0.12
    finally:
        cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION = saved


def test_applier_is_monotonic_and_clamped():
    """A map must never reward over-confidence or leave its domain."""
    import app.config as cfg
    from app.analysis.confidence_calibration import calibrate_confidence

    saved = (cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION)
    try:
        cfg.CONFIDENCE_CALIBRATION_ENABLED = True
        cfg.CONFIDENCE_CALIBRATION = ((0.0, 0.05), (0.5, 0.4), (1.0, 0.6))
        xs = [i / 100 for i in range(101)]
        ys = [calibrate_confidence(x) for x in xs]
        assert ys == sorted(ys), "the map must be monotonic"
        assert all(0.0 <= y <= 1.0 for y in ys), "outputs must stay in 0..1"
    finally:
        cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION = saved


def test_malformed_map_degrades_to_identity():
    """A hand-edited table must not be able to break scoring."""
    import app.config as cfg
    from app.analysis.confidence_calibration import calibrate_confidence

    saved = (cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION)
    try:
        cfg.CONFIDENCE_CALIBRATION_ENABLED = True
        cfg.CONFIDENCE_CALIBRATION = ("nonsense",)
        assert calibrate_confidence(0.4) == 0.4, (
            "a malformed table must fall back to the identity, not raise")
    finally:
        cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION = saved


def test_non_numeric_confidence_passes_through():
    from app.analysis.confidence_calibration import calibrate_confidence

    for bad in (None, "", "abc", object()):
        assert calibrate_confidence(bad) == bad


def test_status_reports_the_truth():
    """The report and /api/health must not claim calibration that is off."""
    import app.config as cfg
    from app.analysis.confidence_calibration import calibration_status

    saved = (cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION)
    try:
        cfg.CONFIDENCE_CALIBRATION_ENABLED = False
        cfg.CONFIDENCE_CALIBRATION = ((0.0, 0.0), (1.0, 1.0))
        st = calibration_status()
        assert st["enabled"] is False
        assert st["fitted"] is False
        assert st["breakpoints"] == [[0.0, 0.0], [1.0, 1.0]]
    finally:
        cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION = saved


# ---------------------------------------------------------------------------
# The fitter
# ---------------------------------------------------------------------------
def _synthetic_rows():
    """Confidence that over-states the hit rate - the audit's finding."""
    rows = []
    for conf, n, rate in [(0.05, 40, 0.10), (0.15, 40, 0.25),
                          (0.25, 40, 0.45), (0.35, 40, 0.60),
                          (0.50, 40, 0.75), (0.70, 40, 0.88)]:
        worked = int(round(n * rate))
        for i in range(n):
            rows.append({"confidence": conf,
                         "outcome": "WORKED" if i < worked else "FAILED"})
    return rows


def test_fitter_computes_hit_rates_per_bucket():
    mod = _load_fitter()
    buckets = mod.build(_synthetic_rows(), EDGES)

    rates = {round(b["mean_confidence"], 2): b["hit_rate"] for b in buckets}
    assert rates[0.05] == pytest.approx(0.10)
    assert rates[0.15] == pytest.approx(0.25)
    assert rates[0.35] == pytest.approx(0.60)
    assert rates[0.70] == pytest.approx(0.875)   # 35 of 40 worked


def test_fitter_counts_mixed_as_half():
    """Mixed is a partial confirmation, not a pass or a fail."""
    mod = _load_fitter()
    rows = ([{"confidence": 0.35, "outcome": "WORKED"}] * 3 +
            [{"confidence": 0.35, "outcome": "FAILED"}] * 3 +
            [{"confidence": 0.35, "outcome": "Mixed"}] * 2)
    buckets = mod.build(rows, EDGES)
    assert len(buckets) == 1
    assert buckets[0]["hit_rate"] == pytest.approx(0.5)


def test_fitter_refuses_to_fit_without_samples():
    """Too few labels must not produce a confident-looking map."""
    mod = _load_fitter()
    tiny = [{"confidence": 0.1, "outcome": "WORKED"}] * 5
    fitted, pts = mod.fit(mod.build(tiny, EDGES), min_samples=30)
    assert fitted is False
    assert pts == [[0.0, 0.0], [1.0, 1.0]]


def test_fitter_output_is_monotonic():
    mod = _load_fitter()
    fitted, pts = mod.fit(mod.build(_synthetic_rows(), EDGES), min_samples=30)
    assert fitted is True
    raws = [p[0] for p in pts]
    cals = [p[1] for p in pts]
    assert raws == sorted(raws)
    assert cals == sorted(cals), "a higher bucket must never map lower"


def test_fitter_emits_a_pasteable_config_block():
    mod = _load_fitter()
    _fitted, pts = mod.fit(mod.build(_synthetic_rows(), EDGES), min_samples=30)
    block = mod.as_config_block(pts)
    assert "CONFIDENCE_CALIBRATION_ENABLED = True" in block
    assert "CONFIDENCE_CALIBRATION" in block
    assert "(0.050, 0.100)," in block


def test_fitter_ignores_unknown_outcomes():
    """An unrecognised label is not evidence."""
    mod = _load_fitter()
    rows = ([{"confidence": 0.35, "outcome": "WORKED"}] * 4 +
            [{"confidence": 0.35, "outcome": "MAYBE"}] * 10)
    buckets = mod.build(rows, EDGES)
    assert len(buckets) == 1
    assert buckets[0]["n"] == 4, "only recognised labels count"
    assert buckets[0]["hit_rate"] == pytest.approx(1.0)


def test_report_handles_an_empty_table():
    """The real database has no labels; the report must say so and not crash."""
    mod = _load_fitter()
    rows = mod.fetch(mod._db_path(), None)
    buckets = mod.build(rows, EDGES)
    fitted, pts = mod.fit(buckets, 30)
    assert fitted in (True, False)
    assert len(pts) >= 2


def test_fitter_reads_confidence_from_the_snapshot_not_the_label():
    """
    bias_outcome.confidence is written as a literal 0.0 by the POST endpoint, so
    fitting on the label row alone would put every label in the first bucket.

    This is why fetch() JOINs bias_snapshots. Guard the join stays in place.
    """
    text = (REPO / "tools" / "calibration_report.py").read_text(encoding="utf-8")
    assert "JOIN bias_snapshots" in text, (
        "the fitter must join bias_snapshots to get the engine's confidence")
    assert "s.confidence" in text
    assert "o.confidence" not in text, (
        "bias_outcome.confidence is always 0.0 - never fit on it")


# ---------------------------------------------------------------------------
# The wiring into scoring
# ---------------------------------------------------------------------------
def test_confidence_from_applies_the_calibrator():
    """_confidence_from must run the calibrator on its blend."""
    import app.config as cfg
    from app.analysis import confidence_calibration as cc
    from app.analysis import sentiment_engine as se

    saved = (cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION)
    seen = []
    real = cc.calibrate_confidence
    try:
        cfg.CONFIDENCE_CALIBRATION_ENABLED = True
        cfg.CONFIDENCE_CALIBRATION = ((0.0, 0.0), (1.0, 0.5))

        def spy(v):
            seen.append(v)
            return real(v)

        cc.calibrate_confidence = spy
        conf = se._confidence_from(70.0, 30.0, 10.0, 4)

        assert seen, "_confidence_from must run the calibrator"
        assert 0.0 <= conf <= 0.95
    finally:
        cc.calibrate_confidence = real
        cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION = saved


def test_flip_gate_survives_a_calibrated_map():
    """
    The delivered guarantee: turning calibration on must not break scoring.
    """
    import app.config as cfg
    from app.analysis import sentiment_engine as se
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (Asset, Direction, EconomicImportance,
                                  ImpactStrength, NewsCategory, ReportKind)
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)

    def sig(d):
        return build_signal(
            asset=Asset.GOLD, title="t", direction=d,
            strength=ImpactStrength.HIGH,
            importance=EconomicImportance.HIGH,
            published_at=now - timedelta(minutes=20),
            relevance=0.9, persistence=0.9,
            source_quality="TIER_2_MAJOR", source_name="Reuters",
            category=NewsCategory.CENTRAL_BANK,
            report_kind=ReportKind.GENERAL, session_multiplier=1.0)

    saved = (cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION)

    def run():
        se.clear_bias_history()
        return se.compute([sig(Direction.BULLISH)], Asset.GOLD, now=now)

    try:
        cfg.CONFIDENCE_CALIBRATION_ENABLED = False
        cfg.CONFIDENCE_CALIBRATION = ((0.0, 0.0), (1.0, 1.0))
        before = run()

        cfg.CONFIDENCE_CALIBRATION_ENABLED = True
        cfg.CONFIDENCE_CALIBRATION = ((0.0, 0.0), (1.0, 0.5))
        after = run()

        assert after.bias == before.bias, "same evidence, same verdict"
        assert after.score == before.score, "the score must not be remapped"
        assert after.confidence <= before.confidence + 1e-9, (
            "a shrinking map must not raise confidence")
    finally:
        cfg.CONFIDENCE_CALIBRATION_ENABLED, cfg.CONFIDENCE_CALIBRATION = saved
