"""
AUTOMATIC OUTCOME CAPTURE - the measurement loop the project never had.

THE PROBLEM THIS SOLVES
-----------------------
``bias_outcome`` - the table the calibration fitter reads - had ZERO rows for
the entire life of the project, and it could never have had any. The only
writer was ``POST /api/calibration/labels``: a human had to open the
dashboard and click WORKED / FAILED / Mixed for every call, one call at a
time. The engine publishes a verdict every 180 seconds for three assets, so
that is roughly 1,400 clicks a day. Nobody clicked. Consequently:

  * ``tools/calibration_report.py`` had nothing to report.
  * ``calibrate_confidence()`` was an identity BY DESIGN, because there was
    no observed hit rate to map onto.
  * The 0.40 / 0.28 / 0.22 confidence coefficients were hand-picked and had
    never been checked against anything.
  * Every tuning decision - the flip margins, the decay table, the driver
    rankings, the baseline window - was an unfalsifiable opinion.

WHAT THIS DOES
--------------
The engine now scores ITSELF:

  1. ``record_new_calls`` - when the published bias CHANGES for an asset, a
     ``bias_outcome`` row is written with outcome ``PENDING``, carrying the
     bias, the score, the confidence and the moment of the call. Only a
     CHANGE is recorded: a verdict that is still standing is the same call,
     not a new one, and recording it every 180 seconds would inflate the
     sample with duplicates of one signal.

  2. ``record_price_point`` - every tick the engine writes what the price
     actually was, into ``price_point``. This is the ground truth.

  3. ``resolve_pending`` - once the horizon has elapsed and the price
     history covers both ends of the window, the PENDING row is resolved to
     WORKED / FAILED / Mixed by comparing the price at the call with the
     price a horizon later.

The resolved rows are exactly the shape ``tools/calibration_report.py``
already expects, so that tool starts producing a real table with no change
to it.

THE HONEST LIMITS
-----------------
* A price feed only exists for BITCOIN (Coinbase). GOLD and SILVER have no
  reachable spot feed, so their calls stay PENDING until the operator
  supplies a price series. The engine never invents a price.
* A call that is never resolved stays PENDING forever. The calibration
  report already ignores unknown outcome labels, so PENDING rows cannot
  pollute a fit.
* Resolution is deliberately conservative: the entry price must be within
  ``outcome_max_lookback_minutes`` of the call, and the exit price within
  the same distance of the target, so a call is never scored against a price
  from a different part of the session.

NOTHING HERE FEEDS BACK INTO SCORING
------------------------------------
This module only writes. The engine never reads ``bias_outcome`` or
``price_point`` when it decides a bias, so enabling the loop cannot change
what the engine publishes - it can only tell you, afterwards, whether the
engine was right. That is the whole point: measurement first, tuning after.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import Asset
from app.utils import timeutils as T

log = get_logger(__name__)

#: A recorded call whose outcome has not been decided yet. The calibration
#: fitter maps only "worked" / "failed" / "mixed", so a PENDING row is
#: invisible to it - it is evidence of nothing either way.
PENDING = "PENDING"

#: The call was right: the asset moved in the predicted direction by more
#: than the dead-band.
WORKED = "WORKED"

#: The call was wrong: the asset moved against it by more than the dead-band.
FAILED = "FAILED"

#: The asset moved less than the dead-band, so the call was neither
#: confirmed nor contradicted. Counted as half a hit by the fitter.
MIXED = "Mixed"


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def record_price_point(session, asset: Asset, price: float | None,
                       source: str = "", ts: datetime | None = None) -> bool:
    """
    Append one observed price. Returns True when a row was written.

    A missing price (no feed for the asset, or the fetch failed) is not an
    error - it simply writes nothing. The engine never invents a price.
    """
    if price is None:
        return False
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        from app.models.db_models import PricePoint

        session.add(PricePoint(
            asset=asset.value,
            ts_utc=ts or T.now_utc(),
            price=value,
            source=source or "",
        ))
        # The session is configured with autoflush=False, and the resolver
        # runs in a DIFFERENT session - so the row has to be flushed here or
        # the resolution would not see it.
        session.flush()
        return True
    except Exception as exc:  # noqa: BLE001 - measurement never breaks a tick
        log.debug("price point write skipped for %s: %s", asset.value, exc)
        return False


def _latest_snapshot_id(session, asset: str) -> int | None:
    """The id of the most recent bias_snapshots row for the asset."""
    try:
        from app.models.db_models import BiasSnapshot

        row = session.scalars(
            select(BiasSnapshot)
            .where(BiasSnapshot.asset == asset)
            .order_by(BiasSnapshot.timestamp_utc.desc())
            .limit(1)
        ).first()
        return row.id if row is not None else None
    except Exception as exc:  # noqa: BLE001
        log.debug("latest snapshot lookup failed for %s: %s", asset, exc)
        return None


def _latest_recorded_bias(session, asset: str) -> str | None:
    """The bias of the most recent recorded call for the asset."""
    try:
        from app.models.db_models import BiasOutcome

        row = session.scalars(
            select(BiasOutcome)
            .where(BiasOutcome.asset == asset)
            .order_by(BiasOutcome.created_at.desc(), BiasOutcome.id.desc())
            .limit(1)
        ).first()
        return (row.bias or "") if row is not None else None
    except Exception as exc:  # noqa: BLE001
        log.debug("latest outcome lookup failed for %s: %s", asset, exc)
        return None


def record_new_calls(session, verdicts) -> int:
    """
    Record a PENDING outcome row for every asset whose published bias CHANGED.

    ``verdicts`` is the same list the bias_snapshots writer receives, so the
    recorded confidence is the confidence that was published with the call.

    Returns the number of rows written. Never raises: this is measurement,
    and measurement must not be able to break a tick.
    """
    if not settings.outcome_capture_enabled or not verdicts:
        return 0
    written = 0
    try:
        from app.models.db_models import BiasOutcome

        now = T.now_utc()
        for v in verdicts:
            bias = str(getattr(v, "bias", "") or "")
            if bias not in ("BULLISH", "BEARISH"):
                continue
            asset = getattr(v, "asset", None)
            asset_key = getattr(asset, "value", asset)
            if not asset_key:
                continue
            # A standing verdict is the SAME call, not a new one. Recording it
            # every 180 seconds would turn one signal into hundreds of
            # correlated "observations" and make the hit rate look far more
            # precise than it is.
            if _latest_recorded_bias(session, asset_key) == bias:
                continue
            snap_id = _latest_snapshot_id(session, asset_key)
            if snap_id is None:
                # No bias_snapshots row to attach to - the foreign key needs
                # one. Nothing is written rather than writing a broken row.
                continue
            session.add(BiasOutcome(
                bias_snapshot_id=snap_id,
                asset=asset_key,
                date_utc=now,
                bias=bias,
                score=float(getattr(v, "score", 0.0) or 0.0),
                confidence=float(getattr(v, "confidence", 0.0) or 0.0),
                outcome=PENDING,
                note="",
            ))
            written += 1
        if written:
            session.flush()
            log.info("outcome capture: %d new call(s) recorded", written)
        return written
    except Exception as exc:  # noqa: BLE001 - measurement never breaks a tick
        log.warning("outcome capture skipped: %s", exc)
        return 0


def _price_around(session, asset: str, when: datetime,
                  at_or_before: bool, tolerance_min: float):
    """The nearest price point to ``when``, or None when there is not one."""
    try:
        from app.models.db_models import PricePoint

        q = select(PricePoint).where(PricePoint.asset == asset)
        if at_or_before:
            q = q.where(PricePoint.ts_utc <= when).order_by(
                PricePoint.ts_utc.desc())
        else:
            q = q.where(PricePoint.ts_utc >= when).order_by(
                PricePoint.ts_utc.asc())
        point = session.scalars(q.limit(1)).first()
        if point is None:
            return None
        stamp = _aware(point.ts_utc)
        if stamp is None:
            return None
        drift = abs((stamp - when).total_seconds()) / 60.0
        if drift > tolerance_min:
            return None
        return point
    except Exception as exc:  # noqa: BLE001
        log.debug("price lookup failed for %s: %s", asset, exc)
        return None


def classify_move(move_pct: float, expected_up: bool,
                  dead_band_pct: float) -> str:
    """
    Turn a realised move into an outcome label.

    A move inside the dead band is MIXED: the asset did not go anywhere
    either way, so the call was neither confirmed nor contradicted. The
    calibration fitter counts Mixed as half a hit, which is the honest
    reading.
    """
    if move_pct > dead_band_pct:
        return WORKED if expected_up else FAILED
    if move_pct < -dead_band_pct:
        return WORKED if not expected_up else FAILED
    return MIXED


def resolve_pending(session, now: datetime | None = None) -> int:
    """
    Resolve every PENDING call whose horizon has elapsed.

    A call is resolved only when a price exists at BOTH ends of the window -
    the price at (or just before) the call, and the price at (or just after)
    the call plus the horizon. Anything else stays PENDING.

    Returns the number of rows resolved. Never raises.
    """
    if not settings.outcome_capture_enabled:
        return 0
    resolved = 0
    try:
        from app.models.db_models import BiasOutcome

        moment = _aware(now) or T.now_utc()
        horizon = max(1, int(settings.outcome_horizon_minutes))
        tolerance = float(settings.outcome_max_lookback_minutes)
        dead_band = float(settings.outcome_min_move_pct)
        cutoff = moment - timedelta(minutes=horizon)

        rows = session.scalars(
            select(BiasOutcome)
            .where(BiasOutcome.outcome == PENDING)
            .where(BiasOutcome.date_utc <= cutoff)
            .order_by(BiasOutcome.date_utc.asc())
            .limit(500)
        ).all()

        for row in rows:
            decided_at = _aware(row.date_utc)
            if decided_at is None:
                continue
            entry = _price_around(session, row.asset, decided_at,
                                  at_or_before=True, tolerance_min=tolerance)
            target = decided_at + timedelta(minutes=horizon)
            exit_point = _price_around(session, row.asset, target,
                                        at_or_before=False,
                                        tolerance_min=tolerance)
            if entry is None or exit_point is None:
                # Not enough price history yet. The call stays PENDING and is
                # picked up on a later tick.
                continue
            entry_px = float(entry.price)
            exit_px = float(exit_point.price)
            if entry_px <= 0:
                continue
            move = (exit_px - entry_px) / entry_px * 100.0
            outcome = classify_move(move, row.bias == "BULLISH", dead_band)
            row.outcome = outcome
            row.note = (f"{move:+.3f}% over {horizon}m "
                        f"(entry {entry_px:.4f} @ {_aware(entry.ts_utc)}, "
                        f"exit {exit_px:.4f} @ {_aware(exit_point.ts_utc)})")
            resolved += 1

        if resolved:
            session.flush()
            log.info("outcome capture: %d call(s) resolved", resolved)
        return resolved
    except Exception as exc:  # noqa: BLE001 - measurement never breaks a tick
        log.warning("outcome resolution skipped: %s", exc)
        return 0


def trim_price_points(session, now: datetime | None = None) -> int:
    """
    Drop price points older than the retention window.

    The series only has to cover the outcome horizon plus a little slack, so
    a couple of days is plenty. Returns the number of rows deleted.
    """
    try:
        from app.models.db_models import PricePoint

        hours = max(1, int(settings.outcome_price_retention_hours))
        moment = _aware(now) or T.now_utc()
        cutoff = moment - timedelta(hours=hours)
        result = session.execute(
            delete(PricePoint).where(PricePoint.ts_utc < cutoff))
        return result.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        log.debug("price point trim skipped: %s", exc)
        return 0


def outcome_status(session) -> dict:
    """
    A short summary for /api/health: how many calls are recorded, and how
    many of them have been decided.
    """
    out = {"recorded": 0, "pending": 0, "resolved": 0}
    try:
        from sqlalchemy import func

        from app.models.db_models import BiasOutcome

        out["recorded"] = int(session.scalar(
            select(func.count()).select_from(BiasOutcome)) or 0)
        out["pending"] = int(session.scalar(
            select(func.count()).select_from(BiasOutcome)
            .where(BiasOutcome.outcome == PENDING)) or 0)
        out["resolved"] = out["recorded"] - out["pending"]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out
