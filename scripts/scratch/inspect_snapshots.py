"""Read-only inspection of bias_snapshots for Item 1 baseline design."""
import sqlite3

con = sqlite3.connect("data/intraday_sentiment.db")
cur = con.cursor()

cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("TABLES:", [r[0] for r in cur.fetchall()])

cur.execute("SELECT COUNT(*) FROM bias_snapshots")
print("total bias_snapshots:", cur.fetchone()[0])

cur.execute("SELECT asset, COUNT(*) FROM bias_snapshots GROUP BY asset")
print("per asset:", cur.fetchall())

cur.execute("SELECT bias, COUNT(*) FROM bias_snapshots GROUP BY bias")
print("per bias:", cur.fetchall())

cur.execute("SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM bias_snapshots")
print("range:", cur.fetchone())

cur.execute("PRAGMA table_info(bias_snapshots)")
print("columns:", [r[1] for r in cur.fetchall()])

cur.execute("SELECT asset, bias, score, confidence, timestamp_utc FROM bias_snapshots ORDER BY asset, timestamp_utc LIMIT 12")
for r in cur.fetchall():
    print(r)