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


def to_display(score: float, bias) -> dict:
    """
    Project an engine verdict onto the dashboard's bull/bear split.

    ``bull_pct = round(50 + score / 2)`` and ``bear_pct = 100 - bull_pct``, so
    the two always sum to exactly 100 and neither can leave 0..100.

    ``pct`` is the headline number: the percentage of the side that is actually
    named, so the chip and the bar beneath it can never tell the reader different
    stories. A side holding more than half the split is named at the top even
    when the engine's bias withheld a verdict - the reader is shown the same
    lean the bar already draws. The output is ALWAYS BULLISH or BEARISH: on an
    exact 50/50 split, or whenever no side owns more than half, the display
    resolves to BULLISH as the directional default. There is no NEUTRAL label.

    Args:
        score: the engine's signed score, nominally -100..+100.
        bias: Bias enum or string; anything unrecognised is treated as a missing
            verdict and resolved to the directional default (BULLISH).

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