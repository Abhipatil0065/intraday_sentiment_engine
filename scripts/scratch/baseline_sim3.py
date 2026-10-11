"""Read-only: simulate baseline as the MEDIAN of prior raw scores (robust to
the extreme negative clusters that dominate BTCUSD's mean).

adjusted = raw - median(prior window, min M ticks)
No inversion guard: a relative read is the design intent.
Reports bull/bear split over the last 50 ticks and full history.
"""
import sqlite3
import statistics

con = sqlite3.connect("data/intraday_sentiment.db")
cur = con.cursor()
ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD")


def sim(asset, w, m):
    scores = [r[0] for r in cur.execute(
        "SELECT score FROM bias_snapshots WHERE asset=? ORDER BY timestamp_utc",
        (asset,)).fetchall()]
    out = []
    for i, raw in enumerate(scores):
        prior = scores[max(0, i - w):i]
        if len(prior) < m:
            out.append((raw, raw, None))
            continue
        base = statistics.median(prior)
        out.append((raw, raw - base, base))
    return out


def split(rows):
    n = len(rows)
    bull = sum(1 for _, adj, _ in rows if adj > 0)
    bear = sum(1 for _, adj, _ in rows if adj < 0)
    return bull, bear, (100.0 * bull / n if n else 0)


for w, m in ((20, 5), (30, 8), (40, 10), (15, 5)):
    print(f"\n########## MEDIAN baseline  W={w}  M={m} ##########")
    for asset in ASSETS:
        allrows = sim(asset, w, m)
        last50 = allrows[-50:]
        lb = split(last50)
        fa = split(allrows)
        used = sum(1 for _, _, base in allrows if base is not None)
        print(f"  {asset}: last50 BULL={lb[0]}({lb[2]:.0f}%) BEAR={lb[1]} | "
              f"ALL(n={len(allrows)}) BULL={fa[0]}({fa[2]:.0f}%) BEAR={fa[1]} | "
              f"baseline used {used}/{len(allrows)} ticks")