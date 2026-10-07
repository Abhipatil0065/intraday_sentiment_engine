import sqlite3
conn = sqlite3.connect('data/intraday_sentiment.db')
cursor = conn.cursor()

# Check tables
cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
print(cursor.fetchall())

# Check news_article table
cursor.execute('SELECT COUNT(*) FROM news_article')
print('news_article count:', cursor.fetchone()[0])

cursor.execute('SELECT COUNT(*) FROM news_article WHERE is_today = 1')
print('news_article today count:', cursor.fetchone()[0])

cursor.execute('SELECT COUNT(*) FROM asset_impact')
print('asset_impact count:', cursor.fetchone()[0])