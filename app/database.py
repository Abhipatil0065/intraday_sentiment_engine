"""Database engine + session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.logging_conf import get_logger
from app.models.db_models import Base, Settings

log = get_logger(__name__)

_is_sqlite = settings.resolved_database_url().startswith("sqlite")

engine: Engine = create_engine(
    settings.resolved_database_url(),
    future=True,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        # WAL lets the dashboard read while the worker writes.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _purge_legacy_snapshots() -> None:
    """
    Migrate sentiment_snapshot off every earlier era.

    The table has been reshaped twice, and a database created by an older
    revision will still carry the old columns:

    1. Three-way (bull/neutral/bear) era: rows carry a neutral slice and can be
       labelled NEUTRAL/MIXED. ``neutral_pct`` is NOT NULL in those databases,
       so leaving it behind would break every INSERT.
    2. Forced-100% era: ``bull_pct`` / ``bear_pct`` / ``label`` / ``net_bias`` /
       ``conflicting_signals`` / ``risks`` / ``catalysts`` / ``conflicts`` are
       replaced by the score / confidence / bias trio plus the item-10
       explainability fields.

    Snapshots are a DERIVED view rebuilt in full on every pipeline pass (and
    primed at startup), so the rows are cleared rather than migrated - there is
    nothing worth preserving in a stale sentiment reading.

    IDEMPOTENCE
    -----------
    init_db() runs on every process start, and the watchdog restarts the server
    often, so this must be safe to run any number of times. It is:
      * column existence is read from PRAGMA each run, never cached;
      * a column is dropped only if PRAGMA reports it present;
      * a column is added only if PRAGMA reports it ABSENT after the drops;
      * the DELETE is idempotent (a second run deletes zero rows).
    Running it N times leaves exactly the same schema as running it once,
    which tests/test_migration_idempotent.py asserts directly.
    """
    from sqlalchemy import text

    # Old column -> dropped, because the row data it held is regenerated.
    drop_columns = (
        "neutral_pct", "bull_pct", "bear_pct", "label", "net_bias",
        "conflicting_signals", "risks", "catalysts", "conflicts",
    )
    # New columns -> added when absent, so an old DB keeps working. "bias" is
    # a CURRENT column: it is added only when genuinely missing and never
    # dropped or rewritten once present (see the note at the add loop below).
    add_columns = {
        "score": "FLOAT DEFAULT 0.0",
        "bias": "VARCHAR(16) DEFAULT 'NO_BIAS'",
        "mode": "VARCHAR(24) DEFAULT 'heuristic'",
        "delta_60m": "FLOAT DEFAULT 0.0",
        "event_risk": "BOOLEAN DEFAULT 0",
        "event_risk_note": "TEXT DEFAULT ''",
        "conflict": "JSON",
        "note": "TEXT DEFAULT ''",
        # Both driver sides, so the dashboard can show a bullish box beside a
        # bearish one. Additive: "drivers" is unchanged.
        "bull_drivers": "JSON",
        "bear_drivers": "JSON",
        "signal_count": "INTEGER DEFAULT 0",
        # True when both sides are heavily weighted (bull > 80 and bear > 80):
        # the news is genuinely torn, so a flip is held to a wider margin.
        "contradiction_detected": "BOOLEAN DEFAULT 0",
        # News-velocity burst counts in the last 15 minutes: how many same-side
        # headlines landed together. 0 on a stale verdict.
        "burst_bull": "INTEGER DEFAULT 0",
        "burst_bear": "INTEGER DEFAULT 0",
    }

    try:
        with engine.begin() as conn:
            columns = {
                row[1] for row in conn.execute(
                    text("PRAGMA table_info(sentiment_snapshot)")
                )
            }
            if not columns:
                return  # table absent on a brand-new database

            # Only a genuinely LEGACY table needs its rows cleared. Deleting
            # unconditionally would wipe every snapshot written since the last
            # start on EVERY restart - and the dashboard runs under a watchdog
            # that restarts it every few minutes. Snapshots are cheap to
            # recompute, but blanking the feed each restart is not acceptable.
            legacy = bool(columns & set(drop_columns)) or (
                "bias" in columns and "score" not in columns)
            deleted = 0
            if legacy:
                deleted = conn.execute(
                    text("DELETE FROM sentiment_snapshot")).rowcount

            dropped = []
            present = set(columns)
            for column in drop_columns:
                if column in present:
                    conn.execute(text(
                        f"ALTER TABLE sentiment_snapshot DROP COLUMN {column}"))
                    dropped.append(column)
                    present.discard(column)

            # BINARY BIAS (bug fix): "bias" is a CURRENT column - it is never
            # dropped-and-re-added and its rows are never rewritten here.

            # Only add what is genuinely missing after the legacy drops above.
            # (Earlier this spot also dropped and re-added "bias" on every run
            # and then UPDATEd every row from one arbitrary sample - that
            # clobbered correct verdicts to a stale value, which is what made
            # verify_live_cards.py print bias=BULLISH over negative scores.
            # Reshapes delete rows first in the legacy branch, so nothing here
            # is ever preserved or rewritten.)
            for column, ddl in add_columns.items():
                if column not in present:
                    conn.execute(text(
                        f"ALTER TABLE sentiment_snapshot ADD COLUMN {column} {ddl}"))

            if dropped:
                log.info("Dropped legacy sentiment_snapshot columns: %s",
                         ", ".join(dropped))
            if deleted:
                log.info("Cleared %d legacy sentiment snapshot(s) from the "
                         "pre score/bias era", deleted)
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("Legacy snapshot migration skipped: %s", exc)


def _add_missing_bias_snapshot_columns() -> None:
    """
    Item 1: add the baseline columns to ``bias_snapshots`` when absent.

    Same idempotent pattern as the sentiment_snapshot migration above: PRAGMA
    is read fresh each run, and a column is added only when genuinely missing.
    The baseline is computed from this table's own history, so the columns
    must exist before the first baseline-adjusted write.
    """
    from sqlalchemy import text

    add_columns = {
        "raw_score": "FLOAT",
        "baseline_score": "FLOAT",
        "adjusted": "BOOLEAN DEFAULT 0",
    }
    try:
        with engine.begin() as conn:
            present = {
                row[1] for row in conn.execute(
                    text("PRAGMA table_info(bias_snapshots)"))
            }
            if not present:
                return  # table absent on a brand-new database
            for column, ddl in add_columns.items():
                if column not in present:
                    conn.execute(text(
                        f"ALTER TABLE bias_snapshots ADD COLUMN {column} {ddl}"))
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("bias_snapshots migration skipped: %s", exc)


def _add_missing_sentiment_columns() -> None:
    """
    Item 1: add the raw_score / baseline_score columns to sentiment_snapshot.
    Item 2: add stale / confidence_label columns.

    Same idempotent PRAGMA-then-ALTER pattern as the migrations above: columns
    are added only when genuinely absent, so an old database picks up the
    baseline-audit fields without any data loss. New databases get them for free
    from Base.metadata.create_all in init_db().
    """
    from sqlalchemy import text

    add_columns = {
        "raw_score": "FLOAT",
        "bull": "FLOAT",
        "bear": "FLOAT",
        "baseline_score": "FLOAT",
        "contributing_clusters": "INTEGER DEFAULT 0",
        "stale": "BOOLEAN DEFAULT 0",
        "confidence_label": "VARCHAR(8) DEFAULT 'LOW'",
        # CONTRADICTION SPLIT: the old flag is now an alias. Preserve historical
        # cluster-conflict counts by backfilling cluster_conflict from it.
        "cluster_conflict": "BOOLEAN DEFAULT 0",
        "evidence_split": "BOOLEAN DEFAULT 0",
    }
    try:
        with engine.begin() as conn:
            present = {
                row[1] for row in conn.execute(
                    text("PRAGMA table_info(sentiment_snapshot)"))
            }
            if not present:
                return  # table absent on a brand-new database
            for column, ddl in add_columns.items():
                if column not in present:
                    conn.execute(text(
                        f"ALTER TABLE sentiment_snapshot ADD COLUMN {column} {ddl}"))
            # Existing rows predate the split. Their old flag only ever meant
            # a cluster-level conflict, so preserve that count as
            # cluster_conflict. evidence_split is derived from the captured
            # bull/bear totals at the same placeholder threshold the engine
            # uses now; this is historical capture, not threshold tuning.
            conn.execute(text(
                "UPDATE sentiment_snapshot "
                "SET cluster_conflict = 1 "
                "WHERE contradiction_detected = 1 "
                "AND cluster_conflict = 0"))
            conn.execute(text(
                "UPDATE sentiment_snapshot "
                "SET evidence_split = CASE "
                "    WHEN max(bull, bear) > 0.0 "
                "         AND min(bull, bear) / max(bull, bear) > 0.7 "
                "    THEN 1 ELSE 0 END "
                "WHERE bull IS NOT NULL "
                "AND bear IS NOT NULL"))
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("sentiment_snapshot migration skipped: %s", exc)


#: The one correct ``bias_outcome`` shape. Its FK points at ``bias_snapshots``
#: - the table calibration labels attach to - matching db_models.BiasOutcome.
#: An early build shipped the ORM model with the FK on ``sentiment_snapshot``
#: by mistake, so any database whose table came from ``create_all`` rejected
#: every label insert ("FOREIGN KEY constraint failed", FK enforcement is on
#: for every SQLite connection here).
_BIAS_OUTCOME_DDL = (
    "CREATE TABLE bias_outcome ("
    "id INTEGER PRIMARY KEY, "
    "bias_snapshot_id INTEGER REFERENCES bias_snapshots(id) ON DELETE CASCADE, "
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

# Columns copied when a mis-shaped table is rebuilt.
_BIAS_OUTCOME_COLUMNS = ("id", "bias_snapshot_id", "asset", "date_utc",
                         "bias", "score", "confidence", "outcome", "note",
                         "created_at")


def _bias_outcome_fk_target(conn) -> str | None:
    """The table ``bias_outcome.bias_snapshot_id`` references, or None."""
    from sqlalchemy import text

    for row in conn.execute(text("PRAGMA foreign_key_list(bias_outcome)")):
        # id, seq, table, from, to, on_update, on_delete, match
        if len(row) > 3 and row[3] == "bias_snapshot_id":
            return row[2]
    return None


def _add_missing_bias_outcome_table() -> None:
    """
    Item 6: create - or REPAIR - the ``bias_outcome`` calibration table.

    Two paths, both idempotent (PRAGMA is read fresh every run):
      * absent -> create it with the correct FK;
      * present but pointing at the WRONG table -> rebuild it. SQLite cannot
        alter a foreign key in place, so the table is recreated and the rows
        are copied over. Rows whose snapshot no longer exists are dropped:
        they reference a bias_snapshot that is gone, so they can never be
        labelled again and only break the FK.

    Nothing is rewritten when the shape is already right, so a corrected
    database is never touched again. The table is DATA COLLECTION ONLY - it
    never feeds a threshold at decision time.
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            tables = {row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'"))}
            if "bias_outcome" not in tables:
                conn.execute(text(_BIAS_OUTCOME_DDL))
                conn.execute(text("CREATE INDEX IF NOT EXISTS "
                                  "ix_bias_outcome_asset ON bias_outcome(asset)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS "
                                  "ix_bias_outcome_created "
                                  "ON bias_outcome(created_at)"))
                return
            if _bias_outcome_fk_target(conn) == "bias_snapshots":
                return  # already correct

        # --- rebuild, outside the read transaction above: SQLite refuses
        # PRAGMA foreign_keys inside a transaction, and the rename/insert
        # sequence needs enforcement OFF while it runs. Each pragma is
        # committed before the next statement: the Connection autobegins on
        # the first execute, and conn.begin() cannot start a second.
        with engine.connect() as conn:
            enforced = conn.execute(text("PRAGMA foreign_keys")).scalar()
            conn.commit()
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.commit()
            with conn.begin():
                conn.execute(text("ALTER TABLE bias_outcome RENAME TO "
                                  "bias_outcome_old"))
                conn.execute(text(_BIAS_OUTCOME_DDL))
                old_columns = {r[1] for r in conn.execute(
                    text("PRAGMA table_info(bias_outcome_old)"))}
                present = [c for c in _BIAS_OUTCOME_COLUMNS
                           if c in old_columns]
                cols = ", ".join(present)
                conn.execute(text(
                    f"INSERT INTO bias_outcome ({cols}) "
                    f"SELECT {cols} FROM bias_outcome_old "
                    f"WHERE bias_snapshot_id IS NULL OR bias_snapshot_id IN "
                    f"(SELECT id FROM bias_snapshots)"))
                conn.execute(text("DROP TABLE bias_outcome_old"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS "
                                  "ix_bias_outcome_asset "
                                  "ON bias_outcome(asset)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS "
                                  "ix_bias_outcome_created "
                                  "ON bias_outcome(created_at)"))
            if enforced:
                # Restore the connection's own setting, so the pool hands
                # back exactly what the connect event established.
                conn.execute(text("PRAGMA foreign_keys=ON"))
                conn.commit()
            log.info("bias_outcome rebuilt: bias_snapshot_id now references "
                     "bias_snapshots")
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("bias_outcome table migration skipped: %s", exc)


def _add_missing_dropped_article_table() -> None:
    """
    One-shot policy: create ``dropped_article`` when absent.

    Same idempotent pattern as the migrations above - the table is created
    only when it genuinely does not exist, so an existing database picks up
    the low-impact suppression log without any data loss. New databases get it
    for free from ``Base.metadata.create_all`` in ``init_db()``.
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            tables = {row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'"))}
            if "dropped_article" in tables:
                return
            conn.execute(text(
                "CREATE TABLE dropped_article ("
                "guid VARCHAR(64) NOT NULL PRIMARY KEY, "
                "dropped_day VARCHAR(12) NOT NULL, "
                "title TEXT DEFAULT '', "
                "source VARCHAR(160) DEFAULT '', "
                "importance VARCHAR(24) DEFAULT 'LOW', "
                "llm_used BOOLEAN DEFAULT 0, "
                "created_at DATETIME NOT NULL"
                ")"
            ))
            conn.execute(text(
                "CREATE INDEX ix_dropped_article_day "
                "ON dropped_article(dropped_day)"))
            conn.execute(text(
                "CREATE INDEX ix_dropped_article_created "
                "ON dropped_article(created_at)"))
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("dropped_article table migration skipped: %s", exc)


def _add_missing_settings_table() -> None:
    """
    Create the ``settings`` table for persisting key-value settings across restarts.

    Same idempotent pattern as the other migrations - the table is created
    only when it genuinely does not exist.
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            tables = {row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'"))}
            if "settings" in tables:
                return
            conn.execute(text(
                "CREATE TABLE settings ("
                "key VARCHAR(64) NOT NULL PRIMARY KEY, "
                "value TEXT DEFAULT '', "
                "updated_at DATETIME NOT NULL"
                ")"
            ))
            log.info("Created settings table")
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("settings table migration skipped: %s", exc)


def init_db() -> None:
    """Create tables if they do not exist.

    ``Base.metadata.create_all`` creates every mapped table, including the
    item-6 ``bias_outcome`` calibration table, so a brand-new database gets it
    for free. Existing databases get it through the idempotent migration below
    (the table is created only when absent, never dropped or rewritten).
    """
    Base.metadata.create_all(bind=engine)
    _purge_legacy_snapshots()
    _add_missing_bias_snapshot_columns()
    _add_missing_sentiment_columns()
    _add_missing_bias_outcome_table()
    _add_missing_dropped_article_table()
    _add_missing_settings_table()
    log.info("Database ready: %s", _redact(settings.resolved_database_url()))


def _redact(url: str) -> str:
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        rest = "***@" + rest.split("@", 1)[1]
    return f"{scheme}://{rest}"


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope - commits on success, rolls back on error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# Alias for session_scope so that tests and other callers can use get_session().
get_session = session_scope














