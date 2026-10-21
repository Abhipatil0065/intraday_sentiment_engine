#!/usr/bin/env python
"""
HIT-RATE REPORT (improvement item 11).

Answers the only question that matters when tuning a sentiment engine:

    "When the engine said BULLISH at 0.8 confidence, how often was the asset
     actually higher afterwards?"

It joins the engine's own decision log against a price CSV and reports hit rate
BROKEN DOWN BY CONFIDENCE BUCKET and BY ASSET, so a weak category or an
over-confident bucket can be spotted and the map/decay tables retuned.

Usage
-----
    python tools/hitrate_report.py --prices data/xauusd.csv
    python tools/hitrate_report.py --prices data/xauusd.csv --horizon 30

Price CSV format (minimal):
    timestamp,close
    2026-10-04T09:00:00+00:00,2650.10
    ...

The horizon is in MINUTES and is measured from the DECISION timestamp, because
the question is "did the call work from the moment it was published".

A NO_BIAS decision is counted separately and never counted as a hit or a miss:
declining to take a side is the correct behaviour, not a failure.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.analysis.decision_log import DecisionLog  # noqa: E402

# Confidence buckets. These are the tuning surface: if the 0.0-0.3 bucket hits
# as often as the 0.6-0.9 bucket, the confidence formula is lying.
BUCKETS = ((0.0, 0.2, "0.0-0.2"), (0.2, 0.4, "0.2-0.4"),
           (0.4, 0.6, "0.4-0.6"), (0.6, 0.8, "0.6-0.8"),
           (0.8, 1.01, "0.8-1.0"))

# Below this many observations a bucket's hit rate is reported as NOT MEANINGFUL.
# 30 is the conventional floor for treating a proportion as an estimate worth
# acting on; under it, a single lucky call swings the rate by ~3 points.
MIN_SAMPLES_PER_BUCKET = 30


def parse_ts(raw):
    """Parse a CSV timestamp into aware UTC. Returns None if unusable."""
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_prices(path: Path):
    """
    Parse a price CSV into a chronologically sorted list of (ts, close).

    Raises SystemExit with an actionable message when the file is missing or
    malformed - a raw traceback here tells the operator nothing they don't
    already know, and this tool is run by hand.
    """
    if not path.exists():
        raise SystemExit(
            f"Price file not found: {path}\n"
            f"Create it with a header row and two columns, e.g.\n"
            f"  timestamp,close\n"
            f"  2026-10-04T09:00:00+00:00,2650.10\n"
            f"Column names may be timestamp/time/date/datetime and "
            f"close/price/value/last.")

    series = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row")
        lowered = {c.lower(): c for c in reader.fieldnames}
        ts_col = next((lowered[c] for c in
                       ("timestamp", "time", "date", "datetime") if c in lowered),
                      None)
        px_col = next((lowered[c] for c in
                       ("close", "price", "value", "last") if c in lowered), None)
        if not ts_col or not px_col:
            raise SystemExit(
                f"{path} needs a timestamp column and a close column; got "
                f"{reader.fieldnames}")
        for row in reader:
            ts = parse_ts(row.get(ts_col))
            try:
                close = float(row[px_col])
            except (TypeError, ValueError):
                continue
            if ts is not None:
                series.append((ts, close))
    series.sort(key=lambda x: x[0])
    if not series:
        raise SystemExit(
            f"{path} contained no parsable rows. Check the timestamp format "
            f"(ISO-8601 or 'YYYY-MM-DD HH:MM') and that close is numeric.")
    return series


def price_after(series, when: datetime, horizon_minutes: int):
    """First close at or after `when + horizon`; None if the data runs out."""
    target = when + timedelta(minutes=horizon_minutes)
    for ts, close in series:
        if ts >= target:
            return close
    return None


def price_at(series, when: datetime):
    """First close at or after `when`; None if the data starts later."""
    for ts, close in series:
        if ts >= when:
            return close
    return None


def bucket_for(confidence: float) -> str:
    """Confidence -> bucket label, for the calibration table."""
    for low, high, label in BUCKETS:
        if low <= confidence < high:
            return label
    return BUCKETS[-1][2]


def evaluate(records, series, horizon_minutes: int):
    """Join decisions to outcomes. One row per scored call."""
    rows = []
    for record in records:
        if record.kind != "bias" or record.bias not in ("BULLISH", "BEARISH"):
            continue          # NO_BIAS is neither a hit nor a miss
        when = parse_ts(record.ts)
        if when is None:
            continue
        entry_price = price_at(series, when)
        later = price_after(series, when, horizon_minutes)
        if entry_price is None or later is None or entry_price == later:
            continue
        moved_up = later > entry_price
        expected_up = record.bias == "BULLISH"
        rows.append({
            "hit": moved_up == expected_up,
            "confidence": record.confidence,
            "bucket": bucket_for(record.confidence),
            "asset": record.asset,
        })
    return rows


def _rate(rows):
    if not rows:
        return None
    return sum(1 for r in rows if r["hit"]) / len(rows)


def _count_no_bias(records) -> int:
    return sum(1 for r in records
               if r.kind == "bias" and r.bias == "NO_BIAS")


# --------------------------------------------------------------------------
# Database mode: the engine's own automatic outcomes.
# --------------------------------------------------------------------------
def _db_path() -> str:
    """The live database, resolved the same way the app resolves it."""
    try:
        from app.config import settings
        url = settings.resolved_database_url()
        if ":///" in url:
            return url.split(":///", 1)[1]
        return str(BASE_DIR / "data" / "intraday_sentiment.db")
    except Exception:
        return str(BASE_DIR / "data" / "intraday_sentiment.db")


def load_db_outcomes(asset: str | None = None) -> list[dict]:
    """
    The engine's OWN resolved outcomes, read from ``bias_outcome``.

    This is the automatic path: the outcome loop records a call when the
    published bias changes, stores the price at the call, and resolves the
    call to WORKED / FAILED / Mixed once the horizon has elapsed. A
    "Mixed" call counts as half a hit, the same reading the calibration
    fitter uses.

    PENDING rows are excluded - an undecided call is not evidence either
    way.
    """
    import sqlite3

    path = _db_path()
    if not Path(path).exists():
        return []
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        q = """
            SELECT o.asset AS asset,
                   o.bias  AS bias,
                   o.outcome AS outcome,
                   s.confidence AS confidence
            FROM bias_outcome o
            JOIN bias_snapshots s ON s.id = o.bias_snapshot_id
            WHERE LOWER(o.outcome) IN ('worked', 'failed', 'mixed')
        """
        params: tuple = ()
        if asset:
            q += " AND o.asset = ?"
            params = (asset.upper(),)
        q += " ORDER BY o.date_utc ASC"
        return [dict(r) for r in con.execute(q, params)]
    finally:
        con.close()


def db_rows_to_scored(rows: list[dict]) -> list[dict]:
    """Turn resolved outcome rows into the shape ``report`` expects."""
    scored = []
    for r in rows:
        outcome = (r.get("outcome") or "").strip().lower()
        if outcome == "worked":
            hit = True
        elif outcome == "failed":
            hit = False
        else:
            hit = True          # mixed counts as half; tracked separately
        conf = r.get("confidence")
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        scored.append({
            "hit": hit,
            "confidence": conf,
            "bucket": bucket_for(conf),
            "asset": r.get("asset") or "",
            "mixed": outcome == "mixed",
        })
    return scored


def report_db(rows: list[dict], by: str) -> None:
    """Print the hit rate straight from the engine's own outcomes."""
    print()
    print("=" * 66)
    print("HIT-RATE REPORT (engine's own automatic outcomes)")
    print("=" * 66)
    scored = db_rows_to_scored(rows)
    print(f"Resolved calls   : {len(scored)}")
    mixed = sum(1 for r in scored if r.get("mixed"))
    pure = len(scored) - mixed
    if pure:
        rate = sum(1 for r in scored if r["hit"]) / pure
        print(f"Hit rate (pure)  : {rate:.1%}  (Mixed excluded)")
    if mixed:
        half = (sum(1 for r in scored if r["hit"]) + 0.5 * mixed) / len(scored)
        print(f"Hit rate (mixed=0.5): {half:.1%}")
    print()
    if not scored:
        print("No resolved outcomes yet.")
        print("The outcome loop resolves a call once the horizon has elapsed and")
        print("the price history covers both ends of the window. Only BITCOIN has")
        print("a reachable price feed, so the other assets stay PENDING.")
        return
    if by in ("confidence", "both"):
        _print_grouped("BY CONFIDENCE BUCKET", scored, "bucket",
                       order=[label for _, _, label in BUCKETS])
    if by in ("asset", "both"):
        _print_grouped("BY ASSET", scored, "asset")


