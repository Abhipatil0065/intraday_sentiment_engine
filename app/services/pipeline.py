"""
PIPELINE ORCHESTRATOR (section 26).

  INGESTION -> NEWS EXTRACTION -> TODAY FILTER -> DEDUP -> CLASSIFY
  -> RECENCY -> ASSET IMPACT -> AGGREGATE -> PREP/WRAP -> PERSIST

This module owns the end-to-end control flow. The background scheduler calls
``run_once``; the API and dashboard read the results it persists.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, delete, exists, func, or_, select

from app.analysis.economic_impact import (
    CalendarSignal,
    headline as calendar_headline,
    signals_for_events,
    to_calendar_summary,
)
from app.analysis.event_deduplication import EventDeduplicator
from app.analysis.fj_sessions import FINANCIAL_JUICE_SOURCE
from app.analysis.news_classifier import analyze_article
from app.analysis.sentiment_engine import WeightedSignal
from app.analysis.signal_factory import build_signal
from app.services.bias_snapshots import AssetVerdict
from app.config import settings
from app.database import session_scope
from app.ingestion.base import RawArticle
from app.ingestion.economic_calendar import fetch_calendar
from app.ingestion.news_fetcher import fetch_all_news
from app.logging_conf import get_logger
from app.market import market_prep, market_wrap, regime as regime_mod
from app.market.session_detector import (
    detect_session,
    next_session,
    previous_session,
    session_relevance,
)
from app.models.db_models import (
    Alert,
    AssetImpact,
    NewsArticle,
    NewsEvent,
    ArticleEvent,
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
    SessionName,
)
from app.utils import timeutils as T

log = get_logger(__name__)

# Shared across runs so dedup survives within a process session.
_dedup = EventDeduplicator()
_last_update: Optional[datetime] = None
_news_since_update = 0
_running = False
_last_regime = None  # RegimeAssessment from the most recent recompute
# Economic-calendar signals from the most recent fetch, shared with the API.
_calendar_signals: list[CalendarSignal] = []


def runtime_state() -> dict:
    return {
        "running": _running,
        "last_updated": T.stamp() if _last_update else None,
        "new_news_since_last_update": _news_since_update,
        "regime": _last_regime,
    }


def reset_counter() -> None:
    """Called by the API so 'new since last update' tracks the dashboard view."""
    global _news_since_update
    _news_since_update = 0


def _parse_article_time(art: RawArticle) -> tuple[Optional[datetime], Optional[datetime], Optional[str]]:
    published, label = T.parse_timestamp(art.published_raw)
    updated, _ = T.parse_timestamp(art.updated_raw)
    return published, updated, label


def _is_update_article(published: Optional[datetime], updated: Optional[datetime]) -> bool:
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
    llm_ok: int = 0
    llm_failed: int = 0
    calendar_events: int = 0
    calendar_signals: int = 0
    errors: list[str] = field(default_factory=list)
    purged: dict = field(default_factory=dict)


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


def _fj_session_report_is_admissible(published: datetime,
                                    now: Optional[datetime] = None) -> bool:
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


async def _ingest_article(art: RawArticle, session, stats: IngestStats):
    """Apply the TODAY filter, classify, cluster and store one article."""
    published, updated, tz_label = _parse_article_time(art)

    # --- ROLLING-WINDOW FILTER (improvement item 3) ---
    # Replaces the "same market-timezone calendar date" gate. Admitting news on
    # RECENCY rather than on which calendar day it landed in means the overnight
    # US wrap (~01:05 IST) passes on age alone, so the old FinancialJuice / IST
    # date carve-out below is no longer needed for ordinary headline flow.
    if published is None:
        stats.rejected += 1
        return None
    if not T.within_rolling_window(published) or not T.within_max_age(published):
        stats.rejected += 1
        return None
    if await _is_fj_session_report(art):
        # Session reports still get their own IST-date rule on top, because a
        # US wrap published ~15:35 ET the previous day must be able to appear on
        # the Indian trading date the dashboard renders it under.
        if not _fj_session_report_is_admissible(published):
            stats.rejected += 1
            return None

    # --- EVENT DEDUPLICATION (section 16) ---
    dedupe = _dedup.match(art.title, published)

    row = NewsArticle(
        guid=art.guid,
        source=art.source,
        source_quality=art.source_quality,
        url=art.url,
        title=art.title,
        body=(art.body or "")[: settings.article_fetch_max_chars],
        summary=art.summary or "",
        published_at_utc=published,
        published_tz_label=tz_label,
        updated_at_source=updated,
        is_today=True,
        is_updated_article=_is_update_article(published, updated),
    )

    # --- NLP / LLM ANALYSIS ---
    analysis, llm_ok = await analyze_article(art.title, art.body)
    stats.llm_ok += 1 if llm_ok else 0
    stats.llm_failed += 0 if llm_ok else 1

    row.category = analysis.category.value
    row.importance = analysis.importance.value
    row.report_kind = analysis.report_kind.value
    row.verification = analysis.verification.value
    row.relevance_score = analysis.relevance_score
    row.analysis_mode = analysis.mode
    row.analysis_note = analysis.note

    session.add(row)
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
            first_seen_utc=published,
            last_updated_utc=published,
            article_count=1,
        )
        session.add(event)
        session.flush()
    else:
        event.article_count += 1
        event.last_updated_utc = published
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


def _last_signal_age_min(signals, now: datetime) -> Optional[float]:
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
        hours = T.age_hours(s.published_at, now=now)
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


def _recompute_sentiment(session):
    """
    Rebuild every asset's CURRENT / PREP / WRAP sentiment from today's rows.

    Always recomputed from scratch rather than incrementally patched, so
    impact decay (section 17) is applied consistently on every pass.
    """
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

        for impact in row.impacts:
            # Phase 1: category drives the decay half-life, and the event key
            # is the cluster identity - both were previously left at their
            # dataclass defaults, which pinned every article to the 6h "Other"
            # half-life and left corroboration at 1.0.
            signals.append(
                build_signal(
                    asset=Asset(impact.asset),
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
                    report_kind=ReportKind(row.report_kind),
                    session_multiplier=session_relevance(
                        detect_session(), cat_enum.value),
                )
            )
            category_weights[cat_enum] = (
                category_weights.get(cat_enum, 0.0) + impact.relevance
            )

    assessment = regime_mod.detect_regime(category_weights)

    # Economic calendar joins the same aggregation as the news flow, so the
    # bullish/bearish verdict reflects scheduled macro risk too.
    for sig in _calendar_signals:
        signals.append(_calendar_signal_to_weighted(sig))
        category_weights[sig.profile.category] = (
            category_weights.get(sig.profile.category, 0.0) + sig.relevance
        )

    global _last_regime
    _last_regime = assessment

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

    return signals, assessment


def _is_article_today_query(published: Optional[datetime]) -> bool:
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
    from app.analysis.fj_sessions import FINANCIAL_JUICE_SOURCE

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


def purge_old_news(session, *, keep_days: Optional[int] = None) -> dict:
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
        from datetime import datetime as _dt, timedelta, timezone

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


async def run_once() -> IngestStats:
    """
    Execute one full pipeline pass.

    Never raises - errors are logged and returned in ``stats.errors`` so the
    scheduler keeps running.
    """
    global _running, _last_update, _news_since_update, _calendar_signals

    if _running:
        log.debug("Pipeline already running; skipping this tick")
        return IngestStats(errors=["pipeline already running"])

    _running = True
    stats = IngestStats()
    started = T.now_utc()

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

            for art in report.articles:
                if art.guid in existing:
                    continue
                try:
                    row = await _ingest_article(art, session, stats)
                    if row is not None:
                        existing.add(art.guid)
                        new_articles.append(row)
                except Exception as exc:  # noqa: BLE001 - isolate per-article
                    log.exception("Failed to ingest: %s", art.title[:80])
                    stats.errors.append(f"ingest: {type(exc).__name__}: {exc}")

            _news_since_update += len(new_articles)
            stats.articles_new = len(new_articles)

            # 1b) RETENTION -------------------------------------------------
            # Runs BEFORE alerts and the sentiment recompute so both work
            # against an already-trimmed database. Runs on every pass, not
            # just once a day: the window is date-based, so a machine that
            # was off overnight still self-corrects on its next tick.
            stats.purged = purge_old_news(session)

            # 2) ALERTS ----------------------------------------------------
            from app.services.alerts import evaluate_and_emit
            evaluate_and_emit(session, stats)

            # 3) SENTIMENT + PREP/WRAP ------------------------------------
            _recompute_sentiment(session)

            run_row.articles_new = stats.new_today
            run_row.articles_rejected = stats.rejected
            run_row.llm_errors = stats.llm_failed
            run_row.status = "ok"
            run_row.finished_at = T.now_utc()

        _last_update = T.now_utc()
        log.info(
            "Pipeline complete: fetched=%d new=%d rejected=%d dup=%d "
            "llm_ok=%d llm_fail=%d purged=%d",
            stats.fetched, stats.new_today, stats.rejected, stats.duplicates,
            stats.llm_ok, stats.llm_failed, stats.purged.get("articles", 0),
        )
        return stats

    except Exception as exc:  # noqa: BLE001 - scheduler must survive
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
