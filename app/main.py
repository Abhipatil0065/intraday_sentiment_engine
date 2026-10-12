"""
FastAPI application (section 26 - REAL-TIME DASHBOARD + API).

Serves the JSON API, a live WebSocket feed and a zero-dependency HTML
dashboard, and runs the ingestion pipeline on an APScheduler background job.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from app.analysis import fj_sessions, llm_client
from app.analysis.display import to_display
from app.analysis.sentiment_engine import DEFAULT_BIAS
from app.config import settings
from app.database import init_db, session_scope
from app.ingestion.base import parse_source_config
from app.logging_conf import get_logger, setup_logging
from app.market.session_detector import (
    detect_session,
    equity_session,
    equity_title,
    next_session,
    previous_session,
)
from app.models.db_models import (
    Alert,
    BiasOutcome,
    NewsArticle,
    NewsEvent,
    PipelineRun,
    SentimentSnapshot,
)
from app.models.enums import (
    Asset,
    MarketRegime,
)
from app.services import overnight, pipeline
from app.utils import timeutils as T

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "dashboard" / "web"
_scheduler: AsyncIOScheduler | None = None

# Connected WebSocket clients for live push updates.
_clients: set[WebSocket] = set()


def _run_pipeline_sync() -> pipeline.IngestStats:
    """Run the pipeline in a separate thread with its own event loop."""
    return asyncio.run(pipeline.run_once())


async def _scheduled_tick() -> None:
    """Background job: run the pipeline in a worker thread, then push an update.

    Running the pipeline in a thread pool prevents synchronous SQLite writes
    from blocking the main event loop, so /api/health and /api/dashboard
    remain responsive during a tick.
    """
    started = T.stamp()
    try:
        stats = await asyncio.to_thread(_run_pipeline_sync)
        log.info(
            "Tick %s done: fetched=%d new=%d rejected=%d dup=%d "
            "unroutable=%d llm_new_ok=%d llm_new_fail=%d llm_backlog_ok=%d llm_backlog_fail=%d errors=%d purged=%d",
            started, stats.fetched, stats.new_today, stats.rejected,
            stats.duplicates, stats.skipped_unroutable,
            stats.llm_new_ok, stats.llm_new_failed,
            stats.llm_backlog_ok, stats.llm_backlog_failed,
            len(stats.errors), stats.purged.get("articles", 0),
        )
        if stats.errors:
            log.warning("Tick %s errors: %s", started, stats.errors[:5])
    except Exception as exc:
        log.exception("Scheduled pipeline tick failed: %s", exc)


async def _boot_morning_brief() -> None:
    """
    Overnight catch-up: LLM-judge everything published while the laptop was
    off, then rebuild sentiment from the refreshed verdicts.

    Runs in the background so the dashboard is never blocked by it, is
    skipped when the shutdown gap is too small to matter, and is guarded by a
    cross-process lock so the Windows logon task cannot duplicate the work.
    Never raises.
    """
    try:
        report = await overnight.run_boot_catchup()
        status = report.get("status")
        if status == "completed":
            log.info("MORNING BRIEF: %s", overnight.summarise(report))
        else:
            log.info("Morning brief %s: %s", status or "skipped",
                     report.get("reason", ""))
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("Morning brief failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    global _scheduler

    # Hermetic test mode: never start the APScheduler and never prime the
    # pipeline. ``TestClient`` entering this lifespan would otherwise spawn a
    # background tick that outlives the test process (orphaned python
    # processes), and the scheduler thread keeps the event loop alive after
    # the test finishes. Set APP_TESTING=1 (see tests/conftest.py) to opt in.
    _testing = os.environ.get("APP_TESTING", "").strip() in ("1", "true", "yes")

    # Warm bias memory from the database: load the last real biases
    # (HIGH/MEDIUM confidence, non-stale) so the pipeline doesn't start cold.
    if not _testing:
        try:
            from app.analysis.sentiment_engine import warm_bias_history
            from app.database import session_scope
            with session_scope() as session:
                rows = {}
                for asset in Asset:
                    snap = session.scalar(
                        select(SentimentSnapshot)
                        .where(SentimentSnapshot.asset == asset.value)
                        .where(SentimentSnapshot.scope == "CURRENT")
                        .where(SentimentSnapshot.stale == False)
                        .where(SentimentSnapshot.confidence_label.in_(("HIGH", "MEDIUM")))
                        .order_by(SentimentSnapshot.computed_at.desc())
                        .limit(1)
                    )
                    if snap:
                        rows[asset.value] = snap.bias
                if rows:
                    warm_bias_history(rows)
                    log.info("Bias warm-up loaded: %s", {k: v for k, v in rows.items()})
        except Exception as exc:  # noqa: BLE001 - never block startup
            log.warning("Bias warm-up skipped: %s", exc)

    if not _testing and settings.morning_brief_on_boot:
        # Overnight catch-up first: it rewrites the rows the morning's first
        # sentiment recompute must read, so the gap is judged before the
        # session starts, not three minutes into it.
        asyncio.create_task(_boot_morning_brief())

    if not _testing and settings.poll_interval_seconds > 0:
        _scheduler = AsyncIOScheduler(timezone=settings.market_timezone)
        _scheduler.add_job(
            _scheduled_tick,
            trigger=IntervalTrigger(seconds=settings.poll_interval_seconds),
            id="intraday_pipeline",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )
        _scheduler.start()
        log.info("Scheduler started (every %ds)", settings.poll_interval_seconds)

    if not _testing:
        # Prime the pipeline immediately so the dashboard is never empty.
        asyncio.create_task(_scheduled_tick())

    yield

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    # Stamp the engine's exit so the next boot can size the overnight window.
    # Best-effort: a hard power-off leaves the database fallback instead.
    try:
        overnight.record_shutdown()
    except Exception as exc:  # noqa: BLE001 - never block shutdown
        log.debug("Shutdown marker skipped: %s", exc)
    log.info("Shutdown complete")


app = FastAPI(
    title="Intraday News Sentiment & Market Impact Engine",
    description="News-based intraday sentiment for XAUUSD, XAGUSD and BTCUSD.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: the Streamlit tab embeds the HTML dashboard, which fetches /api/*
# and opens /ws from a different port (8501 -> 8000). Browsers treat the two
# as distinct origins, so without this every dashboard request is blocked.
# Allow anything on loopback so localhost and 127.0.0.1 both work.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8501", "http://127.0.0.1:8501",
                   "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _latest_snapshot(session, asset: str, scope: str):
    """Most recent snapshot for an asset+scope (snapshots are append-only)."""

    return session.scalar(
        select(SentimentSnapshot)
        .where(SentimentSnapshot.asset == asset)
        .where(SentimentSnapshot.scope == scope)
        .order_by(SentimentSnapshot.computed_at.desc())
        .limit(1)
    )


def _bias_since(session, asset: str, scope: str, score: float) -> str | None:
    """
    When the current dominant side began, as an ISO/UTC string (or None).

    The dashboard's flip stamp is client-side, so a browser that opens the page
    mid-session has never seen a flip happen and shows no time at all - even for
    an asset that has clearly been leaning one way for hours. This walks the
    append-only snapshot history backwards until it reaches the opposite side and
    returns the start of the current run, which is exactly what the user wants to
    read: "this stance has held since ...".

    Zero scores are skipped rather than treated as a change, matching the display
    contract where an even 50/50 has no dominant side to flip.

    Bounded by _BIAS_LOOKBACK: an asset that has held one side for longer than
    that many snapshots reports the oldest one it has, which is the earliest
    moment the engine can actually prove. It never invents a time.
    """

    if not score:                      # even split -> no dominant side
        return None
    side = 1 if score > 0 else -1

    rows = session.execute(
        select(SentimentSnapshot.computed_at, SentimentSnapshot.score)
        .where(SentimentSnapshot.asset == asset)
        .where(SentimentSnapshot.scope == scope)
        .order_by(SentimentSnapshot.computed_at.desc())
        .limit(_BIAS_LOOKBACK)
    ).all()

    started = None
    for computed_at, s in rows:
        if s and (1 if s > 0 else -1) == side:
            started = computed_at      # keep walking back over the same side
        else:
            break                      # reached the opposite side - stop
    if started is None:
        return None
    # SQLite hands back naive datetimes; these were written as UTC.
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started.isoformat()


# How far back the "current side started" search looks. 500 snapshots is far
# more than a trading day at a 180s poll, so this only ever bites on a feed that
# has held one side for days.
_BIAS_LOOKBACK = 500

# No successful tick in this many minutes => /api/health returns 503 "degraded".
# Defined in the pipeline (next to the state it evaluates, and next to
# compute_stale) so the health endpoint and the dashboard payload builder can
# never drift apart; aliased here for existing importers of app.main.
HEALTH_STALE_MINUTES = pipeline.HEALTH_STALE_MINUTES


def _block(session, asset: str, scope: str) -> dict:
    """
    One sentiment block, or an explicit empty state (never invented data).

    BINARY BIAS: the block reports score / confidence / bias, where bias is
    strictly BULLISH or BEARISH. ``stale`` marks a carried-forward verdict -
    no signals reached this asset in the window (n=0), or no snapshot exists
    at all.
    """
    snap = _latest_snapshot(session, asset, scope)
    if snap is None:
        # Nothing computed yet. No signals = NO_BIAS verdict, passed through
        # to to_display() which will render NEUTRAL with a 50/50 split.
        return {
            "asset": asset, "scope": scope,
            "score": 0.0, "confidence": 0.0, "bias": "NO_BIAS",
            "display": to_display(0.0, "NO_BIAS"),
            # No snapshots at all, so there is no stance to date.
            "bias_since_utc": None,
            "mode": "heuristic", "delta_60m": 0.0,
            "event_risk": False, "event_risk_note": "",
            "drivers": [], "bull_drivers": [], "bear_drivers": [], "conflict": None,
            "stale": True,
            "confidence_label": "LOW",
            "note": "No sentiment computed yet for this scope.",
            "contributing_articles": 0, "contributing_clusters": 0,
            "signal_count": 0,
            "contradiction_detected": False,
            "burst_bull": 0, "burst_bear": 0,
            # Item 1: no baseline yet during warm-up.
            "raw_score": None, "baseline_score": None,
            "price_note": "Price-action confirmation unavailable.",
            "last_updated": "",
        }
    # The display bull/bear split is derived from the raw engine verdict
    # (raw_score), NOT the baseline-adjusted score. The adjusted score
    # centres around 0 and is therefore useless for a magnitude-based
    # percentage - using it would make every card read 50/50.
    _display_raw = getattr(snap, "raw_score", snap.score)
    if _display_raw is None:
        _display_raw = snap.score
    return {
        "asset": asset, "scope": scope,
        # score / confidence / bias are the ENGINE's answers and stay exactly
        # as they are: the decision log, hit-rate tooling and any other
        # consumer read these fields. "display" is added alongside them for the
        # dashboard's own use and is never written back.
        "score": snap.score,
        "damped_score": snap.damped_score,
        "confidence": snap.confidence,
        "bias": snap.bias,
        "display": to_display(_display_raw, snap.bias),
        # When this dominant side began, from snapshot history. The card uses it
        # to show "since ..." on a fresh page load, before the browser has
        # witnessed a flip of its own.
        "bias_since_utc": _bias_since(session, asset, scope, snap.score),
        "mode": snap.mode or "heuristic",
        "delta_60m": snap.delta_60m,
        "event_risk": bool(snap.event_risk),
        "event_risk_note": snap.event_risk_note or "",
        "drivers": snap.drivers or [],
        "bull_drivers": snap.bull_drivers or [],
        "bear_drivers": snap.bear_drivers or [],
        "conflict": snap.conflict,
        # Binary bias: a snapshot with n=0 carried its bias forward from
        # history (or the newest article / DEFAULT_BIAS) instead of news in
        # this window.
        "stale": bool(getattr(snap, "stale", False)) or snap.contributing_articles == 0,
        "confidence_label": getattr(snap, "confidence_label", "LOW"),
        "note": snap.note or "",
        "contributing_articles": snap.contributing_articles,
        "contributing_clusters": 0,
        "signal_count": getattr(snap, "signal_count", 0),
        "contradiction_detected": bool(getattr(snap, "contradiction_detected", False)),
        "burst_bull": int(getattr(snap, "burst_bull", 0) or 0),
        "burst_bear": int(getattr(snap, "burst_bear", 0) or 0),
        # Item 1: baseline audit fields. raw_score is the unadjusted level the
        # engine computed; baseline_score is the per-asset reference it was
        # normalised against (None during warm-up / feature off).
        "raw_score": getattr(snap, "raw_score", None),
        "baseline_score": getattr(snap, "baseline_score", None),
        "price_note": snap.price_note or "Price-action confirmation unavailable.",
        "last_updated": T.local_clock(snap.computed_at),
    }


def _news_feed(session, limit: int | None = None,
               offset: int = 0) -> tuple[list[dict], int]:
    """
    Today's news, newest first, each with its full impact analysis.

    Returns ``(items, total)`` where ``total`` is the size of the WHOLE day,
    not of this page. The dashboard header renders "N of TOTAL" from it, so a
    reader can always see how many articles are still behind "Load more".

    ``limit`` defaults to ``settings.news_feed_page_size`` and is clamped to
    ``settings.news_feed_max_page_size`` so one request can never ask the server
    to serialise an unbounded number of rows.

    "Today" is resolved by ``pipeline.news_day_predicate`` rather than by the
    stored ``is_today`` column, which is written once at ingest and never
    recomputed - trusting it is what let yesterday's headlines sit in today's
    feed indefinitely.
    """
    page = settings.news_feed_page_size if limit is None else int(limit)
    page = max(1, min(page, settings.news_feed_max_page_size))
    offset = max(0, int(offset))

    predicate = pipeline.news_day_predicate()

    total = session.scalar(
        select(func.count(NewsArticle.id)).where(predicate)
    ) or 0

    rows = session.scalars(
        select(NewsArticle)
        .where(predicate)
        # id is the tiebreaker on purpose: publishers stamp whole batches with
        # one timestamp (Investing.com alone fires dozens per minute), so
        # published_at_utc alone is not a stable sort key and offset paging
        # would otherwise duplicate or skip rows between pages.
        .order_by(NewsArticle.published_at_utc.desc(), NewsArticle.id.desc())
        .offset(offset)
        .limit(page)
    ).all()

    feed: list[dict] = []
    for row in rows:
        event = row.event_links[0].event if row.event_links else None
        feed.append({
            "id": row.id,
            "source": row.source,
            "url": row.url,
            "title": row.title,
            "summary": (row.summary or "")[:220],
            "published_at_utc": row.published_at_utc,
            "display_time": T.local_clock(row.published_at_utc),
            "published_tz_label": row.published_tz_label,
            "category": row.category,
            "importance": row.importance,
            "report_kind": row.report_kind,
            "verification": row.verification,
            "relevance_score": row.relevance_score,
            "analysis_mode": row.analysis_mode,
            "analysis_note": row.analysis_note,
            "is_today": row.is_today,
            "is_update": row.is_updated_article,
            "event_key": event.event_key if event else None,
            "event_article_count": event.article_count if event else 1,
            "impacts": [
                {
                    "asset": i.asset,
                    "direction": i.direction,
                    "strength": i.strength,
                    "relevance": i.relevance,
                    "persistence": i.persistence,
                    "reason": i.reason,
                    "transmission": i.transmission,
                    "metal_character": i.metal_character,
                    "recency_weight": i.recency_weight,
                    "importance_weight": i.importance_weight,
                    "final_weight": i.final_weight,
                    "price_confirmation": i.price_confirmation,
                    "session": i.session,
                }
                for i in row.impacts
            ],
        })
    return feed, total


def build_payload(session) -> dict:
    """Assemble the complete dashboard payload (section 20)."""
    state = pipeline.runtime_state()
    health_metrics = pipeline.get_health_metrics()

    # Staleness: no successful tick in the last HEALTH_STALE_MINUTES.
    # Shares the pipeline's exact rule with /api/health (pipeline.compute_stale),
    # including the startup grace: while the first tick is legitimately in
    # flight after a restart, the dashboard keeps showing live data instead of
    # a red STALE banner over carried-forward (but perfectly valid) state.
    stale, stale_reason = pipeline.compute_stale(HEALTH_STALE_MINUTES)

    assets_out = []
    for asset in Asset:
        assets_out.append({
            "asset": asset.value,
            "label": asset.label,
            "symbol": asset.value,
            "market_prep": _block(session, asset.value, "MARKET_PREP"),
            "market_wrap": _block(session, asset.value, "MARKET_WRAP"),
            "current": _block(session, asset.value, "CURRENT"),
            "economic_calendar": pipeline.calendar_summary(asset),
        })

    news_feed, news_feed_total = _news_feed(session)

    articles_today = session.scalar(
        select(func.count(NewsArticle.id)).where(pipeline.news_day_predicate())
    ) or 0
    events_today = session.scalar(select(func.count(NewsEvent.id))) or 0
    last_run = session.scalar(
        select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(1))
    alerts = [
        {
            "id": a.id, "asset": a.asset, "headline": a.headline,
            "severity": a.severity, "direction": a.direction,
            "sentiment_delta": a.sentiment_delta, "reason": a.reason,
            "created_at": T.local_clock(a.created_at),
        }
        for a in session.scalars(
            select(Alert).order_by(Alert.created_at.desc()).limit(15)).all()
    ]

    specs = parse_source_config(settings.news_sources)
    detail = (last_run.detail or {}) if last_run else {}

    # Regime comes from the most recent sentiment recompute, not a constant.
    regime = state.get("regime")
    # Session-aware global equity card: NOW-only, computed in the recompute.
    # Falls back to a neutral card so the frontend never sees a missing key.
    equity = pipeline.current_equity_card()
    if not equity:
        from app.analysis.equity_card import neutral_card, to_payload
        active = equity_session()
        equity = to_payload(neutral_card(active))
    return {
        "date": T.now_market().strftime("%Y-%m-%d"),
        "current_time": T.now_market().astimezone(T.display_tz()).strftime("%H:%M:%S"),
        "market_timezone": settings.market_timezone,
        "display_timezone": settings.display_timezone,
        "current_session": detect_session().value,
        "previous_session": previous_session().value,
        "next_session": next_session().value,
        "equity": equity,
        "equity_title": equity.get("title", equity_title(equity_session())),
        "equity_region": equity.get("region", equity_session().value),
        # CoinGlass panel: liquidation heatmap clusters / CVD divergence /
        # footprint imbalance, scraped from the public chart pages (no API
        # key). Read from the scraper's cache so the panel still shows the
        # last good reading on a tick where no fresh fetch was spent. Empty
        # when the feature is off or every poll has failed so far.
        "coinglass": pipeline.current_coinglass(),
        "coinglass_caution_note": pipeline.coinglass_caution_note(),
        "regime": (regime.regime.value if regime else MarketRegime.MIXED.value),
        "regime_confidence": (regime.confidence if regime else 0.0),
        "regime_drivers": (regime.drivers if regime else []),
        "assets": assets_out,
        "news_feed": news_feed,
        "news_feed_total": news_feed_total,
        "alerts": alerts,
        "status": {
            "running": state["running"],
            "last_updated": state["last_updated"] or "",
            "last_poll_ok": state.get("last_successful_tick") or "",
            "stale": stale,
            "stale_reason": stale_reason,
            "stale_after_minutes": HEALTH_STALE_MINUTES,
            "new_news_since_last_update": state["new_news_since_last_update"],
            "articles_today": articles_today,
            "events_today": events_today,
            "sources_polled": len(specs) + (1 if settings.use_mock_source else 0),
            "sources_ok": detail.get("sources_ok", 0),
            "analysis_mode": "llm" if settings.has_llm else "heuristic",
            "llm_enabled": settings.has_llm,
            "llm_requests_per_minute": settings.llm_requests_per_minute,
            "llm_requests_used_last_min": llm_client.requests_in_window(),
            "poll_interval_seconds": settings.poll_interval_seconds,
            "errors": detail.get("errors", []),
        },
        "health": {
            "status": "degraded" if stale else "ok",
            "last_article_utc": health_metrics.get("last_article_utc"),
            "last_snapshot_utc": health_metrics.get("last_snapshot_utc"),
            "per_source": health_metrics.get("per_source", {}),
        },
        "disclaimer": (
            "NEWS-BASED INTRADAY SENTIMENT. Not a price prediction, not a "
            "trading signal, and not financial advice."
        ),
    }


# ==========================================================================
# API endpoints
# ==========================================================================
@app.get("/api/health")
def health():
    """Liveness probe including engine mode and pipeline freshness.

    Responds from in-memory pipeline state only - no database session
    is opened and no SQLite lock is acquired, so the endpoint stays
    sub-millisecond even while a pipeline tick holds a write
    transaction.

    This is a SYNC (non-async) endpoint on purpose: FastAPI runs sync
    endpoints in a thread pool, so the probe still answers while the
    main event loop is blocked by a tick's synchronous SQLite writes.
    The previous ``async def`` version could not run until the tick
    yielded, which made the watchdog see a timeout mid-tick and restart
    a perfectly healthy engine.

    Beyond the basic ``status: ok`` this reports the metrics that tell an
    operator whether the engine is actually catching up:

    * ``last_poll_ok`` - ISO timestamp of the most recent *successful* pipeline
      tick (empty if no tick has ever completed). A clock that is not moving is
      the failure mode this endpoint exists to surface.
    * ``last_article_utc`` - ``published_at_utc`` of the newest article cached
      from the last tick (no DB hit).
    * ``last_snapshot_utc`` - ``computed_at`` of the newest sentiment snapshot
      cached from the last tick.
    * ``per_source`` - items fetched / errors for each configured source on the
      most recent tick.

    STALE / DEGRADED: when no successful tick has completed in the last
    ``HEALTH_STALE_MINUTES`` (default 15) the endpoint returns HTTP 503 with
    ``status: "degraded"`` so an external watchdog can detect a frozen pipeline
    even though the process itself is technically alive. A tick running past
    ``MAX_TICK_DURATION_SECONDS`` is ``tick_hung``; a first tick still in
    flight after a restart is healthy, not stale. The same rule
    (``pipeline.compute_stale``) drives the dashboard payload, so both agree.
    """
    state = pipeline.runtime_state()
    health_metrics = pipeline.get_health_metrics()

    last_poll_ok = state.get("last_successful_tick")

    # Freshness rule shared with the /api/dashboard payload builder (and with
    # the dashboard's STALE banner), so the two can never disagree. Covers
    # hung ticks, unparseable timestamps, a missing recent tick, and the
    # startup grace for a first tick still in flight.
    stale, stale_reason = pipeline.compute_stale(HEALTH_STALE_MINUTES)

    status_code = 503 if stale else 200
    status_label = "degraded" if stale else "ok"

    return JSONResponse(
        status_code=status_code,
        content={
            "status": status_label,
            "llm_enabled": settings.has_llm,
            "llm_status": llm_client.llm_status(),
            "analysis_mode": "llm" if settings.has_llm else "heuristic",
            "llm_requests_per_minute": settings.llm_requests_per_minute,
            "llm_requests_used_last_min": llm_client.requests_in_window(),
            "market_timezone": settings.market_timezone,
            "today": T.market_today().isoformat(),
            "current_session": detect_session().value,
            "last_poll_ok": last_poll_ok,
            "last_article_utc": health_metrics.get("last_article_utc"),
            "last_snapshot_utc": health_metrics.get("last_snapshot_utc"),
            "per_source": health_metrics.get("per_source", {}),
            "stale": stale,
            "stale_reason": stale_reason,
            "stale_after_minutes": HEALTH_STALE_MINUTES,
        },
    )


@app.get("/api/dashboard")
async def dashboard():
    """Full dashboard payload consumed by both UIs."""
    with session_scope() as session:
        return build_payload(session)


@app.get("/api/financialjuice/sessions")
async def financialjuice_sessions():
    """
    FinancialJuice Session Analysis - five boxes, IST-dated.

    Read-only and isolated: it never writes to the database and never triggers
    an extra FinancialJuice fetch. It reads the rows the existing pipeline has
    already ingested, so the dashboard's normal refresh/WebSocket cycle keeps
    it current with no second connection.

    The boxes themselves are still FinancialJuice-only, but their gold/silver/
    bitcoin split also folds in HIGH-importance Section B news and the economic
    calendar, so a session is read against the day's real backdrop.

    LEGACY SCORING: this section still returns a forced bull_pct/bear_pct pair
    that sums to 100, from the older scorer in fj_sessions.py. It is NOT on the
    current Bias/score/confidence contract and cannot express "no view". The
    payload carries an explicit ``scoring`` block so any consumer can tell the
    two systems apart without hard-coding knowledge of the history.

    TODO(unify-fj): port fj_sessions onto the transmission map and the NO_BIAS
    contract, then delete the marker below.
    """
    with session_scope() as session:
        payload = fj_sessions.session_payload_from_db(
            session, pipeline.current_calendar_signals())

    payload["scoring"] = {
        "system": "legacy_fj_sessions",
        "shape": "bull_pct/bear_pct",
        "sums_to_100": True,
        "supports_no_bias": False,
        "note": ("This section uses an older scorer than the rest of the "
                 "engine. It reports a forced Bullish+Bearish=100% split and "
                 "cannot express NO_BIAS. The main asset panels use the "
                 "current Bias/score/confidence contract. Do not compare "
                 "the two."),
    }
    return payload


@app.get("/api/news")
async def news(limit: int | None = None, offset: int = 0):
    """
    Today's news feed only, one page at a time.

    ``total`` is the size of the entire day, so a client can page until
    ``has_more`` is false and still render an honest "N of TOTAL" counter.
    """
    with session_scope() as session:
        items, total = _news_feed(session, limit=limit, offset=offset)
    return {
        "items": items,
        "total": total,
        "limit": len(items),
        "offset": max(0, int(offset)),
        "has_more": (max(0, int(offset)) + len(items)) < total,
    }


@app.get("/api/sentiment/{asset}")
async def sentiment(asset: str):
    """Per-asset CURRENT / PREP / WRAP sentiment."""
    key = asset.upper()
    if key not in Asset.values():
        return JSONResponse({"error": f"Unsupported asset: {asset}"}, status_code=404)
    with session_scope() as session:
        return {
            "asset": key,
            "market_prep": _block(session, key, "MARKET_PREP"),
            "market_wrap": _block(session, key, "MARKET_WRAP"),
            "current": _block(session, key, "CURRENT"),
        }


@app.get("/api/alerts")
async def alerts():
    """High-impact alerts raised today."""
    with session_scope() as session:
        return {"items": [
            {
                "asset": a.asset, "headline": a.headline, "severity": a.severity,
                "direction": a.direction, "sentiment_delta": a.sentiment_delta,
                "reason": a.reason, "created_at": T.local_clock(a.created_at),
            }
            for a in session.scalars(
                select(Alert).order_by(Alert.created_at.desc()).limit(30)).all()
        ]}


@app.get("/api/coinglass")
async def coinglass():
    """
    CoinGlass scrapes: liquidation heatmap / CVD / footprint per asset.

    Served from the scraper's cache, so this endpoint NEVER opens a browser
    and never blocks on the site - it reports the last good reading and an
    empty dict when there is none. Poll this independently of /api/dashboard
    to render the panel without dragging the whole payload along.
    """
    from app.config import settings as _settings

    if not _settings.coinglass_enabled:
        return {"enabled": False, "assets": {}, "caution_note": ""}
    return {
        "enabled": True,
        "assets": pipeline.current_coinglass(),
        "caution_note": pipeline.coinglass_caution_note(),
        "poll_seconds": _settings.coinglass_poll_seconds,
        "disclaimer": (
            "Scraped from public CoinGlass chart pages. Rate-limited and "
            "cached; a missing reading means the poll was skipped, not that "
            "the market is quiet."
        ),
    }


# ---------------------------------------------------------------------------
# Item 6: calibration labels. DATA COLLECTION ONLY - no auto-tuning.
#
# These endpoints let a trader record, and later read, manual outcome labels
# against bias snapshots. Nothing here feeds a threshold at decision time:
# the engine never queries bias_outcome when scoring. The labels exist so the
# tunables in config.py (FLIP_MIN_CONFIDENCE, gold_silver_bleed, the baseline
# window, the driver rankings) can later be calibrated against real results.
# ---------------------------------------------------------------------------
VALID_OUTCOMES = ("WORKED", "FAILED", "Mixed")


def _normalize_outcome(value: str) -> str:
    """Case-insensitive outcome match; Mixed is a valid label as-is."""
    raw = (value or "").strip()
    for valid in VALID_OUTCOMES:
        if raw.lower() == valid.lower():
            return valid
    return ""


@app.get("/api/calibration/labels")
async def calibration_labels(asset: str | None = None,
                              limit: int = 50):
    """Recent manual calibration labels, newest first."""
    with session_scope() as session:
        q = select(BiasOutcome).order_by(BiasOutcome.created_at.desc())
        if asset:
            q = q.where(BiasOutcome.asset == asset.upper())
        rows = session.scalars(q.limit(limit)).all()
    return {
        "items": [
            {
                "id": r.id,
                "asset": r.asset,
                "bias_snapshot_id": r.bias_snapshot_id,
                "outcome": r.outcome,
                "note": r.note,
                "created_at": T.local_clock(r.created_at),
            }
            for r in rows
        ],
        "valid_outcomes": list(VALID_OUTCOMES),
    }


@app.post("/api/calibration/labels")
async def calibration_label(asset: str, outcome: str,
                             bias_snapshot_id: int | None = None,
                             note: str | None = None):
    """
    Record one manual outcome label against a bias snapshot.

    If ``bias_snapshot_id`` is omitted the most recent snapshot for the asset
    is used. outcome is case-insensitive: WORKED / Failed / Mixed all accepted.
    """
    outcome = _normalize_outcome(outcome)
    if not outcome:
        return JSONResponse(
            status_code=400,
            content={"error": f"outcome must be one of {VALID_OUTCOMES}"},
        )
    from app.models.db_models import BiasSnapshot

    with session_scope() as session:
        snap_id = bias_snapshot_id
        if snap_id is None:
            q = select(BiasSnapshot).where(
                BiasSnapshot.asset == asset.upper())
            q = q.order_by(BiasSnapshot.timestamp_utc.desc()).limit(1)
            snap = session.scalars(q).first()
            if snap is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": f"no bias_snapshot for {asset}"},
                )
            snap_id = snap.id
        row = BiasOutcome(
            bias_snapshot_id=snap_id,
            asset=asset.upper(),
            date_utc=datetime.now(timezone.utc),
            bias="",
            score=0.0,
            confidence=0.0,
            outcome=outcome,
            note=(note or "").strip(),
        )
        session.add(row)
    return {
        "status": "recorded",
        "asset": asset.upper(),
        "bias_snapshot_id": snap_id,
        "outcome": outcome,
    }


@app.post("/api/run")
async def trigger_run():
    """Manually trigger a pipeline pass."""
    if pipeline.runtime_state()["running"]:
        return {"status": "already_running"}
    stats = await pipeline.run_once()
    return {
        "status": "completed",
        "fetched": stats.fetched, "new": stats.new_today,
        "rejected": stats.rejected, "duplicates": stats.duplicates,
        "llm_ok": stats.llm_ok, "llm_failed": stats.llm_failed,
        "errors": stats.errors[:10],
    }


@app.get("/api/console")
async def console():
    """Plain-text console rendering of the dashboard (section 20 layout)."""
    with session_scope() as session:
        data = build_payload(session)

    st = data["status"]
    out: list[str] = []
    out.append("=" * 74)
    out.append("TODAY'S INTRADAY MARKET SENTIMENT")
    out.append("=" * 74)
    out.append(f"DATE:            {data['date']}")
    out.append(f"CURRENT TIME:    {data['current_time']} ({data['display_timezone']})")
    out.append(f"MARKET TIMEZONE: {data['market_timezone']}")
    out.append(f"CURRENT SESSION: {data['current_session']}")
    out.append(f"PREV / NEXT:     {data['previous_session']} / {data['next_session']}")
    out.append(f"REGIME:          {data['regime']} "
               f"(confidence {data.get('regime_confidence', 0):.2f})")
    out.append(f"MODE:            {st['analysis_mode']}")
    out.append(f"LAST UPDATED:    {st['last_updated'] or 'n/a'}")
    out.append(f"NEW NEWS SINCE LAST UPDATE: {st['new_news_since_last_update']}")
    out.append("")

    for asset in data["assets"]:
        out.append("-" * 74)
        out.append(f"{asset['label'].upper()} - {asset['symbol']}")
        out.append("-" * 74)
        for key, title in (("market_prep", "MARKET PREP"),
                           ("market_wrap", "MARKET WRAP"),
                           ("current", "CURRENT DAY-TRADING SENTIMENT")):
            b = asset[key]
            out.append(f"  {title}")
            out.append(f"    Bias:          {b['bias']}")
            out.append(f"    Score:         {b['score']:+.1f}  (-100..+100)")
            out.append(f"    Confidence:    {b['confidence']:.2f} "
                       f"(events: {b['contributing_articles']}, "
                       f"mode: {b['mode']})")
            if b["delta_60m"]:
                out.append(f"    Changed 60min: {b['delta_60m']:+.1f}")
            for d in b["drivers"][:3]:
                out.append(f"    + {d['contribution']:+.3f}  {d['title'][:74]}")
            if b["conflict"]:
                out.append(f"    ! CONFLICT: {b['conflict']['title'][:74]}")
            if b["event_risk"]:
                out.append(f"    !! {b['event_risk_note']}")
            if b["note"]:
                out.append(f"    Note:          {b['note'][:70]}")
            out.append(f"    Price check:   {b['price_note']}")
            out.append("")

    out.append("=" * 74)
    out.append(f"TODAY'S NEWS IMPACT FEED  "
               f"(showing {min(25, len(data['news_feed']))} of "
               f"{data.get('news_feed_total', len(data['news_feed']))})")
    out.append("=" * 74)
    for item in data["news_feed"][:25]:
        hit = ", ".join(sorted({i["asset"] for i in item["impacts"]})) or "None"
        top = max(item["impacts"], key=lambda i: i["relevance"], default=None)
        out.append(f"[{item['display_time']}] {item['title'][:98]}")
        out.append(f"    Source: {item['source']} | Category: {item['category']} "
                   f"| Importance: {item['importance']}")
        out.append(f"    Asset: {hit} | Report: {item['report_kind']} "
                   f"| Verification: {item['verification']}")
        if top:
            out.append(f"    Impact: {top['direction']} ({top['strength']})")
            out.append(f"    Transmission: {top['transmission'][:108]}")
        out.append("")

    out.append("-" * 74)
    out.append(data["disclaimer"])
    return "\n".join(out)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Live push channel - the UI refreshes when new news arrives."""
    await websocket.accept()
    _clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive
    except WebSocketDisconnect:
        _clients.discard(websocket)
    except Exception:  # noqa: BLE001
        _clients.discard(websocket)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Built-in live dashboard (no build step, no npm)."""
    page = WEB_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>Dashboard asset missing</h1>", status_code=500)
    # The page is read from disk on every request and the UI polls itself, so a
    # cached copy only ever serves stale markup. Explicit no-store prevents the
    # browser from pinning an old revision after we edit the file.
    return HTMLResponse(
        page.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )