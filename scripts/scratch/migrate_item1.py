"""Apply the Item 1 baseline columns to an existing bias_snapshots table.

SQLite ALTER TABLE ADD COLUMN is the only supported additive change here, so
this is a one-shot migration script: it is idempotent (each column is added only
if PRAGMA reports it absent) and appends nothing but columns, so existing rows
carry NULLs for the new fields until the next baseline-adjusted write.
"""
import sqlite3

DB = "data/intraday_sentiment.db"
COLS = [("raw_score", "FLOAT"), ("baseline_score", "FLOAT"),
        ("adjusted", "BOOLEAN DEFAULT 0")]

con = sqlite3.connect(DB)
cur = con.cursor()
present = {r[1] for r in cur.execute("PRAGMA table_info(bias_snapshots)")}
for name, ddl in COLS:
    if name not in present:
        cur.execute(f"ALTER TABLE bias_snapshots ADD COLUMN {name} {ddl}")
        print(f"added {name}")
    else:
        print(f"already present: {name}")
con.commit()
con.close()
print("done")