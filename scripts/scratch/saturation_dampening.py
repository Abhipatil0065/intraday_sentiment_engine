"""
SATURATION AT -100: analysis + PROPOSED dampening (not applied).

Answers three questions for the two real-data sets:
  1. How is the score computed when one side (bull) is ~0?
  2. Do evidence volume or confidence enter the score?
  3. What would the PROPOSED dampening
         damped = raw * total_weight / (total_weight + k)
         total_weight = bull + bear
     score the Fed window and the live set at, for a grid of k?

Read-only. Nothing is written to the database and no production
code path is changed.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, r"C:\Users\HP\.vscode\intraday_sentiment_engine")
sys.path.insert(0, r"C:\Users\HP\.vscode\intraday_sentiment_engine"
                    r"\scripts\scratch")

from replay_window import ASSETS, build_signals, parse_window
from app.database import init_db, session_scope
from app.analysis.event_clustering import event_anchor
from app.analysis.sentiment_engine import (
    _aggregate, _score_from, compute, clear_bias_history,
)

K_GRID = (0.25, 0.5, 1.0, 2.0)


def damped(raw: float, total_weight: float, k: float) -> float:
    """PROPOSED dampening - NOT applied anywhere in production."""
    if total_weight <= 0:
        return 0.0
    return round(raw * total_weight / (total_weight + k), 1)


def score_set(signals, asset, label):
    """One ON/OFF scoring pass for one asset; returns the numbers."""
    clear_bias_history()
    res_off = compute(signals, asset)
    agg_off = _aggregate(signals)
    clear_bias_history()
    clustered = __import__(
        "app.analysis.event_clustering", fromlist=["cluster_signals"]
    ).cluster_signals(list(signals), window_hours=0.5)
    res_on = compute(clustered, asset)
    agg_on = _aggregate(clustered)
    return {
        "label": label,
        "asset": asset.value,
        "n_off": agg_off["used"],
        "n_on": agg_on["used"],
        "clusters_off": agg_off["clusters"],
        "clusters_on": agg_on["clusters"],
        "raw_off": res_off.raw_score,
        "raw_on": res_on.raw_score,
        "bull_off": agg_off["bull"],
        "bear_off": agg_off["bear"],
        "bull_on": agg_on["bull"],
        "bear_on": agg_on["bear"],
        "dirless_on": agg_on["directionless"],
        "conf_off": res_off.confidence,
        "conf_on": res_on.confidence,
    }


def report(rows):
    for r in rows:
        print(f"\n--- {r['asset']} | {r['label']} ---")
        print(f"  OFF: raw={r['raw_off']:+7.2f}  articles={r['n_off']}  "
              f"clusters={r['clusters_off']}  conf={r['conf_off']:.3f}")
        print(f"       bull={r['bull_off']:.6f}  bear={r['bear_off']:.6f}  "
              f"total={r['bull_off'] + r['bear_off']:.6f}")
        print(f"  ON : raw={r['raw_on']:+7.2f}  articles={r['n_on']}  "
              f"clusters={r['clusters_on']}  conf={r['conf_on']:.3f}")
        print(f"       bull={r['bull_on']:.6f}  bear={r['bear_on']:.6f}  "
              f"total={r['bull_on'] + r['bear_on']:.6f}  "
              f"dirless={r['dirless_on']:.6f}")
        print(f"  raw DELTA (ON - OFF): {r['raw_on'] - r['raw_off']:+.2f}")
        tw_off = r["bull_off"] + r["bear_off"]
        tw_on = r["bull_on"] + r["bear_on"]
        print("  PROPOSED dampening  raw x total/(total+k):")
        for k in K_GRID:
            d_off = damped(r["raw_off"], tw_off, k)
            d_on = damped(r["raw_on"], tw_on, k)
            print(f"    k={k:<4}  OFF={d_off:+7.1f}  ON={d_on:+7.1f}  "
                  f"delta={d_on - d_off:+.1f}")


def main():
    init_db()

    print("=" * 78)
    print("SET 1 - FED WINDOW 2026-10-07T17:30 .. 19:30 UTC")
    print("=" * 78)
    with session_scope() as session:
        _, all_signals = build_signals(
            session, *parse_window("2026-10-07T17:30", "2026-10-07T19:30"))

    # The 7-article Fed/FOMC-minutes set: every signal whose headline
    # anchors on the fed_minutes event key.
    fed_only = [s for s in all_signals if event_anchor(s.title) == "fed_minutes"]
    print(f"full window signals: {len(all_signals)}  "
          f"fed-minutes-anchored signals: {len(fed_only)}")
    for asset in ASSETS:
        n = len([s for s in fed_only if s.asset is asset])
        dirs = {s.direction.value for s in fed_only if s.asset is asset}
        print(f"  {asset.value}: {n} signals, directions={sorted(dirs)}")

    rows = []
    for asset in ASSETS:
        rows.append(score_set(
            [s for s in fed_only if s.asset is asset], asset,
            "fed-minutes set ONLY (the 7 articles)"))
    report(rows)

    rows = []
    for asset in ASSETS:
        rows.append(score_set(
            [s for s in all_signals if s.asset is asset], asset,
            "full window (all 80 articles)"))
    report(rows)

    print("\n" + "=" * 78)
    print("SET 2 - LIVE SET 2026-10-08T04:15 .. 05:15 UTC "
          "(latest stored hour)")
    print("=" * 78)
    with session_scope() as session:
        _, live_signals = build_signals(
            session, *parse_window("2026-10-08T04:15", "2026-10-08T05:15"))
    print(f"live signals: {len(live_signals)}")
    for asset in ASSETS:
        n = len([s for s in live_signals if s.asset is asset])
        dirs = {}
        for s in live_signals:
            if s.asset is asset:
                dirs[s.direction.value] = dirs.get(s.direction.value, 0) + 1
        print(f"  {asset.value}: {n} signals, directions={dirs}")
    rows = []
    for asset in ASSETS:
        rows.append(score_set(
            [s for s in live_signals if s.asset is asset], asset,
            "live set"))
    report(rows)

    print("\n" + "=" * 78)
    print("HOW THE SCORE IS COMPUTED WHEN bull IS ~0")
    print("=" * 78)
    print("""
score = _score_from(bull, bear) = round(100 * (bull - bear) / (bull + bear), 1)

  * bull = 0 and bear > 0  ->  100 * (-bear) / bear = -100.0 EXACTLY,
    for ANY bear > 0. One article at weight 0.001 scores the same
    -100 as forty articles at weight 0.4. The formula is a pure RATIO:
    it is invariant to scaling both sides, so evidence VOLUME never
    enters the score.
  * Confidence does not enter the score either. _confidence_from()
    blends conviction (= |bull-bear|/directional, which is 1.0 when one
    side is zero), breadth (= used/8, an ARTICLE COUNT), agreement and
    certainty (= directional / (directional + directionless)). It sees
    how many articles and how one-sided they are - never how heavy.
  * Consequences, both visible in the Fed window:
      - the 7-article Fed-minutes set (4 BEARISH + 3 MIXED, no
        BULLISH) scores -100.0 on all three assets whether clustering
        is ON or OFF, because bull stays 0.0;
      - the ON/OFF score delta is 0.0 for that set even though the
        bear total moves (0.7228 -> 0.7143 on XAUUSD): a ratio with
        a zero numerator-side cannot move.
  * The full window (which adds 3 BULLISH signals, bull = 0.0023 on
    XAUUSD) is no longer saturated: the same clustering change moves
    the score by +0.1 to +0.2.

PROPOSED DAMPENING (NOT APPLIED - awaiting your decision):

  damped = raw * total_weight / (total_weight + k),
  total_weight = bull + bear, k > 0 a scale constant in the same
  units as the weights (single-event contributions in these windows
  run ~0.05-0.40, so k in [0.25, 2.0] is the interesting range).

  Properties:
    * sign-preserving: the binary BULLISH/BEARISH label (sign of the
      raw score) is unchanged - only the magnitude is shrunk;
    * volume-aware: |score| grows with total_weight and saturates to
      the raw score only for very large totals;
    * it breaks the scale-invariance, so the ON/OFF delta becomes
      non-zero for the Fed window (see the k-grid above): clustering
      changes the bear total, and the damped score can now see it;
    * k = 0 is the identity (today's behaviour).
  Open questions for your decision: the value of k, and whether the
  damped score should also feed the confidence/breadth terms or only
  the reported score.
""")


if __name__ == "__main__":
    main()
