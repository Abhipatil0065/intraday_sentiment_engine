import sqlite3
conn = sqlite3.connect('data/intraday_sentiment.db')
cursor = conn.cursor()

# Check sentiment_snapshot for current scores
cursor.execute('''
    SELECT asset, scope, score, confidence, bias, mode, drivers, bull_drivers, bear_drivers, contributing_articles, computed_at
    FROM sentiment_snapshot
    WHERE scope = 'CURRENT'
    ORDER BY computed_at DESC
    LIMIT 10
''')
rows = cursor.fetchall()
for row in rows:
    print(f"Asset: {row[0]}, Scope: {row[1]}, Score: {row[2]}, Conf: {row[3]}, Bias: {row[4]}, Mode: {row[5]}, Articles: {row[9]}, Time: {row[10]}")
    print(f"  Drivers: {row[6]}")
    print(f"  Bull: {row[7]}")
    print(f"  Bear: {row[8]}")
    print()