import sqlite3
conn = sqlite3.connect('C:/Users/HP/.vscode/intraday_sentiment_engine/data/intraday_sentiment.db')
cursor = conn.cursor()
cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
tables = [row[0] for row in cursor.fetchall()]
print('Tables:', tables)
for t in ['sentiment_snapshot', 'bias_outcome', 'bias_snapshots', 'news_article']:
    cursor.execute('PRAGMA table_info(' + t + ')')
    cols = [row[1] for row in cursor.fetchall()]
    print(t + ':', cols)
conn.close()