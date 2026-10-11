"""Item 6 verification: calibration labels (data collection only, no auto-tuning).

Read-only against a TEMP database so the live data/intraday_sentiment.db is
never touched. Asserts:
  1. bias_outcome table exists with the expected columns.
  2. A label can be recorded against a bias snapshot id.
  3. A label can be recorded by resolving asset + most-recent snapshot.
  4. Invalid outcomes are rejected.
  5. The CLI rejects an unknown outcome.
  6. The API endpoints list and create labels.
  7. Nothing reads bias_outcome at decision time (no reference in the engine).

Usage: python scripts/scratch/e2e_item6_calibration.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
os.environ.setdefault("APP_TESTING", "1")
_TMP = Path(tempfile.mkdtemp(prefix="item6_e2e_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.as_posix()}"
sys.path.insert(0, str(BASE))

from sqlalchemy import text, select  # noqa: E402

from app.database import init_db, engine, session_scope  # noqa: E402
from app.models.db_models import BiasOutcome, BiasSnapshot  # noqa: E402
from app.services import bias_snapshots as bs  # noqa: E402
from app.models.enums import Asset  # noqa: E402


def main() -> None:
    init_db()

    # 1. Table exists with the expected columns.
    with engine.connect() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(bias_outcome)"))}
    assert cols == {
        "id", "bias_snapshot_id", "asset", "date_utc", "bias", "score",
        "confidence", "outcome", "note", "created_at",
    }, cols
    print("  OK bias_outcome table exists with 10 columns")

    # 2. Record a label against a snapshot id.
    with session_scope() as s:
        n = bs.write_bias_snapshots(s, [bs.AssetVerdict(
            asset=Asset.GOLD, score=-40.0, confidence=0.5, bias="BEARISH",
            article_count=1, raw_score=-40.0),
            bs.AssetVerdict(
                asset=Asset.SILVER, score=12.0, confidence=0.6, bias="BULLISH",
                article_count=1, raw_score=12.0),
            bs.AssetVerdict(
                asset=Asset.BITCOIN, score=20.0, confidence=0.5, bias="BULLISH",
                article_count=1, raw_score=20.0)])
        assert n == 3
        snap_id = s.execute(text(
            "SELECT id FROM bias_snapshots WHERE asset='XAUUSD'")).first()[0]
        s.add(BiasOutcome(bias_snapshot_id=snap_id, asset="XAUUSD",
                          outcome="WORKED", note="held overnight"))
    with session_scope() as s:
        rows = s.scalars(select(BiasOutcome)).all()
    assert len(rows) == 1 and rows[0].outcome == "WORKED"
    print("  OK label recorded against snapshot id", snap_id)

    # 3. Resolve by asset + most-recent snapshot.
    from tools.label_bias import cmd_label
    import argparse
    with session_scope() as s:
        rc = cmd_label(argparse.Namespace(asset="XAGUSD", outcome="FAILED",
                                          snapshot_id=None, date=None,
                                          note=None),
                       session=s)
        assert rc == 0, rc
    with session_scope() as s2:
        rows = s2.scalars(select(BiasOutcome)).all()
    assert len(rows) == 2 and rows[1].outcome == "FAILED"
    print("  OK label resolved by asset + most-recent snapshot")

    # 4. Invalid outcome rejected.
    rc = cmd_label(argparse.Namespace(asset="XAUUSD", outcome="MAYBE",
                                      snapshot_id=None, date=None, note=None))
    assert rc == 2, rc
    print("  OK invalid outcome rejected by the CLI")

    # 5. The API endpoints work.
    async def _api_checks():
        from app.main import calibration_labels, calibration_label
        import json as _json
        listed = await calibration_labels(asset="XAUUSD", limit=50)
        if hasattr(listed, "body"):
            listed = _json.loads(listed.body)
        assert len(listed["items"]) == 1
        assert listed["valid_outcomes"] == ["WORKED", "FAILED", "Mixed"]
        created = await calibration_label(asset="BTCUSD", outcome="Mixed",
                                          note="choppy")
        # calibration_label returns a dict (FastAPI serialises it). Read it
        # directly, or fall back to a JSONResponse body.
        if hasattr(created, "body"):
            body = _json.loads(created.body) if created.body else {}
        else:
            body = created
        assert body.get("status") == "recorded", body
        rejected = await calibration_label(asset="BTCUSD", outcome="nope")
        assert rejected.status_code == 400
        print("  OK API list + create endpoints work, invalid rejected")

    import asyncio
    asyncio.run(_api_checks())

    # 6. Nothing reads bias_outcome at decision time.
    import app.analysis.sentiment_engine as se
    import app.services.pipeline as pl
    for mod in (se, pl):
        src = open(mod.__file__).read()
        assert "bias_outcome" not in src, mod.__file__
    print("  OK no engine/pipeline code references bias_outcome (no auto-tuning)")

    print("\nALL ITEM 6 CHECKS PASSED")
    import shutil
    shutil.rmtree(_TMP.parent, ignore_errors=True)


if __name__ == "__main__":
    main()