"""
Dashboard actions: the LLM status pill, the RUN NOW button, and calibration
labels - plus the bias-snapshot writer they depend on.

Regression 1: ``build_payload()``'s status block never set ``llm_status``, so
the header pill could only read LIVE or STOPPED; a circuit-breaker-open model
was indistinguishable from a healthy one. ``/api/health`` reported the real
state all along - these tests pin that the two now agree.

Regression 2: ``POST /api/calibration/labels`` attaches a label to the newest
``bias_snapshots`` row - which never existed, because ``BiasSnapshot`` was
missing the ``raw_score`` / ``baseline_score`` / ``adjusted`` Mapped columns
the writer passes. ``BiasSnapshot(**row)`` raised, the module's guard logged
one line ("bias snapshot write skipped") and swallowed it, so every snapshot
row was silently dropped for the life of the process. The test at the bottom
writes through the real session and reads the row back.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analysis import llm_client
from app.config import settings
from app.database import session_scope
from app.main import app
from app.models.db_models import BiasSnapshot
from app.models.enums import Asset
from app.services import pipeline
from app.services.bias_snapshots import AssetVerdict, write_bias_snapshots


@pytest.fixture
def client():
    return TestClient(app)


def client_for_labels():
    """A fresh client for the calibration list (used by migration tests)."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. The LLM status pill
# ---------------------------------------------------------------------------
def test_payload_publishes_llm_status(client):
    """status.llm_status is present and carries a real state."""
    payload = client.get("/api/dashboard").json()
    status = payload["status"]

    assert status["llm_status"] in {"ok", "degraded", "disabled"}
    assert status["llm_status"] == llm_client.llm_status()
    # The pill keys off enabled + status, so a missing key would have been
    # indistinguishable from "ok" in the JS.
    assert "llm_enabled" in status


def test_payload_reports_degraded_when_breaker_open(client, monkeypatch):
    """A breaker-open model must be visible as 'degraded', not 'ok'."""
    monkeypatch.setattr(llm_client, "llm_status", lambda: "degraded")

    assert client.get("/api/dashboard").json()["status"]["llm_status"] == "degraded"


def test_payload_reports_disabled_without_credentials(client, monkeypatch):
    monkeypatch.setattr(llm_client, "llm_status", lambda: "disabled")

    status = client.get("/api/dashboard").json()["status"]
    assert status["llm_status"] == "disabled"
    # llm_enabled reports configuration, llm_status the live model: the pill
    # needs both, so they are published separately.
    assert status["llm_enabled"] == settings.has_llm


def test_health_and_payload_agree_on_llm_status(client, monkeypatch):
    """The two endpoints must never disagree about LLM health."""
    for state in ("ok", "degraded", "disabled"):
        monkeypatch.setattr(llm_client, "llm_status", lambda s=state: s)
        payload = client.get("/api/dashboard").json()
        health = client.get("/api/health").json()
        assert payload["status"]["llm_status"] == state
        assert health["llm_status"] == state


def test_payload_publishes_the_rate_limit_counters(client):
    """The pill's 'calls/min' readout needs the request-window counters."""
    status = client.get("/api/dashboard").json()["status"]
    assert "llm_requests_used_last_min" in status
    assert "llm_requests_per_minute" in status


# ---------------------------------------------------------------------------
# 2. The RUN NOW button
# ---------------------------------------------------------------------------
def test_run_endpoint_reports_already_running(client):
    """A second pass while one is in flight is answered, not started."""
    saved = (pipeline._running, pipeline._tick_started_at)
    pipeline._running = True
    try:
        r = client.post("/api/run")
        assert r.status_code == 200
        assert r.json() == {"status": "already_running"}
    finally:
        (pipeline._running, pipeline._tick_started_at) = saved


def test_run_endpoint_declares_post_only():
    """The button is a POST; a stray GET must not start a pipeline pass."""
    r = TestClient(app).get("/api/run")
    assert r.status_code == 405


