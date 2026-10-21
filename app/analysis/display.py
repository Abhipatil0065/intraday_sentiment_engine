"""
DASHBOARD DISPLAY MAPPING (presentation only).

WHAT THIS IS
------------
The engine already answers with three numbers: ``score`` (-100..+100),
``confidence`` (0..1) and ``bias`` (BULLISH / BEARISH / NO_BIAS). The dashboard
used to show the raw score and confidence, which asked a reader to do the
interpretation. This module converts that verdict into the one thing the reader
actually wants: a direction and a percentage.

WHAT THIS IS NOT
----------------
It changes NOTHING about how a verdict is reached. It does not read
confidence, it does not apply a threshold, and it does not feed back into
scoring: the engine's own ``bias`` is still stored, logged and reported
unchanged. Scoring, the NO_BIAS gate, the transmission map and the decision log
are all untouched; this is a pure projection applied at render time. What it may
do is NAME a side the engine's own split already points at, so the label and the
bar beneath it tell one story rather than two.

THE ONE RULE THAT MATTERS
-------------------------
The output is ALWAYS BULLISH or BEARISH - never NEUTRAL, never a withheld
verdict. The top label names the side that owns MORE THAN HALF the split. On
an exact 50/50 (or any other tie) the display resolves to BULLISH as a
directional default, because a day trader cannot act on a grey chip and the
split is already the engine's own number (``bull_pct = 50 + score/2``), so
following it does not invent a signal: it stops the display from contradicting
the bar printed directly beneath it.

Two guards survive that, because a bare sign still is not a verdict:

* A side the engine NAMED is never downgraded. BULLISH/BEARISH keep their label
  even at an even split, so the display can never contradict an explicit call.
* A missing or unrecognised ``bias`` fails safe to the directional default
  (BULLISH) instead of being inferred from the raw number, so a malformed
  payload is never guessed at - it is resolved to a direction the same way a
  50/50 split is, never left blank.

THE SPLIT IS THE RATIO OF THE EVIDENCE
--------------------------------------
When the two aggregate totals are available - which is every live tick, since
the engine captures them on every reading - the split is their ratio and
nothing else:
2.80 bullish weight against 1.21 bearish weight is a 70/30 split. The card's
evidence line already prints those two numbers, so the bar and the boxes
finally show the same thing the reader can check.

This replaced a score-based projection that could not express the split when a
verdict and the aggregate disagreed: a fresh Tier-1 release flips the badge on
its own while the day's aggregate still leans the other way, and the old code
answered that by replacing the whole split with a synthetic one-point lean. A
card then published "Bullish 49% / Bearish 51%" against evidence that was
really 70/30. The badge keeps the engine's verdict and the boxes keep the
evidence; where the two disagree the badge withholds its percentage rather
than printing one it cannot support.
"""

from __future__ import annotations

BULLISH = "BULLISH"
BEARISH = "BEARISH"

# The ONE lean shown when a named side contradicts the raw split (a tie-break
# verdict whose raw score leans the other way): one point over half toward the
# label - never a strong-looking bar for a call the engine had to tie-break.
LEAN_BULL_PCT = 51

# BINARY DISPLAY CONTRACT: the output is ALWAYS BULLISH or BEARISH. There is
# no NEUTRAL label and no 50/50 chip. On an exact 50/50 split (or any other
# case where no side owns more than half) the display resolves to BULLISH as
# the directional default, so a card can never render a withheld verdict.
DEFAULT_DISPLAY_BIAS = BULLISH


def _bias_name(bias) -> str | None:
    """
    Normalise Bias enum / str to the engine's own vocabulary.

    Returns ``None`` for anything unrecognised, which is deliberately NOT the
    same answer as a genuine verdict. A real BULLISH/BEARISH is a decision the
    engine made and is reported honestly; a missing or malformed value is an
    absent decision, and resolving it from the score would be guessing. The
    caller resolves ``None`` to the directional default (BULLISH).
    """
    if bias is None:
        return None
    value = getattr(bias, "value", bias)
    text = str(value).strip().upper()
    if text == "NO_BIAS":
        return "NEUTRAL"
    return text if text in (BULLISH, BEARISH) else None


def _clamp_pct(value: int) -> int:
    return max(0, min(100, value))


def _leading_side(bull_pct: int) -> str:
    """
    The side holding more than half the split.

    ``bear_pct`` is always ``100 - bull_pct``, so a bull figure above 50 makes a
    bear figure above 50 impossible and vice versa - the two cases cannot both
    fire. On an exact 50/50 there is no leading side, so the caller resolves to
    the directional default (BULLISH) instead of emitting a withheld verdict.
    """
    if bull_pct > 50:
        return BULLISH
    if 100 - bull_pct > 50:
        return BEARISH
    return DEFAULT_DISPLAY_BIAS


