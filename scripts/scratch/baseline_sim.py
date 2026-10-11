"""Read-only: simulate rolling baseline adjustment on the last 50 ticks per asset.

Replicates the Item 1 design: baseline = mean of raw scores from the most recent
prior persisted snapshots (window W, minimum M), adjusted = raw - baseline_mean.
Reports the resulting bull/bear split so we can check for inversion before
committing to config defaults.
"""
import sqlite3

con = sqlite3.connect("data/intraday_sentiment.db")
cur = con.cursor()

W = 20   # window
M = 5    # min ticks


def sim(asset, w=W, m=M, damp=1.0):
    rows = cur.execute(
        "SELECT score FROM bias_snapshots WHERE asset=? "
        "ORDER BY timestamp_utc", (asset,)).fetchall()
    scores = [r[0] for r in rows]
    out = []
    for i, raw in enumerate(scores):
        prior = scores[max(0, i - w):i]
        if len(prior) < m:
            out.append((raw, raw, None))
            continue
        base = sum(prior) / len(prior)
        adj = damp * (raw - base)
        out.append((raw, adj, base))
    return out


for asset in ("XAUUSD", "XAGUSD", "BTCUSD"):
    allrows = sim(asset)
    last50 = allrows[-50:]
    bull = sum(1 for _, adj, _ in last50 if adj > 0)
    bear = sum(1 for _, adj, _ in last50 if adj < 0)
    zero = len(last50) - bull - bear
    print(f"\n=== {asset} (last 50 ticks, W={W}, M={M}, damp=1.0) ===")
    print(f"  BULLISH={bull} ({100*bull/len(last50):.0f}%)  BEARISH={bear} ({100*bear/len(last50):.0f}%)  flat={zero}")
    # how many used a baseline vs raw
    used = sum(1 for _, _, base in last50 if base is not None)
    print(f"  ticks with baseline: {used}/{len(last50)}")
    for raw, adj, base in last50:
        tag = "raw" if base is None else f"base={base:+.1f}"
        print(f"    raw={raw:+6.1f}  adj={adj:+6.1f}  ({tag})")