# ---------------------------------------------------------------------------
# 3. Bias calibration labels
# ---------------------------------------------------------------------------
def _seed_snapshot(asset: str = "XAUUSD", bias: str = "BEARISH") -> int:
    stamp = dt.datetime.now(dt.timezone.utc)
    with session_scope() as session:
        session.add(BiasSnapshot(
            timestamp_utc=stamp, asset=asset, bias=bias, score=-30.0,
            confidence=0.5, raw_score=-30.0, adjusted=False))
    with session_scope() as session:
        row = session.scalars(select(BiasSnapshot)).one()
        return row.id


def test_post_calibration_label_attaches_to_newest_snapshot(client):
    """WORKED / FAILED are accepted case-insensitively against the newest row."""
    snap_id = _seed_snapshot()

    r = client.post("/api/calibration/labels",
                    params={"asset": "XAUUSD", "outcome": "worked",
                            "note": "manual check"})

    assert r.status_code == 200, r.text
    assert r.json()["bias_snapshot_id"] == snap_id
    assert r.json()["outcome"] == "WORKED"


def test_get_calibration_labels_lists_what_was_posted(client):
    _seed_snapshot()
    client.post("/api/calibration/labels",
                params={"asset": "XAUUSD", "outcome": "FAILED"})

    items = client.get("/api/calibration/labels?limit=10").json()["items"]

    assert len(items) == 1
    assert items[0]["asset"] == "XAUUSD"
    assert items[0]["outcome"] == "FAILED"


def test_get_calibration_labels_reports_valid_outcomes(client):
    body = client.get("/api/calibration/labels").json()
    assert set(body["valid_outcomes"]) == {"WORKED", "FAILED", "Mixed"}


def test_post_calibration_label_rejects_unknown_outcome(client):
    _seed_snapshot()
    r = client.post("/api/calibration/labels",
                    params={"asset": "XAUUSD", "outcome": "PROFIT"})
    assert r.status_code == 400


def test_post_calibration_label_without_snapshot_404s(client):
    """No snapshot written yet (writer broken) - the label cannot attach."""
    r = client.post("/api/calibration/labels",
                    params={"asset": "BTCUSD", "outcome": "WORKED"})
    assert r.status_code == 404


def test_post_calibration_label_honours_explicit_snapshot(client):
    snap_id = _seed_snapshot()
    r = client.post("/api/calibration/labels",
                    params={"asset": "XAUUSD", "outcome": "Mixed",
                            "bias_snapshot_id": snap_id})
    assert r.status_code == 200
    assert r.json()["bias_snapshot_id"] == snap_id


# ---------------------------------------------------------------------------
# The bias-snapshot writer (no snapshots => no labels to attach)
# ---------------------------------------------------------------------------
def test_model_carries_the_baseline_columns():
    """The ORM must map what baseline_score_ passes, or every write raises."""
    cols = {c.name for c in BiasSnapshot.__table__.columns}
    assert {"raw_score", "baseline_score", "adjusted"} <= cols


