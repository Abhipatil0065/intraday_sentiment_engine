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
The top label names the side that owns MORE THAN HALF the split, and NEUTRAL is
reserved for a genuinely even 50/50. That is a deliberate change from the earlier
rule, which derived the label purely from the engine's ``bias`` and so printed a
grey NEUTRAL 50% over a visibly lopsided bar - a withheld call the reader could
see the evidence for and could not act on. The split is already the engine's own
number (``bull_pct = 50 + score/2``), so following it does not invent a signal: it
stops the display from contradicting the bar printed directly beneath it.

Two guards survive that relaxation, because a bare sign still is not a verdict:

* A side the engine NAMED is never downgraded. BULLISH/BEARISH keep their label
  even at an even split, so the display can never contradict an explicit call.
* A missing or unrecognised ``bias`` fails safe to NEUTRAL instead of being
  inferred from the raw number, so a malformed payload is never guessed at.
"""

from __future__ import annotations

BULLISH = "BULLISH"
BEARISH = "BEARISH"
NO_BIAS = "NO_BIAS"

# The engine's own vocabulary is accepted, plus the enum's .value form, so
# callers may pass either Bias.BULLISH or the plain string.
_LABEL_FOR_BIAS = {
    BULLISH: "BULLISH",
    BEARISH: "BEARISH",
    NO_BIAS: "NEUTRAL",
}


def _bias_name(bias) -> str | None:
    """
    Normalise Bias enum / str to the engine's own vocabulary.

    Returns ``None`` for anything unrecognised, which is deliberately NOT the
    same answer as a genuine ``NO_BIAS``. A real NO_BIAS is a decision the
    engine made and is reported honestly; a missing or malformed value is an
    absent decision, and resolving it from the score would be guessing. The
    caller renders ``None`` as NEUTRAL and leaves it there.
    """
    if bias is None:
        return None
    value = getattr(bias, "value", bias)
    text = str(value).strip().upper()
    return text if text in _LABEL_FOR_BIAS else None


def _clamp_pct(value: int) -> int:
    return max(0, min(100, value))


def _leading_side(bull_pct: int) -> str:
    """
    The side holding more than half the split, or NO_BIAS on an exact 50/50.

    ``bear_pct`` is always ``100 - bull_pct``, so a bull figure above 50 makes a
    bear figure above 50 impossible and vice versa - the two cases cannot both
    fire, and an even split falls through to NO_BIAS.
    """
    if bull_pct > 50:
        return BULLISH
    if 100 - bull_pct > 50:
        return BEARISH
    return NO_BIAS


def to_display(score: float, bias) -> dict:
    """
    Project an engine verdict onto the dashboard's bull/bear split.

    ``bull_pct = round(50 + score / 2)`` and ``bear_pct = 100 - bull_pct``, so
    the two always sum to exactly 100 and neither can leave 0..100.

    ``pct`` is the headline number: the percentage of the side that is actually
    named, so the chip and the bar beneath it can never tell the reader different
    stories. A side holding more than half the split is named at the top even
    when the engine's bias withheld a verdict - the reader is shown the same
    lean the bar already draws. NEUTRAL survives only where there is genuinely
    no side to name, i.e. an exact 50/50 split (or no usable bias at all).

    Args:
        score: the engine's signed score, nominally -100..+100.
        bias: Bias enum or string; anything unrecognised is treated as a missing
            verdict and renders NEUTRAL, which is the safe direction to fail in.

    Returns:
        ``{"label", "pct", "bull_pct", "bear_pct"}``.
    """
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        numeric = 0.0
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        numeric = 0.0  # NaN / inf must not propagate into the UI

    name = _bias_name(bias)

    # A score outside the nominal range would push the split out of 0..100 and
    # break the 100-total invariant, so it is clamped before the arithmetic.
    bull_pct = _clamp_pct(int(round(50 + numeric / 2.0)))
    bear_pct = 100 - bull_pct

    if name == BULLISH:
        # An explicit call is never downgraded, even at an even split.
        label, pct = BULLISH, bull_pct
    elif name == BEARISH:
        label, pct = BEARISH, bear_pct
    elif name == NO_BIAS:
        # A withheld call still has to be readable: whichever side holds more
        # than half the split is what the bar shows, so that is what the chip
        # names. Only an exact 50/50 has no side to name and stays NEUTRAL.
        lead = _leading_side(bull_pct)
        if lead == BULLISH:
            label, pct = BULLISH, bull_pct
        elif lead == BEARISH:
            label, pct = BEARISH, bear_pct
        else:
            label, pct = _LABEL_FOR_BIAS[NO_BIAS], 50
    else:
        # No usable bias at all: fail safe to NEUTRAL rather than inventing one.
        label, pct = _LABEL_FOR_BIAS[NO_BIAS], 50

    return {
        "label": label,
        "pct": pct,
        "bull_pct": bull_pct,
        "bear_pct": bear_pct,
    }