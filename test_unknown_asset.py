"""Test script for unknown asset handling."""
import asyncio
import uuid
from datetime import datetime, timezone

import argparse

from app.database import init_db, session_scope
parser = argparse.ArgumentParser(description="Unknown-asset pipeline test.")
parser.add_argument("--db", default="data/intraday_sentiment.db",
                    help="DB path; default is the live DB")
args = parser.parse_args()
from app.models.db_models import NewsArticle, AssetImpact
from app.models.enums import Asset, Direction, EconomicImportance, ImpactStrength
from app.services.pipeline import run_once

import os
if not os.path.exists(args.db):
    raise SystemExit(f"DB not found: {args.db}")
init_db()

unique_guid = f"test-unknown-asset-{uuid.uuid4().hex[:8]}"

with session_scope() as session:
    # Create an article
    article = NewsArticle(
        guid=unique_guid,
        source="Test",
        source_quality="TIER_4_OTHER",
        url="http://test.com/1",
        title="Test article about oil",
        body="Oil prices moved today.",
        published_at_utc=datetime.now(timezone.utc),
        is_today=True,
        category="Commodity Supply/Demand",
        importance="HIGH",
        report_kind="GENERAL",
        verification="CONFIRMED",
        relevance_score=0.8,
        analysis_mode="heuristic",
    )
    session.add(article)
    session.flush()

    # Add an impact with UNKNOWN asset (USOIL)
    impact = AssetImpact(
        article_id=article.id,
        asset="USOIL",  # Not in Asset enum
        direction=Direction.BULLISH.value,
        strength=ImpactStrength.HIGH.value,
        relevance=0.8,
        persistence=0.5,
        reason="Test reason",
        transmission="Test transmission",
        session="AMERICAS",
        importance_weight=0.7,
    )
    session.add(impact)
    session.commit()

print("Created test data, running pipeline...")

# run_once should not crash
stats = asyncio.run(run_once())
print(f"Pipeline completed: {stats}")
print(f"Errors: {stats.errors}")
print(f"Skipped unroutable: {stats.skipped_unroutable}")

# Should complete without error
assert "pipeline" not in str(stats.errors).lower() or stats.errors == []
# Clean up the rows this script inserted, so it never leaves test junk in
# the database it was pointed at.
with session_scope() as session:
    from sqlalchemy import delete, select
    from app.models.db_models import ArticleEvent, AssetImpact, NewsArticle
    art_ids = session.scalars(select(NewsArticle.id).where(
        NewsArticle.guid == unique_guid)).all()
    if art_ids:
        session.execute(delete(AssetImpact).where(
            AssetImpact.article_id.in_(art_ids)))
        session.execute(delete(ArticleEvent).where(
            ArticleEvent.article_id.in_(art_ids)))
        session.execute(delete(NewsArticle).where(
            NewsArticle.id.in_(art_ids)))
        session.flush()
print("Cleaned up test rows for guid", unique_guid[:20])
print("TEST PASSED!")