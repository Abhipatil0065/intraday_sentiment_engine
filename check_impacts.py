import sqlite3
conn = sqlite3.connect('data/intraday_sentiment.db')
cursor = conn.cursor()

# Get all articles with their impacts for XAUUSD, XAGUSD, BTCUSD
query = '''
SELECT 
    a.guid, a.source, a.title, a.published_at_utc, a.llm_used,
    ai.asset, ai.direction, ai.strength, ai.relevance, ai.persistence,
    ai.reason, ai.transmission, ai.importance_weight, ai.session
FROM news_article a
JOIN asset_impact ai ON a.id = ai.article_id
WHERE ai.asset IN ('GOLD', 'SILVER', 'BITCOIN')
AND a.is_today = 1
ORDER BY a.published_at_utc DESC
'''
cursor.execute(query)
rows = cursor.fetchall()
for row in rows[:50]:
    print(row)