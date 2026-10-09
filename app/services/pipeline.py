"""
PIPELINE ORCHESTRATOR (section 26).

  INGESTION -> NEWS EXTRACTION -> TODAY FILTER -> DEDUP -> CLASSIFY
  -> RECENCY -> ASSET IMPACT -> AGGREGATE -> PREP/WRAP -> PERSIST

This module owns the end-to-end control flow. The background scheduler calls
``run_once``; the API and dashboard read the results it persists.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import and_, delete, exists, func, or_, select

from app.analysis import llm_client, release_surprise
from app.analysis.economic_impact import (
    CalendarSignal,
    signals_for_events,
    to_calendar_summary,
)
from app.analysis.economic_impact import (
    headline as calendar_headline,
)
from app.analysis.event_deduplication import EventDeduplicator
from app.analysis.fj_sessions import FINANCIAL_JUICE_SOURCE
from app.analysis.news_classifier import analyze_article
from app.analysis.sentiment_engine import WeightedSignal, remember_bias
from app.analysis.signal_factory import build_bleed_signal, build_signal
from app.config import settings
from app.database import session_scope
from app.ingestion.base import RawArticle
from app.ingestion.economic_calendar import fetch_calendar
from app.ingestion.news_fetcher import fetch_all_news
from app.logging_conf import get_logger
from app.market import market_prep, market_wrap
from app.market import regime as regime_mod
from app.market.session_detector import (
    detect_session,
    session_relevance,
)
from app.models.db_models import (
    ArticleEvent,
    AssetImpact,
    NewsArticle,
    NewsEvent,
    PipelineRun,
    SentimentSnapshot,
)
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
)
from app.services.bias_snapshots import AssetVerdict
from app.utils import timeutils as T
from app.utils.timestamp_parse import (
    age_hours,
    parse_timestamp,
    within_max_age,
    within_rolling_window,
)

log = get_logger(__name__)

# Shared across runs so dedup survives within a process session.
_dedup = EventDeduplicator()
_last_update: datetime | None = None
_last_successful_tick: str | None = None
_news_since_update = 0
_running = False
_last_regime = None  # RegimeAssessment from the most recent recompute
# Economic-calendar signals from the most recent fetch, shared with the API.
_calendar_signals: list[CalendarSignal] = []
# Session-aware global equity card from the most recent recompute (NOW-only).
_last_equity_card: dict | None = None

# Cached health metrics - written by the pipeline, read by /api/health without
# touching the database so the endpoint stays instant even while a tick runs.
_health_metrics: dict = {
    "last_article_utc": None,
    "last_snapshot_utc": None,
    "per_source": {},
}

# Tick start time for hung-tick detection
_tick_started_at: datetime | None = None

# Maximum tick duration before considering it hung (10 minutes)
MAX_TICK_DURATION_SECONDS = 600

# Hard cap on LLM classifications started within a single tick. Beyond this the
# classifier falls back to deterministic heuristics, keeping every tick under
# the ~120s budget even when the LLM endpoint is degraded.
LLM_BATCH_LIMIT = 15

# Source-quality tiers, ordered best-first. The priority sort below uses this to
# rank a batch before the LLM cap is applied, so the 15 most trustworthy /
# freshest articles get model attention and the tail falls back to heuristics.
_SOURCE_QUALITY_RANK = {
    "TIER_1_OFFICIAL": 0,
    "TIER_2_MAJOR": 1,
    "TIER_3_CRYPTO": 2,
    "TIER_4_OTHER": 3,
}


def _make_aware(dt: datetime | None) -> datetime | None:
    """Force a datetime to aware UTC before it touches the database.

    SQLite strips ``tzinfo`` on write and read, so a naive timestamp stored
    today comes back naive tomorrow and every downstream subtraction against an
    aware clock raises TypeError. This guard makes the write path safe by
    construction instead of relying on every caller to remember.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _priority_sort_articles(articles: list) -> list:
    """
    Sort a fetched batch so the highest-priority articles reach the LLM cap.

    Order, in priority:
      1. source_quality  - TIER_1_OFFICIAL first, then TIER_2_MAJOR, etc.
      2. published_at    - newest first (parsed from published_raw).
      3. title           - stable tiebreaker so the sort is deterministic.

    Why this matters: LLM_BATCH_LIMIT caps model calls per tick, and without a
    sort the cap is hit in arrival order - a rush of Tier 4 social-media noise
    can consume every slot and leave a Tier 1 central-bank release to heuristics.
    Sorting first means the 15 most important articles are the ones classified
    by the model; the remainder are still ingested and scored, just deterministically.

    Note on importance: RawArticle carries no importance field - importance is a
    CLASSIFICATION output (see cue_rules.detect_importance), so it cannot be a
    sort input here without classifying first, which defeats the purpose of the
    cap. Source quality is the pre-classification proxy that best predicts it:
    official statistical releases and central-bank statements outrank wire
    copy, which outranks aggregator chatter.
    """
    def _key(art):
        quality_rank = _SOURCE_QUALITY_RANK.get(
            (getattr(art, "source_quality", "") or "").upper(), 99)
        published = getattr(art, "published_raw", None)
        # Newest first: later timestamps sort before earlier ones, so negate the
        # rank. Unparseable / missing timestamps sort last (rank 999) rather than
        # being silently dropped - they are still ingested, just deprioritised.
        try:
            ts_rank = -_parse_timestamp_rank(published)
        except Exception:
            ts_rank = 999
        return (quality_rank, ts_rank, (getattr(art, "title", "") or "").lower())

    return sorted(articles, key=_key)


def _parse_timestamp_rank(value: str | None) -> float:
    """
    A sort key for a raw publication timestamp: higher = more recent.

    Prefers an ISO-8601 string (the shape every source emits). Falls back to a
    numeric epoch so a bare-number timestamp still sorts rather than raising.
    """
    if not value:
        return 0.0
    text = str(value).strip()
    try:
        return float(datetime.fromisoformat(text).timestamp())
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return 0.0

# Backlog re-classification: how many heuristic articles to re-process with
# LLM per tick (low priority, only when circuit breaker is closed).
LLM_BACKLOG_PER_TICK = 5


