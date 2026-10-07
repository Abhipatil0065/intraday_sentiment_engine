from app.models.db_models import SentimentSnapshot, NewsArticle
from app.database import SessionLocal
from datetime import datetime, timezone, timedelta

session = SessionLocal()
today_start = datetime(2026, 10, 7, 0, 0, 0, tzinfo=timezone.utc)
today_end = datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc)

for asset in ['XAUUSD', 'XAGUSD', 'BTCUSD']:
    snap = session.query(SentimentSnapshot).filter(
        SentimentSnapshot.asset == asset,
        SentimentSnapshot.scope == 'CURRENT',
        SentimentSnapshot.computed_at >= today_start,
        SentimentSnapshot.computed_at < today_end
    ).order_by(SentimentSnapshot.computed_at.desc()).first()

    if not snap:
        print(f'{asset}: No snapshot found')
        continue

    print(f'=== {asset} (score={snap.score:.1f}, bias={snap.bias}, conf={snap.confidence:.2f}) ===')

    all_drivers = snap.bull_drivers + snap.bear_drivers
    all_drivers.sort(key=lambda d: abs(d.get('contribution', 0)), reverse=True)

    print(f'{"#":>3} | {"Source":<20} | {"Published":<20} | {"Title":<60} | {"Dir":<8} | {"Weight":>8} | {"LLM":>4}')
    print('-' * 140)

    for i, d in enumerate(all_drivers[:10]):
        title = d.get('title', '')[:58]
        art = session.query(NewsArticle).filter(NewsArticle.title.like(f'%{title[:30]}%')).first()
        if art:
            src = art.source[:18]
            pub = art.published_at_utc.strftime('%H:%M:%S') if art.published_at_utc else 'N/A'
            direction = d.get('direction', '?')
            weight = d.get('weight', 0)
            llm = 'Y' if art.llm_used else 'N'
        else:
            src = 'Unknown'
            pub = 'N/A'
            direction = d.get('direction', '?')
            weight = d.get('weight', 0)
            llm = '?'
        print(f'{i+1:>3} | {src:<20} | {pub:<20} | {title:<60} | {direction:<8} | {weight:>8.4f} | {llm:>4}')

    print()

session.close()