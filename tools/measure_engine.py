"""
STEP/TASK 2 - measurement, not fixes.

Reads the live database and reports how the engine actually behaves. Nothing in
here tunes anything; it only measures.

    python tools/measure_engine.py                 # all sections
    python tools/measure_engine.py --tiers 500     # tick count
    python tools/measure_engine.py --live          # also re-derive bull/bear now
    python tools/measure_engine.py --section C     # one section

SECTIONS
    A  per-asset per-tick CSV dump
    B  aggregate stats per asset
    C  the contradiction_detected question
    D  the Step 5 confidence shrink, validated

DATA PROVENANCE (read this before trusting any number)
------------------------------------------------------
``bull`` and ``bear`` are stored on SentimentSnapshot, populated from the
engine's aggregation totals in ``pipeline._recompute_sentiment``. Historical
ticks written before that migration still have NULL there; they are reported
as missing rows, not as zero evidence.

  * Recent ticks: r = min(bull,bear)/max(bull,bear) is directly recoverable
    from the two persisted totals.

  * Legacy ticks: rows without bull/bear are counted as NOT CAPTURED, while
    rows with both totals are still used whenever they exist. Section C no
    longer reports r as UNRECOVERABLE.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TIERS_DEFAULT = 500
SECTIONS = ("A", "B", "C", "D")


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------
def _db_path() -> str:
    try:
        sys.path.insert(0, str(REPO))
        from app.config import settings
        url = settings.resolved_database_url()
        if ":///" in url:
            return url.split(":///", 1)[1]
    except Exception:  # noqa: BLE001
        pass
    return str(REPO / "data" / "intraday_sentiment.db")


ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD", "EQUITY")


def fetch_ticks(db: str, limit: int) -> list[dict]:
    """The most recent ``limit`` CURRENT-scope rows, oldest first."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        q = """
            SELECT id, computed_at, asset, scope, score, raw_score, confidence,
                   confidence_label, mode, stale, contradiction_detected,
                   cluster_conflict, evidence_split,
                   burst_bull, burst_bear, signal_count, bias, event_risk,
                   bull, bear
            FROM sentiment_snapshot
            WHERE scope = 'CURRENT'
            ORDER BY id DESC
            LIMIT ?
        """
        rows = [dict(r) for r in con.execute(q, (limit * len(ASSETS),))]
    finally:
        con.close()

    # Group by (computed_at) so a "tick" is one asset set, then keep the last N.
    by_tick: dict[str, dict] = {}
    for r in rows:
        by_tick.setdefault(r["computed_at"], {})[r["asset"]] = r
    ticks = [by_tick[k] for k in sorted(by_tick)]
    return ticks[-limit:]


def fetch_all_current(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        q = """
            SELECT id, computed_at, asset, score, raw_score, confidence,
                   confidence_label, mode, stale, contradiction_detected,
                   cluster_conflict, evidence_split,
                   signal_count, bias, bull, bear
            FROM sentiment_snapshot
            WHERE scope = 'CURRENT'
            ORDER BY id
        """
        return [dict(r) for r in con.execute(q)]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _pct(vals, p):
    if not vals:
        return None
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    frac = k - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac


def _line(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# Section A
# ---------------------------------------------------------------------------
def section_a(ticks: list[dict]) -> None:
    _line("SECTION A - per-asset per-tick dump (last %d ticks)" % len(ticks))
    print("tick_time,asset,bull,bear,score,raw_score,confidence,"
          "confidence_label,signal_count,mode,stale,cluster_conflict,"
          "evidence_split,contradiction_detected_alias,burst_bull,burst_bear")
    n_bull_missing = 0
    for tick in ticks:
        for asset in ASSETS:
            r = tick.get(asset)
            if not r:
                continue
            if r.get("bull") is None or r.get("bear") is None:
                n_bull_missing += 1
            row = [
                r["computed_at"][:19], asset,
                ("%.6f" % r["bull"]) if r.get("bull") is not None else "NA",
                ("%.6f" % r["bear"]) if r.get("bear") is not None else "NA",
                ("%.1f" % r["score"]) if r["score"] is not None else "NA",
                ("%.1f" % r["raw_score"]) if r.get("raw_score") is not None else "NA",
                ("%.3f" % r["confidence"]) if r["confidence"] is not None else "NA",
                r["confidence_label"] or "-",
                str(r["signal_count"] or 0),
                r["mode"] or "-",
                "1" if r["stale"] else "0",
                "1" if r["cluster_conflict"] else "0",
                "1" if r["evidence_split"] else "0",
                "1" if r["contradiction_detected"] else "0",
                str(r["burst_bull"] or 0),
                str(r["burst_bear"] or 0),
            ]
            print(",".join(row))
    print()
    print("bull/bear missing on %d of %d rows: NULL legacy ticks are NOT "
          "CAPTURED, while rows with values are stored for r."
          % (n_bull_missing, len(ticks) * len(ASSETS)))


# ---------------------------------------------------------------------------
# Section B
# ---------------------------------------------------------------------------
def section_b(db: str) -> None:
    rows = fetch_all_current(db)
    _line("SECTION B - aggregate stats (%d CURRENT rows)" % len(rows))

    for asset in ASSETS:
        ar = [r for r in rows if r["asset"] == asset]
        print()
        print("--- %s  (n=%d) ---" % (asset, len(ar)))
        if not ar:
            print("    no data")
            continue

        confs = [r["confidence"] for r in ar if r["confidence"] is not None]
        counts = [r["signal_count"] or 0 for r in ar]

        print("    confidence      : min=%.3f p10=%.3f p25=%.3f med=%.3f "
              "p75=%.3f p90=%.3f max=%.3f"
              % (min(confs), _pct(confs, 10), _pct(confs, 25),
                 statistics.median(confs), _pct(confs, 75), _pct(confs, 90),
                 max(confs)))
        low = sum(1 for c in confs if c < 0.30)
        print("    confidence<0.30 : %d (%.1f%%)  <- the TIEBREAK_MAX_CONFIDENCE line"
              % (low, 100.0 * low / len(confs) if confs else 0.0))

        modes = {}
        for r in ar:
            modes[r["mode"] or "-"] = modes.get(r["mode"] or "-", 0) + 1
        print("    mode            : %s"
              % ", ".join(f"{k}={v}" for k, v in sorted(modes.items())))

        stale = sum(1 for r in ar if r["stale"])
        print("    stale           : %d" % stale)

        print("    signal_count    : min=%d med=%d max=%d"
              % (min(counts), statistics.median(counts), max(counts)))
        thin = sum(1 for c in counts if c < 3)
        print("    signal_count<3  : %d (%.1f%%)"
              % (thin, 100.0 * thin / len(counts)))

        contra = sum(1 for r in ar if r["contradiction_detected"])
        cluster = sum(1 for r in ar if r["cluster_conflict"])
        split = sum(1 for r in ar if r["evidence_split"])
        print("    contradiction   : alias=%d cluster_conflict=%d "
              "evidence_split=%d" % (contra, cluster, split))


# ---------------------------------------------------------------------------
# Section C
# ---------------------------------------------------------------------------
def section_c(db: str, live: bool) -> None:
    _line("SECTION C - contradiction split: cluster_conflict vs evidence_split")
    rows = fetch_all_current(db)
    captured = [
        r for r in rows
        if r.get("bull") is not None and r.get("bear") is not None
    ]
    missing_bull_bear = len(rows) - len(captured)

    # Denominators are explicit. Flag counts below are against captured rows,
    # i.e. rows where bull and bear are stored, so they can be compared with
    # the r distribution on the next line.
    print("all CURRENT rows     : %d" % len(rows))
    print("captured bull/bear   : %d  (rows where evidence_split is computable)"
          % len(captured))
    print("missing bull/bear    : %d  (NULL legacy ticks: NOT CAPTURED)"
          % missing_bull_bear)

    cluster_conflict = sum(1 for r in captured if r["cluster_conflict"])
    evidence_split = sum(1 for r in captured if r["evidence_split"])
    both = sum(1 for r in captured
               if r["cluster_conflict"] and r["evidence_split"])

    print("cluster_conflict = cluster collapsed opposing readings:")
    print("    ticks with the flag : %d of %d"
          % (cluster_conflict, len(captured)))
    print("evidence_split     = ratio > 0.7 placeholder threshold:")
    print("    ticks with the flag : %d of %d"
          % (evidence_split, len(captured)))
    print("overlap:")
    print("    both flags          : %d of %d" % (both, len(captured)))
    print("    cluster_conflict only: %d of %d"
          % (cluster_conflict - both, len(captured)))
    print("    evidence_split only  : %d of %d"
          % (evidence_split - both, len(captured)))

    _live_cluster_conflict_metrics(db)

    print()
    print("r = min(bull,bear)/max(bull,bear) for captured rows:")
    stored = []
    no_evidence = 0
    for r in captured:
        hi = max(r["bull"], r["bear"])
        if hi <= 0:
            no_evidence += 1
            continue
        stored.append(min(r["bull"], r["bear"]) / hi)

    print("    stored rows      : %d of %d captured rows"
          % (len(stored), len(captured)))
    print("    zero-evidence    : %d  (both totals stored as 0.0)" % no_evidence)
    if stored:
        print("    r min            : %.3f" % min(stored))
        print("    r p25            : %.3f" % _pct(stored, 25))
        print("    r p50            : %.3f" % statistics.median(stored))
        print("    r p75            : %.3f" % _pct(stored, 75))
        print("    r max            : %.3f" % max(stored))
        for threshold in (0.6, 0.7, 0.8, 0.85, 0.9):
            n = sum(1 for x in stored if x > threshold)
            print("    r>%s             : %d of %d (%.1f%%)"
                  % (threshold, n, len(stored),
                     100.0 * n / len(stored)))
    else:
        print("    no captured row has nonzero bull and bear")

    if live:
        _live_r()


def _live_cluster_conflict_metrics(db: str) -> None:
    """
    Recompute one tick on a DB copy and report the cluster-conflict share.

    Historical stored flags are not rewritten here; this shows what the new
    share-based row-level gate computes for the current captured asset rows.
    """
    import os
    import sqlite3
    import tempfile

    import app.analysis.event_clustering as event_clustering
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.config import (
        CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT,
        CLUSTER_CONFLICT_WEIGHT_SHARE,
    )
    from app.models.db_models import Base
    from app.services.pipeline import IngestStats, _recompute_sentiment

    tmp_dir = tempfile.mkdtemp(prefix="measure_cluster_")
    tmp_db = os.path.join(tmp_dir, "sentiment.db")
    try:
        src = sqlite3.connect(db)
        dst = sqlite3.connect(tmp_db)
        src.backup(dst)
        dst.close()
        src.close()

        engine = create_engine(f"sqlite:///{tmp_db}")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        real_cluster_signals = event_clustering.cluster_signals
        last = {}

        def wrapped_cluster_signals(signals, **kwargs):
            clustered = real_cluster_signals(signals, **kwargs)
            last["signals"] = clustered
            return clustered

        event_clustering.cluster_signals = wrapped_cluster_signals
        try:
            with Session() as session:
                _recompute_sentiment(session, IngestStats())
                session.commit()
        finally:
            event_clustering.cluster_signals = real_cluster_signals

        by_asset = {}
        for signal in last.get("signals", []):
            by_asset.setdefault(signal.asset.value, []).append(signal)

        shares = []
        weights = []
        flagged_rows = 0
        for signals in by_asset.values():
            total_weight = sum(
                getattr(s, "magnitude", 0.0) or 0.0 for s in signals
            )
            conflicted_weight = sum(
                getattr(s, "magnitude", 0.0) or 0.0
                for s in signals
                if getattr(s, "cluster_conflict", False)
            )
            share = conflicted_weight / total_weight if total_weight else 0.0
            shares.append(share)
            weights.append(conflicted_weight)
            if (
                total_weight >= CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT
                and share >= CLUSTER_CONFLICT_WEIGHT_SHARE
            ):
                flagged_rows += 1

        if shares:
            print()
            print("live recompute: current captured asset rows under new gate")
            print("    current_flagged_rows: %d of %d"
                  % (flagged_rows, len(shares)))
            print("    cluster_conflict mean share : %.6f"
                  % (sum(shares) / len(shares)))
            print("    cluster_conflict mean weight: %.6f"
                  % (sum(weights) / len(weights)))
    except Exception as exc:  # noqa: BLE001 - measurement must not crash
        print()
        print("live cluster-conflict metrics skipped: %s" % exc)
    finally:
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
            if os.path.isdir(tmp_dir):
                os.rmdir(tmp_dir)
        except OSError:
            pass


def _live_r() -> None:
    """Re-derive bull/bear for the current inputs and report r directly."""
    print()
    print("--- live recompute: intercepting _aggregate for every asset ---")
    import app.database as dbmod  # noqa: F401
    import app.models.db_models as models
    from app.analysis import sentiment_engine as se
    from app.services.pipeline import IngestStats, _recompute_sentiment

    import tempfile
    from datetime import datetime, timedelta, timezone

    captured: dict = {}
    orig = se._aggregate

    def spy(signals, now=None):
        agg = orig(signals, now)
        for s in signals:
            key = s.asset.value
            entry = captured.setdefault(key, {"runs": 0, "max_r": 0.0,
                                              "hit": False, "detail": None})
            entry["runs"] += 1
            bull, bear = agg["bull"], agg["bear"]
            lo, hi = min(bull, bear), max(bull, bear)
            if hi > 0:
                r = lo / hi
                if r > entry["max_r"]:
                    entry["max_r"] = r
                    entry["detail"] = (bull, bear)
                if r > 0.6:
                    entry["hit"] = True
        return agg

    with SessionLocal() as session:
        se._aggregate = spy
        try:
            _recompute_sentiment(session, IngestStats())
        finally:
            se._aggregate = orig
        session.rollback()

    print()
    print("%-8s %-10s %-14s %s" % ("asset", "max r", "dampener>0.6", "bull / bear"))
    for a in ASSETS:
        e = captured.get(a)
        if not e or not e["detail"]:
            print("%-8s %-10s" % (a, "no data"))
            continue
        bull, bear = e["detail"]
        print("%-8s %-10.3f %-14s %.4f / %.4f"
              % (a, e["max_r"], "FIRES" if e["hit"] else "no", bull, bear))


def SessionLocal():
    from app.database import SessionLocal as SL
    return SL()


# ---------------------------------------------------------------------------
# Section D
# ---------------------------------------------------------------------------
def section_d(db: str) -> None:
    _line("SECTION D - the Step 5 confidence shrink, validated")
    rows = [r for r in fetch_all_current(db)
            if r.get("raw_score") is not None and r["confidence"] is not None]

    print("rows with a raw_score (the only ones S5 could have acted on): %d"
          % len(rows))
    if not rows:
        print("none - nothing to measure")
        return

    from app.analysis.display import to_display
    from app.analysis.sentiment_engine import TIEBREAK_MAX_CONFIDENCE

    print()
    print("raw_pct = 50 + raw_score/2 ; shrunk = to_display(raw, bias, conf)")
    print("TIEBREAK_MAX_CONFIDENCE = %.2f (the shrink's full-strength gate)"
          % TIEBREAK_MAX_CONFIDENCE)
    print()

    for asset in ASSETS:
        ar = [r for r in rows if r["asset"] == asset]
        if not ar:
            continue
        deltas = []
        moved3 = moved5 = 0
        for r in ar:
            raw_pct = int(round(50 + r["raw_score"] / 2.0))
            actual_pct = int(round(50 + r["raw_score"] / 2.0))
            shown = to_display(r["raw_score"], r["bias"], r["confidence"])
            shrunk = shown["pct"]
            d = abs(raw_pct - shrunk)
            deltas.append(d)
            if d >= 3:
                moved3 += 1
            if d >= 5:
                moved5 += 1

        med = statistics.median(
            abs(50 - (50 + r["raw_score"] / 2.0)) -
            abs(50 - to_display(r["raw_score"], r["bias"],
                                r["confidence"])["pct"])
            for r in ar)

        print("--- %s (n=%d) ---" % (asset, len(ar)))
        print("    median |raw_pct-50| - |shrunk_pct-50| : %+.1f" % med)
        print("    shrink moved the integer by >=3 pts    : %d (%.1f%%)"
              % (moved3, 100.0 * moved3 / len(ar)))
        print("    shrink moved the integer by >=5 pts    : %d (%.1f%%)"
              % (moved5, 100.0 * moved5 / len(ar)))
        print("    verdict                                : %s"
              % ("cosmetic (<10% by >=3pt)" if moved3 < 0.10 * len(ar)
                 else "substantive" if moved3 <= 0.30 * len(ar)
                 else "STRONG"))


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tiers", type=int, default=TIERS_DEFAULT)
    ap.add_argument("--section", choices=list(SECTIONS), action="append",
                    help="restrict to one or more sections")
    ap.add_argument("--live", action="store_true",
                    help="also re-derive bull/bear via a live recompute")
    args = ap.parse_args(argv)

    sections = args.section or list(SECTIONS)
    db = _db_path()
    ticks = fetch_ticks(db, args.tiers)

    print("=" * 78)
    print("ENGINE MEASUREMENT  (Task 2 - measurement only, no tuning)")
    print("=" * 78)
    print("database : %s" % db)
    print("ticks    : %d" % len(ticks))
    print("sections : %s" % ", ".join(sections))

    if "A" in sections:
        section_a(ticks)
    if "B" in sections:
        section_b(db)
    if "C" in sections:
        section_c(db, args.live)
    if "D" in sections:
        section_d(db)

    print()
    print("=" * 78)
    print("DATA COVERAGE - bull/bear capture")
    print("=" * 78)
    print("bull and bear are stored on SentimentSnapshot and populated from")
    print("pipeline._recompute_sentiment. Rows written before that migration")
    print("remain NULL and are reported as NOT CAPTURED, while future ticks are")
    print("directly usable for r = min(bull,bear)/max(bull,bear).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
