"""Unit tests for the Financial Juice DOM extractor (no network required)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.ingestion.financialjuice import _extract, _parse_fj_time, _relative_seconds

SAMPLE_HTML = """
<html><body>
<div class="col-xs-12 infinite-item headline-item summary-item">
  <div class="media-body">
    <p class="headline-title">
      <span class="headline-title-nolink">Traders pare ECB rate bets, favor three
      hikes by end of 2027</span>
    </p>
    <div class="headline-content-container"><div class="headline-content">
      <p>European rate expectations shifted sharply today.</p></div></div>
    <span class="headline-time">15:55 Oct 01</span>
  </div>
</div>
<div class="headline-item">
  <p class="headline-title">
    <span class="headline-title-nolink">Gold hits fresh high as dollar weakens
    across the board</span>
  </p>
  <div class="headline-content"><p>Spot gold climbed.</p></div>
  <span class="headline-time">16:01 Oct 01</span>
</div>
<div class="headline-item">
  <p class="headline-title"><span class="headline-title-nolink">Need to know
  market risk</span></p>
  <div class="headline-content"><p>-</p></div>
</div>
</body></html>
"""


def test_extracts_real_headlines():
    arts = _extract(SAMPLE_HTML, "Financial Juice", "TIER_2_MAJOR")
    titles = [a.title for a in arts]
    assert any("ECB rate bets" in t for t in titles)
    assert any("Gold hits fresh high" in t for t in titles)


def test_guids_are_unique():
    """Regression: identical fallback URLs once collapsed every item into
    one GUID and the deduplicator silently dropped all but one article."""
    arts = _extract(SAMPLE_HTML, "Financial Juice", "TIER_2_MAJOR")
    assert len({a.guid for a in arts}) == len(arts)
    assert len({a.url for a in arts}) == len(arts)


def test_body_is_captured():
    arts = _extract(SAMPLE_HTML, "Financial Juice", "TIER_2_MAJOR")
    assert any("European rate expectations" in a.body for a in arts)


def test_timestamp_parsed_from_item():
    arts = _extract(SAMPLE_HTML, "Financial Juice", "TIER_2_MAJOR")
    stamped = [a for a in arts if a.published_raw]
    assert stamped, "at least one headline should carry a timestamp"
    assert all("Oct" in a.published_raw or "T" in a.published_raw
               for a in stamped)


def test_relative_seconds():
    assert _relative_seconds("just now") == 0.0
    assert _relative_seconds("5 minutes ago") == 300.0
    assert _relative_seconds("2 hours ago") == 7200.0
    assert _relative_seconds("14:30") is None      # absolute -> never guessed
    assert _relative_seconds(None) is None


def test_parse_fj_time_converts_from_browser_timezone():
    """
    FinancialJuice renders times in the BROWSER's timezone, so '15:55' on a
    machine set to Asia/Kolkata must resolve to 10:25 UTC - NOT 15:55 UTC.
    Getting this wrong made every headline look hours in the future and the
    today-filter rejected the entire feed.
    """
    from zoneinfo import ZoneInfo

    from app.config import settings

    now = datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)
    out = _parse_fj_time("15:55 Oct 01", now, now.date())
    assert out is not None

    parsed = datetime.fromisoformat(out)
    assert parsed.tzinfo is not None
    local = parsed.astimezone(ZoneInfo(settings.display_timezone))
    assert (local.hour, local.minute) == (15, 55)


def test_parse_fj_time_never_produces_future_news():
    """A rendered time must never resolve to a timestamp in the future."""
    from zoneinfo import ZoneInfo

    from app.config import settings
    from app.utils.timestamp_parse import age_hours

    now = datetime(2026, 10, 1, 10, 30, tzinfo=timezone.utc)
    # Two hours before "now", expressed in the browser's own timezone.
    local_now = now.astimezone(ZoneInfo(settings.display_timezone))
    earlier = local_now - timedelta(hours=2)
    rendered = f"{earlier.hour:02d}:{earlier.minute:02d} " \
               f"{earlier.strftime('%b')} {earlier.day:02d}"

    out = _parse_fj_time(rendered, now, now.date())
    assert out is not None
    assert age_hours(datetime.fromisoformat(out), now=now) >= 0


def test_parse_fj_time_rejects_garbage():
    now = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
    assert _parse_fj_time("no time here", now, now.date()) is None
    assert _parse_fj_time("", now, now.date()) is None


def test_no_headlines_returns_empty():
    assert _extract("<html><body></body></html>", "FJ", "TIER_4_OTHER") == []