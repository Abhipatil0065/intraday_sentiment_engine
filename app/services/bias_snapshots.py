"""
BIAS SNAPSHOT WRITER (phase 2).

Appends one ``bias_snapshots`` row per asset per poll, recording what the engine
believed at that moment and which headlines drove it, so the log can be reviewed
by hand afterwards.

NEWS-ONLY. Nothing in this module reads, fetches, stores or derives a price. The
engine's score comes from news alone; this log exists to make that decision
auditable, and price comparison is done manually against it.

TWO HARD RULES
--------------
1. It never raises. Every entry point is wrapped, so a failure here cannot break
   a poll: this is a diagnostic log, not part of the product.
2. It is cheap. Pure in-memory formatting plus one INSERT batch - no network
   call of any kind, so it cannot slow a poll down.

It takes already-computed verdicts. It never calls ``compute()`` and cannot
influence a bias.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.analysis.display import to_display
from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import Asset
from app.utils import timeutils as T

log = get_logger(__name__)

# How many drivers are stored per side.
DRIVER_LIMIT = 3


@dataclass
class AssetVerdict:
    """Minimal view of a ``SentimentResult`` needed to write one row."""

    asset: Asset
    score: float
    confidence: float
    bias: str
    article_count: int = 0
    last_signal_age_min: float | None = None
    bull_drivers: list = field(default_factory=list)
    bear_drivers: list = field(default_factory=list)
    # Item 1: baseline normalization. ``raw_score`` is the unadjusted level the
    # engine actually computed; it is carried through so the snapshot row can
    # show both the level and the baseline-adjusted published score.
    raw_score: float | None = None
    baseline_score: float | None = None
    adjusted: bool = False


def _drivers(rows: Sequence) -> list[dict]:
    """Top-N drivers as flat dicts, each carrying the weight it contributed."""
    return [
        {"title": d.title, "direction": d.direction,
         "contribution": round(float(d.contribution), 4),
         "weight": round(float(d.weight), 4)}
        for d in list(rows)[:DRIVER_LIMIT]
    ]


def ist_stamp(utc_dt: datetime) -> str:
    """12-hour IST stamp, e.g. ``02:35:07 PM IST``, for reading by hand."""
    return utc_dt.astimezone(T.display_tz()).strftime("%I:%M:%S %p IST")


def build_rows(verdicts: Sequence[AssetVerdict],
               now: datetime | None = None) -> list[dict]:
    """
    Turn verdicts into plain dicts, capturing the DISPLAY split verbatim.

    ``bullish_pct`` / ``bearish_pct`` come from ``display.to_display`` - the same
    projection the dashboard renders - so a manual reader compares against
    exactly what was shown, never a re-derived number.

    Item 1: the published ``score`` is baseline-adjusted when a baseline is
    available; ``raw_score`` / ``baseline_score`` / ``adjusted`` are carried
    through verbatim so the audit trail is complete. Contains no price field,
    and reads nothing outside its arguments.

    A verdict that carries no explicit ``raw_score`` falls back to its
    ``score`` for that column, so the row never loses the level it was
    published from.
    """
    stamp = now or T.now_utc()
    rows: list[dict] = []
    for v in verdicts:
        shown = to_display(v.score, v.bias, v.confidence)
        level = v.raw_score if v.raw_score is not None else v.score
        rows.append({
            "timestamp_utc": stamp,
            "timestamp_ist": ist_stamp(stamp),
            "asset": v.asset.value,
            "bias": v.bias,
            "score": float(v.score),
            "confidence": float(v.confidence),
            "raw_score": (None if level is None else float(level)),
            "baseline_score": (None if v.baseline_score is None
                               else float(v.baseline_score)),
            "adjusted": bool(v.adjusted),
            "bullish_pct": float(shown["bull_pct"]),
            "bearish_pct": float(shown["bear_pct"]),
            "article_count": int(v.article_count),
            "last_signal_age_min": (None if v.last_signal_age_min is None
                                    else float(v.last_signal_age_min)),
            "bull_drivers": _drivers(v.bull_drivers),
            "bear_drivers": _drivers(v.bear_drivers),
        })
    return rows


def write_bias_snapshots(session, verdicts: Sequence[AssetVerdict]) -> int:
    """
    Append the rows. Returns the number written, or 0 if logging is off.

    Wrapped end-to-end in try/except: a failure is logged and swallowed so the
    poll continues untouched.

    Item 1: the verdict's score is ALREADY baseline-adjusted - the pipeline
    made the decision on the centred lean and published that number. This
    writer therefore records what the reader was shown, verbatim, and does
    NOT adjust it a second time. Re-adjusting here would subtract the
    baseline twice and leave the audit trail disagreeing with the card.

    The unadjusted level travels on ``raw_score`` and the baseline that was
    subtracted on ``baseline_score``, so both numbers are reconstructable
    from the row.
    """
    if not settings.bias_snapshot_enabled or not verdicts:
        return 0
    try:
        from app.models.db_models import BiasSnapshot

        rows = build_rows(list(verdicts))
        for row in rows:
            session.add(BiasSnapshot(**row))
        session.flush()
        log.debug("bias_snapshots: wrote %d row(s)", len(rows))
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - never break the poll
        log.warning("bias snapshot write skipped: %s", exc)
        return 0