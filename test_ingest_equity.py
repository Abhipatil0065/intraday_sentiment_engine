import os, sys, tempfile
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("APP_TESTING", "1")
p = Path(tempfile.mkdtemp()) / "t.db"
os.environ["DATABASE_URL"] = f"sqlite:///{p.as_posix()}"

from app.database import init_db, session_scope
from app.ingestion.base import RawArticle
from app.services.pipeline import _ingest_article, IngestStats
from sqlalchemy import select
from app.models.db_models import AssetImpact

init_db()
art = RawArticle(
    guid="test-equity-1", source="Reuters", source_quality="TIER_2_MAJOR",
    url="http://t/1", title="S&P 500 earnings beat estimates as Nvidia and Microsoft surge",
    body="Nvidia and Microsoft beat revenue estimates; the S&P 500 rises on strong corporate earnings.",
    published_raw="2026-10-10T12:00:00+00:00",
)

async def go():
    with session_scope() as s:
        stats = IngestStats()
        row = await _ingest_article(art, s, stats, use_llm=False)
        print("ingest returned:", row.id if row else None)
        print("stats.new_today:", stats.new_today)
    with session_scope() as s:
        rows = s.scalars(select(AssetImpact)).all()
        print("asset_impact rows:", len(rows))
        for r in rows:
            print("  asset=%s direction=%s relevance=%.2f" % (r.asset, r.direction, r.relevance))

import asyncio
asyncio.run(go())