"""
TEST CONFIGURATION (hermetic).

Every test must run against a throwaway database, never against the live
``data/intraday_sentiment.db``. This file enforces that invariant in three
ways:

1. ``DATABASE_URL`` (and the derived DB path) is pointed at a temp file BEFORE
   any ``app`` module is imported, so the Settings singleton reads it from the
   environment and never falls back to the production default.

2. An autouse fixture fails fast if the resolved engine URL ever points at
   ``data/intraday_sentiment.db``. This is the guard that catches a future
   regression where a test forgets to retarget the engine.

3. ``APP_TESTING=1`` is exported so ``app.main``'s lifespan skips the scheduler
   and the watchdog hooks, so ``TestClient`` leaves no orphaned processes.

The temp DB is created per-session and removed at teardown.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------
# 1. Point DATABASE_URL at a temp file BEFORE any app import.
#    pytest collects conftest first, so this runs before Settings() is built.
# --------------------------------------------------------------------------
_BASE = Path(__file__).resolve().parent.parent
_TMP_DB = Path(tempfile.mkdtemp(prefix="intraday_test_")) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ["APP_TESTING"] = "1"
# Prevent the real .env from overriding the temp DB path.
os.environ.pop("DATABASE_URL_2", None)

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import engine as _engine  # noqa: E402


_LIVE_DB = (_BASE / "data" / "intraday_sentiment.db").resolve()


def _resolved_url() -> str:
    """The URL the active engine is actually using."""
    try:
        return str(_engine.url)
    except Exception:
        return settings.resolved_database_url()


@pytest.fixture(autouse=True)
def _fail_if_engine_points_at_live_db():
    """
    Fail fast if any test (or a fixture it depends on) retargeted the engine
    back at the production database.
    """
    url = _resolved_url()
    live = _LIVE_DB.as_posix()
    assert live not in url, (
        f"TEST ENGINE POINTS AT THE LIVE DATABASE: {url}. "
        f"Tests must use a temp DB, never data/intraday_sentiment.db."
    )
    yield


@pytest.fixture(autouse=True)
def _reset_test_db():
    """Drop all tables at the start of every test so state never leaks."""
    from app.models.db_models import Base

    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    yield
    Base.metadata.drop_all(bind=_engine)


def pytest_sessionfinish(session, exitstatus):
    """Clean up the temp DB directory created for this session."""
    try:
        import shutil

        shutil.rmtree(_TMP_DB.parent, ignore_errors=True)
    except Exception:
        pass
