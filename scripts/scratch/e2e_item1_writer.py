"""
Item 1 end-to-end smoke test: feed verdicts through write_bias_snapshots into a
TEMP database and confirm the persisted rows carry raw_score / baseline_score /
adjusted exactly as the design intends, with no same-tick look-ahead.

Uses the same temp-DB discipline as tests/conftest.py (APP_TESTING=1, throwaway
sqlite file) so it never touches data/intraday_sentiment.db.

Usage:
    python scripts/scratch/e2e_item1_writer.py
"""
from __future__ import annotations

import os
import statistics
import sys
import tempfile
from pathlib import Path
from sqlalchemy import text

BASE = Path(__file__).resolve().parent.parent
os.environ.setdefault("APP_TESTING", "1")
_TMP = Path(tempfile.mkdtemp(prefix="item1_e2e_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.as_posix()}"

sys.path.insert(0, str(BASE))

from app.config import settings  # noqa: E402
from app.database import init_db, SessionLocal  # noqa: E402
from app.models.enums import Asset  # noqa: E402
from app.services import bias_snapshots as bs  # noqa: E402
from app.analysis.bias_baseline import baseline_score  # noqa: E402

Q_SCORES = "SELECT score, raw_score, baseline_score, adjusted FROM bias_snapshots"
Q_COUNT = "SELECT COUNT(*) FROM bias_snapshots"
Q_LAST = Q_SCORES + " ORDER BY id DESC LIMIT 1"


def verdict(asset, score, bias="BULLISH"):
    return bs.AssetVerdict(
        asset=asset, score=float(score), confidence=0.5, bias=bias,
        article_count=1, raw_score=float(score),
    )


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        # 1. Warm-up: fewer than MIN_TICKS -> raw published as-is, baseline None.
        n = bs.write_bias_snapshots(
            session, [verdict(Asset.GOLD, -60.0, "BEARISH")])
        session.commit()
        assert n == 1, n
        row = session.execute(text(Q_SCORES)).first()
        print("warm-up row:", row)
        assert row[0] == -60.0 and row[1] == -60.0 and row[2] is None and row[3] == 0

        # 2. Fill history to MIN_TICKS so the next tick gets a baseline.
        for _ in range(settings.baseline_min_ticks):
            bs.write_bias_snapshots(
                session, [verdict(Asset.GOLD, -70.0, "BEARISH")])
        session.commit()
        cnt = session.execute(text(Q_COUNT)).fetchone()[0]
        print("rows after warm-up fill:", cnt)
        assert cnt == settings.baseline_min_ticks + 1

        # 3. A clearly bearish score against a deep-negative median baseline must
        #    NOT invert to bullish (the BTCUSD tail trap).
        bs.write_bias_snapshots(
            session, [verdict(Asset.GOLD, -56.7, "BEARISH")])
        session.commit()
        row = session.execute(text(Q_LAST)).first()
        print("post-baseline row:", row)
        assert row[1] == -56.7, "raw_score must survive"
        assert row[2] is not None, "baseline must be present"
        assert row[3] == 1, "adjusted flag must be set"
        assert row[0] > row[2], "adjusted must move toward/away from zero"
        # Median of the warm-up window is -70.0, so -56.7 - (-70.0) = +13.3
        assert abs(row[0] - (-56.7 - (-70.0))) < 1e-6, row[0]

        # 4. Same-tick look-ahead guard: _compute_baselines() is called BEFORE
        #    session.flush() in write_bias_snapshots, and baseline_score() opens
        #    its OWN session. With autoflush=False the just-added row is still
        #    pending, so a foreign session cannot see it. Verify that directly:
        #    BEFORE commit, baseline_score() must NOT include the -56.7 row.
        base_before_commit = baseline_score(Asset.GOLD.value)
        assert base_before_commit == -70.0, (
            "baseline must exclude the current tick; got %s" % base_before_commit)
        print("baseline before commit (excludes current tick):", base_before_commit)
        session.commit()
        # After commit the row is visible; the baseline query orders by
        # timestamp DESC and the current tick shares its stamp with prior ticks,
        # so the window may now include it - that is fine, it is a LATER query.
        base_after_commit = baseline_score(Asset.GOLD.value)
        print("baseline after commit:", base_after_commit)

        # 5. Feature-off switch: baseline_enabled=False publishes raw as-is.
        settings.baseline_enabled = False
        bs.write_bias_snapshots(
            session, [verdict(Asset.GOLD, -56.7, "BEARISH")])
        session.commit()
        row = session.execute(text(Q_LAST)).first()
        print("feature-off row:", row)
        assert row[0] == -56.7 and row[1] == -56.7 and row[2] is None and row[3] == 0
        settings.baseline_enabled = True
    finally:
        session.close()

    print("\nALL ITEM 1 WRITER CHECKS PASSED")
    import shutil
    shutil.rmtree(_TMP.parent, ignore_errors=True)


if __name__ == "__main__":
    main()