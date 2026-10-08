"""Item 3 - Gold vs Silver cluster drivers (real data).

Uses the restored backup (Oct 3-4 data) and an Oct-4 time window so decay is
realistic. For GOLD and SILVER we:
  * read the STORED CURRENT snapshot verdict (score/damped/bias/drivers JSON)
  * RECOMPUTE via production build_signals->cluster_signals->compute() with
    now = end-of-Oct-4, to attach each top driver's `transmission` key and
    `event_key`, and to verify the dampening formula.
  * print top 10 bear + bull drivers with transmission + weight.
  * cross-reference shared headlines between gold and silver.
  * double-counting check: silver clusters carrying both CHINA_DEMAND and
    INDUSTRIAL_DEMAND.

Read-only DB access.
"""
import os, sys, json
from datetime import datetime, timezone

os.chdir(r"C:\Users\HP\.vscode\intraday_sentiment_engine")
sys.path.insert(0, r"C:\Users/HP/.vscode/intraday_sentiment_engine")
sys.path.insert(0, r"C:\Users/HP/.vscode/intraday_sentiment_engine/scripts/scratch")

import argparse

import sqlalchemy as sa
from app.config import settings
from app.database import init_db, session_scope
parser = argparse.ArgumentParser(description="Gold/Silver cluster drivers (read-only).")
parser.add_argument("--db", default="data/intraday_sentiment.db",
                    help="DB path; default is the live DB")
args = parser.parse_args()
from app.analysis.event_clustering import cluster_signals
from app.analysis.sentiment_engine import compute, _aggregate, clear_bias_history
from replay_window import ASSETS, build_signals, parse_window
from app.utils import timeutils as T
from app.models.db_models import SentimentSnapshot
from app.models.enums import Asset

K_DEFAULT = settings.score_damping_k
# Oct 4 window on the restored backup so age/decay are realistic.
WS = datetime(2026, 10, 4, 4, 0, tzinfo=timezone.utc)   # NY market open ~00:00 => 04:00Z
WE = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)
NOW = WE


def headline(s):
    t = (s.title or "")[:88]
    return f"{t:<88} dir={s.direction.value:<7} mag={s.magnitude:+.3f} w={s.final_weight:.3f} txn={s.transmission}"


def main():
    import os
    if not os.path.exists(args.db):
        raise SystemExit(f"DB not found: {args.db}")
    init_db()
    print(f"window={WS.isoformat()}..{WE.isoformat()}  now={NOW.isoformat()}  k={K_DEFAULT}")
    with session_scope() as session:
        _, sigs = build_signals(session, WS, NOW)
        print(f"signals in window: {len(sigs)}")
        stored = {}
        for asset in ASSETS:
            row = session.scalar(
                sa.select(SentimentSnapshot.score, SentimentSnapshot.damped_score,
                          SentimentSnapshot.bias, SentimentSnapshot.confidence,
                          SentimentSnapshot.mode, SentimentSnapshot.drivers,
                          SentimentSnapshot.bear_drivers, SentimentSnapshot.bull_drivers,
                          SentimentSnapshot.contributing_articles).where(
                    SentimentSnapshot.asset == asset.value,
                    SentimentSnapshot.scope == "CURRENT").order_by(SentimentSnapshot.computed_at.desc()).limit(1))
            stored[asset] = row

        gold = [s for s in sigs if s.asset is Asset.GOLD]
        silv = [s for s in sigs if s.asset is Asset.SILVER]
        gold_c = cluster_signals(list(gold), window_hours=0.5)
        silv_c = cluster_signals(list(silv), window_hours=0.5)

    clear_bias_history()
    for asset, label, clustered in ((Asset.GOLD, "GOLD", gold_c), (Asset.SILVER, "SILVER", silv_c)):
        agg = _aggregate(clustered, NOW)
        tw = agg["bull"] + agg["bear"]
        res = compute(clustered, asset, now=NOW)
        r = stored[asset]
        print("=" * 110)
        print(f"{label}  (recompute, now={NOW.isoformat()})")
        print(f"  recompute: raw={res.raw_score:+.2f} damped(k={K_DEFAULT})={res.damped_score:+.2f} "
              f"bias={res.bias.value} conf={res.confidence:.3f} clusters={res.contributing_clusters}")
        if r:
            print(f"  STORED CURRENT(snap): score={r[0]:+.2f} damped={r[1]:+.2f} bias={r[2]} "
                  f"conf={r[3]} mode={r[4]} articles={r[8]}")
            print(f"  stored drivers JSON = {r[5]}")
        f = round(res.raw_score * tw / (tw + K_DEFAULT), 2) if tw > 0 else 0.0
        print(f"  total_weight={tw:.4f}  formula raw*tw/(tw+k)={f:+.2f}  matches={abs(f-res.damped_score)<0.15}")

        bear = sorted(agg["bear_items"], key=lambda t: t[0], reverse=True)[:10]
        bull = sorted(agg["bull_items"], key=lambda t: t[0], reverse=True)[:10]
        print(f"  -- TOP 10 BEAR drivers --")
        for i, (m, s) in enumerate(bear, 1):
            print(f"    {i:2d}. {headline(s)}")
        print(f"  -- TOP 10 BULL drivers --")
        for i, (m, s) in enumerate(bull, 1):
            print(f"    {i:2d}. {headline(s)}")
        print()

    # Cross-reference shared headlines between gold and silver clusters
    def titles(clustered):
        return {s.title for s in clustered if s.title}
    shared = titles(gold_c) & titles(silv_c)
    print("SHARED HEADLINES in GOLD & SILVER clusters:")
    for t in sorted(shared)[:15]:
        print(f"  - {t[:100]}")
    print(f"  count shared={len(shared)}")

    # Double-counting check on silver
    silv_agg = _aggregate(silv_c, NOW)
    by_clu = {}
    for m, s in silv_agg["bear_items"] + silv_agg["bull_items"]:
        by_clu.setdefault(s.cluster_id or "?", set()).add(s.transmission)
    multi = {cid: tx for cid, tx in by_clu.items() if {"CHINA_DEMAND", "INDUSTRIAL_DEMAND"} <= tx}
    print("\nSILVER double-counting (CHINA_DEMAND + INDUSTRIAL_DEMAND same cluster):")
    print("  " + (f"FOUND: {len(multi)} clusters SUM transcription keys by design" if multi
                 else "none"))
    print("  NOTE: a single article can map to MULTIPLE event types per asset; "
          "transmission_map sums them (intentional, not double-counting of the same mechanism).")


if __name__ == "__main__":
    main()