async def _reclassify_backlog(session, stats, tick_start: datetime) -> int:
    """
    Re-classify up to LLM_BACKLOG_PER_TICK articles that were previously
    processed with heuristics (llm_used=false), newest first.

    Only runs when:
    - LLM is configured (settings.has_llm)
    - Circuit breaker is closed (llm_client.circuit_breaker().is_open == False)
    - There are articles with llm_used=false and is_today=true
    - Articles must have been created BEFORE this tick started (to avoid
      re-processing articles that were just batch-capped in this tick).

    Returns the number of articles successfully re-classified.
    """
    if not settings.has_llm:
        return 0

    cb = llm_client.circuit_breaker()
    if cb.is_open:
        log.debug("Circuit breaker open - skipping backlog re-classification")
        return 0

    # Find articles processed with heuristics today, newest first
    # Exclude articles created in the current tick (batch-capped articles)
    articles = session.execute(
        select(NewsArticle)
        .where(
            and_(
                NewsArticle.llm_used == False,
                NewsArticle.is_today == True,
                NewsArticle.created_at < tick_start,
            )
        )
        .order_by(NewsArticle.published_at_utc.desc())
        .limit(LLM_BACKLOG_PER_TICK)
    ).scalars().all()

    if not articles:
        return 0

    log.info("Re-classifying %d backlog articles with LLM", len(articles))
    reclassified = 0

    for article in articles:
        try:
            analysis, llm_ok, skip_reason = await analyze_article(article.title, article.body or "")
            if llm_ok:
                # Update the article with new LLM analysis
                article.category = analysis.category.value
                article.importance = analysis.importance.value
                article.report_kind = analysis.report_kind.value
                article.verification = analysis.verification.value
                article.relevance_score = analysis.relevance_score
                article.analysis_mode = analysis.mode
                article.analysis_note = analysis.note
                article.llm_used = True
                reclassified += 1
                stats.llm_backlog_ok += 1
            else:
                stats.llm_backlog_failed += 1
        except Exception as exc:  # noqa: BLE001 - isolate per-article
            log.warning("Backlog re-classification failed for %s: %s", article.guid[:8], exc)
            stats.llm_backlog_failed += 1

    if reclassified:
        log.info("Successfully re-classified %d backlog articles", reclassified)

    return reclassified


def runtime_state() -> dict:
    return {
        "running": _running,
        "last_updated": T.stamp() if _last_update else None,
        "last_successful_tick": _last_successful_tick,
        "new_news_since_last_update": _news_since_update,
        "regime": _last_regime,
        "health_metrics": _health_metrics,
        "tick_started_at": _tick_started_at.isoformat() if _tick_started_at else None,
    }


def last_successful_tick() -> str | None:
    """ISO timestamp of the most recent tick that finished without raising."""
    return _last_successful_tick


def get_health_metrics() -> dict:
    """Snapshot of cached health data (no DB hit required)."""
    return dict(_health_metrics)


def reset_running_flag() -> None:
    """Unstick the pipeline if a previous process was killed mid-tick.

    Called from atexit so a SIGTERM/SIGKILL from the watchdog does not leave
    the in-process flag stuck on ``True`` for the NEXT process in the same
    worktree (e.g. a long-running test worker).  Each fresh process starts with
    ``_running`` already False, but the handler makes that invariant explicit
    and visible to any in-process caller.
    """
    global _running
    if _running:
        log.info("Resetting _running flag on process exit")
        _running = False


def check_and_release_hung_tick() -> bool:
    """
    Check if the current tick has exceeded MAX_TICK_DURATION_SECONDS.
    If so, release the running flag and return True.
    Returns False if no tick is running or tick is within time limit.
    """
    global _running, _tick_started_at
    if not _running or _tick_started_at is None:
        return False
    elapsed = (T.now_utc() - _tick_started_at).total_seconds()
    if elapsed > MAX_TICK_DURATION_SECONDS:
        log.warning("Tick hung for %.0fs (limit %ds) - releasing running flag",
                    elapsed, MAX_TICK_DURATION_SECONDS)
        _running = False
        _tick_started_at = None
        return True
    return False


atexit.register(reset_running_flag)


def reset_counter() -> None:
    """Called by the API so 'new since last update' tracks the dashboard view."""
    global _news_since_update
    _news_since_update = 0


def _parse_article_time(art: RawArticle) -> tuple[datetime | None, datetime | None, str | None]:
    published, label = parse_timestamp(art.published_raw)
    updated, _ = parse_timestamp(art.updated_raw)
    return published, updated, label


def _is_update_article(published: datetime | None, updated: datetime | None) -> bool:
    """
    Section 19: an old article re-stamped today is NOT automatically a new event.

    If the source bumped the item within UPDATE_WINDOW_HOURS of its original
    publication, it is flagged as an update.
    """
    if published is None or updated is None:
        return False
    delta_h = abs((updated - published).total_seconds()) / 3600.0
    return delta_h <= settings.update_window_hours


@dataclass
class IngestStats:
    fetched: int = 0
    new_today: int = 0
    rejected: int = 0
    duplicates: int = 0
    # New-article LLM outcomes (sum must equal new_today)
    llm_new_ok: int = 0
    llm_new_failed: int = 0
    llm_new_skipped_batch: int = 0
    llm_new_skipped_breaker: int = 0
    # Backlog re-classification outcomes (separate from new articles)
    llm_backlog_ok: int = 0
    llm_backlog_failed: int = 0
    llm_backlog_reclassified: int = 0
    calendar_events: int = 0
    calendar_signals: int = 0
    skipped_unroutable: int = 0
    errors: list[str] = field(default_factory=list)
    purged: dict = field(default_factory=dict)

    def verify_new_article_counters(self) -> bool:
        """Assert that the four new-article LLM buckets sum to new_today.

        The buckets are mutually exclusive: each new article is counted
        in exactly one of llm_new_ok / llm_new_failed /
        llm_new_skipped_breaker / llm_new_skipped_batch. Their sum must
        therefore equal new_today exactly.
        """
        total = (self.llm_new_ok + self.llm_new_failed +
                 self.llm_new_skipped_batch +
                 self.llm_new_skipped_breaker)
        return total == self.new_today


def _importance_weight(importance: EconomicImportance) -> float:
    return {
        EconomicImportance.VERY_HIGH: 1.0,
        EconomicImportance.HIGH: 0.70,
        EconomicImportance.MEDIUM: 0.42,
        EconomicImportance.LOW: 0.18,
    }.get(importance, 0.42)


async def _is_fj_session_report(art: RawArticle) -> bool:
    """True when this article is one of the five FinancialJuice session reports.

    Imported lazily so the analysis layer stays free of any pipeline dependency.
    """
    from app.analysis.fj_sessions import (
        classify_fj_session,
        is_financial_juice,
    )

    if not is_financial_juice(art.source):
        return False
    return classify_fj_session(art.title or "", art.body or "") is not None


