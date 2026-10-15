"""
STEP 10 - confidence calibration.

The engine blends conviction, breadth, agreement and certainty into a single
0..1 number with fixed hand-picked coefficients. Nothing in that blend was ever
checked against what actually happened: ``tools/calibration_report.py`` compares
each confidence against the human WORKED / FAILED / Mixed label recorded for the
same snapshot, and reports the observed hit rate per confidence bucket.

A confidence of 0.30 should mean "right about 30% of the time", and 0.70 should
mean "right about 70% of the time". When it does not, this module remaps the
blend's output onto the observed rates through
``CONFIDENCE_CALIBRATION`` in ``app/config.py``.

WHY IT IS OFF BY DEFAULT
------------------------
``bias_outcome`` is empty: no calibration label has ever been recorded, so there
is nothing to fit and any mapping invented here would be exactly the unfounded
formula the audit warned against. The applier is therefore an identity until a
map is fitted and CONFIDENCE_CALIBRATION_ENABLED is set to True. The fitter
script prints the block to paste into config.py once enough labels exist.

The map is a piecewise-linear interpolation over breakpoints, so it is monotonic
by construction (a higher raw confidence never maps to a lower calibrated one)
and it cannot push a value outside 0..1.
"""
from __future__ import annotations

import app.config as _config
from app.logging_conf import get_logger

log = get_logger(__name__)

# Breakpoints are (raw_confidence, calibrated_confidence) pairs, ascending in
# raw. Anything below the first raw is clamped to the first calibrated value;
# anything above the last raw is clamped to the last.
_DEFAULT_MAP: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (1.0, 1.0),
)


def _flag(name: str, default):
    """
    Read a module-level constant from app.config.

    These are plain module constants, NOT fields on the pydantic ``settings``
    object - reading them with ``getattr(settings, ...)`` always returns the
    default, which silently made the calibration permanently unfixable. Reading
    them off the module also lets a test monkeypatch the module attribute.
    """
    return getattr(_config, name, default)


def _map() -> tuple[tuple[float, float], ...]:
    """The active breakpoint table, validated and always safe to interpolate."""
    raw = _flag("CONFIDENCE_CALIBRATION", None)
    if not raw:
        return _DEFAULT_MAP
    try:
        pts = sorted((float(a), float(b)) for a, b in raw)
    except (TypeError, ValueError):
        log.warning("CONFIDENCE_CALIBRATION is malformed; using identity")
        return _DEFAULT_MAP
    if len(pts) < 2:
        return _DEFAULT_MAP
    # Clamp the outputs into 0..1 - a hand-edited table must not be able to
    # push a confidence out of its domain.
    return tuple((a, max(0.0, min(1.0, b))) for a, b in pts)


def calibrate_confidence(confidence: float) -> float:
    """
    Remap one raw confidence onto the calibrated scale.

    Identity when no map is configured, so existing behaviour is unchanged
    until a fit exists. Never raises: a malformed value degrades to identity.
    """
    conf = confidence
    try:
        conf = float(confidence)
        if conf != conf:                     # NaN
            return confidence
    except (TypeError, ValueError):
        return confidence

    if not _flag("CONFIDENCE_CALIBRATION_ENABLED", False):
        return conf

    pts = _map()
    if conf <= pts[0][0]:
        return pts[0][1]
    if conf >= pts[-1][0]:
        return pts[-1][1]

    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= conf <= x1:
            if x1 == x0:
                return y0
            t = (conf - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return pts[-1][1]


def calibration_status() -> dict:
    """What the runtime is actually using, for /api/health and the report."""
    enabled = bool(_flag("CONFIDENCE_CALIBRATION_ENABLED", False))
    pts = _map()
    identity = pts == ((0.0, 0.0), (1.0, 1.0))
    return {
        "enabled": enabled,
        "fitted": not identity,
        "breakpoints": [list(p) for p in pts],
        "min_samples": _flag("CONFIDENCE_CALIBRATION_MIN_SAMPLES", 30),
    }
