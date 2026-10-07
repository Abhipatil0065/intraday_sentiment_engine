import sqlite3

conn = sqlite3.connect('data/intraday_sentiment.db')
c = conn.cursor()

# Check if llm_used column exists
c.execute("PRAGMA table_info(news_article)")
columns = [row[1] for row in c.fetchall()]

if 'llm_used' not in columns:
    c.execute("ALTER TABLE news_article ADD COLUMN llm_used BOOLEAN DEFAULT 0")
    conn.commit()
    print("Added llm_used column to news_article")
else:
    print("llm_used column already exists")

conn.close()