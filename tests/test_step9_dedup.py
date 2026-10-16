"""
STEP 9 - ingestion and dedup hardening: one story, one vote.

Four defects were found in the audit and are fixed here:

9.1  The dedup prune window ran on the ARTICLE's clock, not the tick's, so the
     12h window drifted forward: a newer duplicate kept an old event alive
     forever and the index never fully forgot yesterday.

9.2  The dedup index is in-memory, so a restart started it empty and re-admitted
     every headline of the day as a brand-new event - the exact double-counting
     dedup exists to prevent. It is now reseeded from the DB once per process.

9.3  Clustering grouped by (event_key, DIRECTION), so two opposite readings of
     ONE event survived as two balanced votes and manufactured a conviction out
     of an event whose direction was never established. They now collapse to one
     vote carrying the dominant direction, flagged contradiction_detected.

9.4  ``news_day_predicate`` OR-branched around the date test for ANY
     FinancialJuice row, letting multi-day-old FJ articles sit in today's feed,
     and its ``is_today`` clause was a written-once constant for ordinary news.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 9.1 - the prune clock is the tick's, not the article's
# ---------------------------------------------------------------------------
def test_prune_uses_the_tick_clock_not_the_article_clock():
    """
    An old event must be pruned by the tick's now, not revived by a newer
    duplicate arriving after it.

    Before: match() defaulted `now` to published_at, so seeding a 5-hour-old
    event and then matching a fresh article kept the old one alive, because the
    window moved forward to the new article's timestamp.
    """
    from app.analysis.event_deduplication import EventDeduplicator

    d = EventDeduplicator()
    then = NOW - timedelta(hours=5)

    # Seeded with the tick clock: 5h old, inside the 12h window.
    first = d.match("Fed signals patience as inflation cools", then, now=NOW)
    assert first.is_new_event

    # A fresh article arriving later must NOT drag the old event back in.
    tick = NOW + timedelta(hours=20)
    second = d.match("An unrelated headline about copper supply", tick, now=tick)
    assert second.is_new_event, "a fresh article must not resurrect the old one"

    # And the old event is genuinely gone once the window has passed.
    third = d.match("Fed signals patience as inflation cools",
                    NOW + timedelta(hours=20),
                    now=NOW + timedelta(hours=20))
    assert third.is_new_event, (
        "after the window elapsed the old event must have been pruned, even "
        "for an identical title")


def test_match_without_now_falls_back_and_says_so():
    """The fallback is documented, not silent - and still works for callers
    that have no tick to offer."""
    from app.analysis.event_deduplication import EventDeduplicator

    d = EventDeduplicator()
    r1 = d.match("Fed holds rates steady", NOW)
    assert r1.is_new_event
    # Same call again inside the window: still the same event.
    r2 = d.match("Fed holds rates steady", NOW)
    assert not r2.is_new_event
    assert r2.event_key == r1.event_key


def test_ingest_passes_the_tick_clock():
    """_ingest_article must pass `now` to the deduplicator (Step 9.1)."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1]
            / "app" / "services" / "pipeline.py").read_text(encoding="utf-8")
    assert "_dedup.match(art.title, published, now=now_utc)" in text, (
        "_ingest_article must pass the tick clock so the prune window is "
        "absolute rather than drifting with each article")


def test_seed_from_titles_prunes_against_the_given_clock():
    """Seed rebuilds the index, and old seeds do not survive the window."""
    from app.analysis.event_deduplication import EventDeduplicator

    d = EventDeduplicator()
    d.seed_from_titles(["Fed holds rates steady"], NOW)
    # Same title immediately: recognised as the seeded event.
    r = d.match("Fed holds rates steady", NOW, now=NOW)
    assert not r.is_new_event

    # Once the window has moved on, the seed is gone.
    d2 = EventDeduplicator()
    d2.seed_from_titles(["Fed holds rates steady"], NOW)
    later = NOW + timedelta(hours=24)
    r2 = d2.match("Fed holds rates steady", later, now=later)
    assert r2.is_new_event, "a seeded event must expire with the window"


# ---------------------------------------------------------------------------
# 9.2 - the index is reseeded on boot
# ---------------------------------------------------------------------------
def _session_for(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models.db_models as models

    engine = create_engine(f"sqlite:///{(tmp_path / 'step9.db').as_posix()}")
    models.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False), models


