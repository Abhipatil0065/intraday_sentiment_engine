"""
THE CALENDAR HISTORY - so the time decay survives a restart.

The FinancialJuice provider only ever exposes TODAY. An in-memory calendar
therefore loses yesterday's releases on every restart, and with them the decay
that keeps their contribution alive: a print six hours old is still worth 90%
of its weight, and that weight must not evaporate because the process
restarted.

So the last 48 hours of events are persisted and merged back into every poll.
The history holds the RAW feed - the whitelist and the forced impact are
re-applied to it on every poll - so a config edit still takes effect
immediately instead of resurrecting a release the user has since excluded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.economic_impact import CalendarEvent
from app.ingestion import calendar_cache

#: The history is pruned against the REAL clock, so the fixtures are built
#: from the real now - a frozen clock would age differently from the file.
NOW = datetime.now(timezone.utc)


def _event(title: str, hours_ago: float, country: str = "USD",
           actual: float | None = None, forecast: float | None = None
           ) -> CalendarEvent:
    return CalendarEvent(title=title, country=country,
                         when_utc=NOW - timedelta(hours=hours_ago),
                         impact="High", forecast=forecast, previous=1.0,
                         actual=actual, provider="financialjuice")


def _use_tmp_history(monkeypatch, tmp_path):
    """Point the history file at a temp path so the test never touches data/."""
    target = tmp_path / "calendar_history.json"
    monkeypatch.setattr(calendar_cache, "HISTORY_FILE",
                        str(target.relative_to(tmp_path)).replace("\\", "/")
                        if False else "calendar_history.json")
    monkeypatch.setattr(calendar_cache, "_history_path", lambda: target)
    return target


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------
def test_an_event_survives_a_restart(monkeypatch, tmp_path):
    """Saved, then read back by a fresh process: the event is still there."""
    _use_tmp_history(monkeypatch, tmp_path)
    event = _event("Core CPI y/y", 3.0, actual=3.5, forecast=3.2)
    assert calendar_cache.save_history([event]) is True

    # A "restart": nothing in memory, only the file on disk.
    restored = calendar_cache.load_history()
    assert len(restored) == 1
    assert restored[0].title == "Core CPI y/y"
    assert restored[0].actual == 3.5
    assert restored[0].forecast == 3.2
    assert restored[0].when_utc == event.when_utc


def test_the_history_is_pruned_to_the_window(monkeypatch, tmp_path):
    """An event older than the window is not carried forward."""
    _use_tmp_history(monkeypatch, tmp_path)
    fresh = _event("Core CPI y/y", 3.0)
    old = _event("Old CPI y/y", 72.0)
    calendar_cache.save_history([fresh, old])

    restored = calendar_cache.load_history()
    titles = {e.title for e in restored}
    assert "Core CPI y/y" in titles
    assert "Old CPI y/y" not in titles


def test_the_history_window_is_configurable(monkeypatch, tmp_path):
    _use_tmp_history(monkeypatch, tmp_path)
    calendar_cache.save_history([_event("Core CPI y/y", 30.0)])
    assert calendar_cache.load_history(48) != []
    assert calendar_cache.load_history(24) == []


def test_the_fresh_poll_wins_over_the_history(monkeypatch, tmp_path):
    """The same release with a newer actual must not be duplicated."""
    _use_tmp_history(monkeypatch, tmp_path)
    stale = _event("Core CPI y/y", 3.0, actual=3.1, forecast=3.2)
    calendar_cache.save_history([stale])

    fresh = _event("Core CPI y/y", 3.0, actual=3.5, forecast=3.2)
    merged = calendar_cache.merge_history([fresh])
    assert len(merged) == 1
    assert merged[0].actual == 3.5


def test_the_history_adds_yesterdays_releases(monkeypatch, tmp_path):
    """The whole point: yesterday's print is still there after a restart."""
    _use_tmp_history(monkeypatch, tmp_path)
    calendar_cache.save_history([_event("Yesterday CPI y/y", 20.0,
                                        actual=3.5, forecast=3.2)])

    todays = [_event("Today CPI y/y", 1.0, actual=3.1, forecast=3.2)]
    merged = calendar_cache.merge_history(todays)
    titles = {e.title for e in merged}
    assert "Yesterday CPI y/y" in titles
    assert "Today CPI y/y" in titles


def test_a_missing_history_file_is_not_an_error(monkeypatch, tmp_path):
    _use_tmp_history(monkeypatch, tmp_path)
    assert calendar_cache.load_history() == []
    assert calendar_cache.merge_history([_event("Core CPI y/y", 1.0)]) != []


def test_a_corrupt_history_file_is_not_an_error(monkeypatch, tmp_path):
    target = _use_tmp_history(monkeypatch, tmp_path)
    target.write_text("{not json", encoding="utf-8")
    assert calendar_cache.load_history() == []


# --------------------------------------------------------------------------
# The decay survives
# --------------------------------------------------------------------------
def test_the_decay_still_works_after_a_restart(monkeypatch, tmp_path):
    """The whole point of the history: the score survives the restart."""
    from app.analysis.regional_bias import compute_regional_bias
    from app.models.enums import Asset

    _use_tmp_history(monkeypatch, tmp_path)
    # A print 30 hours old: still inside the 48h window, decayed but alive.
    old = _event("Core CPI y/y", 30.0, actual=3.5, forecast=3.2)
    calendar_cache.save_history([old])

    # A restart: the pipeline has no events in memory, only the file.
    restored = calendar_cache.merge_history([])
    result = compute_regional_bias(restored, now=NOW)
    assert result.asset(Asset.GOLD).contributions
    assert result.asset(Asset.GOLD).score != 0.0


def test_the_history_does_not_resurrect_an_excluded_release(monkeypatch,
                                                             tmp_path):
    """The history is RAW: the whitelist is re-applied to it on every poll."""
    from app.analysis.calendar_config import (
        classify_region,
        load_calendar_config,
    )

    _use_tmp_history(monkeypatch, tmp_path)
    calendar_cache.save_history([
        _event("Swiss CPI y/y", 5.0, country="CHF", actual=1.4, forecast=1.0),
        _event("Core CPI y/y", 5.0, actual=3.5, forecast=3.2),
    ])

    merged = calendar_cache.merge_history([])
    cfg = load_calendar_config()
    kept, dropped = [], []
    for event in merged:
        region, reason = classify_region(event, cfg)
        if region is None:
            dropped.append((event, reason))
        else:
            kept.append(event)
    titles = {e.title for e in kept}
    assert "Core CPI y/y" in titles
    assert "Swiss CPI y/y" not in titles
    assert len(dropped) == 1


def test_the_history_is_written_after_every_poll(monkeypatch, tmp_path):
    """A poll that saw events must leave them on disk for the next restart."""
    target = _use_tmp_history(monkeypatch, tmp_path)
    calendar_cache.save_history([_event("Core CPI y/y", 2.0)])
    assert target.exists()
    payload = target.read_text(encoding="utf-8")
    assert "Core CPI y/y" in payload