def _print_grouped(title: str, rows, key: str, order=None) -> None:
    print("-" * 66)
    print(title)
    print("-" * 66)
    print(f"{key:<12}{'n':>6}{'hit rate':>12}{'  status':>14}")
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    keys = order or sorted(grouped)
    for name in keys:
        group = grouped.get(name) or []
        rate = _rate(group)
        shown = f"{rate:.1%}" if rate is not None else "-"
        print(f"{str(name):<12}{len(group):>6}{shown:>12}"
              f"{_sample_flag(len(group)):>14}")
    print()


def _sample_flag(n: int) -> str:
    """
    Mark buckets whose sample size cannot support a conclusion.

    Below MIN_SAMPLES_PER_BUCKET a hit rate is noise: with 4 samples the only
    possible rates are 0/25/50/75/100%, so quoting one as a calibration reading
    would be actively misleading.
    """
    if n == 0:
        return "no data"
    if n < MIN_SAMPLES_PER_BUCKET:
        return f"NOT MEANING. (<{MIN_SAMPLES_PER_BUCKET})"
    return "ok"


def report(rows, no_bias_count: int, by: str) -> None:
    print()
    print("=" * 66)
    print("HIT-RATE REPORT (news sentiment vs realised price)")
    print("=" * 66)
    print(f"Scored decisions : {len(rows)}")
    rate = _rate(rows)
    print(f"Overall hit rate : {rate:.1%}" if rate is not None
          else "Overall hit rate : n/a (no matched decisions)")
    print(f"NO_BIAS skipped  : {no_bias_count} (declining to call is not a miss)")
    if len(rows) < MIN_SAMPLES_PER_BUCKET:
        print(f"!! SAMPLE TOO SMALL: {len(rows)} scored decision(s); the overall "
              f"rate below is NOT MEANINGFUL (<{MIN_SAMPLES_PER_BUCKET}).")
    print()

    if not rows:
        print("No decisions could be matched to price data.")
        print("Check that the log and the price CSV cover the same period.")
        return

    if by in ("confidence", "both"):
        _print_grouped("BY CONFIDENCE BUCKET", rows, "bucket",
                       order=[label for _, _, label in BUCKETS])
        print("If a low bucket hits as often as a high one, the confidence")
        print("formula is overstating certainty and should be recalibrated.")
        print()
        if any(r["bucket"] and len([x for x in rows if x["bucket"] == r["bucket"]])
               < MIN_SAMPLES_PER_BUCKET for r in rows[:1]):
            print("(Buckets marked NOT MEANINGFUL need more data before they")
            print(" can be used to justify retuning a threshold.)")
            print()

    if by in ("asset", "both"):
        _print_grouped("BY ASSET", rows, "asset")


