"""
Tests for OVERNIGHT WINDOW ADMISSION.

A laptop powered off from ~01:00 to ~10:00 IST sleeps straight through the ET
market-day boundary (00:00 ET = 09:30 IST). Everything published in the rest of
that off-window is therefore stamped on the PREVIOUS market day, and the plain
``published_at >= day_start`` test dropped all of it from the morning's feed,
boxes and bias scores - even after the morning brief had just judged every one
of those headlines. These tests pin the fix:

  1. ``overnight.admit_window`` exposes the covered window only while its
     market day is current, and degrades to None on any bad/expired state.
  2. ``news_day_predicate`` admits rows published inside that window without
     changing the day filter for live rows (or the FJ carve-out).
  3. An admitted row ages from the WINDOW END - the moment the engine came
     back and the model judged the headline - so the 8h trading-day cutoff
     cannot zero the whole overnight batch. With the flag off, publication
     ageing is restored (headlines visible, but weighted ~0).

All paths are retargeted into ``tmp_path`` and the clock is frozen, so no test
touches the real ``data/`` directory or depends on when it runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.analysis.sentiment_engine import _signal_weight
from app.analysis.signal_factory import build_signal
from app.config import settings
from app.database import session_scope
from app.models.db_models import AssetImpact, NewsArticle, SentimentSnapshot
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
)
from app.services import overnight
from app.services.pipeline import IngestStats, _recompute_sentiment, news_day_predicate
from app.utils import timeutils as T
from tests.fixed_clock import freeze_now

# Boot instant: 10:00 IST on 2026-10-05 = 04:30 UTC = 00:30 EDT, i.e. the ET
# market day has JUST rolled over to Oct 5. The off-window covers the night.
BOOT = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)
# 01:00 IST = 19:30 UTC the previous day.
WINDOW_START = datetime(2026, 10, 4, 19, 30, tzinfo=timezone.utc)
WINDOW_END = BOOT


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    """Retarget the brief state file into the test's temp dir."""
    path = tmp_path / "morning_brief_state.json"
    monkeypatch.setattr(overnight, "BRIEF_STATE", path)
    return path


def write_admission(path, *, start=WINDOW_START, end=WINDOW_END,
                    valid_through="2026-10-05"):
    path.write_text(json.dumps({
        "last_run_utc": end.isoformat(),
        "last_window_start_utc": start.isoformat(),
        "last_window_end_utc": end.isoformat(),
        "admit_window": {
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "valid_through_market_day": valid_through,
        },
    }), encoding="utf-8")


def _report(start=WINDOW_START, end=WINDOW_END, generated_at=BOOT):
    return {
        "generated_at_utc": generated_at.isoformat(),
        "window": {"start_utc": start.isoformat(), "end_utc": end.isoformat()},
        "totals": {"articles": 3},
        "llm": {"llm_judged": 2},
    }


@pytest.fixture(autouse=True)
def _market_tz(monkeypatch):
    """Pin the market timezone so results are deterministic anywhere."""
    monkeypatch.setattr(T.settings, "market_timezone", "America/New_York")


# ---------------------------------------------------------------------------
# 1. admit_window: valid, lapsed, corrupt
# ---------------------------------------------------------------------------
def test_save_state_records_the_admission_block(state_file):
    overnight._save_state(_report())
    data = json.loads(state_file.read_text(encoding="utf-8"))
    block = data["admit_window"]
    assert block["start_utc"] == WINDOW_START.isoformat()
    assert block["end_utc"] == WINDOW_END.isoformat()
    # The market day of the RUN bounds the admission.
    assert block["valid_through_market_day"] == "2026-10-05"


def test_admit_window_returns_the_covered_window(state_file):
    write_admission(state_file)
    window = overnight.admit_window(now=BOOT)
    assert window is not None
    assert window.start == WINDOW_START
    assert window.end == WINDOW_END
    assert window.valid_through.isoformat() == "2026-10-05"
    assert window.contains(WINDOW_START + timedelta(hours=1))
    assert not window.contains(WINDOW_START - timedelta(minutes=1))


def test_admission_lapses_on_a_later_market_day(state_file):
    """The next ET midnight (09:30 IST) must end the admission."""
    write_admission(state_file)
    tomorrow = BOOT + timedelta(days=1)
    assert overnight.admit_window(now=tomorrow) is None


