"""
MAKE A CONSISTENT BACKUP OF THE ENGINE DATABASE.

    python tools/backup_db.py

Uses the SQLite backup API rather than a file copy, so the copy is consistent
even while the live server holds a WAL. Run this BEFORE any migration or schema
change.
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import settings  # noqa: E402


def main() -> int:
    url = settings.resolved_database_url()
    if not url.startswith("sqlite"):
        print(f"Only SQLite backups are supported here; got {url}")
        return 1

    src_path = Path(url.replace("sqlite:///", ""))
    if not src_path.exists():
        print(f"No database at {src_path}")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = src_path.with_name(f"{src_path.stem}.backup_{stamp}.db")

    src = sqlite3.connect(src_path)
    dst_conn = sqlite3.connect(dst)
    try:
        # The backup API produces a consistent snapshot even with an open WAL.
        src.backup(dst_conn)
    finally:
        dst_conn.close()
        src.close()

    size_mb = round(dst.stat().st_size / 1048576, 2)
    print(f"Backup written: {dst}")
    print(f"  size: {size_mb} MB")

    check = sqlite3.connect(dst)
    try:
        tables = sorted(r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        rows = list(check.execute(
            "SELECT COUNT(*) FROM sentiment_snapshot"))[0][0]
    finally:
        check.close()
    print(f"  tables: {', '.join(tables)}")
    print(f"  sentiment_snapshot rows: {rows}")
    print("Backup verified readable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())