def _is_fj_session_report_sync(art: RawArticle) -> bool:
    """Synchronous twin of ``_is_fj_session_report`` for use in the ingest loop.

    ``classify_fj_session`` is pure-Python, so no await is needed; the async
    wrapper exists only to keep the public surface consistent.
    """
    from app.analysis.fj_sessions import (
        classify_fj_session,
        is_financial_juice,
    )

    if not is_financial_juice(art.source):
        return False
    return classify_fj_session(art.title or "", art.body or "") is not None


def _fj_session_report_is_admissible(published: datetime,
                                    now: datetime | None = None) -> bool:
    """
    Admit a FinancialJuice session report on the INDIAN trading date.

    The general TODAY filter is anchored to MARKET_TIMEZONE (America/New_York)
    because the sentiment engine trades US hours. FJ session reports are not
    aligned to those hours: the Asia wrap lands around 11:00 IST (01:00 EDT,
    same day) but the US wrap lands ~01:05 IST, which is 15:35 EDT on the
    PREVIOUS day. Applying the market-timezone filter therefore threw the US
    wrap away every single evening - the report was published, scraped and
    then discarded before it could ever reach the FJ section.

    Only the IST date decides admission here, mirroring the rule the FJ
    section itself applies when rendering (today, plus one lookback day for
    the US Market Wrap). Nothing else about the TODAY filter changes, and no
    non-FJ article is affected.

    ``now`` is injectable so the test suite can pin "today" instead of
    depending on the wall clock. It defaults to the real clock in production,
    so behaviour is unchanged.
    """
    from app.analysis.fj_sessions import india_date_of, india_today

    report_date = india_date_of(published)
    if report_date is None:
        return False
    age_days = (india_today(now) - report_date).days
    return 0 <= age_days <= 1


async def _ingest_article(art: RawArticle, session, stats: IngestStats,
                          use_llm: bool = True):
    """Apply the TODAY filter, classify, cluster and store one article."""
    published, updated, tz_label = _parse_article_time(art)
    is_fj = _is_fj_session_report_sync(art)

    # --- ROLLING-WINDOW FILTER (improvement item 3) ---
    # Replaces the "same market-timezone calendar date" gate. Admitting news on
    # RECENCY rather than on which calendar day it landed in means the overnight
    # US wrap (~01:05 IST) passes on age alone, so the old FinancialJuice / IST
    # date carve-out below is no longer needed for ordinary headline flow.
    if published is None:
        stats.rejected += 1
        if is_fj:
            log.info("FJ-SESSION REJECT (no published time): %r", art.title[:80])
        return None
    if not within_rolling_window(published) or not within_max_age(published):
        stats.rejected += 1
        if is_fj:
            log.info(
                "FJ-SESSION REJECT (outside window): %r published=%s age_h=%.1f "
                "rolling=%s max_age=%s",
                art.title[:80], published,
                (published - datetime.now(timezone.utc)).total_seconds() / 3600.0,
                within_rolling_window(published), within_max_age(published),
            )
        return None
    if is_fj:
        # Session reports still get their own IST-date rule on top, because a
        # US wrap published ~15:35 ET the previous day must be able to appear on
        # the Indian trading date the dashboard renders it under.
        if not _fj_session_report_is_admissible(published):
            stats.rejected += 1
            log.info(
                "FJ-SESSION REJECT (not IST-admissible): %r published=%s",
                art.title[:80], published,
            )
            return None
        log.info(
            "FJ-SESSION ADMITTED: %r guid=%s published=%s",
            art.title[:80], art.guid, published,
        )

    # --- EVENT DEDUPLICATION (section 16) ---
    dedupe = _dedup.match(art.title, published)
    if is_fj:
        log.info(
            "FJ-SESSION DEDUP: %r -> event_key=%s is_new=%s score=%.2f",
            art.title[:80], dedupe.event_key, dedupe.is_new_event, dedupe.score,
        )

    row = NewsArticle(
        guid=art.guid,
        source=art.source,
        source_quality=art.source_quality,
        url=art.url,
        title=art.title,
        body=(art.body or "")[: settings.article_fetch_max_chars],
        summary=art.summary or "",
        published_at_utc=_make_aware(published),
        published_tz_label=tz_label,
        updated_at_source=_make_aware(updated),
        is_today=True,
        is_updated_article=_is_update_article(published, updated),
    )

    # --- NLP / LLM ANALYSIS ---
    # Commit the article row BEFORE contacting the LLM endpoint. A live
    # SQLite write transaction blocks every other writer (the watchdog's
    # legacy-snapshot migration on restart included), and a slow or
    # rate-limited LLM endpoint would hold that lock for minutes.
    # expire_on_commit=False keeps the ORM object usable; session.merge
    # re-attaches it for the field updates below.
    session.add(row)
    session.flush()
    session.commit()
    row = session.merge(row)
    if use_llm:
        analysis, llm_ok, skip_reason = await analyze_article(art.title, art.body)
    else:
        from app.analysis.news_classifier import heuristic_analyze

        analysis = heuristic_analyze(art.title, art.body)
        analysis.note = "LLM batch cap reached; heuristic used for this article."
        llm_ok = False
        skip_reason = "batch_cap"
    if use_llm:
        # The four new-article buckets are MUTUALLY EXCLUSIVE: every new
        # article lands in exactly one of llm_new_ok / llm_new_failed /
        # llm_new_skipped_breaker / llm_new_skipped_batch, and their sum
        # must equal new_today. A circuit-breaker skip is its own bucket,
        # NOT a member of llm_new_failed - counting it in both double-counted
        # the article and broke the invariant.
        if llm_ok:
            stats.llm_new_ok += 1
        elif skip_reason == "circuit_breaker":
            stats.llm_new_skipped_breaker += 1
        else:
            stats.llm_new_failed += 1
    else:
        # use_llm=False means batch cap reached
        stats.llm_new_skipped_batch += 1

    row.category = analysis.category.value
    row.importance = analysis.importance.value
    row.report_kind = analysis.report_kind.value
    row.verification = analysis.verification.value
    row.relevance_score = analysis.relevance_score
    row.analysis_mode = analysis.mode
    row.analysis_note = analysis.note
    row.llm_used = llm_ok

    session.flush()

    # --- EVENT CLUSTERING ---
    event = session.scalar(
        select(NewsEvent).where(NewsEvent.event_key == dedupe.event_key)
    )
    if event is None:
        event = NewsEvent(
            event_key=dedupe.event_key,
            canonical_headline=dedupe.canonical_title,
            summary=analysis.event_summary,
            category=analysis.category.value,
            importance=analysis.importance.value,
            verification=analysis.verification.value,
            first_seen_utc=_make_aware(published),
            last_updated_utc=_make_aware(published),
            article_count=1,
        )
        session.add(event)
        session.flush()
    else:
        event.article_count += 1
        event.last_updated_utc = _make_aware(published)
        event.confirmed_by_followup = True
        rank = {EconomicImportance.LOW: 0, EconomicImportance.MEDIUM: 1,
                EconomicImportance.HIGH: 2, EconomicImportance.VERY_HIGH: 3}
        if rank.get(analysis.importance, 0) > rank.get(
                EconomicImportance(event.importance), 0):
            event.importance = analysis.importance.value

    session.add(ArticleEvent(article_id=row.id, event_id=event.id,
                             similarity=dedupe.score))
    if not dedupe.is_new_event:
        stats.duplicates += 1

    # --- ASSET IMPACT ---
    current_session = detect_session()
    for asset_analysis in analysis.assets:
        session.add(
            AssetImpact(
                article_id=row.id,
                event_id=event.id,
                asset=asset_analysis.asset.value,
                direction=asset_analysis.direction.value,
                strength=asset_analysis.strength.value,
                relevance=asset_analysis.relevance,
                persistence=asset_analysis.persistence,
                reason=asset_analysis.reason,
                transmission=asset_analysis.transmission,
                metal_character=asset_analysis.metal_character,
                session=current_session.value,
                importance_weight=_importance_weight(analysis.importance),
            )
        )

    stats.new_today += 1
    return row


