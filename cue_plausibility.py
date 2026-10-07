"""
Cue Plausibility Analysis - Top 10 contributing articles for each asset
"""
import sqlite3
from collections import defaultdict

conn = sqlite3.connect('data/intraday_sentiment.db')
cursor = conn.cursor()

# Get all impacts for the three assets
query = '''
SELECT 
    a.guid, a.source, a.title, a.published_at_utc, a.llm_used,
    ai.asset, ai.direction, ai.strength, ai.relevance, ai.persistence,
    ai.reason, ai.transmission, ai.importance_weight, ai.session,
    ai.id as impact_id
FROM news_article a
JOIN asset_impact ai ON a.id = ai.article_id
WHERE ai.asset IN ('XAUUSD', 'XAGUSD', 'BTCUSD')
AND a.is_today = 1
ORDER BY a.published_at_utc DESC
'''
cursor.execute(query)
rows = cursor.fetchall()

# Calculate contribution for each impact
# contribution = weight * STRENGTH_MULT * direction_sign
# where weight = decay * relevance * persistence * source_quality * session * corroboration * surprise * tier
# This is complex to replicate exactly, but we can approximate using the stored data

# Let's use the sentiment_snapshot drivers which have the actual contributions
# But we need to show the top 10 ARTICLES, not just the top 3 drivers

# Get all articles with their impacts and calculate approximate contributions
articles_by_asset = defaultdict(list)

for row in rows:
    guid, source, title, published_at_utc, llm_used, asset, direction, strength, relevance, persistence, reason, transmission, importance_weight, session, impact_id = row
    
    # Strength multiplier
    strength_mult = {
        'VERY_HIGH': 1.00,
        'HIGH': 0.72,
        'MEDIUM': 0.45,
        'LOW': 0.20,
    }.get(strength, 0.45)
    
    # Direction sign
    dir_sign = 1.0 if direction == 'BULLISH' else (-1.0 if direction == 'BEARISH' else 0.0)
    
    # Approximate contribution (simplified)
    contribution = relevance * persistence * importance_weight * strength_mult * dir_sign
    
    articles_by_asset[asset].append({
        'guid': guid[:8],
        'source': source,
        'title': title[:80],
        'published_at_utc': published_at_utc,
        'llm_used': bool(llm_used),
        'direction': direction,
        'strength': strength,
        'relevance': relevance,
        'persistence': persistence,
        'importance_weight': importance_weight,
        'contribution': contribution,
    })

# For each asset, sort by absolute contribution and show top 10
for asset in ['XAUUSD', 'XAGUSD', 'BTCUSD']:
    articles = articles_by_asset[asset]
    articles.sort(key=lambda x: abs(x['contribution']), reverse=True)
    
    print(f"\n{'='*100}")
    print(f"TOP 10 CONTRIBUTING ARTICLES FOR {asset}")
    print(f"{'='*100}")
    print(f"{'#':<3} {'GUID':<10} {'SOURCE':<25} {'PUBLISHED':<20} {'DIR':<10} {'WGT':>8} {'LLM':<5} {'TITLE'}")
    print(f"{'-'*100}")
    
    for i, art in enumerate(articles[:10], 1):
        llm_str = "YES" if art['llm_used'] else "NO"
        print(f"{i:<3} {art['guid']:<10} {art['source']:<25} {art['published_at_utc']:<20} {art['direction']:<10} {art['contribution']:>8.4f} {llm_str:<5} {art['title']}")

# Now let's also show what dominates each score from the sentiment_snapshot
print(f"\n\n{'='*100}")
print("SCORE DOMINANCE ANALYSIS FROM SNAPSHOTS")
print(f"{'='*100}")

cursor.execute('''
    SELECT asset, scope, score, confidence, bias, bull_drivers, bear_drivers, contributing_articles
    FROM sentiment_snapshot
    WHERE scope = 'CURRENT'
    ORDER BY computed_at DESC
    LIMIT 3
''')
for row in cursor.fetchall():
    asset, scope, score, conf, bias, bull_drivers, bear_drivers, n_articles = row
    import json
    bull = json.loads(bull_drivers) if bull_drivers else []
    bear = json.loads(bear_drivers) if bear_drivers else []
    
    print(f"\n{asset} ({bias}): score={score:.1f}, conf={conf:.2f}, articles={n_articles}")
    print(f"  Bull drivers (top 3):")
    for d in bull[:3]:
        print(f"    {d['contribution']:+.4f}  {d['title'][:70]}")
    print(f"  Bear drivers (top 3):")
    for d in bear[:3]:
        print(f"    {d['contribution']:+.4f}  {d['title'][:70]}")
    
    # Calculate net bull/bear
    bull_sum = sum(d['contribution'] for d in bull)
    bear_sum = sum(abs(d['contribution']) for d in bear)
    print(f"  Bull sum: {bull_sum:.4f}, Bear sum: {bear_sum:.4f}, Net: {bull_sum - bear_sum:.4f}")
    print(f"  Score formula: 100 * (bull - bear) / (bull + bear) = 100 * ({bull_sum:.4f} - {bear_sum:.4f}) / ({bull_sum + bear_sum:.4f}) = {100 * (bull_sum - bear_sum) / (bull_sum + bear_sum):.1f}")

conn.close()