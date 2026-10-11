"""
Item 1 offline verification: recompute the last 50 bias_snapshots per asset with
the baseline adjustment applied and report the bull/bear distribution.

This script is READ-ONLY against the live DB. It never writes anything. It
re-implements the baseline logic inline (baseline_score + adjusted_score) so
the numbers come from the same rules the writer uses, without importing the
writer (which would open a session and could see in-flight rows).

For each asset it replays the persisted history in timestamp order, computing
each tick's baseline from the PRIOR ticks only (no same-tick look-ahead), then
reports the split over the last 50 ticks and over the full history.

Usage:
    python scripts/scratch/verify_item1_dist.py
"""
from __future__ import annotations

import statistics
import sqlite3

DB = "data/intraday_sentiment.db"
ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD")
W = 20
M = 5
MIN_SCORE = 12.0


def baseline(prior, statistic):
    if len(prior) < M:
        return None
    return (statistics.median if statistic == "median" else statistics.mean)(prior)


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    print("columns:", [r[1] for r in cur.execute("PRAGMA table_info(bias_snapshots)")])
    total = cur.execute("SELECT COUNT(*) FROM bias_snapshots").fetchone()[0]
    print(f"total bias_snapshots: {total}")
    non_null_raw = cur.execute(
        "SELECT COUNT(*) FROM bias_snapshots WHERE raw_score IS NOT NULL").fetchone()[0]
    print(f"rows with raw_score populated: {non_null_raw}")
    print()

    for statistic in ("median", "mean"):
        print(f"########## baseline_statistic = {statistic}  W={W} M={M} MIN_SCORE={MIN_SCORE} ##########")
        for asset in ASSETS:
            rows = cur.execute(
                "SELECT score, raw_score FROM bias_snapshots WHERE asset=? "
                "ORDER BY timestamp_utc", (asset,)).fetchall()
            # Use ONLY persisted raw_score, exactly like the writer. Falling back
            # to ``score`` would be wrong now that new rows store the
            # baseline-ADJUSTED value in ``score``: mixing the two corrupts the
            # re-derived baseline. Rows with raw_score IS NULL are skipped from
            # the baseline window (they are pre-writer history) but still
            # counted in the raw comparison below.
            scores = [float(r[1]) for r in rows if r[1] is not None]
            if not scores:
                print(f"  {asset}: no persisted raw_score rows yet")
                continue
            out = []
            for i, raw in enumerate(scores):
                prior = scores[max(0, i - W):i]
                base = baseline(prior, statistic)
                if base is None:
                    out.append((raw, raw, None))
                    continue
                if abs(raw) < MIN_SCORE:
                    out.append((raw, raw, base))
                    continue
                out.append((raw, round(raw - base, 4), base))

            def split(seq):
                n = len(seq)
                bull = sum(1 for _, adj, _ in seq if adj > 0)
                bear = sum(1 for _, adj, _ in seq if adj < 0)
                flat = n - bull - bear
                return bull, bear, flat, (100.0 * bull / n if n else 0.0)

            last50 = out[-50:]
            lb = split(last50)
            fa = split(out)
            used = sum(1 for _, _, base in out if base is not None)
            print(f"  {asset}: n={len(out)}")
            print(f"    last50: BULLISH={lb[0]} ({lb[3]:.1f}%)  BEARISH={lb[1]}  flat={lb[2]}")
            print(f"    all:   BULLISH={fa[0]} ({fa[3]:.1f}%)  BEARISH={fa[1]}  flat={fa[2]}")
            print(f"    baseline used: {used}/{len(out)} ticks")
        print()

    # Raw (unadjusted) baseline for comparison, over the SAME persisted rows.
    print("########## RAW (no baseline), persisted raw_score rows only ##########")
    for asset in ASSETS:
        rows = cur.execute(
            "SELECT raw_score FROM bias_snapshots WHERE asset=? "
            "ORDER BY timestamp_utc", (asset,)).fetchall()
        scores = [float(r[0]) for r in rows if r[0] is not None]
        if not scores:
            print(f"  {asset}: no persisted raw_score rows yet")
            continue
        last50 = scores[-50:]
        bull = sum(1 for s in last50 if s > 0)
        bear = sum(1 for s in last50 if s < 0)
        flat = len(last50) - bull - bear
        print(f"  {asset}: last50 BULLISH={bull} ({100*bull/len(last50):.1f}%) "
              f"BEARISH={bear} ({100*bear/len(last50):.1f}%) flat={flat}")
    con.close()


if __name__ == "__main__":
    main()