def log_only_stats(records, by: str) -> int:
    """
    Report the DECISION LOG's own distribution, with no price data.

    This answers "how much data do we even have?" without pretending to a hit
    rate. Every bucket below the minimum is flagged, because a hit rate cannot
    be computed at all until a price CSV is supplied.
    """
    biases = [r for r in records if r.kind == "bias"]
    scored = [r for r in biases if r.bias in ("BULLISH", "BEARISH")]
    print()
    print("=" * 66)
    print("DECISION LOG STATISTICS (no price data - hit rate NOT computed)")
    print("=" * 66)
    print(f"log records total      : {len(records)}")
    print(f"bias records           : {len(biases)}")
    print(f"  directional (scored) : {len(scored)}")
    print(f"  NO_BIAS (declined)   : {len(biases) - len(scored)}")
    if biases:
        print(f"time span              : {min(r.ts for r in biases)} "
              f"-> {max(r.ts for r in biases)}")

    print()
    print("-" * 66)
    print("SAMPLE SIZE BY BIAS")
    print("-" * 66)
    print(f"{'bias':<12}{'n':>6}{'  status':>24}")
    for name in ("BULLISH", "BEARISH", "NO_BIAS"):
        n = sum(1 for r in biases if r.bias == name)
        flag = ("excluded from hit rate"
                if name == "NO_BIAS" else _sample_flag(n))
        print(f"{name:<12}{n:>6}{flag:>24}")

    if by in ("confidence", "both"):
        print()
        print("-" * 66)
        print("SAMPLE SIZE BY CONFIDENCE BUCKET")
        print("-" * 66)
        print(f"{'bucket':<12}{'n':>6}{'  status':>24}")
        for _, _, label in BUCKETS:
            n = sum(1 for r in scored if bucket_for(r.confidence) == label)
            print(f"{label:<12}{n:>6}{_sample_flag(n):>24}")

    if by in ("asset", "both"):
        print()
        print("-" * 66)
        print("SAMPLE SIZE BY ASSET")
        print("-" * 66)
        print(f"{'asset':<12}{'n':>6}{'  status':>24}")
        for asset in sorted({r.asset for r in scored if r.asset}):
            n = sum(1 for r in scored if r.asset == asset)
            print(f"{asset:<12}{n:>6}{_sample_flag(n):>24}")

    print()
    print("Supply a price CSV to convert these into hit rates:")
    print("  python tools/hitrate_report.py --prices data/<asset>.csv")
    print()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hit-rate report")
    parser.add_argument("--prices", type=Path, default=None,
                        help="CSV with timestamp,close columns")
    parser.add_argument("--horizon", type=int, default=30,
                        help="minutes to wait before judging a call (default 30)")
    parser.add_argument("--by", choices=["confidence", "asset", "both"],
                        default="both")
    parser.add_argument("--log", type=Path, default=None,
                        help="override the decision log path")
    parser.add_argument("--stats-only", action="store_true",
                        help="report decision-log sample sizes without needing "
                             "a price CSV")
    parser.add_argument("--from-db", action="store_true",
                        help="use the engine's own automatically resolved "
                             "outcomes instead of a price CSV")
    parser.add_argument("--asset", default=None,
                        help="restrict the DB mode to one asset (e.g. XAUUSD)")
    args = parser.parse_args(argv)

    if args.from_db:
        rows = load_db_outcomes(args.asset)
        report_db(rows, args.by)
        return 0

    log_reader = DecisionLog(args.log) if args.log else DecisionLog()
    records = log_reader.read()
    if not records:
        print("Decision log is empty - has the pipeline ever run?",
              file=sys.stderr)
        return 1

    if args.stats_only or args.prices is None:
        return log_only_stats(records, args.by)

    series = load_prices(args.prices)
    rows = evaluate(records, series, args.horizon)
    report(rows, _count_no_bias(records), args.by)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())