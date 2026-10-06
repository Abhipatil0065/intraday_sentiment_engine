"""
DASHBOARD DISPLAY MAPPING - contract tests.

The dashboard shows a verdict and a percentage instead of the engine's raw
score and confidence. These tests pin the three properties that make that
honest:

  1. The split always totals 100 (a bar that doesn't add up is a bug).
  2. The chip names the side holding more than half the split, and NEUTRAL is
     reserved for a genuinely even 50/50.
  3. A named side never shows a percentage below 50, and is never downgraded.

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
# 2. The chip names the side holding more than half the split
# --------------------------------------------------------------------------
@pytest.mark.parametrize("score, expected, expected_pct", [
    (40, "BULLISH", 70),
    (3, "BULLISH", 52),
    (1, "NEUTRAL", 50),   # rounds to an even 50/50 - no side to name
    (0, "NEUTRAL", 50),
    (-1, "NEUTRAL", 50),
    (-3, "BEARISH", 52),
    (-40, "BEARISH", 70),
])
def test_label_names_the_side_over_fifty_percent(score, expected, expected_pct):
    """
    Required case: the top of the card must never read NEUTRAL while one side
    owns more than half the split.

    That is the bug this change fixes. A 52/48 bar was being drawn directly
    beneath a grey NEUTRAL chip, so the card withheld a side the reader could
    plainly see - and NEUTRAL then meant "a call exists but is too weak to
    print", which is not a direction anyone can trade off. NEUTRAL now means
    exactly one thing: an even 50/50 split.
    """
    d = to_display(score, "NO_BIAS")
    assert d["label"] == expected, (
        f"score={score} split {d['bull_pct']}/{d['bear_pct']} labelled "
        f"{d['label']}, expected {expected}")
    assert d["pct"] == expected_pct

    # The chip can never contradict the bar printed beneath it.
    if expected == "BULLISH":
        assert d["pct"] == d["bull_pct"] > 50
    elif expected == "BEARISH":
        assert d["pct"] == d["bear_pct"] > 50
    else:
        assert (d["bull_pct"], d["bear_pct"]) == (50, 50)


def test_thin_lean_is_named_rather_than_withheld():
    """
    The borderline case that matters in practice: score=3 is exactly where a
    near-floor asset such as bitcoin sits. The bar reads 52/48, so the chip
    names BULLISH 52% - the reader is given the same lean the bar already shows
    instead of a withheld call they cannot act on.
    """
    d = to_display(3, "NO_BIAS")
    assert (d["label"], d["pct"]) == ("BULLISH", 52)
    assert (d["bull_pct"], d["bear_pct"]) == (52, 48)


def test_an_explicitly_named_side_is_never_downgraded():
    """
    Following the split must not weaken an explicit engine call. BULLISH/BEARISH
    keep their label even at an even split, so the display can never contradict
    a verdict the engine did make.
    """
    assert to_display(0, "BULLISH")["label"] == "BULLISH"
    assert to_display(0, "BEARISH")["label"] == "BEARISH"


def test_unrecognised_bias_fails_safe_to_neutral():
    """
    An absent or malformed verdict must NOT be resolved from the score.

    This is the one place the "follow the split" rule is deliberately not
    applied. A real NO_BIAS is a decision the engine made and is reported
    honestly; garbage in the payload is no decision at all, and inferring a side
    from a raw number off it would be inventing a signal out of a bug.
    """
    for value in (None, "", "MAYBE", "bullish-ish", 42, object()):
        assert to_display(10, value)["label"] == "NEUTRAL"


def test_bias_accepts_enum_and_string():
    """Callers may pass the Bias enum or its plain-string value."""
    from app.analysis.sentiment_engine import Bias

    assert to_display(10, Bias.BULLISH)["label"] == "BULLISH"
    assert to_display(10, "BULLISH")["label"] == "BULLISH"
    # The enum and its string form must resolve identically through the split.
    assert to_display(10, Bias.NO_BIAS) == to_display(10, "NO_BIAS")
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


def test_js_fallback_mirrors_the_python_rule():
    """
    disp()'s fallback path - used when a payload predates the display block -
    must apply the same rule, or the same card renders differently depending on
    which server answered it. NEUTRAL may only be produced by the leftover case
    of an even 50/50, never by a withheld verdict.
    """
    html = _html()
    disp_js = html.split("function disp(", 1)[1].split("function verdictClass", 1)[0]
    # A named side still wins outright.
    assert "bias==='BULLISH'" in disp_js and "bias==='BEARISH'" in disp_js
    # Otherwise the side past 50% is what gets named, both ways round.
    assert "bull>50" in disp_js and "100-bull>50" in disp_js
    # The label branches on the split, so a plain "NO_BIAS -> NEUTRAL" shortcut
    # (the old rule) must not reappear in any form.
    assert "NO_BIAS" not in disp_js


def test_js_fallback_matches_python_split_arithmetic():
    """
    The fallback recomputes the split, and JavaScript rounds half away from zero
    where Python rounds half to even. Using Math.round directly would put the
    two one point apart on odd scores (server 50, JS 51), so the tie-break is
    spelled out in rnd() instead.
    """
    html = _html()
    rnd_js = html.split("function rnd(", 1)[1].split("function disp(", 1)[0]
    assert "diff>0.5" in rnd_js and "diff<0.5" in rnd_js, (
        "rnd() must branch on the fractional part, not delegate to Math.round")
    assert "Math.round" not in rnd_js, (
        "Math.round ties upward and would drift from the server's split")
    disp_js = html.split("function disp(", 1)[1].split("function verdictClass", 1)[0]
    assert "rnd(50+s/2)" in disp_js, (
        "the fallback must split the score with the shared rnd() helper")


def test_bias_flip_tracking_is_wired_up():
    """
    Each card must be able to say when its bias last changed sides.

    The state is client-side (localStorage) rather than server-side: the backend
    has no "previous bias" concept, and adding one would mean new columns plus a
    migration for a cosmetic timestamp. localStorage also gives
    survive-a-refresh for free.
    """
    html = _html()
    assert "'biasFlipState'" in html, "the storage key must be the agreed name"
    assert "function loadFlipState()" in html
    assert "function saveFlipState()" in html
    assert "function trackFlip(" in html
    # Guarded on both sides: corrupt JSON must not break the dashboard.
    load = html.split("function loadFlipState()", 1)[1].split("function saveFlipState", 1)[0]
    assert "try{" in load and "catch" in load


def test_flip_time_is_always_rendered_in_ist():
    """
    The stamp must be IST regardless of where the browser or server is, and in
    12-hour form with an AM/PM marker.
    """
    html = _html()
    fn = html.split("function flipTimeLabel(", 1)[1].split("function msplit(", 1)[0]
    assert "Asia/Kolkata" in fn, "the timezone must be pinned, not local"
    assert "hour12:true" in fn
    assert "hour:'2-digit'" in fn and "minute:'2-digit'" in fn
    assert "IST" in fn


def test_a_tie_never_records_a_flip():
    """
    An exact 50/50 has no dominant side, so it must leave the stored bias and
    its timestamp untouched - otherwise a tie would wipe the flip time off an
    asset that has been leaning one way all morning.
    """
    html = _html()
    fn = html.split("function trackFlip(", 1)[1].split("function flipTimeLabel", 1)[0]
    # The tie branch returns the previous timestamp rather than clearing it.
    tie = fn.split("if(!side)", 1)[1].split("}", 1)[0]
    assert "prev.at" in tie, "a tie must keep the previous flip time"


def test_first_sighting_uses_the_server_history_not_a_blank():
    """
    There is nothing to have flipped FROM on first load, so a real flip time must
    never be invented - but the box must not be left blank either. The server's
    `bias_since_utc` (derived from snapshot history) seeds the clock, and only
    that: it is never replaced by `new Date()`, which would claim a flip that
    did not happen while the reader was watching.
    """
    html = _html()
    fn = html.split("function trackFlip(", 1)[1].split("function flipTimeLabel", 1)[0]
    first = fn.split("if(!prev.bias)", 1)[1].split("if(prev.bias!==side)", 1)[0]
    assert "seed" in first, "the first sighting must take the server's timestamp"
    # The stamping branch is a different one - guard that first-sighting cannot
    # reach it by reaching for the wall clock.
    assert "new Date().toISOString()" not in first, (
        "the first sighting must never stamp the current time as a flip")


def test_a_null_record_is_topped_up_from_the_server():
    """
    A tab opened before this feature existed holds `at: null` forever otherwise,
    because the bias never changes and the first-sighting branch never re-runs.
    """
    html = _html()
    fn = html.split("function trackFlip(", 1)[1].split("function flipTimeLabel", 1)[0]
    backfill = fn.split("Same side as last time", 1)[1]
    assert "prev.at" in backfill and "seed" in backfill, (
        "a null timestamp must be repaired from the server's value")


def test_backend_publishes_when_the_current_side_began():
    """
    The server must derive "this side started at ..." from snapshot history so a
    fresh browser has something truthful to show.
    """
    from app.main import _bias_since, _block
    from app.database import init_db, session_scope

    init_db()
    with session_scope() as s:
        block = _block(s, "XAUUSD", "MARKET_PREP")
        assert "bias_since_utc" in block, (
            "the block must carry the stance start time for the card")
        # Either an ISO stamp or None - never a fabricated value.
        assert block["bias_since_utc"] is None or isinstance(
            block["bias_since_utc"], str)
        # An even split has no dominant side, so it can have no stance date.
        assert _bias_since(s, "XAUUSD", "MARKET_PREP", 0.0) is None


def test_the_dominant_box_grows_and_animates():
    """
    The box holding the flip time is the wider one. Only the WIDTH changes - the
    green/red colouring stays keyed to the side.
    """
    html = _html()
    assert ".mcard .bpct .side.lead" in html
    assert "flex-grow" in html
    assert "transition:flex-grow" in html, "a flip must animate, not snap"
    msplit = html.split("function msplit(", 1)[1].split("function sessionChain", 1)[0]
    assert "' lead'" in msplit, "the leading box must get the lead class"


def test_flip_markup_only_lands_in_the_dominant_box():
    """
    Exactly one box may carry the stamp, and it must be the dominant side: the
    opposite box loses the time the moment a new flip is recorded.
    """
    msplit = _html().split("function msplit(", 1)[1].split("function sessionChain", 1)[0]
    assert "bullAt?" in msplit and "bearAt?" in msplit
    # Each side's stamp is gated on its own lead flag, never on the other's.
    assert "bullLead?flipTimeLabel" in msplit
    assert "bearLead?flipTimeLabel" in msplit


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
    # prepCard must actually call it, directly after the bar. msplit now also
    # takes the asset symbol so each card keys its own flip time.
    card = html.split("function prepCard(", 1)[1].split("function renderPrepStrip", 1)[0]
    assert "mbar(b)" in card and "msplit(b," in card
    assert card.index("mbar(b)") < card.index("msplit(b,"), (
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
    # The markup now appends a conditional ' lead' class for the dominant
    # side, so match the prefix rather than a closed class attribute.
    assert 'class="side bull' in html
    assert 'class="side bear' in html
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


def test_the_two_sided_numbers_agree_with_the_chip():
    """
    The card prints BOTH figures and the chip names ONE of them. They must be
    two views of the same split: the chip's figure is always the side it names,
    so a reader who compares the green box against the top line sees a match.
    """
    for score in (-40, -3, 3, 40):
        d = to_display(score, "NO_BIAS")
        assert d["bull_pct"] + d["bear_pct"] == 100
        if d["label"] == "BULLISH":
            assert d["pct"] == d["bull_pct"]
        elif d["label"] == "BEARISH":
            assert d["pct"] == d["bear_pct"]
        else:
            assert d["pct"] == 50


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


def test_no_bias_gate_is_removed():
    """
    Task 1: the old score/confidence gate (_decide) that withheld a label is
    gone. A signed score always names a side and confidence never gates it.
    """
    from app.analysis import sentiment_engine as se

    assert not hasattr(se, "_decide"), "the NO_BIAS gate must stay removed"
    r = se.compute([], se.Asset.GOLD)
    assert r.bias.value in ("BULLISH", "BEARISH")


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


def test_empty_state_block_is_directional_and_even(session):
    """
    With nothing stored, _block must produce the explicit empty state - and
    binary bias means it names a side (DEFAULT_BIAS) rather than a grey
    NO_BIAS, while the split stays an honest 50/50.
    """
    from app.main import _block

    empty = _block(session, "ZZZ_NO_SUCH_ASSET", "MARKET_PREP")
    assert empty["score"] == 0.0
    assert empty["confidence"] == 0.0
    assert empty["bias"] in ("BULLISH", "BEARISH")
    assert empty["display"]["label"] == empty["bias"]
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