def _last_signal_age_min(signals, now: datetime) -> float | None:
    """
    Minutes since the NEWEST signal that actually carried weight.

    "Carried weight" means a signal that survived ``_signal_weight``'s decay
    floor - a headline already decayed to zero is not evidence of a recent
    view, so counting it would make a stale asset look fresh.

    Returns None when the asset has no usable signal at all, which phase 4
    treats as maximally stale and the snapshot log records as NULL.

    Derived from news timestamps only. No price is involved.
    """
    ages = []
    for s in signals:
        if s.published_at is None or s.final_weight <= 0:
            continue
        hours = age_hours(s.published_at, now=now)
        if hours is None:
            continue
        ages.append(max(0.0, float(hours) * 60.0))
    return round(min(ages), 2) if ages else None


def _decision_log():
    """Lazy singleton for the item-11 audit log (never blocks the pipeline)."""
    global _DECISION_LOG
    if _DECISION_LOG is None:
        from app.analysis.decision_log import get_decision_log
        _DECISION_LOG = get_decision_log()
    return _DECISION_LOG


_DECISION_LOG = None


def _price_note(asset: Asset) -> str:
    from app.market.price_data import UNAVAILABLE_NOTE
    if not settings.price_data_enabled:
        return UNAVAILABLE_NOTE
    return "Price module enabled - see price-action panel."


def _calendar_signal_to_weighted(sig: CalendarSignal) -> WeightedSignal:
    """
    Convert one calendar signal into an aggregator input.

    This is what makes the economic calendar count towards the bullish /
    bearish verdict: the same ``WeightedSignal`` shape the news feed produces,
    fed into the same ``compute()`` weighting, so there is exactly one place
    where a direction is decided.
    """
    event = sig.event
    return build_signal(
        asset=sig.asset,
        title=calendar_headline(event, sig),
        direction=sig.direction,
        strength=sig.strength,
        importance=sig.importance,
        published_at=event.when_utc,
        relevance=sig.relevance,
        persistence=sig.persistence,
        # Official statistical releases are primary sources.
        source_quality="TIER_1_OFFICIAL",
        source_name=event.provider,
        # Phase 1: a scheduled print decays on its own category's half-life,
        # same as a news article. Without this it used the 6h default.
        category=sig.profile.category,
        session_multiplier=1.0,
        is_primary=True,
        event_key=event.event_key,
        reason=sig.reason,
        transmission=sig.transmission,
        # Upcoming prints are forward-looking; released prints are facts.
        # This keeps PREP and WRAP honest.
        report_kind=(ReportKind.GENERAL
                     if event.is_released else ReportKind.PREP),
    )


def _suppress_previews(signals: list[WeightedSignal]) -> list[WeightedSignal]:
    """
    Task 5b: When a scheduled release (ACTUAL_DATA) arrives for an indicator,
    earlier preview or forecast headlines for the same indicator family and
    same day stop counting (weight 0). Match by indicator family and date,
    not by exact text.

    Modifies signals in place by setting relevance=0 for suppressed signals.
    """
    from app.analysis.release_surprise import indicator_family
    from app.config import settings

    if not settings.preview_suppression_enabled:
        return signals

    # Collect ACTUAL signals by (indicator_family, date)
    actual_keys = set()
    for s in signals:
        if s.report_kind is ReportKind.ACTUAL:
            family = indicator_family(s.title)
            if family != "unknown":
                date_key = s.published_at.date() if s.published_at else None
                if date_key:
                    actual_keys.add((family, date_key))

    # Suppress PREP/GENERAL signals that match an ACTUAL
    for s in signals:
        if s.report_kind in (ReportKind.PREP, ReportKind.GENERAL):
            family = indicator_family(s.title)
            if family != "unknown":
                date_key = s.published_at.date() if s.published_at else None
                if date_key and (family, date_key) in actual_keys:
                    s.relevance = 0.0  # Weight will be 0 in _signal_weight
                    s.persistence = 0.0  # Extra safety

    return signals


def calendar_summary(asset: Asset, limit: int = 8) -> list[dict]:
    """Dashboard rows for one asset's economic calendar."""
    return to_calendar_summary(_calendar_signals, asset, limit=limit)


def current_calendar_signals() -> list[CalendarSignal]:
    """
    Read-only view of the most recent economic-calendar signals.

    Returned as a copy so a caller cannot mutate the module-level state that
    the scheduler owns.
    """
    return list(_calendar_signals)


