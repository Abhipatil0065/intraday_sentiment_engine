"""
MIGRATION IDEMPOTENCE (validation item 6).

The dashboard runs under a watchdog that restarts the process every few
minutes, so init_db() - and therefore the snapshot migration - runs repeatedly.
It must converge: running it once and running it five times must leave exactly
the same schema, and a second run must never destroy rows written since.

These tests build a REAL legacy-schema SQLite database in a temp directory and
run the real migration against it. No mocks: the point is to prove the actual
DDL sequence is repeatable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

LEGACY_FORCED_100_SCHEMA = """
CREATE TABLE sentiment_snapshot (
    id INTEGER PRIMARY KEY,
    asset VARCHAR(12) NOT NULL,
    scope VARCHAR(24) NOT NULL,
    bull_pct FLOAT NOT NULL,
    bear_pct FLOAT NOT NULL,
    label VARCHAR(16) NOT NULL,
    net_bias FLOAT NOT NULL,
    confidence FLOAT NOT NULL,
    conflicting_signals INTEGER NOT NULL,
    drivers JSON NOT NULL,
    risks JSON NOT NULL,
    catalysts JSON NOT NULL,
    conflicts JSON NOT NULL,
    contributing_articles INTEGER NOT NULL,
    price_note TEXT NOT NULL,
    computed_at DATETIME NOT NULL
)
"""

LEGACY_THREE_WAY_SCHEMA = """
CREATE TABLE sentiment_snapshot (
    id INTEGER PRIMARY KEY,
    asset VARCHAR(12) NOT NULL,
    scope VARCHAR(24) NOT NULL,
    bull_pct FLOAT NOT NULL,
    bear_pct FLOAT NOT NULL,
    neutral_pct FLOAT NOT NULL,
    label VARCHAR(16) NOT NULL,
    net_bias FLOAT NOT NULL,
    confidence FLOAT NOT NULL,
    drivers JSON NOT NULL,
    risks JSON NOT NULL,
    conflicts JSON NOT NULL,
    contributing_articles INTEGER NOT NULL,
    price_note TEXT NOT NULL,
    computed_at DATETIME NOT NULL
)
"""

EXPECTED_COLUMNS = {
    "id", "asset", "scope", "score", "confidence", "bias", "mode",
    "delta_60m", "event_risk", "event_risk_note", "drivers",
    # Both driver sides, so the dashboard can show a bullish box beside a
    # bearish one. "drivers" still holds only the leading side.
    "bull_drivers", "bear_drivers",
    "conflict", "note", "contributing_articles", "price_note", "computed_at",
}

LEGACY_COLUMNS = {
    "bull_pct", "bear_pct", "neutral_pct", "label", "net_bias",
    "conflicting_signals", "risks", "catalysts", "conflicts",
}


def _columns(db_path: Path) -> set:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(sentiment_snapshot)")}
    finally:
        conn.close()


def _make_legacy(db_path: Path, schema: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(schema)
    conn.commit()
    conn.close()


def _retarget_engine(monkeypatch, db_path: Path):
    """
    Point app.database at a temp DB.

    Re-points the module-level ``engine`` rather than the settings singleton,
    so the real production database is never touched by a test.
    """
    import app.database as dbmod
    from sqlalchemy import create_engine

    test_engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(dbmod, "engine", test_engine)
    return dbmod, test_engine


def _insert_snapshot(db_path: Path, asset: str, score: float, bias: str) -> None:
    """
    Insert a row the way the ORM would.

    The legacy ``drivers``, ``price_note`` and ``computed_at`` columns are
    declared NOT NULL, so they must be supplied - omitting them fails with an
    IntegrityError that has nothing to do with the migration being tested.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sentiment_snapshot (asset, scope, score, confidence, "
        "bias, mode, drivers, price_note, contributing_articles, computed_at) "
        f"VALUES ('{asset}', 'CURRENT', {score}, 0.6, '{bias}', "
        f"'heuristic', '[]', '', 3, '2026-10-04 12:00:00')")
    conn.commit()
    conn.close()


def _rows(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute(
            "SELECT asset, score, bias FROM sentiment_snapshot"))
    finally:
        conn.close()


@pytest.mark.parametrize("schema,name", [
    (LEGACY_FORCED_100_SCHEMA, "forced-100%"),
    (LEGACY_THREE_WAY_SCHEMA, "three-way"),
])
def test_migration_converges_to_current_schema(tmp_path, monkeypatch, schema, name):
    db = tmp_path / f"legacy_{name}.db"
    _make_legacy(db, schema)
    dbmod, _ = _retarget_engine(monkeypatch, db)
    dbmod._purge_legacy_snapshots()
    assert _columns(db) == EXPECTED_COLUMNS


@pytest.mark.parametrize("schema,name", [
    (LEGACY_FORCED_100_SCHEMA, "forced-100%"),
    (LEGACY_THREE_WAY_SCHEMA, "three-way"),
])
def test_migration_is_idempotent(tmp_path, monkeypatch, schema, name):
    """Running 1x vs 5x must leave an identical schema."""
    once = tmp_path / f"once_{name}.db"
    many = tmp_path / f"many_{name}.db"
    _make_legacy(once, schema)
    _make_legacy(many, schema)

    dbmod, _ = _retarget_engine(monkeypatch, once)
    dbmod._purge_legacy_snapshots()

    dbmod2, _ = _retarget_engine(monkeypatch, many)
    for _ in range(5):
        dbmod2._purge_legacy_snapshots()

    assert _columns(once) == _columns(many) == EXPECTED_COLUMNS


def test_repeated_migration_does_not_delete_new_rows(tmp_path, monkeypatch):
    """
    The most important idempotence property.

    A second migration run must NOT wipe rows written after the first, or the
    watchdog would blank the dashboard on every restart.
    """
    db = tmp_path / "repeat.db"
    _make_legacy(db, LEGACY_FORCED_100_SCHEMA)
    dbmod, _ = _retarget_engine(monkeypatch, db)
    dbmod._purge_legacy_snapshots()

    _insert_snapshot(db, "XAUUSD", 42.0, "BULLISH")

    dbmod._purge_legacy_snapshots()
    dbmod._purge_legacy_snapshots()

    assert _rows(db) == [("XAUUSD", 42.0, "BULLISH")]


def test_migration_on_already_current_schema_is_a_no_op(tmp_path, monkeypatch):
    """A current database must pass through completely untouched."""
    db = tmp_path / "current.db"
    _make_legacy(db, LEGACY_FORCED_100_SCHEMA)
    dbmod, _ = _retarget_engine(monkeypatch, db)
    dbmod._purge_legacy_snapshots()

    _insert_snapshot(db, "BTCUSD", -11.0, "NO_BIAS")
    before = _rows(db)

    dbmod._purge_legacy_snapshots()

    assert _rows(db) == before


def test_no_legacy_column_survives(tmp_path, monkeypatch):
    """Explicit guard: every known legacy column must be gone."""
    db = tmp_path / "legacy_check.db"
    _make_legacy(db, LEGACY_THREE_WAY_SCHEMA)
    dbmod, _ = _retarget_engine(monkeypatch, db)
    for _ in range(3):
        dbmod._purge_legacy_snapshots()
    assert not (_columns(db) & LEGACY_COLUMNS)