def test_write_bias_snapshots_persists_through_the_real_session():
    """The regression: rows must actually reach the table.

    ``write_bias_snapshots`` passes raw_score / baseline_score / adjusted to
    the ORM. Without the Mapped columns above, ``BiasSnapshot(**row)`` raised
    and the guard turned every write into one WARNING line - so this
    assertion is the difference between an audit log and silence.
    """
    verdict = AssetVerdict(asset=Asset.GOLD, score=-30.0, confidence=0.4,
                           bias="BEARISH", article_count=3)

    with session_scope() as session:
        written = write_bias_snapshots(session, [verdict])
    assert written == 1

    with session_scope() as session:
        rows = session.scalars(select(BiasSnapshot)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.asset == "XAUUSD"
    assert row.bias == "BEARISH"
    assert row.raw_score == pytest.approx(-30.0)
    assert row.adjusted is False
    # The audit trail records what the reader was shown, not a re-derivation.
    assert row.bullish_pct + row.bearish_pct == pytest.approx(100.0)


def test_write_bias_snapshots_one_row_per_asset():
    verdicts = [AssetVerdict(asset=a, score=10.0, confidence=0.5,
                             bias="BULLISH", article_count=1)
                for a in Asset]
    with session_scope() as session:
        assert write_bias_snapshots(session, verdicts) == len(Asset.values())

    with session_scope() as session:
        assert len(session.scalars(select(BiasSnapshot)).all()) == len(Asset.values())


# ---------------------------------------------------------------------------
# The bias_outcome repair migration
# ---------------------------------------------------------------------------
#: Exactly the table an early build created from the wrong ORM model: an FK
#: that pointed at sentiment_snapshot while every label POST references a
#: bias_snapshots id. SQLite enforced it (PRAGMA foreign_keys=ON is set for
#: every connection in app/database.py), so each insert failed.
_BROKEN_BIAS_OUTCOME_DDL = (
    "CREATE TABLE bias_outcome ("
    "id INTEGER PRIMARY KEY, "
    "bias_snapshot_id INTEGER REFERENCES sentiment_snapshot(id) "
    "ON DELETE CASCADE, "
    "asset VARCHAR(12) NOT NULL, "
    "date_utc DATETIME NOT NULL, "
    "bias VARCHAR(16) DEFAULT '', "
    "score FLOAT DEFAULT 0.0, "
    "confidence FLOAT DEFAULT 0.0, "
    "outcome VARCHAR(16) DEFAULT '', "
    "note TEXT DEFAULT '', "
    "created_at DATETIME NOT NULL"
    ")"
)


def _insert_label_row(row_id: int, snap_id: int, asset: str = "BTCUSD",
                      outcome: str = "WORKED", note: str = ""):
    """Insert straight into the broken table, enforcement aside.

    The historical table only accepted ids that existed in
    sentiment_snapshot - which is precisely why labels never landed. The
    pragma is committed before/after the write because a SQLAlchemy
    Connection autobegins on the first execute and SQLite treats a pragma
    inside a transaction as a no-op.
    """
    from sqlalchemy import text

    from app.database import engine

    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.commit()
        conn.execute(text(
            "INSERT INTO bias_outcome (id, bias_snapshot_id, asset, "
            "date_utc, bias, score, confidence, outcome, note, created_at) "
            f"VALUES ({row_id}, {snap_id}, '{asset}', "
            "'2026-10-13 10:00:00', 'BULLISH', 10.0, 0.5, "
            f"'{outcome}', '{note}', '2026-10-13 10:00:00')"))
        conn.commit()
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()


def test_migration_repairs_a_misshaped_bias_outcome_table():
    """Wrong FK -> rebuilt with the right one, keeping the rows that can
    still be labelled and dropping the ones that cannot."""
    from sqlalchemy import text

    from app.database import _add_missing_bias_outcome_table, engine

    snap_id = _seed_snapshot(asset="BTCUSD", bias="BULLISH")

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE bias_outcome"))
        conn.execute(text(_BROKEN_BIAS_OUTCOME_DDL))
    _insert_label_row(1, snap_id, outcome="WORKED", note="real")   # labelable
    _insert_label_row(2, 999999, asset="XAUUSD", outcome="FAILED")  # orphan

    _add_missing_bias_outcome_table()

    with engine.begin() as conn:
        targets = {row[2] for row in conn.execute(
            text("PRAGMA foreign_key_list(bias_outcome)"))
            if len(row) > 3 and row[3] == "bias_snapshot_id"}
        assert targets == {"bias_snapshots"}

    # The row whose snapshot exists survives; the orphan cannot be labelled
    # against anything any more, so it is dropped rather than left dangling.
    items = client_for_labels().get("/api/calibration/labels").json()["items"]
    assert [(i["outcome"], i["bias_snapshot_id"]) for i in items] == [
        ("WORKED", snap_id)]


def test_migration_is_a_noop_when_the_fk_is_already_correct():
    """A corrected database is never rewritten again."""
    from app.database import _add_missing_bias_outcome_table

    snap_id = _seed_snapshot()
    c = client_for_labels()
    c.post("/api/calibration/labels",
           params={"asset": "XAUUSD", "outcome": "WORKED"})

    _add_missing_bias_outcome_table()
    _add_missing_bias_outcome_table()

    items = client_for_labels().get("/api/calibration/labels").json()["items"]
    assert len(items) == 1
    assert items[0]["bias_snapshot_id"] == snap_id
