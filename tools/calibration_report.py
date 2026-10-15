"""
STEP 10 - offline confidence calibration report.

Reads the human WORKED / FAILED / Mixed labels recorded against bias snapshots,
buckets them by the engine's own confidence, and reports the observed hit rate
per bucket. That table is the evidence for whether the 0..1 confidence number
actually means what it says.

    python tools/calibration_report.py                 # default buckets
    python tools/calibration_report.py --asset XAUUSD  # one asset
    python tools/calibration_report.py --json          # machine-readable

The join is bias_outcome.bias_snapshot_id -> bias_snapshots. Do NOT read
bias_outcome.confidence: the POST endpoint writes it as a literal 0.0 and never
copies the snapshot's value, so the label row alone cannot be calibrated.

A bucket is a "hit" when the label is WORKED, a "miss" when FAILED, and Mixed
counts as half - it is the honest reading of a partial confirmation, and
treating it as either extreme is what makes a fit look better than it is.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _db_path() -> str:
    """Resolve the live database the same way the app does."""
    try:
        sys.path.insert(0, str(REPO))
        from app.config import settings
        url = settings.resolved_database_url()
        if ":///" in url:
            return url.split(":///", 1)[1]
        return str(REPO / "data" / "intraday_sentiment.db")
    except Exception:
        return str(REPO / "data" / "intraday_sentiment.db")


# Outlcome -> contribution to the hit-rate numerator.
_OUTCOME_WEIGHT = {
    "worked": 1.0,
    "failed": 0.0,
    "mixed": 0.5,
}


def fetch(db: str, asset: str | None) -> list[dict]:
    """One row per labelled snapshot: the engine's confidence and the label."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        q = """
            SELECT o.id            AS label_id,
                   o.asset         AS asset,
                   o.outcome       AS outcome,
                   o.date_utc      AS date_utc,
                   s.id            AS snapshot_id,
                   s.confidence    AS confidence,
                   s.raw_score     AS raw_score,
                   s.score         AS score,
                   s.bias          AS bias
            FROM bias_outcome o
            JOIN bias_snapshots s ON s.id = o.bias_snapshot_id
        """
        params: tuple = ()
        if asset:
            q += " WHERE o.asset = ?"
            params = (asset.upper(),)
        q += " ORDER BY o.date_utc ASC"
        return [dict(r) for r in con.execute(q, params)]
    finally:
        con.close()


def bucket_of(conf: float, edges) -> int:
    for i in range(len(edges) - 1):
        if edges[i] <= conf < edges[i + 1]:
            return i
    return len(edges) - 2


def build(rows: list[dict], edges) -> list[dict]:
    """Bucket the labels and compute the observed hit rate in each."""
    buckets: dict[int, dict] = {}
    for r in rows:
        conf = r.get("confidence")
        if conf is None:
            continue
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        weight = _OUTCOME_WEIGHT.get((r.get("outcome") or "").strip().lower())
        if weight is None:
            continue                        # unknown label: not evidence
        b = bucket_of(conf, edges)
        slot = buckets.setdefault(
            b, {"bucket": b, "lo": edges[b], "hi": edges[b + 1],
                "n": 0, "hits": 0.0, "worked": 0, "failed": 0, "mixed": 0,
                "conf_sum": 0.0})
        slot["n"] += 1
        slot["hits"] += weight
        slot["conf_sum"] += conf
        key = (r.get("outcome") or "").strip().lower()
        if key == "worked":
            slot["worked"] += 1
        elif key == "failed":
            slot["failed"] += 1
        elif key == "mixed":
            slot["mixed"] += 1

    out = []
    for b in sorted(buckets):
        s = buckets[b]
        s["hit_rate"] = (s["hits"] / s["n"]) if s["n"] else None
        s["mean_confidence"] = (s["conf_sum"] / s["n"]) if s["n"] else None
        out.append(s)
    return out


def fit(buckets: list[dict], min_samples: int) -> tuple[bool, list[list[float]]]:
    """
    Fit a monotone piecewise-linear map from mean_confidence to hit_rate.

    Returns (fitted, breakpoints). Not fitted when any bucket with enough
    samples is missing, or when there are too few usable buckets - a two-point
    identity is the honest answer then, and the caller must not pretend
    otherwise.
    """
    usable = [b for b in buckets
              if b["n"] >= min_samples and b["hit_rate"] is not None]
    if len(usable) < 2:
        return False, [[0.0, 0.0], [1.0, 1.0]]

    pts = sorted((round(b["mean_confidence"], 3), round(b["hit_rate"], 3))
                 for b in usable)
    # Enforce monotonicity in the OUTPUT: a higher raw bucket can never map to
    # a lower calibrated value, or the map would reward over-confidence.
    for i in range(1, len(pts)):
        if pts[i][1] < pts[i - 1][1]:
            pts[i] = (pts[i][0], pts[i - 1][1])
    return True, [list(p) for p in pts]


def as_config_block(breakpoints: list[list[float]]) -> str:
    lines = ["CONFIDENCE_CALIBRATION_ENABLED = True",
             "CONFIDENCE_CALIBRATION: tuple[tuple[float, float], ...] = ("]
    for raw, cal in breakpoints:
        lines.append(f"    ({raw:.3f}, {cal:.3f}),")
    lines.append(")")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset", default=None,
                    help="restrict to one asset (e.g. XAUUSD)")
    ap.add_argument("--min-samples", type=int, default=30,
                    help="minimum labels per bucket to fit (default 30)")
    ap.add_argument("--edges", default="0,0.1,0.2,0.3,0.45,0.6,0.8,1.01",
                    help="comma-separated bucket edges")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    edges = tuple(float(x) for x in args.edges.split(",") if x.strip())
    if len(edges) < 2:
        print("need at least two bucket edges")
        return 2

    db = _db_path()
    rows = fetch(db, args.asset)
    buckets = build(rows, edges)
    fitted, breakpoints = fit(buckets, args.min_samples)

    if args.json:
        print(json.dumps({
            "asset": args.asset,
            "labels": len(rows),
            "fitted": fitted,
            "breakpoints": breakpoints,
            "buckets": buckets,
        }, indent=2))
        return 0

    print("=" * 78)
    print("STEP 10 - CONFIDENCE CALIBRATION REPORT")
    print("=" * 78)
    print(f"database : {db}")
    print(f"asset    : {args.asset or 'ALL'}")
    print(f"labels   : {len(rows)}")
    print(f"min/bucket: {args.min_samples}")
    print()

    if not buckets:
        print("No usable calibration labels yet.")
        print()
        print("Nothing has been fitted and nothing has been changed: an empty")
        print("table cannot support a mapping, and inventing one is exactly")
        print("the unfounded formula this step exists to avoid.")
        print()
        print("Record labels with:")
        print('  curl -X POST "http://localhost:8000/api/calibration/labels'
              '?asset=XAUUSD&outcome=WORKED"')
        print()
        print("Current runtime status:")
        print(f"  {json.dumps(_runtime_status())}")
        return 0

    hdr = f"{'bucket':<14}{'n':>5}{'worked':>8}{'mixed':>7}{'failed':>8}" \
          f"{'mean conf':>11}{'hit rate':>10}"
    print(hdr)
    print("-" * len(hdr))
    for b in buckets:
        hr = "     -" if b["hit_rate"] is None else f"{b['hit_rate']:>10.3f}"
        print(f"{b['lo']:.2f}-{b['hi']:.2f}{'':<{14 - 8}}"
              f"{b['n']:>5}{b['worked']:>8}{b['mixed']:>7}{b['failed']:>8}"
              f"{b['mean_confidence']:>11.3f}{hr}")

    print()
    if fitted:
        print("FITTED - paste this into app/config.py to enable:")
        print()
        print(as_config_block(breakpoints))
        print()
        print("A confidence of", f"{breakpoints[0][0]:.2f}",
              "now maps to", f"{breakpoints[0][1]:.2f},")
        print("so the dashboard's number means the observed hit rate.")
    else:
        print("NOT FITTED - too few buckets reached the minimum sample count.")
        print("The runtime keeps the identity mapping and the flag stays off.")
    return 0


def _runtime_status() -> dict:
    try:
        sys.path.insert(0, str(REPO))
        from app.analysis.confidence_calibration import calibration_status
        return calibration_status()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


if __name__ == "__main__":
    raise SystemExit(main())
