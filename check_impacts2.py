import sqlite3
conn = sqlite3.connect('data/intraday_sentiment.db')
cursor = conn.cursor()

# Check distinct assets in asset_impact
cursor.execute('SELECT DISTINCT asset FROM asset_impact')
print('Distinct assets:', cursor.fetchall())

# Check articles with impacts
cursor.execute('SELECT a.id, a.guid, a.source, a.title, a.published_at_utc, a.llm_used FROM news_article a JOIN asset_impact ai ON a.id = ai.article_id WHERE ai.asset IN ("GOLD", "SILVER", "BITCOIN") AND a.is_today = 1 ORDER BY a.published_at_utc DESC LIMIT 20')
rows = cursor.fetchall()
for row in rows:
    print(row)