def _recompute_sentiment(session, stats: IngestStats):
    """
    Rebuild every asset's CURRENT / PREP / WRAP sentiment from today's rows.

    Always recomputed from scratch rather than incrementally patched, so
    impact decay (section 17) is applied consistently on every pass.
    """
    # IMPROVEMENT 4: detect a session change at the start of this tick.
    # The reweight itself happens below, after the in-memory signal list
    # is built, so the new multipliers feed the same aggregation that runs
    # on every other tick.
    current_session = detect_session()
    previous_session = getattr(_recompute_sentiment, '_last_session', None)
    session_changed = (previous_session is not None and
                       previous_session != current_session)
    _recompute_sentiment._last_session = current_session

    from app.analysis.sentiment_engine import compute as compute_sentiment

    rows = session.scalars(
        select(NewsArticle).where(news_day_predicate())
    ).all()

    signals: list[WeightedSignal] = []
    category_weights: dict = {}
    # Phase 2: verdicts collected per asset, logged once after the loop so the
    # snapshot rows are written after every asset's verdict is persisted.
    verdicts: list = []

    for row in rows:
        if row.published_at_utc is None:
            continue
        event = row.event_links[0].event if row.event_links else None
        is_primary = (event.article_count <= 1) if event else True

        try:
            cat_enum = NewsCategory(row.category)
        except ValueError:
            cat_enum = NewsCategory.OTHER

        # Task 3: an ACTUAL scheduled-data report also carries a surprise
        # weight - its direction was already fixed at ingest from the
        # surprise sign, this is only how LARGE the surprise was.
        kind = ReportKind(row.report_kind)
        release_text = f"{row.title} {row.body}"
        printed = (release_surprise.parse(release_text)
                   if kind is ReportKind.ACTUAL else None)

        for impact in row.impacts:
            # Phase 1: category drives the decay half-life, and the event key
            # is the cluster identity - both were previously left at their
            # dataclass defaults, which pinned every article to the 6h "Other"
            # half-life and left corroboration at 1.0.
            #
            # HARDENING (2026-10-07): Asset() is no longer allowed to take the
            # recompute down with it. A legacy row whose asset string is not a
            # member of the Asset enum (e.g. "USOIL" after the OIL card was
            # retired in commit 31bf524) is skipped and counted, instead of
            # raising ValueError and killing the whole tick. The offending row
            # stays in the DB (it is still readable by the feed) but stops
            # poisoning every future sentiment computation.
            try:
                asset_enum = Asset(impact.asset)
            except ValueError:
                stats.skipped_unroutable += 1
                log.debug(
                    "Skipping unroutable impact asset %r on article %d (%s)",
                    impact.asset, row.id, row.title[:80],
                )
                continue
            signal = build_signal(
                asset=asset_enum,
                title=row.title,
                direction=Direction(impact.direction),
                strength=ImpactStrength(impact.strength),
                importance=EconomicImportance(row.importance),
                published_at=row.published_at_utc,
                relevance=impact.relevance,
                persistence=impact.persistence,
                source_quality=row.source_quality,
                source_name=row.source,
                category=cat_enum,
                is_primary=is_primary,
                event_key=event.event_key if event else "",
                # An event covered by several outlets is corroborated; a
                # single-source story is not.
                independent_sources=(event.article_count if event else 1),
                reason=impact.reason,
                transmission=impact.transmission,
                report_kind=kind,
                surprise_weight=release_surprise.surprise_weight(
                    printed, release_text),
                session_multiplier=session_relevance(
                    detect_session(), cat_enum.value),
                # Section 7: silver's dual-nature character, carried from
                # the AssetImpact row so the weight path can reward a
                # characterised XAGUSD story on every recompute.
                metal_character=impact.metal_character,
            )
            signals.append(signal)

            # IMPROVEMENT 5: gold/silver cross-asset bleed. A high-relevance,
            # high-strength macro story lands on both precious metals; the
            # secondary copy is auto-filtered to XAUUSD/XAGUSD only, so
            # Bitcoin and the equity proxy (Asset.BITCOIN) never reach it.
            bleed = build_bleed_signal(signal)
            if bleed is not None:
                signals.append(bleed)
            category_weights[cat_enum] = (
                category_weights.get(cat_enum, 0.0) + impact.relevance
            )

    assessment = regime_mod.detect_regime(category_weights)

    # Economic calendar joins the same aggregation as the news flow, so the
    # bullish/bearish verdict reflects scheduled macro risk too.
    for sig in _calendar_signals:
        cal_signal = _calendar_signal_to_weighted(sig)
        signals.append(cal_signal)
        # IMPROVEMENT 5: calendar bleed too - a high-impact FOMC/CPI print
        # moves both metals; the secondary copy is auto-filtered to the
        # XAUUSD/XAGUSD pair and is a no-op for every other asset.
        bleed = build_bleed_signal(cal_signal)
        if bleed is not None:
            signals.append(bleed)
        category_weights[sig.profile.category] = (
            category_weights.get(sig.profile.category, 0.0) + sig.relevance
        )

    # IMPROVEMENT 4, Step 2: on the tick where the session changed, rewrite
    # every signal's session_multiplier IN MEMORY so the aggregation that
    # follows uses the new session's relevance weights. The new value is
    # clamped to [0.5, 1.5] so a session flip can never swing a verdict by
    # more than a factor of 1.5x in either direction.
    #
    # This is a per-tick in-memory adjustment ONLY. The DB rows (news_article
    # / asset_impact / the WeightedSignal built from them) keep their
    # original session_multiplier; nothing is written back. The reweight is
    # recomputed from scratch on every tick, so a later tick in the same
    # session starts from the stored value again.
    if session_changed:
        for s in signals:
            new_mult = session_relevance(current_session, s.category.value)
            new_mult = max(0.5, min(1.5, new_mult))
            s.session_multiplier = new_mult
        log.info(
            "Session changed %s→%s: reweighted %d signals",
            previous_session, current_session, len(signals),
        )

    global _last_regime
    _last_regime = assessment

    # Task 5b: suppress preview/forecast headlines when actual release arrives
    signals = _suppress_previews(signals)

    # Near-duplicate clustering at scoring time (item 1 fix)
    # Collapse near-identical headlines within a 30-minute window across sources
    from app.analysis.event_clustering import cluster_signals
    signals = cluster_signals(signals, window_hours=0.5)

    # Item 8: an imminent high-impact release makes any standing bias unreliable.
    at_risk, risk_note = (False, "")
    try:
        from app.analysis.calendar_risk import event_risk_check
        at_risk, risk_note = event_risk_check(
            [s.event for s in _calendar_signals])
    except Exception as exc:  # noqa: BLE001 - never break the recompute
        log.debug("event_risk_check failed: %s", exc)

    for asset in Asset:
        asset_signals = [s for s in signals if s.asset is asset]

        current_result = compute_sentiment(
            asset_signals, asset, event_risk=at_risk,
            event_risk_note=risk_note)
        prep = market_prep.build(asset_signals, category_weights, asset)
        wrap = market_wrap.build(asset_signals, asset)

        # Item 11: persist every published bias so it can be scored later.
        try:
            _decision_log().record_bias(current_result)
        except Exception as exc:  # noqa: BLE001 - auditing is best-effort
            log.debug("decision log write failed: %s", exc)

        # Binary bias: carry-forward history must equal the PUBLISHED (CURRENT)
        # verdict. market_prep/market_wrap computed their own views above via
        # compute() and would otherwise be the last write to the shared
        # per-asset state.
        remember_bias(asset.value, current_result.bias)

        for scope, result in (
            ("CURRENT", current_result),
            ("MARKET_PREP", prep.sentiment),
            ("MARKET_WRAP", wrap.sentiment),
        ):
            session.add(
                SentimentSnapshot(
                    asset=asset.value,
                    scope=scope,
                    score=result.score,
                    confidence=result.confidence,
                    bias=result.bias.value,
                    mode=result.mode,
                    delta_60m=result.delta_60m,
                    event_risk=result.event_risk,
                    event_risk_note=result.event_risk_note,
                    damped_score=result.damped_score,
                    drivers=[{"title": d.title, "contribution": d.contribution,
                              "direction": d.direction, "weight": d.weight}
                             for d in result.drivers],
                    bull_drivers=[{"title": d.title,
                                   "contribution": d.contribution,
                                   "direction": d.direction, "weight": d.weight}
                                  for d in result.bull_drivers],
                    bear_drivers=[{"title": d.title,
                                   "contribution": d.contribution,
                                   "direction": d.direction, "weight": d.weight}
                                  for d in result.bear_drivers],
                    conflict=({"title": result.conflict.title,
                               "contribution": result.conflict.contribution,
                               "direction": result.conflict.direction,
                               "weight": result.conflict.weight}
                              if result.conflict else None),
note=result.note,
                     contributing_articles=result.contributing_articles,
                     signal_count=result.signal_count,
                     contradiction_detected=result.contradiction_detected,
                     burst_bull=result.burst_bull,
                     burst_bear=result.burst_bear,
                     price_note=_price_note(asset),
                )
            )
        log.info(
            "%s: %s score=%+.1f (n=%d conf=%.2f event_risk=%s)",
            asset.value, current_result.bias.value, current_result.score,
            current_result.contributing_articles, current_result.confidence,
            at_risk,
        )
        verdicts.append(AssetVerdict(
            asset=asset,
            score=current_result.score,
            confidence=current_result.confidence,
            bias=current_result.bias.value,
            article_count=current_result.contributing_articles,
            last_signal_age_min=_last_signal_age_min(
                asset_signals, T.now_utc()),
            bull_drivers=current_result.bull_drivers,
            bear_drivers=current_result.bear_drivers,
        ))

    # Phase 2: append-only validation log, written AFTER every verdict is
    # already persisted. Wrapped so it can never break or slow a poll.
    try:
        from app.services.bias_snapshots import write_bias_snapshots

        written = write_bias_snapshots(session, verdicts)
        if written:
            log.debug("bias_snapshots: %d row(s) appended", written)
    except Exception as exc:  # noqa: BLE001 - diagnostics never break a poll
        log.warning("bias snapshot stage skipped: %s", exc)

    # Session-aware global equity card: NOW-only, in-memory, never persisted.
    # No SentimentSnapshot / BiasSnapshot rows - the frontend keys flip state
    # by the region field in the payload.
    global _last_equity_card
    try:
        from app.analysis.equity_card import build_equity_card, to_payload
        _last_equity_card = to_payload(build_equity_card(signals))
    except Exception as exc:  # noqa: BLE001 - equity card never breaks a poll
        log.debug("equity card build failed: %s", exc)
        _last_equity_card = None

    return signals, assessment


