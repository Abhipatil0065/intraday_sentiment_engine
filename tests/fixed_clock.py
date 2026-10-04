"""
FIXED TEST CLOCK.

Every test that needs "now" must use this module instead of the wall clock.

Why this matters
----------------
A sentiment engine is a function of TIME: signals decay, windows admit or
reject articles, and confidence depends on recency. A test anchored to
``datetime.now()`` therefore passes or fails depending on when it runs, which
is exactly how the previous suite acquired a time-bomb test (a hardcoded
2026-10-01 that started failing once real time passed it) and how tests can
quietly start exercising a different code path at 2am vs 2pm.

``FIXED_NOW`` is a stable instant chosen to sit inside a normal US trading
session, so session-dependent behaviour is deterministic too. Tests pass
``now=`` explicitly wherever the code accepts it; the monkeypatch helper is
there for the places that read the clock internally.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 2026-10-04 14:30 UTC = 10:30 America/New_York = 20:00 Asia/Kolkata.
# Inside the US session on a Sunday, so no weekend edge cases fire.
FIXED_NOW = datetime(2026, 10, 4, 14, 30, tzinfo=timezone.utc)

# A Sunday: financial markets are closed, which makes weekday-sensitive
# assertions predictable.
FIXED_TRADING_DAY = datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)


def hours_ago(hours: float, *, now: datetime = FIXED_NOW) -> datetime:
    """A published_at ``hours`` before the fixed clock."""
    return now - timedelta(hours=hours)


def minutes_ago(minutes: float, *, now: datetime = FIXED_NOW) -> datetime:
    return now - timedelta(minutes=minutes)


def days_ago(days: float, *, now: datetime = FIXED_NOW) -> datetime:
    return now - timedelta(days=days)


def freeze_now(monkeypatch, instant: datetime = FIXED_NOW):
    """
    Pin every internal ``datetime.now()`` read to ``instant``.

    Only for the handful of helpers that call the clock internally (the
    calendar horizon sweep, the FJ session-report admission test). Everything
    that accepts a ``now=`` argument should pass one instead, which is clearer
    and does not depend on monkeypatching.

    Every module that reads the clock directly must be listed here. A module
    missed here shows up as a test that passes at 14:00 and fails at 03:47,
    which is precisely the class of bug this helper exists to eliminate.
    """
    import app.analysis.economic_impact as ei
    import app.analysis.fj_sessions as fj
    import app.utils.timestamp_parse as tp
    import app.utils.timeutils as tu

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(tu, "datetime", _Frozen)
    monkeypatch.setattr(tp, "datetime", _Frozen)
    monkeypatch.setattr(fj, "datetime", _Frozen)
    monkeypatch.setattr(ei, "datetime", _Frozen)
    return instant