def split_from_totals(bull, bear) -> tuple | None:
    """
    The bull/bear split implied by the two aggregate evidence totals.

    ``bull_pct = round(100 * bull / (bull + bear))`` - the RATIO, nothing
    else. This is the same pair the dashboard card already prints on its
    evidence line ("score 39.8 - bull 2.8 - bear 1.2"), so the bar and the two
    percentage boxes finally show numbers the reader can check against the
    line above them.

    Returns ``None`` when the totals are unusable - missing (legacy rows
    written before TASK 2 captured them), NaN, or adding up to nothing - so
    the caller falls back to the score projection instead of inventing a
    split.
    """
    try:
        b = float(bull)
        r = float(bear)
    except (TypeError, ValueError):
        return None
    if b != b or r != r:                      # NaN must not reach the UI
        return None
    b = max(0.0, b)
    r = max(0.0, r)
    total = b + r
    if total <= 0.0:
        return None
    bull_pct = max(0, min(100, int(round(100.0 * b / total))))
    return bull_pct, 100 - bull_pct


def to_display(score: float, bias, confidence: float | None = None,
               tie_break: bool = False,
               *, bull: float | None = None, bear: float | None = None) -> dict:
    """
    Project an engine verdict onto the dashboard's bull/bear split.

    THE SPLIT IS THE RATIO OF THE TWO EVIDENCE TOTALS.
    -----------------------------------------------
    When ``bull`` and ``bear`` are supplied - the aggregate weights behind the
    verdict - the split IS their ratio: 2.80 bullish weight against 1.21
    bearish weight is a 70/30 bullish split, whatever the badge says. That is
    the number the card's own evidence line already prints, so the bar, the
    two percentage boxes and the line above them are one story.

    WHY THE SCORE IS NOT THE SPLIT ANY MORE
    The score projection (``bull_pct = round(50 + score / 2)``) answers a
    different question - "how far from even is the net lean" - and it cannot
    express the split when a verdict and the aggregate disagree. A fresh
    Tier-1 release flips the badge on its own (``tier1_override``) while the
    day's aggregate is still strongly the other way, and the score projection
    then had to be REPLACED by a synthetic one-point lean
    (``LEAN_BULL_PCT``), so the card published "Bullish 49% / Bearish 51%"
    against evidence that was really 70/30. The ratio removes that
    fabrication: the split is always the evidence, and the badge keeps the
    engine's verdict.

    The confidence shrink is deliberately NOT applied to the ratio path:
    confidence already carries its own badge on the card, and shrinking the
    ratio toward 50/50 would re-introduce exactly the "the boxes say 49/51
    while the evidence says 70/30" problem this replaces.

    ``bull_pct = round(50 + score / 2)`` and ``bear_pct = 100 - bull_pct``, so
    the two always sum to exactly 100 and neither can leave 0..100.

    ``pct`` is the headline number: the percentage of the side that is actually
    named, so the chip and the bar beneath it can never tell the reader different
    stories. A side holding more than half the split is named at the top even
    when the engine's bias withheld a verdict - the reader is shown the same
    lean the bar already draws. The output is BULLISH, BEARISH, or NEUTRAL:
    on an exact 50/50 split with no named side, the display resolves to BULLISH
    as the directional default. NEUTRAL is emitted only when the engine
    explicitly returns NO_BIAS, in which case the split is 50/50 and pct=50.

    If ``confidence`` is provided (0.0..1.0), the split is shrunk toward 50/50
    proportionally to confidence. At confidence=0 the split is 50/50; at
    confidence>=TIEBREAK_MAX_CONFIDENCE (0.30) the full score is shown.
    Intermediate values linearly interpolate.

    If ``tie_break`` is True the engine took this label from its tie-break rule
    rather than from the score, so a split that landed on exactly 50/50 is not a
    verdict at all and renders as NEUTRAL rather than being forced to a 51/49
    lean the evidence does not support.

    Args:
        score: the engine's signed score, nominally -100..+100.
        bias: Bias enum or string; anything unrecognised is treated as a missing
            verdict and resolved to the directional default (BULLISH). NO_BIAS
            produces label "NEUTRAL" with a 50/50 split.
        confidence: optional confidence 0.0..1.0; when provided, shrinks the
            split toward 50/50 proportionally. None means no shrinking.
        tie_break: optional; True when the label came from the tie-break rule
            (|raw_score| < EPS) rather than from the score's own sign.
        bull: optional; the aggregate bullish weight behind the verdict. When
            it and ``bear`` are both usable, the split is their ratio and the
            score projection is not used at all.
        bear: optional; the aggregate bearish weight behind the verdict.

    Returns:
        ``{"label", "pct", "bull_pct", "bear_pct"}``. ``pct`` is None only on
        the ratio path, when the engine's verdict contradicts the day's
        evidence split - the badge then names the side without a percentage it
        cannot honestly print.
    """
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        numeric = 0.0
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        numeric = 0.0  # NaN / inf must not propagate into the UI

    name = _bias_name(bias)

    # --- THE RATIO SPLIT ---------------------------------------------------
    # When the two aggregate totals are known, the split IS their ratio. This
    # is the path every live card takes, because the engine captures bull/bear
    # on every reading.
    #
    # The badge keeps the ENGINE's verdict (a fresh Tier-1 release flips it on
    # its own, and that call must stay visible), while the split is the
    # evidence. When the two agree - the normal case - the badge also carries
    # the percentage, exactly as before.
    #
    # When they DISAGREE, the badge shows the verdict and WITHHOLDS the
    # percentage rather than inventing one. The old code answered this case
    # by replacing the whole split with a synthetic one-point lean
    # (LEAN_BULL_PCT), which is how a card ended up publishing "Bullish 49% /
    # Bearish 51%" against evidence that was really 70/30. The note on the
    # card explains the disagreement ("day aggregate still +39.8").
    #
    # NO_BIAS is left alone: an explicit withhold is an honest answer, and it
    # still reads NEUTRAL 50/50.
    ratio = split_from_totals(bull, bear)
    if ratio is not None and name != "NEUTRAL":
        bull_pct, bear_pct = ratio
        owns_split = (
            (name == BULLISH and bull_pct >= bear_pct)
            or (name == BEARISH and bear_pct >= bull_pct)
        )
        if owns_split:
            pct = bull_pct if name == BULLISH else bear_pct
            return {
                "label": name,
                "pct": pct,
                "bull_pct": bull_pct,
                "bear_pct": bear_pct,
            }
        return {
            "label": name,
            "pct": None,
            "bull_pct": bull_pct,
            "bear_pct": bear_pct,
        }

    # A score outside the nominal range would push the split out of 0..100 and
    # break the 100-total invariant, so it is clamped before the arithmetic.
    bull_pct = _clamp_pct(int(round(50 + numeric / 2.0)))
    bear_pct = 100 - bull_pct

    # Confidence-aware shrinkage: if confidence is provided, shrink the split
    # toward 50/50 proportionally to confidence. At confidence=0 the split is 50/50; at
    # confidence>=TIEBREAK_MAX_CONFIDENCE (0.30) the full score is shown.
    # Intermediate values linearly interpolate.
    if confidence is not None:
        try:
            c = float(confidence)
            if c != c or c in (float("inf"), float("-inf")):
                c = 0.0
        except (TypeError, ValueError):
            c = 0.0
        # Clamp to 0..1
        c = max(0.0, min(1.0, c))
        # Shrink factor: at c=0.30 -> 1.0 (full), at c=0 -> 0.0 (no lean)
        shrink = min(1.0, c / 0.30)
        # Shrink toward 50/50: bull_pct moves from its value toward 50
        # New bull_pct = 50 + (bull_pct - 50) * shrink
        bull_pct = int(round(50 + (bull_pct - 50) * shrink))
        bear_pct = 100 - bull_pct

    # STEP 8: a tie-break label with an even split is not a verdict.
    #
    # The engine takes a label from its tie-break rule when |raw| < EPS even
    # though the weighted totals balance out. Left alone the lean-fixup below
    # would turn that into a 51/49 named side, publishing a conviction the
    # evidence does not support. Where the split is genuinely even it now reads
    # NEUTRAL 50/50, and the card's flag carries the explanation.
    if tie_break and name in (BULLISH, BEARISH) and bull_pct == 50:
        return {
            "label": "NEUTRAL",
            "pct": 50,
            "bull_pct": 50,
            "bear_pct": 50,
        }

    # The split must never contradict a named side. A tie-break verdict can
    # carry a raw score leaning the other way, and a bar that disagrees with
    # the badge tells the reader two stories. When that happens the split is
    # replaced by the one small lean toward the label (LEAN_BULL_PCT), so a
    # weak call still reads as weak instead of flipping into a strong one.
    if name == BULLISH and bull_pct < 50:
        bull_pct = LEAN_BULL_PCT
        bear_pct = 100 - bull_pct
    elif name == BEARISH and bear_pct < 50:
        bull_pct = 100 - LEAN_BULL_PCT
        bear_pct = 100 - bull_pct

    if name == BULLISH:
        # An explicit call is never downgraded, even at an even split.
        label, pct = BULLISH, bull_pct
    elif name == BEARISH:
        label, pct = BEARISH, bear_pct
    elif name == "NEUTRAL":
        # Engine withheld a verdict: show NEUTRAL with even 50/50 split.
        label, pct = "NEUTRAL", 50
        bull_pct = bear_pct = 50
    else:
        # No named side: follow the split, and on an exact 50/50 (or any other
        # tie) resolve to the directional default BULLISH. The label is always
        # one of the two directions - NEUTRAL is never emitted.
        label = _leading_side(bull_pct)
        pct = bull_pct if label == BULLISH else bear_pct

    return {
        "label": label,
        "pct": pct,
        "bull_pct": bull_pct,
        "bear_pct": bear_pct,
    }