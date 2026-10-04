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
confidence, it does not apply a threshold, and it cannot turn NO_BIAS into a
direction. Scoring, the NO_BIAS gate, the transmission map and the decision log
are all untouched; this is a pure projection applied at render time.

THE ONE RULE THAT MATTERS
-------------------------
``label`` is derived ONLY from the engine's ``bias`` - never from the sign of
the percentage. That is why the mapping is written as an explicit lookup on
``bias`` rather than as ``label = 'BULLISH' if pct > 50 else 'BEARISH'``.

The reason is a specific and very plausible bug: the engine deliberately
publishes NO_BIAS for weak evidence, and the score behind it is often a small
non-zero number. A display rule that keys off the sign would show "BULLISH 51%"
for an asset the engine explicitly declined to call. The display would then
contradict the engine and the reader would have no way to tell which was wrong.
Here, NO_BIAS renders as a flat grey NEUTRAL 50% no matter what the score says.
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


def _bias_name(bias) -> str:
    """Normalise Bias enum / str / None to the engine's own vocabulary."""
    if bias is None:
        return NO_BIAS
    value = getattr(bias, "value", bias)
    text = str(value).strip().upper()
    return text if text in _LABEL_FOR_BIAS else NO_BIAS


def _clamp_pct(value: int) -> int:
    return max(0, min(100, value))


def to_display(score: float, bias) -> dict:
    """
    Project an engine verdict onto the dashboard's bull/bear split.

    ``bull_pct = round(50 + score / 2)`` and ``bear_pct = 100 - bull_pct``, so
    the two always sum to exactly 100 and neither can leave 0..100.

    ``pct`` is the headline number: the side the engine named, or a flat 50 for
    NO_BIAS. Because ``bull_pct`` is derived from the score and ``pct`` is
    derived from ``bias``, the two can legitimately disagree in strength - and
    that is deliberate. A NO_BIAS block still shows its real bull/bear split in
    the thin bar, so the reader can see how close it came to calling a side,
    while the label stays honest.

    Args:
        score: the engine's signed score, nominally -100..+100.
        bias: Bias enum or string; anything unrecognised is treated as NO_BIAS,
            which is the safe direction to fail in.

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
    label = _LABEL_FOR_BIAS[name]

    # A score outside the nominal range would push the split out of 0..100 and
    # break the 100-total invariant, so it is clamped before the arithmetic.
    bull_pct = _clamp_pct(int(round(50 + numeric / 2.0)))
    bear_pct = 100 - bull_pct

    if name == BULLISH:
        pct = bull_pct
    elif name == BEARISH:
        pct = bear_pct
    else:
        # NO_BIAS always renders neutral, even at a positive or negative score.
        pct = 50

    return {
        "label": label,
        "pct": pct,
        "bull_pct": bull_pct,
        "bear_pct": bear_pct,
    }