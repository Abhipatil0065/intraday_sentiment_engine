import sqlite3
conn = sqlite3.connect('C:/Users/HP/.vscode/intraday_sentiment_engine/data/intraday_sentiment.db')
cursor = conn.cursor()
cursor.execute('SELECT asset, bias, score, confidence, note FROM sentiment_snapshot ORDER BY computed_at DESC LIMIT 20')
for row in cursor.fetchall():
    print(row)
conn.close()