"""
FastAPI application (section 26 - REAL-TIME DASHBOARD + API).

Serves the JSON API, a live WebSocket feed and a zero-dependency HTML
dashboard, and runs the ingestion pipeline on an APScheduler background job.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select

from app.analysis import llm_client
from app.config import settings
from app.database import init_db, session_scope
from app.ingestion.base import parse_source_config
from app.logging_conf import get_logger, setup_logging
from app.market.session_detector import (
    detect_session, next_session, previous_session,
)
from app.models.db_models import Alert, NewsArticle, NewsEvent, PipelineRun
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength, MarketRegime,
    NewsCategory, PriceConfirmation, ReportKind, SessionName, Verification,
)
from app.models.schemas import DashboardPayload
from app.services import pipeline
from app.utils import timeutils as T

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent / "dashboard" / "web"
_scheduler: AsyncIOScheduler | None = None

# Connected WebSocket clients for live push updates.
_clients: set[WebSocket] = set()


async def _broadcast(payload: dict) -> None:
    if not _clients:
        return
    message = json.dumps(payload, default=str)
    dead: list[WebSocket] = []
    for ws in list(_clients):
        try:
            await ws.send_text(message)
        except Exception:  # noqa: BLE001 - client vanished
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


async def _scheduled_tick() -> None:
    """Background job: run the pipeline, then push an update."""
    try:
        stats = await pipeline.run_once()
        await _broadcast({"type": "update", "new_news": stats.new_today,
                          "at": T.stamp()})
    except Exception as exc:  # noqa: BLE001 - never kill the scheduler
        log.exception("Scheduled pipeline tick failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    global _scheduler

    if settings.poll_interval_seconds > 0:
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

    # Prime the pipeline immediately so the dashboard is never empty.
    asyncio.create_task(_scheduled_tick())

    yield

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    log.info("Shutdown complete")


app = FastAPI(
    title="Intraday News Sentiment & Market Impact Engine",
    description="News-based intraday sentiment for XAUUSD, XAGUSD and BTCUSD.",
    version="1.0.0",
    lifespan=lifespan,
)


def _latest_snapshot(session, asset: str, scope: str):
    """Most recent snapshot for an asset+scope (snapshots are append-only)."""
    from app.models.db_models import SentimentSnapshot

    return session.scalar(
        select(SentimentSnapshot)
        .where(SentimentSnapshot.asset == asset)
        .where(SentimentSnapshot.scope == scope)
        .order_by(SentimentSnapshot.computed_at.desc())
        .limit(1)
    )


def _block(session, asset: str, scope: str) -> dict:
    """One sentiment block, or an explicit empty state (never invented data)."""
    snap = _latest_snapshot(session, asset, scope)
    if snap is None:
        return {
            "asset": asset, "scope": scope,
            "bull_pct": 50.0, "bear_pct": 50.0,
            "label": Direction.BULLISH.value, "net_bias": 0.0,
            "confidence": 0.0, "conflicting_signals": 0,
            "drivers": [], "risks": [], "catalysts": [], "conflicts": [],
            "contributing_articles": 0,
            "price_note": "Price-action confirmation unavailable.",
            "last_updated": "",
        }
    return {
        "asset": asset, "scope": scope,
        "bull_pct": snap.bull_pct, "bear_pct": snap.bear_pct,
        "label": snap.label,
        "net_bias": snap.net_bias, "confidence": snap.confidence,
        "conflicting_signals": snap.conflicting_signals,
        "drivers": snap.drivers or [], "risks": snap.risks or [],
        "catalysts": snap.catalysts or [], "conflicts": snap.conflicts or [],
        "contributing_articles": snap.contributing_articles,
        "price_note": snap.price_note or "Price-action confirmation unavailable.",
        "last_updated": T.local_clock(snap.computed_at),
    }


def _news_feed(session, limit: int = 60) -> list[dict]:
    """Today's news, newest first, each with its full impact analysis."""
    rows = session.scalars(
        select(NewsArticle)
        .where(NewsArticle.is_today.is_(True))
        .order_by(NewsArticle.published_at_utc.desc())
        .limit(limit)
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
    return feed


def build_payload(session) -> dict:
    """Assemble the complete dashboard payload (section 20)."""
    state = pipeline.runtime_state()

    assets_out = []
    for asset in (Asset.GOLD, Asset.SILVER, Asset.BITCOIN):
        assets_out.append({
            "asset": asset.value,
            "label": asset.label,
            "symbol": asset.value,
            "market_prep": _block(session, asset.value, "MARKET_PREP"),
            "market_wrap": _block(session, asset.value, "MARKET_WRAP"),
            "current": _block(session, asset.value, "CURRENT"),
            "economic_calendar": pipeline.calendar_summary(asset),
        })

    articles_today = session.scalar(
        select(func.count(NewsArticle.id)).where(NewsArticle.is_today.is_(True))
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
    return {
        "date": T.now_market().strftime("%Y-%m-%d"),
        "current_time": T.now_market().astimezone(T.display_tz()).strftime("%H:%M:%S"),
        "market_timezone": settings.market_timezone,
        "display_timezone": settings.display_timezone,
        "current_session": detect_session().value,
        "previous_session": previous_session().value,
        "next_session": next_session().value,
        "regime": (regime.regime.value if regime else MarketRegime.MIXED.value),
        "regime_confidence": (regime.confidence if regime else 0.0),
        "regime_drivers": (regime.drivers if regime else []),
        "assets": assets_out,
        "news_feed": _news_feed(session),
        "alerts": alerts,
        "status": {
            "running": state["running"],
            "last_updated": state["last_updated"] or "",
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
        "disclaimer": (
            "NEWS-BASED INTRADAY SENTIMENT. Not a price prediction, not a "
            "trading signal, and not financial advice."
        ),
    }


# ==========================================================================
# API endpoints
# ==========================================================================
@app.get("/api/health")
async def health():
    """Liveness probe including engine mode."""
    return {
        "status": "ok",
        "llm_enabled": settings.has_llm,
        "analysis_mode": "llm" if settings.has_llm else "heuristic",
        "llm_requests_per_minute": settings.llm_requests_per_minute,
        "llm_requests_used_last_min": llm_client.requests_in_window(),
        "market_timezone": settings.market_timezone,
        "today": T.market_today().isoformat(),
        "current_session": detect_session().value,
    }


@app.get("/api/dashboard")
async def dashboard():
    """Full dashboard payload consumed by both UIs."""
    with session_scope() as session:
        return build_payload(session)


@app.get("/api/news")
async def news(limit: int = 60):
    """Today's news feed only."""
    with session_scope() as session:
        return {"items": _news_feed(session, limit=limit)}


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
            out.append(f"    Bullish:       {b['bull_pct']:.1f}%")
            out.append(f"    Bearish:       {b['bear_pct']:.1f}%")
            out.append(f"    Sentiment:     {b['label']}")
            out.append(f"    Confidence:    {b['confidence']:.2f} "
                       f"(articles: {b['contributing_articles']})")
            for d in b["drivers"][:3]:
                out.append(f"    + {d[:88]}")
            for d in b["risks"][:3]:
                out.append(f"    - {d[:88]}")
            for c in b["conflicts"][:1]:
                out.append(f"    ! CONFLICT: {c['resolution']}")
            out.append(f"    Price check:   {b['price_note']}")
            out.append("")

    out.append("=" * 74)
    out.append("TODAY'S NEWS IMPACT FEED")
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
    return HTMLResponse(page.read_text(encoding="utf-8"))