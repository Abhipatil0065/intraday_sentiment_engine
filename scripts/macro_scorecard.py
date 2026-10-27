"""
MACRO NEWS ANALYST - scorecard
==============================

Reads data/macro_analyst_log.jsonl and reports how the analyst's biases
performed against realised price action::

    python scripts/macro_scorecard.py
    python scripts/macro_scorecard.py --log data/macro_analyst_log.jsonl

For every ANALYZE record with an EVALUATE record (same id, window 15/60/240)
each asset carries a verdict (CORRECT / WRONG / PARTLY / INCONCLUSIVE) and,
when wrong, an error_type.  Hit rate = CORRECT / (CORRECT + WRONG) with PARTLY
counted as 0.5; INCONCLUSIVE is excluded from the denominator.  Average
confidence is reported for CORRECT vs WRONG verdicts.  Any bucket with fewer
than 10 samples prints "insufficient data" instead of a rate.

This tool only reads the log. It never trades and never writes to it.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Allow `python scripts/macro_scorecard.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ASSETS = ("GOLD", "SILVER", "BITCOIN", "EQUITIES")
WINDOWS = (15, 60, 240)
MIN_SAMPLES = 10

CORRECT = "CORRECT"
WRONG = "WRONG"
PARTLY = "PARTLY"
INCONCLUSIVE = "INCONCLUSIVE"


def _parse_iso(raw: str | None):
    from datetime import datetime, timezone

    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_records(log_path: Path):
    """Return (analyzes: dict[id, record], evaluates: list[record])."""
    analyzes: dict[str, dict] = {}
    evaluates: list[dict] = []
    if not log_path.exists():
        return analyzes, evaluates
    with log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = record.get("type")
            if rtype == "ANALYZE":
                analyzes[record.get("id")] = record
            elif rtype == "EVALUATE":
                evaluates.append(record)
    return analyzes, evaluates


def _confidence(entry) -> float | None:
    try:
        return float(entry)
    except (TypeError, ValueError):
        return None


def build_rows(analyzes, evaluates):
    """One scored row per (asset, evaluation): verdict, confidence, news type, window."""
    rows = []
    for ev in evaluates:
        an = analyzes.get(ev.get("analyze_id"))
        if an is None or not an.get("valid"):
            continue
        output = an.get("output") or {}
        news_type = output.get("news_type") or "UNKNOWN"
        assets = output.get("assets") or {}
        conf_by_asset = {
            asset: _confidence((assets.get(asset) or {}).get("confidence")) for asset in ASSETS
        }
        evaluations = (ev.get("output") or {}).get("evaluations") or {}
        if not isinstance(evaluations, dict):
            continue
        for asset in ASSETS:
            entry = evaluations.get(asset)
            if not isinstance(entry, dict):
                continue
            verdict = entry.get("verdict")
            if verdict not in (CORRECT, WRONG, PARTLY, INCONCLUSIVE):
                continue
            rows.append({
                "analyze_id": ev.get("analyze_id"),
                "asset": asset,
                "window_min": ev.get("window_min"),
                "verdict": verdict,
                "error_type": entry.get("error_type"),
                "confidence": conf_by_asset.get(asset),
                "news_type": news_type,
            })
    return rows


def _hit_rate(rows):
    """(rate, n) with PARTLY = 0.5, INCONCLUSIVE excluded; n = CORRECT + WRONG."""
    correct = sum(1 for r in rows if r["verdict"] == CORRECT)
    wrong = sum(1 for r in rows if r["verdict"] == WRONG)
    partly = sum(1 for r in rows if r["verdict"] == PARTLY)
    n = correct + wrong
    if n == 0:
        return None, 0
    return (correct + 0.5 * partly) / n, n


def _bucket(rows, key):
    grouped: dict = defaultdict(list)
    for r in rows:
        grouped[r[key]].append(r)
    return grouped


def _rate_line(rows, label):
    rate, n = _hit_rate(rows)
    if n < MIN_SAMPLES:
        return f"insufficient data (n={n} < {MIN_SAMPLES})"
    return f"{rate:.1%}  (n={n})"


def _conf_avg(rows, verdict):
    confs = [r["confidence"] for r in rows
             if r["verdict"] == verdict and r["confidence"] is not None]
    if not confs:
        return None, 0
    return sum(confs) / len(confs), len(confs)


def _print_grouped(title, rows, key, order=None):
    print(f"\n{title}")
    print("-" * 66)
    grouped = _bucket(rows, key)
    keys = order or sorted(grouped, key=lambda x: str(x))
    print(f"{'bucket':<18}{'n':>6}{'correct':>9}{'wrong':>8}{'partly':>8}"
          f"{'inconclusive':>14}{'hit rate':>12}")
    for name in keys:
        group = grouped[name]
        correct = sum(1 for r in group if r["verdict"] == CORRECT)
        wrong = sum(1 for r in group if r["verdict"] == WRONG)
        partly = sum(1 for r in group if r["verdict"] == PARTLY)
        inconclusive = sum(1 for r in group if r["verdict"] == INCONCLUSIVE)
        rate, n = _hit_rate(group)
        shown = _rate_line(group, name)
        print(f"{str(name):<18}{len(group):>6}{correct:>9}{wrong:>8}{partly:>8}"
              f"{inconclusive:>14}{shown:>12}")


def _confidence_block(rows):
    print("\nCONFIDENCE CALIBRATION (average analyst confidence)")
    print("-" * 66)
    for verdict, label in ((CORRECT, "when verdict = CORRECT"),
                           (WRONG, "when verdict = WRONG"),
                           (PARTLY, "when verdict = PARTLY")):
        avg, n = _conf_avg(rows, verdict)
        if n < MIN_SAMPLES:
            print(f"{label:<32} insufficient data (n={n} < {MIN_SAMPLES})")
        else:
            print(f"{label:<32} avg confidence {avg:.1f}  (n={n})")


def _error_types(rows):
    counts = Counter(r["error_type"] for r in rows
                     if r["verdict"] == WRONG and r["error_type"])
    print("\nMOST COMMON ERROR TYPES (verdict = WRONG)")
    print("-" * 66)
    if not counts:
        print("(none)")
        return
    for error_type, n in counts.most_common():
        print(f"{error_type:<38}{n:>6}")


def report(log_path: Path) -> int:
    analyzes, evaluates = load_records(log_path)
    total_events = sum(1 for r in analyzes.values() if r.get("valid"))
    rows = build_rows(analyzes, evaluates)
    evaluated_events = {r["analyze_id"] for r in rows}

    print("=" * 66)
    print("MACRO ANALYST SCORECARD")
    print("=" * 66)
    print(f"log file : {log_path}")
    print(f"total events (valid ANALYZE records): {total_events}")
    print(f"events with at least one evaluation : {len(evaluated_events)}")
    print(f"evaluation rows scored              : {len(rows)}")
    print(
        f"hit rate = CORRECT / (CORRECT + WRONG), PARTLY = 0.5, "
        f"INCONCLUSIVE excluded; buckets with n < {MIN_SAMPLES}: insufficient data"
    )

    overall_rate, overall_n = _hit_rate(rows)
    print(f"\nOVERALL hit rate : {_rate_line(rows, 'all')}")

    _print_grouped("BY ASSET", rows, "asset", order=list(ASSETS))
    _print_grouped("BY NEWS TYPE", rows, "news_type")
    _print_grouped("BY WINDOW", rows, "window_min", order=list(WINDOWS))

    _confidence_block(rows)
    _error_types(rows)

    if len(rows) < MIN_SAMPLES:
        print(f"\nNOTE: only {len(rows)} scored row(s) - treat all rates as preliminary.")
    print("=" * 66)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score the macro analyst log: hit rates by asset, news "
                    "type and window, plus confidence calibration.")
    parser.add_argument("--log", default=None,
                        help="path to macro_analyst_log.jsonl "
                             "(default: data/macro_analyst_log.jsonl)")
    args = parser.parse_args(argv)

    from app.config import BASE_DIR  # noqa: E402 - same project root the runner uses
    log_path = Path(args.log) if args.log else BASE_DIR / "data" / "macro_analyst_log.jsonl"

    return report(log_path)


if __name__ == "__main__":
    raise SystemExit(main())
