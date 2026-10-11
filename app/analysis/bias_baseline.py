"""
PER-ASSET BASELINE NORMALIZATION (item 1).

Financial news is structurally negative-heavy, so raw cue scores carry a
persistent per-asset skew: the bias_snapshots log ran ~81% BEARISH. A raw score
is a LEVEL, not a change. Subtracting a rolling baseline of the asset's own
recent raw scores turns "how negative is today" into "how different is today from
the last few ticks" - which is what a direction actually is.

TWO HARD RULES
--------------
1. NO LOOK-AHEAD. The baseline for tick N is computed ONLY from ticks strictly
   before N. It is computed AFTER the verdict is persisted (see pipeline), so
   the current tick's own score can never influence its own baseline, and the
   persisted history is the only source - articles are purged at 48h, snapshots
   are durable, so the baseline survives a purge that would otherwise leave an
   asset with no reference point at all.

2. RAW SURVIVES. The unadjusted score stays on ``raw_score`` in the payload so
   a reader can see both the level and the change. Only the SIGN DECISION and
   the published score use the adjusted value.

BEFORE THE MINIMUM
------------------
With fewer than BASELINE_MIN_TICKS persisted ticks the baseline is not
trustworthy, so the raw score is published as-is and the baseline is reported as
None. This is a warm-up state, not a failure: it resolves on its own as history
accumulates.

WHY MEDIAN, NOT MEAN
--------------------
BTCUSD's raw scores cluster in a deep negative tail (-55..-90 for long
stretches). The MEAN is dragged far below them, so a clearly bearish score such
as -56.7 against a -65.0 mean computes to +8.3 and reads BULLISH - a genuine
inversion of the evidence. The median is robust to that tail and was verified
against the live history before this default was chosen (see
scripts/scratch/baseline_sim3.py). ``BASELINE_STATISTIC=mean`` is still
available for comparison.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)


def baseline_score(asset: str, now: datetime | None = None) -> float | None:
    """
    The baseline for one asset from its own persisted history.

    Returns None when there are not enough ticks yet (warm-up) or when the
    feature is switched off. The caller is responsible for deciding what to do
    with None - the contract is "no baseline", not "baseline is zero".
    """
    if not settings.baseline_enabled:
        return None

    from sqlalchemy import select

    from app.models.db_models import BiasSnapshot
    from app.database import SessionLocal

    window = settings.baseline_window_ticks
    minimum = settings.baseline_min_ticks

    with SessionLocal() as session:
        rows = session.scalars(
            select(BiasSnapshot.raw_score)
            .where(BiasSnapshot.asset == asset)
            .order_by(BiasSnapshot.timestamp_utc.desc())
            .limit(window)
        ).all()

    scores = [float(r) for r in rows if r is not None]
    if len(scores) < minimum:
        log.debug(
            "baseline %s: only %d persisted tick(s), need %d - publishing raw",
            asset, len(scores), minimum)
        return None

    return round(settings.baseline_statistic_fn(scores), 4)


def adjusted_score(raw_score: float, baseline: float | None) -> tuple[float, float | None]:
    """
    Apply the baseline to one raw score.

    Returns ``(adjusted, baseline)``. ``baseline`` is echoed so the caller can
    record it in the snapshot row without re-querying.

    A score inside the tie-break band (|raw| < BASELINE_MIN_SCORE) is published
    as-is: it is not a verdict, so normalizing it would only move noise around.
    """
    if baseline is None:
        return float(raw_score), None
    if abs(raw_score) < settings.baseline_min_score:
        return float(raw_score), baseline
    return round(float(raw_score) - baseline, 4), baseline