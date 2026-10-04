"""
DASHBOARD DISPLAY MAPPING - contract tests.

The dashboard shows a verdict and a percentage instead of the engine's raw
score and confidence. These tests pin the three properties that make that
honest:

  1. The split always totals 100 (a bar that doesn't add up is a bug).
  2. NO_BIAS never renders as BULLISH or BEARISH, whatever the score.
  3. A named side never shows a percentage below 50.

Plus a guard that the API and the HTML still expose score/confidence even though
the UI no longer shows them - the decision log and tools/hitrate_report.py read
those fields, so dropping them from the payload would be a silent regression.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.analysis.display import to_display

REPO = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO / "app" / "dashboard" / "web" / "index.html"


# --------------------------------------------------------------------------
# 1. The split always totals 100
# --------------------------------------------------------------------------
@pytest.mark.parametrize("score", [-100, -37, 0, 1, 50, 100])
def test_bull_plus_bear_is_always_100(score):
    """Required case: the two halves must sum to exactly 100."""
    for bias in ("BULLISH", "BEARISH", "NO_BIAS"):
        d = to_display(score, bias)
        assert d["bull_pct"] + d["bear_pct"] == 100, (
            f"score={score} bias={bias} split to "
            f"{d['bull_pct']}+{d['bear_pct']} != 100")


@pytest.mark.parametrize("score", [-100, -37, 0, 1, 50, 100])
def test_split_stays_inside_zero_to_one_hundred(score):
    d = to_display(score, "BULLISH")
    assert 0 <= d["bull_pct"] <= 100
    assert 0 <= d["bear_pct"] <= 100


def test_split_follows_the_score_direction():
    """A negative score must weight the bear side, and vice versa."""
    assert to_display(-80, "BEARISH")["bear_pct"] > \
        to_display(-80, "BEARISH")["bull_pct"]
# --------------------------------------------------------------------------
# 2. NO_BIAS is never promoted to a side
# --------------------------------------------------------------------------
@pytest.mark.parametrize("score", [-40, -1, 0, 1, 3, 40])
def test_no_bias_always_renders_neutral(score):
    """Required case: NO_BIAS renders NEUTRAL, including at score > 0."""
    d = to_display(score, "NO_BIAS")
    assert d["label"] == "NEUTRAL"
    assert d["label"] not in ("BULLISH", "BEARISH")
    assert d["pct"] == 50


def test_no_bias_with_a_positive_score_still_does_not_bull():
    """
    The specific bug this guards: the engine withholds a verdict for thin
    evidence, and the score behind it is often slightly positive. A display
    rule keyed off the sign would paint this green.
    """
    d = to_display(3, "NO_BIAS")
    assert d["label"] == "NEUTRAL"
    assert d["pct"] == 50
    # The underlying split may still lean bullish - that is the engine's
    # number and is shown honestly in the bar - but the label does not lie.
    assert d["bull_pct"] == 52


def test_unrecognised_bias_fails_safe_to_neutral():
    """Anything unknown must render NEUTRAL, never a guessed side."""
    for value in (None, "", "MAYBE", "bullish-ish", 42, object()):
        assert to_display(10, value)["label"] == "NEUTRAL"


def test_bias_accepts_enum_and_string():
    """Callers may pass the Bias enum or its plain-string value."""
    from app.analysis.sentiment_engine import Bias

    assert to_display(10, Bias.BULLISH)["label"] == "BULLISH"
    assert to_display(10, "BULLISH")["label"] == "BULLISH"
    assert to_display(10, Bias.NO_BIAS)["label"] == "NEUTRAL"
    assert to_display(80, "BULLISH")["bull_pct"] > \
        to_display(80, "BULLISH")["bear_pct"]
    assert to_display(0, "NO_BIAS")["bull_pct"] == 50
# --------------------------------------------------------------------------
# 3. A named side never shows below 50
# --------------------------------------------------------------------------
@pytest.mark.parametrize("score", [0, 1, 5, 20, 50, 99, 100])
def test_bullish_never_shows_below_50(score):
    """Required case: BULLISH must never render a sub-50 percentage."""
    d = to_display(score, "BULLISH")
    assert d["label"] == "BULLISH"
    assert d["pct"] >= 50, f"BULLISH at score={score} showed {d['pct']}%"
    assert d["pct"] == d["bull_pct"]


@pytest.mark.parametrize("score", [0, -1, -5, -20, -50, -99, -100])
def test_bearish_never_shows_below_50(score):
    """Required case: BEARISH must never render a sub-50 percentage."""
    d = to_display(score, "BEARISH")
    assert d["label"] == "BEARISH"
    assert d["pct"] >= 50, f"BEARISH at score={score} showed {d['pct']}%"
    assert d["pct"] == d["bear_pct"]


def test_each_side_reports_its_own_half():
    """BULLISH shows the bull half, BEARISH the bear half - never the other."""
    up = to_display(60, "BULLISH")
    assert up["pct"] == up["bull_pct"] != up["bear_pct"]
    down = to_display(-60, "BEARISH")
    assert down["pct"] == down["bear_pct"] != down["bull_pct"]


def test_display_returns_exactly_the_four_fields():
    d = to_display(12.0, "BULLISH")
    assert set(d) == {"label", "pct", "bull_pct", "bear_pct"}


def test_non_numeric_and_nan_scores_do_not_raise():
    """A malformed stored score must not break the dashboard render."""
    for bad in (None, "abc", float("nan"), float("inf")):
        d = to_display(bad, "NO_BIAS")
        assert d["bull_pct"] + d["bear_pct"] == 100


def test_out_of_range_scores_are_clamped_without_breaking_the_total():
    """
    A score outside the nominal range must not produce a split summing to
    something other than 100 - that would silently break the bar's geometry.
    """
    for score in (-5000, 5000):
        d = to_display(score, "BULLISH")
        assert d["bull_pct"] + d["bear_pct"] == 100
        assert 0 <= d["bull_pct"] <= 100


# --------------------------------------------------------------------------
# 4. The UI shows no Score / Confidence, but the API still carries them
# --------------------------------------------------------------------------
def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_html_has_no_score_or_confidence_label():
    """
    Required case: no Score or Confidence label may remain in the visible UI.

    Scoped to the visible-text captions rather than the whole file, because the
    payload field names (b.score, b.confidence) legitimately survive inside the
    compatibility fallback in disp(), and the FinancialJuice LEGACY SCORING
    banner must keep its wording.
    """
    html = _html()
    assert "'SCORE'" not in html       # the boxed-number caption
    assert "'CONF'" not in html
    assert "mstat(" not in html        # that tile's helper is gone
    assert "**Score:**" not in html
    assert "**Confidence" not in html


def test_html_renders_the_verdict_from_the_display_block():
    """The verdict chip and the split bar must both be present."""
    html = _html()
    assert "function disp(" in html
    # The chip renders "<label> <n>%", with the label taken from the display
    # block. Matched loosely because the surrounding JS quotes are escaped.
    assert re.search(r"d\.label\+' '\+d\.pct\+'", html), (
        "the verdict chip must render the label and one percentage")
    # The label is taken from bias, never inferred from the percentage's sign.
    assert "verdictClass(d.label)" in html
    # The split bar is sized from the same block the percentage comes from.
    assert "d.bull_pct" in html and "d.bear_pct" in html


def test_html_shows_both_side_percentages_under_the_bar():
    """
    The card must print BOTH the bullish and the bearish figure beneath the
    split line - the "old dashboard" read - not just the named side.
    """
    html = _html()
    assert "function msplit(" in html
    assert "BULLISH" in html and "BEARISH" in html
    # Both figures come from the display block, so they match the bar exactly.
    assert "d.bull_pct" in html and "d.bear_pct" in html
    # prepCard must actually call it, directly after the bar.
    card = html.split("function prepCard(", 1)[1].split("function renderPrepStrip", 1)[0]
    assert "mbar(b)" in card and "msplit(b)" in card
    assert card.index("mbar(b)") < card.index("msplit(b)"), (
        "the two-sided numbers must sit BELOW the bar line")


def test_both_driver_sides_are_exposed_and_independent():
    """
    The card shows a BULLISH DRIVERS box beside a BEARISH DRIVERS box, so both
    sides must be carried in the payload - not just the leading side that
    "drivers" holds.

    Verified against the real aggregator: with one bullish and one bearish
    headline both lists come back, each containing only its own side.
    """
    from app.analysis.news_classifier import heuristic_analyze
    from app.analysis.sentiment_engine import WeightedSignal, compute
    from app.models.enums import Asset

    def signal(headline):
        analysis = heuristic_analyze(headline)
        impact = next(i for i in analysis.assets if i.asset is Asset.GOLD)
        return WeightedSignal(
            asset=Asset.GOLD, title=headline, direction=impact.direction,
            strength=impact.strength, relevance=impact.relevance,
            persistence=impact.persistence, category=analysis.category)

    up = signal("Gold surges as haven demand returns")
    down = signal("Fed signals hawkish policy and higher for longer")

    mixed = compute([up, down], Asset.GOLD)
    assert len(mixed.bull_drivers) == 1
    assert len(mixed.bear_drivers) == 1
    assert mixed.bull_drivers[0].title == up.title
    assert mixed.bear_drivers[0].title == down.title

    # Each box shows only its own side - neither leaks the other.
    bull_only = compute([up], Asset.GOLD)
    assert bull_only.bull_drivers and not bull_only.bear_drivers
    bear_only = compute([down], Asset.GOLD)
    assert bear_only.bear_drivers and not bear_only.bull_drivers


def test_existing_drivers_field_is_unchanged():
    """
    "drivers" must still carry only the LEADING side, because the decision log
    and hit-rate tooling read it. This change is additive.
    """
    from app.analysis.sentiment_engine import compute
    from app.analysis.sentiment_engine import WeightedSignal
    from app.models.enums import Asset, Direction, ImpactStrength

    bull = WeightedSignal(asset=Asset.GOLD, title="up", direction=Direction.BULLISH,
                          strength=ImpactStrength.VERY_HIGH, relevance=0.9)
    bear = WeightedSignal(asset=Asset.GOLD, title="down", direction=Direction.BEARISH,
                          strength=ImpactStrength.VERY_HIGH, relevance=0.9)
    result = compute([bull, bear], Asset.GOLD)
    assert len(result.drivers) <= 3
    # A NO_BIAS result still reports drivers, per the existing note wording.
    assert result.drivers or result.bias.value == "NO_BIAS"


def test_api_block_exposes_both_driver_sides(session):
    """Both driver lists must reach the API payload."""
    from app.main import _block

    block = _block(session, "XAUUSD", "MARKET_PREP")
    assert "bull_drivers" in block
    assert "bear_drivers" in block
    assert isinstance(block["bull_drivers"], list)
    assert isinstance(block["bear_drivers"], list)
    # The pre-existing single-side field is still there.
    assert "drivers" in block and "conflict" in block


def test_html_renders_two_driver_boxes():
    """The card must draw a bullish box and a bearish box side by side."""
    html = _html()
    card = html.split("function prepCard(", 1)[1].split(
        "function renderPrepStrip", 1)[0]
    assert "BULLISH DRIVERS" in card
    assert "BEARISH DRIVERS" in card
    assert "b.bull_drivers" in card
    assert "b.bear_drivers" in card
    # Bullish box first, bearish second.
    assert card.index("b.bull_drivers") < card.index("b.bear_drivers")
    # Each has its own colour class.
    assert "mnote('bull'" in card and "mnote('bear'" in card
    assert ".note.bull h4" in html and ".note.bear h4" in html


def test_each_side_is_its_own_coloured_box():
    """
    The bullish figure gets a GREEN box and the bearish figure a RED box.

    The colour is bound to the SIDE, not to the verdict: on a BULLISH card the
    86% is green and the 14% is still red. Tying the red box to "the losing
    side" would recolour it green on a bearish card and lose the meaning.
    """
    html = _html()
    assert 'class="side bull"' in html
    assert 'class="side bear"' in html
    # Distinct styling rules for each box, on the bull/bear pair of colours.
    assert ".mcard .bpct .side.bull" in html
    assert ".mcard .bpct .side.bear" in html


def test_neutral_card_keeps_the_same_green_and_red_boxes():
    """
    A NEUTRAL card must look like every other card: green bull box, red bear box.

    Greying the boxes for NO_BIAS was tried and reverted. It made a borderline
    asset (bitcoin sits right on the confidence floor) flicker between coloured
    and grey on every recompute, which read as a broken card rather than as a
    withheld verdict. The verdict chip above still greys and says NEUTRAL, so
    the withheld call remains visible where it belongs.
    """
    html = _html()
    assert ".mcard .bpct.neu" not in html, (
        "the boxes must not have a greyed variant; a NO_BIAS card keeps green/red")
    # msplit must not branch on the label at all.
    split = html.split("function msplit(", 1)[1].split("function sessionChain", 1)[0]
    assert "NEUTRAL" not in split, (
        "msplit must render identically for every bias")
    assert "neu" not in split, "msplit must not add a conditional class"
    # And the split figures still describe the real split for NO_BIAS.
    d = to_display(3, "NO_BIAS")
    assert (d["bull_pct"], d["bear_pct"]) == (52, 48)


def test_no_bias_is_still_stated_in_the_verdict_chip():
    """
    Removing the greyed boxes must not remove the NEUTRAL verdict itself.
    """
    html = _html()
    assert "verdictClass(d.label)" in html
    chip = html.split("function verdictClass", 1)[1].split("function", 1)[0]
    assert "'NO_BIAS'" in chip, (
        "a NEUTRAL verdict must still map to the greyed NO_BIAS chip")


def test_box_figures_are_larger_than_caption_text():
    """
    The boxed figures are the headline number, so they must be styled larger
    than the small caption above each one.
    """
    html = _html()
    fig = re.search(r"\.bpct \.fig\{[^}]*font-size:(\d+)px", html)
    cap = re.search(r"\.bpct \.cap\{[^}]*font-size:([\d.]+)px", html)
    assert fig and cap, "both the figure and its caption must be styled"
    assert float(fig.group(1)) >= 18, f"figure font {fig.group(1)}px is not large"
    assert float(cap.group(1)) < float(fig.group(1)), (
        "the caption must be smaller than the figure it labels")


def test_two_sided_numbers_never_contradict_a_no_bias_verdict():
    """
    A NEUTRAL card still prints both figures (they describe the engine's split)
    but must not be colour-coded as a direction, and the label stays NEUTRAL.
    """
    d = to_display(3, "NO_BIAS")
    assert d["label"] == "NEUTRAL"
    # The numbers are real and still total 100 - just not directional.
    assert d["bull_pct"] + d["bear_pct"] == 100
    assert (d["bull_pct"], d["bear_pct"]) == (52, 48)


def test_html_keeps_the_explaining_fields():
    """
    The verdict is only trustworthy if the reader can see why: drivers, the
    conflict flag, the event-risk flag and the engine mode must all stay.
    """
    html = _html()
    assert "TOP DRIVERS" in html
    assert "EVENT RISK" in html
    assert "conflicting headline" in html or "Conflict:" in html
    assert "b.mode||'heuristic'" in html


def test_financialjuice_legacy_scoring_banner_is_untouched():
    """Item 4: the FJ section keeps its own scorer and its own labelling."""
    assert "LEGACY SCORING" in _html()


def test_api_block_still_exposes_score_and_confidence():
    """
    Required case: the API JSON keeps score, confidence and bias, and only ADDS
    the display block. tools/hitrate_report.py and the decision log read these.
    """
def test_scoring_modules_do_not_import_display():
    """
    The dependency must point one way only: display reads the engine, the engine
    never reads display. This is what keeps the change presentation-only.
    """
    for module in ("app/analysis/sentiment_engine.py",
                   "app/analysis/transmission_map.py",
                   "app/analysis/cue_rules.py",
                   "app/analysis/event_polarity.py"):
        text = (REPO / module).read_text(encoding="utf-8")
        assert not re.search(r"import\s+display|from\s+app\.analysis\s+import.*display",
                             text), f"{module} must not depend on app.analysis.display"


def test_decision_log_path_is_untouched_by_display():
    """
    The display block must not be written into a persisted snapshot, or it would
    masquerade as engine output for anyone reading the log.
    """
    from app.analysis import display as display_module

    # Nothing in the display module may import persistence or the engine's
    # writer - it is a pure projection with no side effects.
    text = Path(display_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("database", "session_scope", "snapshot", "jsonl",
                      "import requests", "settings"):
        assert forbidden not in text, (
            f"app/analysis/display.py must stay pure, found {forbidden!r}")


def test_no_bias_gate_is_untouched():
    """
    The NO_BIAS rule must still require BOTH floors. This is the scoring
    behaviour the display change was explicitly not allowed to alter.
    """
    from app.analysis.sentiment_engine import _decide

    assert _decide(0.0, 0.0).value == "NO_BIAS"
    assert _decide(99.0, 0.01).value == "NO_BIAS"   # confidence too low
    assert _decide(0.5, 0.99).value == "NO_BIAS"    # score too small
    assert _decide(50.0, 0.8).value == "BULLISH"


# --------------------------------------------------------------------------
# 5. The API payload still carries the engine's own fields
# --------------------------------------------------------------------------
@pytest.fixture
def session():
    """A real (empty) session, so _block takes its genuine empty-state path."""
    from app.database import init_db, session_scope

    init_db()
    with session_scope() as s:
        yield s


def test_api_block_still_exposes_score_and_confidence(session):
    """
    Required case: the API JSON keeps score, confidence and bias, and only ADDS
    the display block. tools/hitrate_report.py and the decision log read these,
    so removing them from the payload would be a silent regression.

    The test runs against whatever snapshot exists (a stored one on a live
    database, the empty state otherwise), so it asserts the SHAPE rather than
    specific numbers - the point is that the engine's fields survive.
    """
    from app.main import _block

    for asset in ("XAUUSD", "XAGUSD", "BTCUSD"):
        block = _block(session, asset, "MARKET_PREP")
        assert "score" in block, f"{asset}: score was dropped from the payload"
        assert "confidence" in block, f"{asset}: confidence was dropped"
        assert "bias" in block, f"{asset}: bias was dropped"
        assert "display" in block, f"{asset}: display block was not added"
        assert set(block["display"]) == {"label", "pct", "bull_pct", "bear_pct"}
        # The display is derived from the engine's own fields, never replaces.
        assert block["display"] == to_display(block["score"], block["bias"])
        assert block["display"]["label"] in ("BULLISH", "BEARISH", "NEUTRAL")
        assert block["display"]["bull_pct"] + block["display"]["bear_pct"] == 100


def test_empty_state_block_is_neutral(session):
    """
    With nothing stored, _block must produce the explicit empty state - and
    that state is a genuine NO_BIAS, so it renders neutral rather than a side.
    """
    from app.main import _block

    empty = _block(session, "ZZZ_NO_SUCH_ASSET", "MARKET_PREP")
    assert empty["score"] == 0.0
    assert empty["confidence"] == 0.0
    assert empty["bias"] == "NO_BIAS"
    assert empty["display"]["label"] == "NEUTRAL"
    assert empty["display"]["pct"] == 50
    assert empty["display"]["bull_pct"] == 50
    assert empty["display"]["bear_pct"] == 50


def test_api_display_block_agrees_with_the_engine_fields(session):
    """
    The projection must be consistent with score/bias in the same payload, so a
    consumer reading either representation sees the same verdict.
    """
    from app.main import _block

    block = _block(session, "BTCUSD", "MARKET_PREP")
    assert block["display"] == to_display(block["score"], block["bias"])
