import sqlite3

conn = sqlite3.connect('data/intraday_sentiment.db')
c = conn.cursor()

# Check asset_impact for USOIL
c.execute("SELECT COUNT(*) FROM asset_impact WHERE asset='USOIL'")
print(f"USOIL count: {c.fetchone()[0]}")

# Check max computed_at and published_at_utc
c.execute('SELECT MAX(computed_at) FROM sentiment_snapshot')
print(f"MAX computed_at: {c.fetchone()[0]}")

c.execute('SELECT MAX(published_at_utc) FROM news_article')
print(f"MAX published_at_utc: {c.fetchone()[0]}")

# Check last_updated for all 3 assets
c.execute('''
    SELECT asset, scope, computed_at
    FROM sentiment_snapshot
    WHERE asset IN ('XAUUSD', 'XAGUSD', 'BTCUSD')
    ORDER BY asset, scope
''')
for row in c.fetchall():
    print(f"{row[0]} {row[1]}: computed_at={row[2]}")

conn.close()