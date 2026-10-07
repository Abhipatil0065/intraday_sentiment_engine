import sys
sys.path.insert(0, r'C:\Users\HP\.vscode\intraday_sentiment_engine')

from app.database import init_db, session_scope
from app.models.db_models import SentimentSnapshot
from app.models.enums import Asset
from sqlalchemy import func

init_db()

with session_scope() as session:
    # Get latest snapshots for each asset
    for asset in [Asset.GOLD, Asset.SILVER, Asset.BITCOIN]:
        snap = session.query(SentimentSnapshot).filter(
            SentimentSnapshot.asset == asset
        ).order_by(SentimentSnapshot.computed_at.desc()).first()
        
        if snap:
            print(f"{asset.label}: score={snap.score:.2f} conf={snap.confidence:.2f} bias={snap.bias} drivers={snap.drivers} computed_at={snap.computed_at}")
        else:
            print(f"{asset.label}: NO SNAPSHOT")

    # Also get Oct 6 snapshots (if any)
    print("\n--- Oct 6 snapshots ---")
    from datetime import datetime, timezone, timedelta
    oct6_start = datetime(2026, 10, 6, 0, 0, 0, tzinfo=timezone.utc)
    oct6_end = datetime(2026, 10, 7, 0, 0, 0, tzinfo=timezone.utc)
    
    for asset in [Asset.GOLD, Asset.SILVER, Asset.BITCOIN]:
        snap = session.query(SentimentSnapshot).filter(
            SentimentSnapshot.asset == asset,
            SentimentSnapshot.computed_at >= oct6_start,
            SentimentSnapshot.computed_at < oct6_end
        ).order_by(SentimentSnapshot.computed_at.desc()).first()
        
        if snap:
            print(f"{asset.label}: score={snap.score:.2f} conf={snap.confidence:.2f} bias={snap.bias} computed_at={snap.computed_at}")
        else:
            print(f"{asset.label}: NO OCT 6 SNAPSHOT")