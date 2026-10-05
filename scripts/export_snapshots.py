"""
SNAPSHOT EXPORT (phase 2).

Exports the ``bias_snapshots`` log to CSV for manual review.

    python scripts/export_snapshots.py
    python scripts/export_snapshots.py --asset BTCUSD
    python scripts/export_snapshots.py --from 2026-10-05 --to 2026-10-06
    python scripts/export_snapshots.py --out review.csv

COLUMNS
-------
timestamp_utc, timestamp_ist, asset, bias, bias_changed, score, confidence,
bullish_pct, bearish_pct, article_count, last_signal_age_min, bull_drivers,
bear_drivers, manual_price_note

``bias_changed`` is TRUE on the row where the bias flipped away from the
previous row for that same asset - i.e. exactly the moments worth looking up in
a chart by hand. The very first row of an asset is not a change.

``manual_price_note`` is included EMPTY for you to fill in. The engine writes
nothing to it and never reads it back; it is a scratch column for your notes.

NEWS-ONLY: this reads the engine's own log. It fetches no price and contacts no
external service.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import engine  # noqa: E402
from app.logging_conf import get_logger  # noqa: E402
from app.models.db_models import BiasSnapshot  # noqa: E402

log = get_logger(__name__)

FIELDNAMES = [
    "timestamp_utc", "timestamp_ist", "asset", "bias", "bias_changed",
    "score", "confidence", "bullish_pct", "bearish_pct", "article_count",
    "last_signal_age_min", "bull_drivers", "bear_drivers",
    "manual_price_note",
]

ALL_ASSETS = ["XAUUSD", "XAGUSD", "BTCUSD", "USOIL"]


def parse_bound(raw: str, *, end_of_day: bool) -> datetime | None:
    """``YYYY-MM-DD`` -> a UTC instant. ``--to`` covers the whole day."""
    if not raw:
        return None
    try:
        d = date.fromisoformat(raw.strip())
    except ValueError:
        return None
    return datetime.combine(
        d, time.max if end_of_day else time.min, tzinfo=timezone.utc)


def load(asset: str | None, start: datetime | None, end: datetime | None):
    """
    Rows in range, oldest first, as plain dicts. Filters applied in SQL.

    Columns are selected explicitly rather than selecting the ORM entity.
    This project runs SQLAlchemy 2.0 in legacy row mode, where a result row
    holds one value per selected column; selecting the entity would yield Row
    tuples of raw column values instead of ``BiasSnapshot`` instances, and the
    attribute access below would fail. Selecting named columns is unambiguous
    and keeps the CSV writer independent of ORM identity-map state.
    """
    cols = (BiasSnapshot.timestamp_utc, BiasSnapshot.timestamp_ist,
            BiasSnapshot.asset, BiasSnapshot.bias, BiasSnapshot.score,
            BiasSnapshot.confidence, BiasSnapshot.bullish_pct,
            BiasSnapshot.bearish_pct, BiasSnapshot.article_count,
            BiasSnapshot.last_signal_age_min, BiasSnapshot.bull_drivers,
            BiasSnapshot.bear_drivers)
    query = select(*cols).order_by(
        BiasSnapshot.timestamp_utc, BiasSnapshot.id)
    if asset:
        query = query.where(BiasSnapshot.asset == asset)
    if start:
        query = query.where(BiasSnapshot.timestamp_utc >= start)
    if end:
        query = query.where(BiasSnapshot.timestamp_utc <= end)

    names = ("timestamp_utc", "timestamp_ist", "asset", "bias", "score",
             "confidence", "bullish_pct", "bearish_pct", "article_count",
             "last_signal_age_min", "bull_drivers", "bear_drivers")
    out: list[dict] = []
    with engine.connect() as conn:
        for row in conn.execute(query):
            # Legacy Row supports positional and key lookup; build an explicit
            # dict so the CSV writer reads plain attributes either way.
            values = list(row)
            out.append({n: (values[i] if i < len(values) else None)
                        for i, n in enumerate(names)})
    return out


def _drivers_cell(entries) -> str:
    """``headline (weight)`` per driver, joined for one spreadsheet cell."""
    parts = []
    for d in (entries or []):
        title = str(d.get("title", "")).strip().replace("\n", " ")
        weight = d.get("weight", "")
        parts.append(f"{title} [{weight}]")
    return " | ".join(parts)


def _iso(value) -> str:
    if value is None:
        return ""
    return value.isoformat()


def to_csv_rows(rows) -> list[dict]:
    """
    Build the CSV rows and flag where the bias flipped.

    ``bias_changed`` compares against the PREVIOUS row of the SAME asset, so a
    gold flip and a bitcoin flip on the same poll are each marked independently.
    The first row seen for an asset is not a change - there is nothing to have
    changed from.
    """
    out: list[dict] = []
    previous: dict[str, str] = {}
    for r in rows:
        bias = r["bias"] or "NO_BIAS"
        asset = r["asset"]
        last = previous.get(asset)
        changed = bool(last) and last != bias
        previous[asset] = bias
        age = r["last_signal_age_min"]
        out.append({
            "timestamp_utc": _iso(r["timestamp_utc"]),
            "timestamp_ist": r["timestamp_ist"] or "",
            "asset": asset,
            "bias": bias,
            "bias_changed": "TRUE" if changed else "FALSE",
            "score": r["score"],
            "confidence": r["confidence"],
            "bullish_pct": r["bullish_pct"],
            "bearish_pct": r["bearish_pct"],
            "article_count": r["article_count"],
            "last_signal_age_min": "" if age is None else age,
            "bull_drivers": _drivers_cell(r["bull_drivers"]),
            "bear_drivers": _drivers_cell(r["bear_drivers"]),
            # Left blank on purpose. The engine never fills or reads this.
            "manual_price_note": "",
        })
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Export the bias snapshot log to CSV for manual review.")
    parser.add_argument("--asset", default=None,
                        help=f"one of {', '.join(ALL_ASSETS)} "
                             "(default: all)")
    parser.add_argument("--from", dest="from_raw", default=None,
                        metavar="YYYY-MM-DD", help="inclusive start date (UTC)")
    parser.add_argument("--to", dest="to_raw", default=None,
                        metavar="YYYY-MM-DD", help="inclusive end date (UTC)")
    parser.add_argument("--out", default="bias_snapshots.csv",
                        help="output CSV path (default: bias_snapshots.csv)")
    args = parser.parse_args(argv[1:])

    if args.asset and args.asset not in ALL_ASSETS:
        print(f"Unknown asset {args.asset!r}. Choose from: "
              f"{', '.join(ALL_ASSETS)}", file=sys.stderr)
        return 2

    rows = to_csv_rows(load(
        args.asset,
        parse_bound(args.from_raw, end_of_day=False),
        parse_bound(args.to_raw, end_of_day=True),
    ))

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    flips = sum(1 for r in rows if r["bias_changed"] == "TRUE")
    print(f"wrote {len(rows)} row(s) to {out_path}")
    print(f"  {flips} bias flip(s) flagged with bias_changed=TRUE")
    print("  manual_price_note is empty - fill it in yourself; the engine "
          "never reads it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))