def current_equity_card() -> dict | None:
    """Read-only view of the most recent equity card (a copied dict)."""
    return dict(_last_equity_card) if _last_equity_card else None


def _is_article_today_query(published: datetime | None) -> bool:
    """
    The TODAY test, re-evaluated NOW rather than trusting a stored flag.

    ``NewsArticle.is_today`` is written once at ingest time and never
    recomputed, so on a long-running process it drifts: every article ever
    ingested stays flagged True, and the feed keeps re-serving yesterday's
    headlines as today's news. Re-deriving the day from the stored publication
    time against the single boundary in ``timeutils.market_day_start_utc`` is
    what keeps "today" meaning today.
    """
    if published is None:
        return False
    return T.is_within_market_day(published)


def news_day_predicate():
    """
    SQLAlchemy predicate matching the rows that count as TODAY right now.

    Shared by every reader (dashboard feed, sentiment recompute, article
    counters) so no consumer can disagree about which day it is looking at.

    FinancialJuice session reports are deliberately excluded from the
    publication-time test: they are admitted on the INDIAN trading date (see
    ``_fj_session_report_is_admissible``), and the US Market Wrap legitimately
    lands on the previous ET day. Applying the ET test to them would throw that
    report away every evening, exactly the regression the IST carve-out exists
    to prevent. They stay governed by ``is_today``, which is the flag ingest set
    for them.
    """

    day_start = T.market_day_start_utc().replace(tzinfo=None)

    def _is_fj_source(col):
        """
        SQL twin of ``fj_sessions.is_financial_juice``.

        That helper matches by stripping separators and lowercasing, so a
        config rename ("Financial Juice" vs "financialjuice") still matches.
        Mirror that normalisation here instead of hard-coding one spelling,
        otherwise the carve-out silently stops applying after a source rename.
        """
        flat = func.lower(col)
        for ch in (" ", "-", "_", ".", "'"):
            flat = func.replace(flat, ch, "")
        return flat.contains(FINANCIAL_JUICE_SOURCE)

    return and_(
        NewsArticle.is_today.is_(True),
        or_(
            NewsArticle.published_at_utc >= day_start,
            _is_fj_source(NewsArticle.source),
        ),
    )


def _load_existing_guids(session) -> set[str]:
    return {g for (g,) in session.execute(select(NewsArticle.guid)).all()}


