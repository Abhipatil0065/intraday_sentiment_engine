"""Read-only: raw score distribution per asset for Item 1 baseline design."""
import sqlite3

con = sqlite3.connect("data/intraday_sentiment.db")
cur = con.cursor()

for asset in ("XAUUSD", "XAGUSD", "BTCUSD"):
    rows = [r[0] for r in cur.execute(
        "SELECT score FROM bias_snapshots WHERE asset=? ORDER BY timestamp_utc",
        (asset,)).fetchall()]
    n = len(rows)
    if not n:
        continue
    mean = sum(rows) / n
    mn, mx = min(rows), max(rows)
    pos = sum(1 for s in rows if s > 0)
    neg = sum(1 for s in rows if s < 0)
    zero = n - pos - neg
    # deciles
    srt = sorted(rows)
    deciles = [srt[int(i * n / 10)] for i in range(10)]
    print(f"{asset}: n={n} mean={mean:+.1f} min={mn:+.1f} max={mx:+.1f} "
          f"pos={pos}({100*pos/n:.0f}%) neg={neg}({100*neg/n:.0f}%) zero={zero}")
    print(f"   deciles(low->high): {[f'{d:+.1f}' for d in deciles]}")

# how many ticks per asset (distinct timestamps)
for asset in ("XAUUSD", "XAGUSD", "BTCUSD"):
    r = cur.execute(
        "SELECT COUNT(DISTINCT timestamp_utc)) FROM bias_snapshots WHERE asset=?",
        (asset,)).fetchone()
    print(asset, "distinct ticks:", r[0])

# gap between consecutive ticks (to size the rolling window)
import datetime
ts = [r[0] for r in cur.execute(
    "SELECT DISTINCT timestamp_utc FROM bias_snapshots WHERE asset='XAUUSD' ORDER BY timestamp_utc").fetchall()]
gaps = []
for a, b in zip(ts, ts[1:]):
    da = datetime.datetime.fromisoformat(a)
    db = datetime.datetime.fromisoformat(b)
    gaps.append((db - da).total_seconds() / 60.0)
print("XAUUSD tick count:", len(ts))
print("median gap min:", sorted(gaps)[len(gaps)//2] if gaps else None)
print("min gap min:", min(gaps) if gaps else None)
print("max gap min:", max(gaps) if gaps else None)
print("p90 gap min:", sorted(gaps)[int(len(gaps)*0.9)] if gaps else None)