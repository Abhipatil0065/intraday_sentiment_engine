import argparse
import os
import sqlite3

parser = argparse.ArgumentParser(description="Add llm_used column if missing.")
parser.add_argument('--db', default='data/intraday_sentiment.db',
                    help='Path to the SQLite DB (default: data/intraday_sentiment.db)')
parser.add_argument('--dry-run', action='store_true',
                    help='Only report whether the column exists; write nothing')
args = parser.parse_args()

if not os.path.exists(args.db):
    raise SystemExit(f'DB not found: {args.db}')

conn = sqlite3.connect(args.db)
c = conn.cursor()

# Check if llm_used column exists
c.execute("PRAGMA table_info(news_article)")
columns = [row[1] for row in c.fetchall()]

if 'llm_used' not in columns:
    if args.dry_run:
        print(f'{args.db}: llm_used column MISSING (would add it)')
    else:
        c.execute("ALTER TABLE news_article ADD COLUMN llm_used BOOLEAN DEFAULT 0")
        conn.commit()
        print(f"Added llm_used column to news_article in {args.db}")
else:
    print(f"{args.db}: llm_used column already exists")

conn.close()