def test_admission_survives_a_stale_state_file(state_file):
    """Old state written before this feature must not break the feed."""
    state_file.write_text(json.dumps({
        "last_run_utc": BOOT.isoformat(),
        "last_window_start_utc": WINDOW_START.isoformat(),
        "last_window_end_utc": WINDOW_END.isoformat(),
    }), encoding="utf-8")
    assert overnight.admit_window(now=BOOT) is None


def test_admission_ignores_corrupt_state(state_file):
    state_file.write_text("{not json", encoding="utf-8")
    assert overnight.admit_window(now=BOOT) is None


def test_admission_ignores_a_reversed_window(state_file):
    write_admission(state_file, start=WINDOW_END, end=WINDOW_START)
    assert overnight.admit_window(now=BOOT) is None


# ---------------------------------------------------------------------------
# 2. news_day_predicate: overnight rows reach the day's readers
# ---------------------------------------------------------------------------
def _article(guid, published, *, is_today=True, source="TestWire"):
    return NewsArticle(
        guid=guid,
        source=source,
        source_quality="TIER_2_MAJOR",
        url=f"http://example.test/{guid}",
        title=f"headline {guid}",
        body="",
        summary="",
        created_at=BOOT.replace(tzinfo=None),
        published_at_utc=published.replace(tzinfo=None),
        is_today=is_today,
        category="Other",
        importance="HIGH",
    )


def _admitted_guids():
    return {a.guid for a in session_scope().__enter__().scalars(
        select(NewsArticle).where(news_day_predicate())).all()}


def test_predicate_admits_overnight_rows(state_file, monkeypatch):
    freeze_now(monkeypatch, BOOT)
    write_admission(state_file)

    with session_scope() as session:
        # Published 07:30 IST: inside the off-window, but BEFORE ET midnight,
        # so only the admission can bring it in.
        session.add(_article("overnight", datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)))
        # Published 23:30 IST the previous evening: before the window opened.
        session.add(_article("stale", datetime(2026, 10, 4, 18, 0, tzinfo=timezone.utc)))
        # Published 09:40 IST: after ET midnight - a live row.
        session.add(_article("live", datetime(2026, 10, 5, 4, 10, tzinfo=timezone.utc)))
        # Inside the window but never flagged today at ingest.
        session.add(_article("unflagged", datetime(2026, 10, 5, 2, 30, tzinfo=timezone.utc),
                             is_today=False))
        session.commit()

        guids = {a.guid for a in session.scalars(
            select(NewsArticle).where(news_day_predicate())).all()}

    assert "overnight" in guids, "the overnight keep must reach the day's readers"
    assert "live" in guids
    assert "stale" not in guids
    assert "unflagged" not in guids


def test_predicate_without_admission_is_unchanged(state_file, monkeypatch):
    """No covered window -> plain market-day filter, nothing else changes."""
    freeze_now(monkeypatch, BOOT)
    # No state file written at all.

    with session_scope() as session:
        session.add(_article("overnight", datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)))
        session.add(_article("live", datetime(2026, 10, 5, 4, 10, tzinfo=timezone.utc)))
        session.commit()
        guids = {a.guid for a in session.scalars(
            select(NewsArticle).where(news_day_predicate())).all()}

    assert guids == {"live"}


# ---------------------------------------------------------------------------
# 3. Ageing: overnight rows age from the boot, not from publication
# ---------------------------------------------------------------------------
def _signal(published_at, *, age_reference=None,
            strength=ImpactStrength.HIGH,
            importance=EconomicImportance.HIGH, relevance=0.9):
    return build_signal(
        asset=Asset.GOLD,
        title="Fed signals patience as inflation cools",
        direction=Direction.BULLISH,
        strength=strength,
        importance=importance,
        published_at=published_at,
        relevance=relevance,
        persistence=0.8,
        category=NewsCategory.INTEREST_RATES,
        age_reference_utc=age_reference,
    )


def test_overnight_signal_survives_the_cutoff_when_aged_from_the_boot(monkeypatch):
    monkeypatch.setattr(settings, "trading_day_cutoff_hours", 8.0)
    published = BOOT - timedelta(hours=9)  # 01:00 IST, nine hours stale

    live_aged = _signal_weight(_signal(published), BOOT)
    boot_aged = _signal_weight(_signal(published, age_reference=BOOT), BOOT)

    # Step 7: past the cutoff the decay floors at DECAY_FLOOR (0.05) instead
    # of dropping to 0.0. That floor then propagates through the remaining
    # multipliers (relevance/persistence/quality/session), so the final weight
    # is a small POSITIVE number - not exactly 0.05, and not zero either.
    assert live_aged > 0.0, (
        "Step 7: without the reference the 8h cutoff must floor, not zero it")
    assert live_aged < 0.5 * boot_aged, (
        "Step 7: a floored stale headline must carry far less weight than one "
        "aged from the boot")
    assert boot_aged > 0.0, "aged from the boot it must still count"
    assert boot_aged <= 1.0


