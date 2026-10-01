"""Logging configuration - console + rotating file."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import settings

_CONFIGURED = False


class _RedactingFilter(logging.Filter):
    """Keeps the API key out of logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        key = settings.llm_api_key
        if key and key in record.getMessage():
            record.msg = record.msg.replace(key, "***REDACTED***")
            record.args = ()
        return True


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = _RedactingFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root.addHandler(console)

    try:
        log_path: Path = settings.resolved_log_file()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        fh.addFilter(redactor)
        root.addHandler(fh)
    except Exception as exc:  # noqa: BLE001 - file logging must never be fatal
        root.warning("File logging disabled: %s", exc)

    # Quieten noisy third-party loggers
    for noisy in ("httpx", "httpcore", "apscheduler.executors", "urllib3", "feedparser"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)