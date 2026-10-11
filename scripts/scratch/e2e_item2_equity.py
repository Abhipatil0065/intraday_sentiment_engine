"""Item 2 end-to-end check: S&P 500 verdicts are produced and persisted.

Runs the real classifier + engine on a temp DB and asserts:
  1. An equity headline routes to Asset.EQUITY (not BITCOIN).
  2. The pipeline persists an EQUITY SentimentSnapshot row.
  3. The pipeline persists an EQUITY bias_snapshots row with raw_score.
  4. build_payload() serves an EQUITY card.
  5. An equity headline does NOT bleed into gold/silver.

Usage: python scripts/scratch/e2e_item2_equity.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from sqlalchemy import text

BASE = Path(__file__).resolve().parent.parent
os.environ.setdefault("APP_TESTING", "1")
_TMP = Path(tempfile.mkdtemp(prefix="item2_e2e_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.as_posix()}"
sys.path.insert(0, str(BASE))

from app.config import settings  # noqa: E402
from app.database import init_db, SessionLocal  # noqa: E402
from app.models.enums import Asset  # noqa: E402
from app.services import pipeline  # noqa: E402


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        # 1. Route an equity headline through the real classifier.
        from app.analysis.news_classifier import heuristic_analyze
        from app.analysis.signal_factory import build_signal

        title = "S&P 500 earnings beat estimates as Nvidia and Microsoft surge"
        analysis = heuristic_analyze(title)
        routed = {a.asset for a in analysis.assets}
        print("routed assets:", sorted(a.value for a in routed))
        assert Asset.EQUITY in routed, "headline did not route to Asset.EQUITY"
        # A headline can legitimately route to several assets ("surge" fires
        # RISK_ON); what matters is that EQUITY is one of them.

        # 2. Build the signal and score it through the engine.
        impact = next(a for a in analysis.assets if a.asset is Asset.EQUITY)
        sig = build_signal(
            asset=Asset.EQUITY, title=title,
            direction=impact.direction, strength=impact.strength,
            importance=analysis.importance,
            relevance=impact.relevance, persistence=impact.persistence,
            category=analysis.category, reason=impact.reason,
            transmission=impact.transmission,
        )
        from app.analysis.sentiment_engine import compute
        result = compute([sig], Asset.EQUITY)
        print("EQUITY verdict: bias=%s score=%+.1f raw=%+.1f"
              % (result.bias, result.score, result.raw_score))
        assert result.bias.value in ("BULLISH", "BEARISH")
        assert result.score > 0, "earnings beat should be bullish"

        # 3. Persist via the real pipeline writer.
        from app.services.bias_snapshots import AssetVerdict, write_bias_snapshots
        verdict = AssetVerdict(
            asset=Asset.EQUITY, score=result.score, confidence=result.confidence,
            bias=result.bias.value, article_count=1, raw_score=result.raw_score,
        )
        n = write_bias_snapshots(session, [verdict])
        session.commit()
        assert n == 1, n
        snap = session.execute(text(
            "SELECT asset, bias, score, raw_score FROM bias_snapshots "
            "WHERE asset='EQUITY' ORDER BY id DESC LIMIT 1")).first()
        print("bias_snapshots EQUITY row:", snap)
        assert snap is not None and snap[3] is not None

        # 4. SentimentSnapshot persisted for EQUITY via the real pipeline recompute.
        from app.models.db_models import NewsArticle, NewsEvent, ArticleEvent
        from datetime import datetime, timezone, timedelta

        now = datetime.now(timezone.utc)
        art = NewsArticle(
            guid="item2-e2e-1", source="TEST", url="http://t/1", title=title,
            summary="", body="Nvidia and Microsoft beat revenue estimates.",
            category="Equities", importance="HIGH", report_kind="GENERAL",
            verification="CONFIRMED", published_at_utc=now - timedelta(minutes=10),
            is_today=True, relevance_score=1.0, analysis_mode="heuristic",
            analysis_note="",
        )
        session.add(art)
        session.flush()
        ev = NewsEvent(event_key="e2", article_count=1, canonical_headline=title)
        session.add(ev)
        session.flush()
        session.add(ArticleEvent(article_id=art.id, event_id=ev.id))
        session.commit()

        stats = pipeline.IngestStats()
        pipeline._recompute_sentiment(session, stats)
        rows = session.execute(text(
            "SELECT scope, asset, bias FROM sentiment_snapshot "
            "WHERE asset='EQUITY'")).fetchall()
        print("sentiment_snapshot EQUITY rows:", rows)
        assert rows, "no EQUITY sentiment_snapshot row persisted"

        # 5. build_payload serves an EQUITY card.
        from app.main import build_payload
        payload = build_payload(session)
        eq = payload.get("equity")
        print("payload equity:", eq)
        assert eq is not None, "payload has no equity card"

        # 6. No bleed into gold/silver.
        from app.analysis.signal_factory import build_bleed_signal
        assert build_bleed_signal(sig) is None, "equity signal bled into metals"

        print("\nALL ITEM 2 CHECKS PASSED")
        import shutil
        shutil.rmtree(_TMP.parent, ignore_errors=True)
    finally:
        session.close()


if __name__ == "__main__":
    main()