"""Binary bias contract tests (Task 1): the label is the sign of the score."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.analysis.sentiment_engine import (
    EPS, MIN_TOTAL_WEIGHT, TIEBREAK_LEAN, TIEBREAK_MAX_CONFIDENCE, Bias,
    WeightedSignal, clear_bias_history, compute, remember_bias,
)
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
)
from tests.fixed_clock import FIXED_NOW as NOW, freeze_now


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Pin the clock and reset carry-forward history around each test."""
    freeze_now(monkeypatch)
    clear_bias_history()
    yield
    clear_bias_history()


def make(asset=Asset.GOLD, direction=Direction.BULLISH,
         strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
         minutes_ago=5, title="test headline", weight=0.8,
         category=NewsCategory.CENTRAL_BANK, **kw) -> WeightedSignal:
    return WeightedSignal(
        asset=asset, title=title, direction=direction, strength=strength,
        importance=importance,
        published_at=NOW - timedelta(minutes=minutes_ago),
        relevance=weight, persistence=kw.get("persistence", 0.7),
        category=category, cluster_id=kw.get("cluster_id", ""),
        corroboration=kw.get("corroboration", 1.0),
        is_primary=kw.get("is_primary", True),
    )


def test_positive_score_is_bullish():
    """A net positive score is labelled BULLISH - never NO_BIAS."""
    signals = [make(direction=Direction.BULLISH, title=f"bull {i}",
                    cluster_id=f"c{i}") for i in range(6)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.score > 0.0, "six bullish headlines must score positive"
    assert r.bias is Bias.BULLISH
    assert r.stale is False


def test_negative_score_is_bearish():
    """A net negative score is labelled BEARISH - never NO_BIAS."""
    signals = [make(direction=Direction.BEARISH, title=f"bear {i}",
                    cluster_id=f"c{i}") for i in range(6)]
    r = compute(signals, Asset.GOLD, now=NOW)
    assert r.score < 0.0, "six bearish headlines must score negative"
    assert r.bias is Bias.BEARISH
    assert r.stale is False


def test_tiny_score_ties_break_to_newest_high_weight_signal():
    """Inside +/- EPS the sign is not trusted: the heaviest, newest
    directional signal picks the side - even against the score's sign."""
    bear = make(direction=Direction.BEARISH, strength=ImpactStrength.VERY_HIGH,
                weight=1.0, minutes_ago=2, title="fresh bear",
                cluster_id="bear")
    # Three bulls with HIGH strength at same age - their aggregate with
    # 1.20x burst bonus slightly exceeds the single VERY_HIGH bear,
    # keeping raw score inside EPS. The bear is heaviest single signal
    # (VERY_HIGH=1.0 vs HIGH=0.72).
    bulls = [make(direction=Direction.BULLISH, strength=ImpactStrength.HIGH,
                  weight=0.45, minutes_ago=2, title=f"old bull {i}",
                  cluster_id=f"b{i}") for i in range(3)]
    r = compute([bear] + bulls, Asset.GOLD, now=NOW)
    assert 0.0 < r.raw_score < EPS, "tiny raw score (naive sign = BULLISH)"
    assert r.bias is Bias.BEARISH, "tie-break follows the heaviest, newest"
    assert r.score == -TIEBREAK_LEAN, "payload shows the lean, not the raw"
    assert r.stale is False


def test_no_signals_carries_last_bias_forward_and_is_stale():
    """No news in the window: the last published verdict is reused (stale)."""
    remember_bias(Asset.GOLD.value, Bias.BEARISH)
    r = compute([], Asset.GOLD, now=NOW)
    assert r.stale is True
    assert r.bias is Bias.BEARISH, "the carried-forward side must be named"
    assert r.score == 0.0
    assert r.confidence == 0.0
    assert r.contributing_articles == 0


def test_no_code_path_returns_neutral_or_no_bias():
    """Every branch of compute() must name a side: BULLISH or BEARISH only."""
    scenarios = {
        "positive": [make(direction=Direction.BULLISH, cluster_id="p")],
        "negative": [make(direction=Direction.BEARISH, cluster_id="n")],
        "no signals": [],
        "all directionless": [make(direction=Direction.NEUTRAL, title="n1",
                                   cluster_id="d1"),
                              make(direction=Direction.MIXED, title="m1",
                                   cluster_id="d2")],
        "balanced conflict": [make(direction=Direction.BULLISH, title="up",
                                   cluster_id="a"),
                              make(direction=Direction.BEARISH, title="down",
                                   cluster_id="b")],
        "tiny tie": [make(direction=Direction.BULLISH, weight=0.4,
                          minutes_ago=1, cluster_id="t1"),
                     make(direction=Direction.BEARISH, weight=0.4,
                          minutes_ago=1, cluster_id="t2")],
    }
    for name, signals in scenarios.items():
        r = compute(signals, Asset.GOLD, now=NOW)
        assert r.bias.value in ("BULLISH", "BEARISH"), f"{name}: {r.bias.value}"
        assert r.bias is not Bias.NO_BIAS, name
        assert r.bias.value != "NEUTRAL", name


def test_equity_tie_break_payload_score_matches_label():
    """The equity card has no server display block, so disp() derives the
    split straight from the payload score. Here the raw score leans bullish
    but the tie-break names BEARISH, so the payload score must be negative
    and the disp() math must put bearish at 50 or more (~51/49)."""
    from app.analysis.equity_card import build_equity_card, to_payload
    from app.market.session_detector import SessionName

    def eq(direction, strength, weight, minutes_ago, cluster, title, category=NewsCategory.EQUITIES):
        return WeightedSignal(
            asset=Asset.BITCOIN, title=title, direction=direction,
            strength=strength, importance=EconomicImportance.HIGH,
            published_at=NOW - timedelta(minutes=minutes_ago),
            relevance=weight, persistence=0.7,
            category=category, cluster_id=cluster)

    # Bearish signal uses EQUITIES category (equity_relevance=1.0), VERY_HIGH
    # strength, 5 min old. The four bullish signals use RISK_SENTIMENT
    # category, HIGH strength, 3 min old. Their aggregate leans bullish, but
    # the bearish signal's single magnitude remains highest, so the tie-break
    # still names BEARISH while the payload score follows the label.
    sigs = [
        eq(Direction.BEARISH, ImpactStrength.VERY_HIGH, 1.0, 5, "bear",
           "Nasdaq slides as yields jump", NewsCategory.EQUITIES),
        eq(Direction.BULLISH, ImpactStrength.HIGH, 1.0, 3, "b1",
           "Stocks rally on strong data", NewsCategory.RISK_SENTIMENT),
        eq(Direction.BULLISH, ImpactStrength.HIGH, 1.0, 3, "b2",
           "Equities surge on strong data", NewsCategory.RISK_SENTIMENT),
        eq(Direction.BULLISH, ImpactStrength.HIGH, 1.0, 3, "b3",
           "Nasdaq rises on strong data", NewsCategory.RISK_SENTIMENT),
        eq(Direction.BULLISH, ImpactStrength.HIGH, 1.0, 3, "b4",
           "S&P 500 rises on strong data", NewsCategory.RISK_SENTIMENT),
    ]
    clear_bias_history()
    card = build_equity_card(sigs, now=NOW, session=SessionName.US)
    payload = to_payload(card)

    assert card.raw_score > 0.0, "the raw score must lean bullish"
    assert payload["bias"] == "BEARISH"
    assert payload["score"] < 0.0, "the payload score must follow the label"

    # The same arithmetic disp() uses: bull = rnd(50 + score / 2).
    bull = int(round(50 + payload["score"] / 2.0))
    bear = 100 - bull
    assert bear >= 50, f"bearish must hold at least half, got {bull}/{bear}"
    assert (bull, bear) == (49, 51)


def test_matching_side_holds_half_across_scores():
    """Label/percentage consistency: the named side owns >= 50 of the split."""
    cases = [(-80.0, "BEARISH"), (-3.0, "BEARISH"), (0.0, "BULLISH"),
             (3.0, "BULLISH"), (80.0, "BULLISH")]
    for score, label in cases:
        bull = int(round(50 + score / 2.0))
        bear = 100 - bull
        if label == "BULLISH":
            assert bull >= 50, f"score={score}"
        else:
            assert bear >= 50, f"score={score}"


def test_strong_score_passes_through_unchanged():
    """A score at/above EPS is published verbatim - the lean never fires."""
    bulls = [make(direction=Direction.BULLISH, title=f"s {i}",
                  cluster_id=f"v{i}") for i in range(6)]
    r = compute(bulls, Asset.GOLD, now=NOW)
    assert abs(r.raw_score) >= EPS
    assert r.score == r.raw_score
    assert r.bias is Bias.BULLISH


def test_sub_floor_evidence_publishes_the_lean_not_zero():
    """Under MIN_TOTAL_WEIGHT the score must be the weighted lean, not 0.0.

    A zeroed score is what froze every dashboard card on "BULLISH 50% /
    BEARISH 50%": display.to_display projects the score straight onto the bar
    (bull_pct = round(50 + score / 2)), so score 0 renders a 50/50 split even
    when the two totals had a side. The weight gate picks the BIAS path (no
    flip / stability machinery); it must not discard the lean the card shows.
    """
    # A stale LOW-strength bearish headline: real weight, far under the floor,
    # nothing on the other side, so the unscaled ratio is a full -100.
    thin = [make(direction=Direction.BEARISH, strength=ImpactStrength.LOW,
                 importance=EconomicImportance.LOW, weight=0.9,
                 minutes_ago=20, title="stale bear", cluster_id="t")]
    r = compute(thin, Asset.GOLD, now=NOW)

    assert r.bias is Bias.BEARISH, "the stronger side still names the label"
    assert r.raw_score == -100.0, "the unscaled ratio survives for audit"
    assert r.score < 0.0, "the lean must be published, not zeroed"
    assert abs(r.score) < 10.0, "but thin evidence reads thin"
    assert r.confidence <= TIEBREAK_MAX_CONFIDENCE
    assert r.confidence_label == "LOW"
    assert "Weak evidence" in r.note

    # The dashboard math the card actually runs: the split must move.
    bull = int(round(50 + r.score / 2.0))
    assert bull < 50, f"score {r.score} must pull the bar bearish"

    # Nearer the floor the same one-sided lean reads proportionally stronger,
    # continuous with the full path, which is only taken AT the floor.
    near = [make(direction=Direction.BULLISH, strength=ImpactStrength.MEDIUM,
                 importance=EconomicImportance.MEDIUM, weight=0.9,
                 minutes_ago=10, title="fresh bull", cluster_id="n")]
    r2 = compute(near, Asset.GOLD, now=NOW)
    assert r2.bias is Bias.BULLISH
    assert r2.score > abs(r.score), "more evidence under the floor leans harder"
    bull2 = int(round(50 + r2.score / 2.0))
    assert bull2 > 50, f"score {r2.score} must pull the bar bullish"

    # No lean at all is still an honest, exact 50/50: directionless-only
    # evidence must publish exactly 0.0, with the label carried as before.
    flat = [make(direction=Direction.NEUTRAL, title="n1", cluster_id="d1"),
            make(direction=Direction.MIXED, title="m1", cluster_id="d2")]
    r3 = compute(flat, Asset.GOLD, now=NOW)
    assert r3.score == 0.0
    assert r3.raw_score == 0.0
    assert r3.confidence == 0.0
    assert r3.confidence_label == "LOW"
    assert r3.bias in (Bias.BULLISH, Bias.BEARISH)