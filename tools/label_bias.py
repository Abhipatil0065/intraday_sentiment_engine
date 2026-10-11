#!/usr/bin/env python
"""
CALIBRATION LABELS (item 6 - DATA COLLECTION ONLY, no auto-tuning).

Records a manual outcome label against a bias snapshot so the thresholds in
config.py can later be CALIBRATED against real results.

    WORKED   - the published bias was directionally right afterwards
    FAILED   - the published bias was directionally wrong afterwards
    Mixed    - the move went both ways / was inconclusive

This tool writes to ``bias_outcome`` and NOTHING ELSE. It never changes a
verdict, never re-scores, never feeds a threshold at decision time. It is the
manual equivalent of the hit-rate report's price join, except the outcome is
recorded by a human rather than derived from a price CSV.

Usage
-----
    # Label the most recent snapshot for one asset:
    python tools/label_bias.py --asset XAUUSD --outcome WORKED

    # Label a specific snapshot id:
    python tools/label_bias.py --asset XAGUSD --snapshot-id 4821 \
        --outcome FAILED --note "rallied then reversed"

    # Label by date (picks the latest snapshot on that date for the asset):
    python tools/label_bias.py --asset BTCUSD --date 2026-10-09 \
        --outcome Mixed

    # List recent labels:
    python tools/label_bias.py --list --asset XAUUSD --limit 20
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select, text  # noqa: E402

from app.database import init_db, session_scope  # noqa: E402
from app.models.db_models import BiasOutcome, BiasSnapshot  # noqa: E402

VALID_OUTCOMES = ("WORKED", "FAILED", "Mixed")


def normalize_outcome(value: str) -> str:
    """Case-insensitive outcome match; Mixed is a valid label as-is."""
    raw = (value or "").strip()
    for valid in VALID_OUTCOMES:
        if raw.lower() == valid.lower():
            return valid
    return ""


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _resolve_snapshot(session, asset: str, date: str | None) -> int:
    """Resolve a bias_snapshot id from asset (+ optional date), else None."""
    from app.models.db_models import BiasSnapshot
    from sqlalchemy import select

    q = select(BiasSnapshot).where(BiasSnapshot.asset == asset.upper())
    if date:
        day = _parse_date(date)
        end = day.replace(hour=23, minute=59, second=59)
        q = q.where(BiasSnapshot.timestamp_utc >= day,
                    BiasSnapshot.timestamp_utc <= end)
    q = q.order_by(BiasSnapshot.timestamp_utc.desc()).limit(1)
    snap = session.scalars(q).first()
    return snap.id if snap else None


def cmd_label(args: argparse.Namespace, session=None) -> int:
    """Record one label. ``session`` overrides the default scope (used by tests)."""
    outcome = normalize_outcome(args.outcome)
    if not outcome:
        print(f"ERROR: outcome must be one of {VALID_OUTCOMES}, got {args.outcome!r}",
              file=sys.stderr)
        return 2

    owns_session = session is None
    if owns_session:
        init_db()
        session = session_scope().__enter__()
    try:
        snap_id = args.snapshot_id
        if snap_id is None:
            snap_id = _resolve_snapshot(session, args.asset, args.date)
            if snap_id is None:
                detail = f"on {args.date}" if args.date else ""
                print(f"ERROR: no bias_snapshot for {args.asset} {detail}".strip(),
                      file=sys.stderr)
                return 1
            print(f"Resolved snapshot id={snap_id}")

        row = BiasOutcome(
            bias_snapshot_id=snap_id,
            asset=args.asset.upper(),
            date_utc=datetime.now(timezone.utc),
            bias="",
            score=0.0,
            confidence=0.0,
            outcome=outcome,
            note=(args.note or "").strip(),
        )
        session.add(row)
        if owns_session:
            session.commit()
    finally:
        if owns_session:
            session.close()
    print(f"Recorded {outcome} label for {args.asset.upper()} "
          f"(snapshot {snap_id}).")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    init_db()
    with session_scope() as session:
        q = select(BiasOutcome).order_by(BiasOutcome.created_at.desc())
        if args.asset:
            q = q.where(BiasOutcome.asset == args.asset.upper())
        q = q.limit(args.limit)
        rows = session.scalars(q).all()
    if not rows:
        print("No labels recorded yet.")
        return 0
    print(f"{'id':>5} {'asset':<8} {'outcome':<8} {'snapshot':>8} {'created_at':<20} note")
    for r in rows:
        print(f"{r.id:>5} {r.asset:<8} {r.outcome:<8} "
              f"{str(r.bias_snapshot_id):>8} "
              f"{r.created_at.strftime('%Y-%m-%d %H:%M'):<20} {r.note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record manual calibration labels for bias outcomes "
                    "(data collection only, no auto-tuning).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_label = sub.add_parser("label", help="record a label")
    p_label.add_argument("--asset", required=True, help="XAUUSD / XAGUSD / BTCUSD / EQUITY")
    p_label.add_argument("--outcome", required=True,
                         help=f"one of {VALID_OUTCOMES}")
    p_label.add_argument("--snapshot-id", type=int, default=None)
    p_label.add_argument("--date", default=None, help="YYYY-MM-DD")
    p_label.add_argument("--note", default=None)
    p_label.set_defaults(func=cmd_label)

    p_list = sub.add_parser("list", help="show recent labels")
    p_list.add_argument("--asset", default=None)
    p_list.add_argument("--limit", type=int, default=25)
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())