def test_reference_still_decays_as_the_day_goes_on(monkeypatch):
    monkeypatch.setattr(settings, "trading_day_cutoff_hours", 8.0)
    published = BOOT - timedelta(hours=9)
    fresh = _signal_weight(_signal(published, age_reference=BOOT), BOOT)
    later = _signal_weight(_signal(published, age_reference=BOOT),
                           BOOT + timedelta(hours=4))
    assert later < fresh, "the overnight batch must fade as the session runs"
    assert later >= 0.0


# ---------------------------------------------------------------------------
# 4. End to end: the recompute scores the overnight keep
# ---------------------------------------------------------------------------
def _overnight_article(session):
    row = NewsArticle(
        guid="overnight-e2e",
        source="Financial Juice",
        source_quality="TIER_2_MAJOR",
        url="http://example.test/overnight-e2e",
        title="Fed Chair signals patience as inflation cools",
        body="federal reserve chair speaking on the rate path",
        summary="",
        created_at=BOOT.replace(tzinfo=None),
        # 01:00 IST - nine hours before the boot, so publication-time ageing
        # would put it past the 8h cutoff.
        published_at_utc=(BOOT - timedelta(hours=9)).replace(tzinfo=None),
        is_today=True,
        category="Interest Rates",
        importance="HIGH",
        report_kind="GENERAL",
        llm_used=True,
        analysis_mode="llm",
    )
    session.add(row)
    session.flush()
    session.add(AssetImpact(
        article_id=row.id,
        asset="XAUUSD",
        direction="BULLISH",
        strength="HIGH",
        relevance=0.92,
        persistence=0.8,
        reason="dovish repricing",
        transmission="policy -> yields -> gold",
        importance_weight=0.7,
    ))
    session.commit()
    return row


@pytest.fixture()
def _quiet_recompute(monkeypatch):
    """Keep the recompute hermetic: no price or snapshot side paths."""
    monkeypatch.setattr(settings, "price_data_enabled", False)
    monkeypatch.setattr(settings, "bias_snapshot_enabled", False)
    monkeypatch.setattr(settings, "corroboration_enabled", False)


def _gold_snapshots(session):
    session.flush()  # the pipeline session runs with autoflush off
    return [s for s in session.scalars(select(SentimentSnapshot)).all()
            if s.asset == "XAUUSD"]


def test_recompute_scores_the_overnight_headline(state_file, monkeypatch,
                                                 _quiet_recompute):
    freeze_now(monkeypatch, BOOT)
    write_admission(state_file)

    with session_scope() as session:
        _overnight_article(session)
        _recompute_sentiment(session, IngestStats())
        gold = _gold_snapshots(session)

    assert gold, "the recompute must publish an XAUUSD verdict"
    assert gold[0].score > 0, (
        "an overnight headline the model just judged must move the bias score"
    )


def test_recompute_without_the_flag_floors_the_headline(
        state_file, monkeypatch, _quiet_recompute):
    """OVERNIGHT_COUNTS_AS_FRESH=false: visible in the feed, and publication-
    time ageing applies.

    STEP 7 changed this from "weighs nothing" to "weighs the DECAY_FLOOR".
    A 9-hour-old headline used to hit the 8h cutoff and return weight 0.0,
    which produced score 0.0. It now floors at 0.05, so the recompute scores
    it through the weak-evidence branch (total_weight < MIN_TOTAL_WEIGHT):
    a real but small contribution, never a full lean. Contrast with
    test_recompute_scores_the_overnight_headline above, which admits it fresh
    and gets the full lean.
    """
    freeze_now(monkeypatch, BOOT)
    write_admission(state_file)
    monkeypatch.setattr(settings, "overnight_counts_as_fresh", False)

    with session_scope() as session:
        _overnight_article(session)
        _recompute_sentiment(session, IngestStats())
        gold = _gold_snapshots(session)

    assert gold
    score = gold[0].score
    assert score != 0.0, (
        "Step 7: a floored signal must still move the score"
    )
    assert abs(score) < 50.0, (
        "Step 7: the floored contribution must stay in the weak band, "
        "never a full lean (got %s)" % score
    )
