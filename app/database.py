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
    Migrate sentiment_snapshot off the old three-way (bull/neutral/bear) era.

    Two things have to change when the engine goes binary-only:

    1. Existing rows carry a neutral slice and can be labelled NEUTRAL/MIXED,
       so they no longer add up to 100 and are not one of the two views the
       dashboard shows. Snapshots are a derived view rebuilt in full on every
       pipeline pass (and primed at startup), so they are cleared, not
       migrated.
    2. The legacy ``neutral_pct`` column is ``NOT NULL`` in databases created
       by the old model, so leaving it behind would break every INSERT. It is
       dropped outright - SQLite has supported DROP COLUMN since 3.35.

    Both steps are idempotent and never block startup.
    """
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            columns = {
                row[1] for row in conn.execute(
                    text("PRAGMA table_info(sentiment_snapshot)")
                )
            }
            if not columns:
                return  # table absent on a brand-new database

            deleted = 0
            if columns:
                deleted = conn.execute(
                    text("DELETE FROM sentiment_snapshot")).rowcount

            if "neutral_pct" in columns:
                conn.execute(
                    text("ALTER TABLE sentiment_snapshot DROP COLUMN neutral_pct")
                )
                log.info("Dropped legacy column sentiment_snapshot.neutral_pct")

            if deleted:
                log.info("Cleared %d legacy sentiment snapshot(s) from the "
                         "three-way (bull/neutral/bear) era", deleted)
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