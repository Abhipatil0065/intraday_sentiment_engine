"""Read-only: simulate rolling baseline WITH no-inversion guard.

Design under test:
    base = mean(raw scores in prior window, min M ticks)
    adj  = raw - base
    guard: a strong directional read may be SOFTENED toward zero by the
    baseline but never flipped across it. Clamped to +/- FLOOR (the trust
    floor: a score inside it is a tie-break, not a verdict).

Reports bull/bear split over the last 50 ticks AND over the full history,
for several (W, M, FLOOR) combinations, so config defaults can be chosen
before any code is written.
"""
import sqlite3

con = sqlite3.connect("data/intraday_sentiment.db")
cur = con.cursor()

ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD")


def sim(asset, w, m, floor):
    scores = [r[0] for r in cur.execute(
        "SELECT score FROM bias_snapshots WHERE asset=? ORDER BY timestamp_utc",
        (asset,)).fetchall()]
    out = []
    for i, raw in enumerate(scores):
        prior = scores[max(0, i - w):i]
        if len(prior) < m:
            out.append((raw, raw, None))
            continue
        base = sum(prior) / len(prior)
        adj = raw - base
        if raw > 0 and adj < 0:
            adj = max(adj, floor)
        elif raw < 0 and adj > 0:
            adj = min(adj, -floor)
        out.append((raw, adj, base))
    return out


def split(rows):
    n = len(rows)
    bull = sum(1 for _, adj, _ in rows if adj > 0)
    bear = sum(1 for _, adj, _ in rows if adj < 0)
    flat = n - bull - bear
    return bull, bear, flat, (100.0 * bull / n if n else 0)


for floor in (12.0,):
    for w, m in ((20, 5), (20, 8), (30, 5), (15, 5)):
        print(f"\n########## FLOOR={floor}  W={w}  M={m} ##########")
        for asset in ASSETS:
            allrows = sim(asset, w, m, floor)
            last50 = allrows[-50:]
            lb = split(last50)
            fa = split(allrows)
            used = sum(1 for _, _, base in allrows if base is not None)
            print(f"  {asset}: last50 BULL={lb[0]}({lb[3]:.0f}%) BEAR={lb[1]} | "
                  f"ALL(n={len(allrows)}) BULL={fa[0]}({fa[3]:.0f}%) BEAR={fa[1]} | "
                  f"baseline used {used}/{len(allrows)} ticks")