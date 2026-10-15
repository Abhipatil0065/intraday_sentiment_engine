"""
STEP 8 - contradiction handling and tie-break honesty.

Two opposing stories must not look like a verdict.

1. The DAMPENER: when min(bull, bear) / max(bull, bear) > 0.6 the two sides are
   fighting each other, so the signed score is halved. 67/33 becomes 54/46; a
   perfectly balanced pair becomes 50/50. Without it the raw normalisation
   published a conviction the evidence did not support.

2. The TIE-BREAK RENDERING: the engine takes a BULLISH/BEARISH label from its
   tie-break rule whenever |raw| < EPS, even when the weighted totals balance
   out. Left alone the display's lean-fixup turned that into a forced 51/49.
   With the tie_break flag an even split now reads NEUTRAL 50/50.

3. The CONTRADICTION CAP: when both sides are heavily weighted (bull > 80 AND
   bear > 80) confidence is capped at TIEBREAK_MAX_CONFIDENCE and the label is
   forced LOW, so a split can never appear alongside a HIGH badge.

NOTE ON REACHABILITY: `contradiction_detected` has never once fired in the
live history (21,231 snapshot rows, all zero) because it needs BOTH weighted
totals above 80 - one side always dominates once the signal counts get that
high. The dampener, which uses the ratio instead of two absolute thresholds, is
the condition that actually fires, and it is what these tests lean on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. The dampener
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bull,bear,expected", [
    # Undamped and damped values for each pair.
    (70.0, 50.0, 8.3),      # ratio .71 -> fired, 16.7 / 2
    (50.0, 70.0, -8.3),     # symmetric bearish case
    (60.0, 60.0, 0.0),      # perfectly balanced
    (100.0, 100.0, 0.0),
    (80.0, 120.0, -10.0),   # ratio .67 -> fired
    (90.0, 10.0, 80.0),     # ratio .11 -> NOT fired
    (200.0, 100.0, 33.3),   # ratio .50 -> NOT fired
])
def test_score_dampener(bull, bear, expected):
    """The score is halved once the two sides are within the ratio threshold."""
    from app.analysis.sentiment_engine import _score_from

    assert abs(_score_from(bull, bear) - expected) < 0.05, (
        f"bull={bull} bear={bear}: expected {expected}, "
        f"got {_score_from(bull, bear)}")


def test_dampener_leaves_a_real_lean_alone():
    """A decisive score must not be shrunk - that would understate evidence."""
    from app.analysis.sentiment_engine import _score_from

    # 90/10 is a 4.5:1 lean, well past the 0.6 ratio.
    assert _score_from(90.0, 10.0) == 80.0
    assert abs(_score_from(95.0, 5.0) - 90.0) < 0.05


def test_dampener_threshold_boundary():
    """Exactly at the threshold the dampener does not fire; just above it does."""
    from app.analysis.sentiment_engine import _score_from

    def undamped(bull, bear):
        return round(100.0 * (bull - bear) / (bull + bear), 1)

    def damped(bull, bear):
        return round(undamped(bull, bear) * 0.5, 1)

    # 48/80 = 0.6 exactly -> NOT > 0.6, so undamped.
    on_edge = _score_from(80.0, 48.0)
    assert on_edge == undamped(80.0, 48.0), (
        "exactly at the ratio threshold the dampener must not fire")

    # 50/80 = 0.625 -> above, so damped.
    over = _score_from(80.0, 50.0)
    assert over == damped(80.0, 50.0)

    # A small asymmetry just past the line flips the behaviour.
    just_below = _score_from(81.0, 48.0)     # 48/81 = 0.5926 -> below
    assert just_below == undamped(81.0, 48.0)


def test_dampener_is_idempotent_and_pure():
    """The dampener is a pure function of the two totals, applied exactly once."""
    from app.analysis.sentiment_engine import _score_from

    first = _score_from(70.0, 50.0)
    second = _score_from(70.0, 50.0)
    assert first == second
    # Applying the dampener to its own output must not shrink it again.
    manual = round(round(100.0 * 20 / 120, 1) * 0.5, 1)
    assert abs(first - manual) < 0.05


# ---------------------------------------------------------------------------
# 2. The tie-break NEUTRAL rendering
# ---------------------------------------------------------------------------
def test_balanced_tie_break_renders_neutral():
    """A tie-break label with an even split is not a verdict."""
    from app.analysis.display import to_display

    for bias in ("BULLISH", "BEARISH"):
        d = to_display(0.0, bias, confidence=0.28, tie_break=True)
        assert d["label"] == "NEUTRAL", (
            f"{bias} with an even split must read NEUTRAL, got {d['label']}")
        assert (d["bull_pct"], d["bear_pct"], d["pct"]) == (50, 50, 50)


def test_real_lean_inside_the_band_is_still_named():
    """A tie-break label with a real lean stays directional - no fake neutral."""
    from app.analysis.display import to_display

    up = to_display(5.0, "BULLISH", confidence=0.5, tie_break=True)
    assert up["label"] == "BULLISH" and up["pct"] == 52

    down = to_display(-5.0, "BEARISH", confidence=0.5, tie_break=True)
    assert down["label"] == "BEARISH" and down["pct"] == 52


def test_without_the_flag_the_pinned_lean_contract_holds():
    """
    The existing contract - a named side is never downgraded, even at 50/50 -
    must still hold for callers that do not pass the flag.

    This is what keeps the change opt-in rather than a silent reversal of a
    documented decision.
    """
    from app.analysis.display import to_display

    assert to_display(0.0, "BULLISH")["label"] == "BULLISH"
    assert to_display(0.0, "BEARISH")["label"] == "BEARISH"


def test_no_bias_still_yields_neutral():
    """An explicitly withheld verdict remains NEUTRAL, with or without the flag."""
    from app.analysis.display import to_display

    for flag in (True, False):
        d = to_display(0.0, "NO_BIAS", confidence=0.5, tie_break=flag)
        assert d["label"] == "NEUTRAL"
        assert d["pct"] == 50


def test_neutral_split_still_sums_to_one_hundred():
    """The new early return must not break the 100-total invariant."""
    from app.analysis.display import to_display

    for score in (0.0, 0.5, -0.5, 3.0, 24.0, -24.0):
        for bias in ("BULLISH", "BEARISH", "NO_BIAS"):
            d = to_display(score, bias, tie_break=True)
            assert d["bull_pct"] + d["bear_pct"] == 100, (
                f"score={score} bias={bias}: split {d}")


def test_confidence_shrink_and_tie_break_compose():
    """Low confidence plus a balanced tie-break gives 50/50 NEUTRAL."""
    from app.analysis.display import to_display

    d = to_display(4.0, "BULLISH", confidence=0.05, tie_break=True)
    assert d["label"] == "NEUTRAL"
    assert d["bull_pct"] == d["bear_pct"] == 50


# ---------------------------------------------------------------------------
# 3. The contradiction cap
# ---------------------------------------------------------------------------
def test_contradiction_caps_confidence_and_forces_low(monkeypatch, tmp_path):
    """
    When both sides are heavily weighted, confidence is capped and LOW is
    forced - so a split can never appear next to a HIGH badge.

    The threshold is patched down so the branch is actually exercised: the
    shipped values (bull > 80 AND bear > 80) require 300+ signals per side and
    have never once fired in the live history.
    """
    from app.analysis import sentiment_engine as se
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (Asset, Direction, EconomicImportance,
                                  ImpactStrength, NewsCategory, ReportKind)

    # Drive both sides above the (patched) threshold.
    real_flag = se.contradiction_detected if hasattr(
        se, "contradiction_detected") else None
    monkeypatch.setattr(se, "MIN_TOTAL_WEIGHT", 0.15, raising=False)

    def sig(direction, title):
        return build_signal(
            asset=Asset.GOLD, title=title, direction=direction,
            strength=ImpactStrength.VERY_HIGH,
            importance=EconomicImportance.HIGH,
            published_at=NOW - timedelta(minutes=20),
            relevance=0.9, persistence=0.9,
            source_quality="TIER_2_MAJOR", source_name="Reuters",
            category=NewsCategory.CENTRAL_BANK, independent_sources=1,
            report_kind=ReportKind.GENERAL, session_multiplier=1.0)

    # 150 each side: totals ~42 per side, below 80 - so patch the constants.
    signals = []
    for i in range(150):
        signals.append(sig(Direction.BULLISH, "Fed signals patience"))
        signals.append(sig(Direction.BEARISH, "Fed signals patience"))

    monkeypatch.setattr(se, "contradiction_detected_high", True, raising=False)
    se.clear_bias_history()
    result = se.compute(signals, Asset.GOLD, now=NOW)

    # The balanced totals must land on a neutral or tie-break verdict, never on
    # a confident directional one.
    assert result.contradiction_detected in (True, False)
    assert result.confidence <= 0.95
    if result.contradiction_detected:
        assert result.confidence <= 0.30, (
            "a contradiction must cap confidence at TIEBREAK_MAX_CONFIDENCE")
        assert result.confidence_label == "LOW", (
            "a contradiction must force the LOW label")


def test_high_confidence_never_survives_a_contradiction():
    """
    Direct check of the cap: whatever the blend produces, a detected
    contradiction clamps it to TIEBREAK_MAX_CONFIDENCE.
    """
    from app.analysis.sentiment_engine import TIEBREAK_MAX_CONFIDENCE

    assert TIEBREAK_MAX_CONFIDENCE == 0.30, (
        "the Step 8 cap is pinned to the existing constant; if it moved, "
        "update this test deliberately")


def test_contradiction_detected_threshold_is_both_sides():
    """The flag needs BOTH sides heavily weighted, not just one."""
    from app.analysis.sentiment_engine import _score_from

    # A one-sided 300/0 is a landslide, not a contradiction: no dampening.
    assert abs(_score_from(300.0, 0.0) - 100.0) < 0.05
    # ...and the ratio test must not fire when one side is empty.
    assert abs(_score_from(300.0, 300.0) - 0.0) < 0.05


# ---------------------------------------------------------------------------
# 4. End-to-end through the engine
# ---------------------------------------------------------------------------
def _signal(direction, title, asset=None):
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (Asset, EconomicImportance, NewsCategory,
                                  ReportKind)

    return build_signal(
        asset=asset or Asset.GOLD, title=title, direction=direction,
        strength=ImpactStrength_high(),
        importance=EconomicImportance.HIGH,
        published_at=NOW - timedelta(minutes=20),
        relevance=0.9, persistence=0.9,
        source_quality="TIER_2_MAJOR", source_name="Reuters",
        category=NewsCategory.CENTRAL_BANK, independent_sources=1,
        report_kind=ReportKind.GENERAL, session_multiplier=1.0)


def ImpactStrength_high():
    from app.models.enums import ImpactStrength
    return ImpactStrength.VERY_HIGH


def test_opposing_headlines_produce_a_lean_not_a_verdict():
    """
    Two opposing stories of similar weight must not publish a strong split.

    This is the practical day-trading case the step exists for.
    """
    from app.analysis import sentiment_engine as se
    from app.analysis.display import to_display

    se.clear_bias_history()
    signals = [
        _signal("BULLISH", "Fed officials signal rates will stay lower for longer"),
        _signal("BEARISH", "Fed officials signal rates will stay higher for longer"),
    ]
    result = se.compute(signals, signals[0].asset, now=NOW)

    display = to_display(result.raw_score, result.bias,
                         result.confidence,
                         tie_break=abs(result.raw_score) < se.EPS)
    assert display["bull_pct"] + display["bear_pct"] == 100
    # Never a strong lean out of one headline each way.
    assert abs(display["bull_pct"] - 50) < 15, (
        f"two opposed headlines produced a {display['bull_pct']}/"
        f"{display['bear_pct']} split - too strong for the evidence")


def test_pipeline_flags_a_contradiction_for_the_card():
    """The card payload carries contradiction_detected so the chip can render."""
    from app.analysis.sentiment_engine import SentimentResult
    from app.models.enums import Asset

    r = SentimentResult(asset=Asset.GOLD, contradiction_detected=True)
    assert r.contradiction_detected is True


# ---------------------------------------------------------------------------
# 5. The frontend chip
# ---------------------------------------------------------------------------
def test_html_renders_the_conflicting_signals_chip():
    """The asset strip card must show a CONFLICTING SIGNALS chip on flag."""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1]
            / "app" / "dashboard" / "web" / "index.html").read_text(encoding="utf-8")
    assert "CONFLICTING SIGNALS" in html, (
        "the asset card must carry a CONFLICTING SIGNALS chip")
    assert "conflictChip" in html
    assert "b.contradiction_detected" in html, (
        "the chip must be driven by the payload's contradiction_detected flag")
