"""Database engine + session management."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.logging_conf import get_logger
from app.models.db_models import Base

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


def init_db() -> None:
    """Create tables if they do not exist."""
    Base.metadata.create_all(bind=engine)
    _purge_legacy_snapshots()
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