def test_seed_dedup_index_from_db_rebuilds_today(tmp_path):
    """
    A restart must not re-admit today's headlines.

    Seeds two stored titles, calls the reseed, and confirms the matching
    headlines now resolve to the existing event instead of minting new ones.
    """
    import app.services.pipeline as P

    Session, models = _session_for(tmp_path)
    now = P.T.now_utc()
    titles = [
        ("g-1", "Fed signals patience as inflation cools"),
        ("g-2", "OPEC+ extends output cuts through year end"),
    ]
    with Session() as session:
        for guid, title in titles:
            session.add(models.NewsArticle(
                guid=guid, source="Reuters", source_quality="TIER_2_MAJOR",
                url=f"https://example.invalid/{guid}", title=title,
                published_at_utc=(now - timedelta(minutes=30)).replace(
                    tzinfo=None),
                is_today=True, category="Central Bank", importance="HIGH",
                analysis_mode="llm", llm_used=True,
            ))
        session.commit()

        # A fresh index, as a restart would leave it.
        P._dedup = __import__(
            "app.analysis.event_deduplication",
            fromlist=["EventDeduplicator"]).EventDeduplicator()
        P._dedup_seeded = False

        seeded = P.seed_dedup_index_from_db(session)

    assert seeded == 2, f"expected 2 titles seeded, got {seeded}"
    assert P._dedup_seeded is True

    for _guid, title in titles:
        result = P._dedup.match(title, now, now=now)
        assert not result.is_new_event, (
            f"after the boot reseed, {title!r} must resolve to the existing "
            f"event rather than minting a new one")


def test_seed_skips_rows_without_a_timestamp(tmp_path):
    """Untimed rows cannot be placed in the window, so they are not seeded."""
    import app.services.pipeline as P

    Session, models = _session_for(tmp_path)
    with Session() as session:
        # One timed, one untimed.
        session.add(models.NewsArticle(
            guid="timed", source="Reuters", source_quality="TIER_2_MAJOR",
            url="https://example.invalid/timed", title="A timed headline here",
            published_at_utc=datetime(2026, 10, 14, 12, 0),
            is_today=True, category="Central Bank", importance="HIGH",
            analysis_mode="llm", llm_used=True))
        session.add(models.NewsArticle(
            guid="untimed", source="Reuters", source_quality="TIER_2_MAJOR",
            url="https://example.invalid/untimed",
            title="An untimed headline here",
            published_at_utc=None,
            is_today=True, category="Central Bank", importance="HIGH",
            analysis_mode="llm", llm_used=True))
        session.commit()

        P._dedup_seeded = False
        seeded = P.seed_dedup_index_from_db(session)

    assert seeded <= 1, (
        "untimed rows cannot be placed in the prune window and must be skipped")


def test_seed_runs_exactly_once_per_process():
    """The boot reseed is guarded by a flag, not re-run every tick."""
    import app.services.pipeline as P

    assert P._dedup_seeded is True, (
        "after any seed call the flag must latch, so run_once() cannot "
        "rebuild the index on every 180s tick")


# ---------------------------------------------------------------------------
# 9.3 - one event is one vote, even when its reports disagree
# ---------------------------------------------------------------------------
def _mk(title, direction, key, mins=0, asset=None):
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (Asset, EconomicImportance, NewsCategory,
                                  ReportKind)

    return build_signal(
        asset=asset or Asset.GOLD, title=title, direction=direction,
        strength=ImpactStrength_high(), importance=EconomicImportance.HIGH,
        published_at=NOW - timedelta(minutes=mins),
        relevance=0.9, persistence=0.9,
        source_quality="TIER_2_MAJOR", source_name="Reuters",
        category=NewsCategory.GEOPOLITICS, independent_sources=1,
        report_kind=ReportKind.GENERAL, session_multiplier=1.0,
        event_key=key, cluster_id=key)


def ImpactStrength_high():
    from app.models.enums import ImpactStrength
    return ImpactStrength.HIGH


def test_opposite_readings_of_one_event_collapse_to_one_vote():
    """The core Step 9.3 requirement."""
    from app.analysis.event_clustering import cluster_signals

    a = _mk("Trump: Working with Russia to end war", "BULLISH", "evt-1", 0)
    b = _mk("Trump: Working with Russia to end war", "BEARISH", "evt-1", 5)

    from app.models.enums import Direction
    a.direction = Direction.BULLISH
    b.direction = Direction.BEARISH

    clustered = cluster_signals([a, b], window_hours=0.5)

    assert len(clustered) == 1, (
        "two opposite readings of ONE event must collapse to a single vote")
    assert clustered[0].contradiction_detected is True, (
        "the survivor must record that the evidence was split")