def purge_old_news(session, *, keep_days: int | None = None) -> dict:
    """
    Delete news older than the retention window (section 19a).

    Keeps the last ``keep_days`` calendar days *in the market timezone* -
    with the default of 2 that means today plus the previous day, and anything
    older is removed on the next pipeline pass.

    Why the market timezone and not UTC: an article's trading day is decided by
    the same market-local test the ingest filter uses (``T.is_today``). Cutting
    on a UTC midnight boundary would keep part of two-day-old news and discard
    part of yesterday's, because ET midnight is 04:00/05:00 UTC.

    Child rows go with it: ``asset_impact``, ``article_event`` and ``alert``
    all declare ``ondelete="CASCADE"`` on ``article_id``, so deleting the
    parent removes them. ``news_event`` does NOT cascade from the article - it
    is a separate cluster row that many articles share - so events left with no
    articles are swept separately, otherwise they would accumulate forever.

    Never raises: a retention failure must not take the pipeline down.
    """
    keep = settings.news_retention_days if keep_days is None else keep_days
    result = {"articles": 0, "events": 0, "cutoff": None}
    if keep <= 0:
        return result  # purging disabled

    try:
        from datetime import datetime as _dt
        from datetime import timedelta, timezone

        tz = T.market_tz()
        today_local = T.now_utc().astimezone(tz).date()
        cutoff_date = today_local - timedelta(days=keep - 1)
        cutoff = _dt.combine(cutoff_date, _dt.min.time(), tzinfo=tz)
        cutoff_utc = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
        result["cutoff"] = cutoff_utc

        # Articles with no usable timestamp cannot be placed on a day, so they
        # are kept rather than silently destroyed - ingest rejects them anyway.
        stale = session.execute(
            select(NewsArticle.id).where(
                NewsArticle.published_at_utc.isnot(None),
                NewsArticle.published_at_utc < cutoff_utc,
            )
        ).all()
        if stale:
            ids = [r[0] for r in stale]
            session.execute(
                delete(NewsArticle).where(NewsArticle.id.in_(ids))
            )
            result["articles"] = len(ids)

        # FLUSH BEFORE looking for orphans. This purge runs in the same session
        # and transaction as ingestion, so brand-new articles have ArticleEvent
        # and AssetImpact rows still pending in the unit of work. The orphan
        # query below is raw SQL and can only see rows already in the database -
        # pending inserts are invisible to it, so a freshly created event looks
        # orphaned, gets deleted, and the subsequent flush then fails with
        # "FOREIGN KEY constraint failed" when its impact row lands. Flushing
        # first makes the orphan query truthful.
        session.flush()

        # Events are only meaningful while an article still points at them.
        orphan_events = session.execute(
            select(NewsEvent.id).where(
                ~exists().where(ArticleEvent.event_id == NewsEvent.id)
            )
        ).all()
        if orphan_events:
            eids = [r[0] for r in orphan_events]
            session.execute(delete(NewsEvent).where(NewsEvent.id.in_(eids)))
            result["events"] = len(eids)

        session.flush()
        if result["articles"] or result["events"]:
            log.info(
                "Retention: removed %d article(s) and %d orphaned event(s) "
                "published before %s (keeping %d day(s))",
                result["articles"], result["events"], cutoff_date, keep,
            )
    except Exception as exc:  # noqa: BLE001 - never break the pipeline
        log.warning("News retention purge skipped: %s", exc)
        result["error"] = str(exc)
    return result


def _purge_unroutable_impacts(session) -> int:
    """
    Delete ``asset_impact`` rows whose asset string is not a member of the
    ``Asset`` enum.

    Hardening added 2026-10-07 after commit 31bf524 retired ``Asset.OIL`` while
    399 legacy rows still carried ``asset='USOIL'``. ``_recompute_sentiment``
    called ``Asset(impact.asset)`` on every one of them and raised
    ``ValueError``, which killed every pipeline tick (the scheduler itself
    never died - ``run_once`` catches and logs it).

    The rows are dropped here, before the recompute, so the enum lookup can
    never see them again. Deleting is safe: the parent ``news_article`` row is
    unaffected (it is still served by the feed), and the article's impact on
    the three supported assets is re-derived from its text on every recompute.

    Also cleans stale ``sentiment_snapshot`` rows whose asset string is not a
    member of the ``Asset`` enum (e.g. legacy ``asset='USOIL'`` snapshots left
    behind by pre-fix pipeline runs). These are harmless but accumulate and
    clutter the DB.
    """
    from sqlalchemy import delete

    valid = tuple(a.value for a in Asset)
    try:
        res = session.execute(
            delete(AssetImpact).where(~AssetImpact.asset.in_(valid))
        )
        session.flush()
        impact_count = res.rowcount or 0
        # Also clean stale snapshots for non-Asset strings.
        snap_count = session.execute(
            delete(SentimentSnapshot).where(~SentimentSnapshot.asset.in_(valid))
        ).rowcount or 0
        session.flush()
        return impact_count
    except Exception as exc:  # noqa: BLE001 - cleanup must not break a poll
        log.warning("Unroutable impact cleanup skipped: %s", exc)
        return 0


