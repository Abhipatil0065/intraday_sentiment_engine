import sqlite3
conn = sqlite3.connect('C:/Users/HP/.vscode/intraday_sentiment_engine/data/intraday_sentiment.db')
cursor = conn.cursor()
cursor.execute('PRAGMA table_info(sentiment_snapshot)')
for row in cursor.fetchall():
    print(row)
conn.close()