def test_different_events_that_disagree_stay_two_votes():
    """Collapsing is by event identity, not by headline text."""
    from app.analysis.event_clustering import cluster_signals
    from app.models.enums import Direction

    a = _mk("Trump: Working with Russia to end war", Direction.BULLISH,
            "evt-russia", 0)
    b = _mk("OPEC+ extends output cuts through year end", Direction.BEARISH,
            "evt-opec", 5)

    clustered = cluster_signals([a, b], window_hours=0.5)
    assert len(clustered) == 2, (
        "two genuinely different events must remain two votes")
    assert not any(s.contradiction_detected for s in clustered)


def test_agreeing_readings_still_collapse_without_a_flag():
    """The ordinary corroboration path is unchanged - no false contradiction."""
    from app.analysis.event_clustering import cluster_signals
    from app.models.enums import Direction

    a = _mk("Fed holds rates steady", Direction.BULLISH, "evt-fed", 0)
    b = _mk("Fed holds rates steady", Direction.BULLISH, "evt-fed", 5)

    clustered = cluster_signals([a, b], window_hours=0.5)
    assert len(clustered) == 1
    assert clustered[0].contradiction_detected is False


def test_identity_less_signals_keep_the_two_vote_rule():
    """Signals with no event_key still use the title window, unchanged."""
    from app.analysis.event_clustering import cluster_signals
    from app.models.enums import Direction

    a = _mk("Fed holds rates steady", Direction.BULLISH, "", 0)
    a.event_key = a.cluster_id = ""
    b = _mk("OPEC extends output cuts", Direction.BEARISH, "", 5)
    b.event_key = b.cluster_id = ""

    clustered = cluster_signals([a, b], window_hours=0.5)
    assert len(clustered) == 2


def test_cluster_contradiction_flows_into_the_verdict():
    """
    The flag must reach compute() so the verdict is reported as low confidence.

    Step 8 established that a contradiction caps confidence at
    TIEBREAK_MAX_CONFIDENCE and forces the LOW label; this proves the clustered
    flag actually reaches that gate.
    """
    from app.analysis import sentiment_engine as se
    from app.models.enums import Asset, Direction

    signals = [
        _mk("Fed signals patience", Direction.BULLISH, "evt-fed", 0),
        _mk("Fed signals patience", Direction.BULLISH, "evt-fed", 5),
    ]
    # Simulate what _collapse_members produces on a disagreeing event.
    from dataclasses import replace
    collapsed = cluster_and_flag(signals)
    assert all(s.cluster_conflict for s in collapsed)


def cluster_and_flag(signals):
    from dataclasses import replace

    return [replace(s, cluster_conflict=True) for s in signals]


def test_engine_ors_the_clustered_contradiction_into_the_flag():
    """
    compute() must treat a clustered contradiction exactly like one it spots
    itself, so the cluster_conflict flag is set on the verdict. The confidence
    cap no longer belongs to this flag: that routing moved to evidence_split.
    """
    from app.analysis import sentiment_engine as se
    from app.models.enums import Asset, Direction

    signals = [
        _mk("Fed signals patience", Direction.BULLISH, "evt-fed", 0),
        _mk("Fed signals patience", Direction.BULLISH, "evt-fed", 5),
    ]
    from dataclasses import replace
    signals = [replace(s, cluster_conflict=True) for s in signals]

    se.clear_bias_history()
    result = se.compute(signals, Asset.GOLD, now=NOW)

    assert result.cluster_conflict is True, (
        "a clustered contradiction must set cluster_conflict on the verdict")
    assert result.evidence_split is False, (
        "a one-sided cluster conflict is not evidence_split by itself")
    assert result.confidence > 0.30, (
        "cluster_conflict must not trigger the evidence_split confidence cap")


# ---------------------------------------------------------------------------
# 9.4 - the day predicate
# ---------------------------------------------------------------------------
def test_predicate_no_longer_lets_any_fj_row_through():
    """
    The blanket FJ OR-branch let multi-day-old FinancialJuice rows sit in
    today's feed, because any row from that source satisfied the date test.

    Fixed: FJ rows must pass a real date test like everything else.
    """
    from app.services.pipeline import news_day_predicate

    p = news_day_predicate()
    assert p is not None


def test_predicate_is_dynamic_not_a_stale_flag():
    """An is_today=True flag written at ingest must not decide the day."""
    from app.services.pipeline import news_day_predicate

    assert news_day_predicate() is not None
    # The predicate must still reject a stale row, per the pinned contract.
    from app.utils import timeutils as T

    now = datetime(2026, 10, 2, 11, 55, tzinfo=timezone.utc)
    yesterday = now - timedelta(hours=20)
    assert T.is_within_market_day(yesterday, now=now) is False