async def run_once() -> IngestStats:
    """
    Execute one full pipeline pass.

    Never raises - errors are logged and returned in ``stats.errors`` so the
    scheduler keeps running.
    """
    global _running, _last_update, _last_successful_tick, _news_since_update, _calendar_signals, _tick_started_at

    if _running:
        log.debug("Pipeline already running; skipping this tick")
        return IngestStats(errors=["pipeline already running"])

    # Reset LLM circuit breaker at the start of each tick so a degraded
    # endpoint gets a fresh chance on the next poll.
    await llm_client.reset_circuit_breaker()

    _running = True
    _tick_started_at = T.now_utc()
    stats = IngestStats()
    started = _tick_started_at

    try:
        # 1) INGESTION ---------------------------------------------------
        report = await fetch_all_news()
        stats.fetched = len(report.articles)
        stats.errors.extend(report.errors)

        # Economic calendar: optional enrichment, never fatal. Failures are
        # recorded and the pipeline continues with news-only sentiment.
        events, calendar_errors = await fetch_calendar()
        stats.errors.extend(calendar_errors)
        _calendar_signals = signals_for_events(
            events, horizon_hours=settings.economic_calendar_horizon_hours
        )
        stats.calendar_events = len(events)
        stats.calendar_signals = len(_calendar_signals)

        with session_scope() as session:
            run_row = PipelineRun(
                started_at=started, status="running",
                articles_fetched=stats.fetched,
                detail={"sources_polled": report.sources_polled,
                        "sources_ok": report.sources_ok},
            )
            session.add(run_row)
            run_id = run_row.id
            session.flush()

            existing = _load_existing_guids(session)
            new_articles: list[NewsArticle] = []

            # CHANGE 7: rank the fetched batch BEFORE the LLM cap is applied,
            # so the 15 highest-priority articles get model attention. Without
            # this the cap is hit in arrival order and a rush of low-quality
            # noise can consume every slot, leaving a Tier 1 release to
            # heuristics. Sort is by source_quality then published_at (newest
            # first); every article is still ingested, only the classification
            # mode differs.
            report.articles = _priority_sort_articles(report.articles)

            llm_calls = 0
            for art in report.articles:
                if art.guid in existing:
                    if _is_fj_session_report_sync(art):
                        log.debug(
                            "FJ-SESSION DUP (guid already in DB): %r guid=%s",
                            art.title[:80], art.guid,
                        )
                    continue
                # Cap LLM classifications per tick. Once the batch limit is
                # reached, remaining new articles get deterministic heuristics
                # so a degraded endpoint cannot turn one tick into a 10-minute
                # slog. Heuristics are always available and correct.
                use_llm = settings.has_llm and llm_calls < LLM_BATCH_LIMIT
                try:
                    row = await _ingest_article(art, session, stats, use_llm)
                    if row is not None:
                        existing.add(art.guid)
                        new_articles.append(row)
                        if use_llm:
                            llm_calls += 1
                    elif _is_fj_session_report_sync(art):
                        log.debug(
                            "FJ-SESSION REJECTED by _ingest_article: %r guid=%s",
                            art.title[:80], art.guid,
                        )
                    elif use_llm and settings.has_llm:
                        # Article was rejected (not new) but would have used LLM slot
                        # Don't count this against the batch limit since it's not a new article
                        pass
                except Exception as exc:
                    log.exception("Failed to ingest: %s", art.title[:80])
                    stats.errors.append(f"ingest: {type(exc).__name__}: {exc}")
                    # A flush failure (e.g. UNIQUE constraint on guid) leaves the
                    # SQLAlchemy session in a "pending rollback" state. Without
                    # this, EVERY subsequent article in the same tick dies with
                    # PendingRollbackError and the whole batch is lost.
                    try:
                        session.rollback()
                    except Exception:
                        pass
                    # Diagnose why a guid that is clearly in the DB still fails.
                    if "UNIQUE constraint" in str(exc):
                        with session_scope() as probe:
                            from app.models.db_models import NewsArticle as _NA
                            from sqlalchemy import select as _sel, func as _fn
                            cnt = probe.scalar(
                                _fn.count(_NA.id).where(_NA.guid == art.guid))
                            log.error(
                                "GUID-COLLISION DIAG: guid=%s in_existing=%s "
                                "db_count=%s source=%s",
                                art.guid, art.guid in existing, cnt, art.source,
                            )

            _news_since_update += len(new_articles)
            stats.articles_new = len(new_articles)

            # 1b) BACKLOG RE-CLASSIFICATION --------------------------------
            # Re-classify up to 5 articles per tick that were previously
            # processed with heuristics (llm_used=false), newest first.
            # Only runs when LLM is configured and circuit breaker is closed.
            stats.llm_backlog_reclassified = await _reclassify_backlog(session, stats, started)

            # 1c) RETENTION -------------------------------------------------
            # Runs BEFORE alerts and the sentiment recompute so both work
            # against an already-trimmed database. Runs on every pass, not
            # just once a day: the window is date-based, so a machine that
            # was off overnight still self-corrects on its next tick.
            stats.purged = purge_old_news(session)

            # 1c) CLEAN UNROUTABLE IMPACTS ---------------------------------
            # Legacy rows whose asset string is not a member of the Asset
            # enum (e.g. "USOIL" after the OIL card was retired) can never be
            # re-scored. Drop their impact rows now so the recompute below
            # never has to see them, and so the DB does not accumulate them.
            unroutable = _purge_unroutable_impacts(session)
            log.info(
                "Removed %d unroutable asset_impact rows",
                unroutable,
            )

            # 2) ALERTS ----------------------------------------------------
            from app.services.alerts import evaluate_and_emit
            evaluate_and_emit(session, stats)

            # 3) SENTIMENT + PREP/WRAP ------------------------------------
            _recompute_sentiment(session, stats)

            run_row.articles_new = stats.new_today
            run_row.articles_rejected = stats.rejected
            run_row.llm_errors = stats.llm_new_failed + stats.llm_backlog_failed
            run_row.status = "ok"
            run_row.finished_at = T.now_utc()

            # 4) HEALTH CACHE ------------------------------------------------
            # Update the in-memory metrics that /api/health and /api/dashboard
            # use to detect staleness without a DB query. Must stay inside the
            # session so the queries below actually run.
            global _health_metrics
            try:
                last_art = session.query(
                    func.max(NewsArticle.published_at_utc)).scalar()
                last_snap = session.query(
                    func.max(SentimentSnapshot.computed_at)).scalar()
                _health_metrics = {
                    "last_article_utc": (
                        last_art.isoformat() if last_art else None
                    ),
                    "last_snapshot_utc": (
                        last_snap.isoformat() if last_snap else None
                    ),
                    "per_source": report.source_stats,
                }
            except Exception as exc:  # noqa: BLE001 - health cache must never break the tick
                log.debug("health metrics cache update skipped: %s", exc)

        _last_update = T.now_utc()
        _last_successful_tick = _last_update.isoformat()
        log.info(
            "Pipeline complete: fetched=%d new=%d rejected=%d dup=%d "
            "llm_new_ok=%d llm_new_fail=%d llm_new_skip_batch=%d llm_new_skip_breaker=%d "
            "llm_backlog_ok=%d llm_backlog_fail=%d llm_backlog_reclassified=%d purged=%d unroutable=%d",
            stats.fetched, stats.new_today, stats.rejected, stats.duplicates,
            stats.llm_new_ok, stats.llm_new_failed, stats.llm_new_skipped_batch,
            stats.llm_new_skipped_breaker,
            stats.llm_backlog_ok, stats.llm_backlog_failed, stats.llm_backlog_reclassified,
            stats.purged.get("articles", 0),
            stats.skipped_unroutable,
        )
        return stats
    except Exception as exc:
        log.exception("Pipeline run failed")
        stats.errors.append(f"pipeline: {type(exc).__name__}: {exc}")
        try:
            with session_scope() as session:
                row = session.get(PipelineRun, run_id) if "run_id" in dir() else None
                if row is not None:
                    row.status = "error"
                    row.finished_at = T.now_utc()
                    row.detail = {"error": str(exc)}
        except Exception:  # noqa: BLE001
            pass
        return stats
    finally:
        _running = False
        _tick_started_at = None
