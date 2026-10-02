"""Unit tests for the Kitco listing extractor (no network required).

The DOM shape here is copied from the live site: each card is an anchor inside
a single-article div, and every card in turn sits inside a container holding
MANY article links.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.ingestion.base import SourceSpec
from app.ingestion.kitco import KitcoSource, _date_to_stamp
from app.utils.timestamp_parse import is_today, parse_timestamp

# The outer div holds THREE articles - this is the trap the teaser walk must
# not fall into.
SAMPLE_HTML = """
<html><body>
  <div class="grid">
    <div class="card-wrap">
      <div class="flex flex-col">
        <a href="/news/article/2026-10-02/gold-holds-firm-despite-elevated-yields">
          Gold holds firm despite elevated yields as Fed bets soften</a>
        <p>(Kitco NewsWire) - Spot gold and silver prices were firmer in late U.S.
           trading, as bullion found support near the session highs.</p>
      </div>
    </div>
    <div class="card-wrap">
      <div class="flex flex-col">
        <a href="/news/article/2026-10-02/strong-payrolls-mean-fed">
          Strong payrolls do not mean the Fed will hike rates any time soon</a>
        <p>(Kitco News) - Even if the September employment report comes in above
           expectations, the labour data alone will not change the policy path.</p>
      </div>
    </div>
    <div class="card-wrap">
      <div class="flex flex-col">
        <a href="/news/article/2026-10-01/risks-rising-gold-below-4000">
          Risks rising that gold prices fall below $4,000 in Q4, BofA says</a>
      </div>
    </div>
  </div>
  <a href="/news/cryptocurrency">Cryptocurrencies</a>
</body></html>
"""

SPEC = SourceSpec(
    name="Kitco", kind="kitco", url="https://www.kitco.com/news/",
    weight=1.0, quality="TIER_2_MAJOR",
)


def parse():
    return KitcoSource(SPEC)._parse(SAMPLE_HTML, SPEC.url)


def test_extracts_real_headlines():
    titles = [a.title for a in parse()]
    assert any("Gold holds firm" in t for t in titles)
    assert any("Strong payrolls" in t for t in titles)


def test_ignores_non_article_navigation_links():
    """'Cryptocurrencies' is a short nav chip, not a headline."""
    titles = [a.title for a in parse()]
    assert not any("Cryptocurrencies" == t for t in titles)
    assert all(len(t) >= 25 for t in titles)


def test_guids_and_urls_are_unique():
    arts = parse()
    assert len({a.guid for a in arts}) == len(arts)
    assert len({a.url for a in arts}) == len(arts)


def test_urls_are_absolute():
    assert all(a.url.startswith("https://www.kitco.com/news/article/")
               for a in parse())


def test_teaser_never_borrows_a_siblings_body():
    """Regression: the teaser walk used to climb into the multi-article grid
    container and file one story's prose under another story's headline, which
    would have poisoned the sentiment classifier."""
    for a in parse():
        if not a.body:
            continue
        assert "payrolls" not in a.body or "payrolls" in a.title
        assert "Spot gold and silver prices" not in a.body or "Gold holds firm" in a.title


def test_title_is_never_duplicated_into_the_body():
    assert all(a.title not in a.body for a in parse() if a.body)


def test_body_is_captured_from_the_card():
    bodies = [a.body for a in parse() if a.body]
    assert any("Spot gold and silver prices were firmer" in b for b in bodies)
    assert any("labour data alone" in b for b in bodies)


def test_date_comes_from_the_url():
    stamped = {a.url: a.published_raw for a in parse()}
    values = set(stamped.values())
    assert "2026-10-02T12:00:00+00:00" in values
    assert "2026-10-01T12:00:00+00:00" in values


def test_date_anchors_at_noon_utc_so_the_day_never_rolls_back():
    """MARKET_TIMEZONE is America/New_York; a 00:00 UTC stamp would land on the
    PREVIOUS local day and the today-filter would drop every Kitco article."""
    stamp = _date_to_stamp("2026-10-02")
    parsed, _ = parse_timestamp(stamp)
    market_day = parsed.astimezone(ZoneInfo(settings.market_timezone)).date()
    assert market_day == datetime(2026, 10, 2, tzinfo=timezone.utc).date()
    assert is_today(parsed, now=datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc))


def test_impossible_date_is_rejected_not_invented():
    assert _date_to_stamp("2026-13-45") is None
    assert _date_to_stamp("not-a-date") is None


def test_no_articles_returns_empty():
    assert KitcoSource(SPEC)._parse("<html><body></body></html>", SPEC.url) == []
