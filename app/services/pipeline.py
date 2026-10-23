"""
PIPELINE ORCHESTRATOR (section 26).

  INGESTION -> NEWS EXTRACTION -> TODAY FILTER -> DEDUP -> CLASSIFY
  -> RECENCY -> ASSET IMPACT -> AGGREGATE -> PREP/WRAP -> PERSIST

This module owns the end-to-end control flow. The background scheduler calls
``run_once``; the API and dashboard read the results it persists.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, delete, exists, func, or_, select

from app.analysis import llm_client, release_surprise
from app.analysis.chart_scope import scope_label
from app.analysis.economic_impact import (
    CalendarEvent,
    CalendarSignal,
    signals_for_events,
    to_calendar_summary,
)
from app.analysis.economic_impact import (
    headline as calendar_headline,
)
from app.analysis.event_deduplication import EventDeduplicator
from app.analysis.fj_sessions import (
    FINANCIAL_JUICE_SOURCE,
    is_fj_session_report_row,
    session_report_asset_allowed,
)
from app.analysis.news_classifier import analyze_article, is_section_b_verdict
from app.analysis.sentiment_engine import WeightedSignal, remember_bias
from app.analysis.signal_factory import build_bleed_signal, build_signal
from app.config import settings
from app.database import session_scope
from app.ingestion.base import RawArticle
from app.ingestion.economic_calendar import fetch_calendar
from app.ingestion.news_fetcher import fetch_all_news
from app.logging_conf import get_logger
from app.market import regime as regime_mod
from app.market.session_detector import (
    detect_session,
    session_relevance,
    asset_session_multiplier,
)
from app.models.db_models import (
    ArticleEvent,
    AssetImpact,
    DroppedArticle,
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

# Cross-tick LLM cooldown.
#
# When the provider answers NOTHING in a tick (every call timed out or
# failed), the next tick does not spend its whole 90s budget asking again:
# the engine would take 90s longer for zero verdicts and would hammer an
# endpoint that is already down. The heuristics still classify every article
# in that tick - the model is simply not asked - and the cooldown expires
# after LLM_COOLDOWN_SECONDS, so a provider that comes back is picked up on
# the next tick.
LLM_COOLDOWN_SECONDS = 150.0
_llm_cooldown_until: float = 0.0


class _DriverView:
    """
    One driver in the shape the decision log reads it.

    ``DecisionLog.record_bias`` does ``d.title`` / ``d.contribution`` /
    ``d.direction``, which is the ``SentimentResult`` driver object shape. The
    equity card's drivers are plain dicts, so they are wrapped here rather
    than changing the log writer's contract for every other caller.
    """

    __slots__ = ("title", "contribution", "direction", "weight")

    def __init__(self, raw):
        if isinstance(raw, dict):
            self.title = str(raw.get("title", ""))
            self.contribution = float(raw.get("contribution", 0.0) or 0.0)
            self.direction = raw.get("direction", "")
            self.weight = float(raw.get("weight", 0.0) or 0.0)
        else:
            self.title = str(getattr(raw, "title", ""))
            self.contribution = float(getattr(raw, "contribution", 0.0) or 0.0)
            self.direction = getattr(raw, "direction", "")
            self.weight = float(getattr(raw, "weight", 0.0) or 0.0)


class _EquityCardVerdict:
    """
    Shape a session-aware equity card like a ``SentimentResult``.

    The decision log and ``bias_snapshots`` both consume a result-shaped
    object. The equity card is a different shape (``EquityCardResult``, with
    plain-dict drivers and a string bias), so this adapter exposes exactly
    the attributes those two writers read.

    It exists so the pipeline can publish the equity card's OWN verdict - the
    one the dashboard shows - instead of a second region-blind proxy.
    """

    __slots__ = ("asset", "bias", "score", "confidence", "mode",
                 "event_risk", "drivers")

    def __init__(self, card):
        from app.analysis.sentiment_engine import Bias

        self.asset = Asset.EQUITY
        raw_bias = getattr(card, "bias", "BULLISH")
        self.bias = (raw_bias if isinstance(raw_bias, Bias)
                     else Bias(str(raw_bias)))
        self.score = float(getattr(card, "score", 0.0))
        self.confidence = float(getattr(card, "confidence", 0.0))
        self.mode = str(getattr(card, "mode", "heuristic"))
        self.event_risk = bool(getattr(card, "event_risk", False))
        self.drivers = [_DriverView(d)
                        for d in (getattr(card, "drivers", None) or [])]

# Shared across runs so dedup survives within a process session.
_dedup = EventDeduplicator()
# Boot reseed latch: the index is rebuilt from the DB exactly once per process
# (STEP 9.2), not on every 180s tick.
_dedup_seeded = False
_last_update: datetime | None = None
_last_successful_tick: str | None = None

# Persistence file for last_successful_tick - survives process restarts so the
# dashboard doesn't flash STALE on every API server restart (watchdog, deploy, etc.).
_LAST_TICK_FILE = Path(__file__).resolve().parents[2] / "data" / "last_successful_tick.txt"


def _load_last_successful_tick() -> str | None:
    """Load persisted last_successful_tick from disk."""
    try:
        if _LAST_TICK_FILE.exists():
            content = _LAST_TICK_FILE.read_text(encoding="utf-8").strip()
            if content:
                # Validate it's a valid ISO timestamp
                datetime.fromisoformat(content)
                return content
    except Exception:
        pass
    return None


def _save_last_successful_tick(value: str) -> None:
    """Persist last_successful_tick to disk (best-effort)."""
    try:
        _LAST_TICK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LAST_TICK_FILE.write_text(value, encoding="utf-8")
    except Exception:
        # Never fail a tick because of persistence issues
        pass


def _refresh_tick_stamp(force: bool = False) -> None:
    """Refresh the pipeline freshness stamp (in memory and on disk).

    The stamp is a HEARTBEAT, not a one-shot event.

    It used to be written exactly once per tick, right after ingestion, so
    the freshness clock froze for the whole rest of the tick. A boot tick
    whose LLM classification phase outlasts ``HEALTH_STALE_MINUTES`` (15
    minutes) therefore looked stale to /api/health for the entire back half
    of its run - the dashboard raised the red "no successful pipeline tick in
    the last 15 minutes" banner over data that was demonstrably still being
    computed. That is exactly the banner that appeared every morning.

    Refreshing the stamp as the tick progresses makes the clock mean what it
    says: "the pipeline last did work at HH:MM:SS". A tick that is alive
    and working stays fresh; a tick that dies stops refreshing and goes
    stale on its own.

    Throttled to one write per ``TICK_HEARTBEAT_SECONDS`` so a 600-article
    batch does not turn into 600 file writes.
    """
    global _last_update, _last_successful_tick, _last_heartbeat_mono
    now_mono = time.monotonic()
    if not force and (now_mono - _last_heartbeat_mono) < TICK_HEARTBEAT_SECONDS:
        return
    _last_heartbeat_mono = now_mono
    _last_update = T.now_utc()
    _last_successful_tick = _last_update.isoformat()
    _save_last_successful_tick(_last_successful_tick)


def set_paused(paused: bool) -> None:
    """Mirror the operator's pause state into the pipeline module.

    The API owns the pause button; the pipeline owns the freshness rule.
    Mirroring the flag here lets ``compute_stale`` tell "the operator
    stopped the engine" apart from "the engine died", which is the
    difference between a PAUSED banner and a STALE banner.
    """
    global _engine_paused
    _engine_paused = bool(paused)


def is_paused() -> bool:
    """True when the operator has deliberately paused the engine."""
    return _engine_paused


def _heartbeat_age_seconds(last_poll_ok: str | None) -> float | None:
    """Seconds since the pipeline last heartbeated, or None if unknown."""
    if not last_poll_ok:
        return None
    try:
        stamp = datetime.fromisoformat(last_poll_ok)
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _heartbeat_is_fresh(last_poll_ok: str | None) -> bool:
    """True when the pipeline heartbeated within HEARTBEAT_STALE_SECONDS.

    This is the "is the tick still working?" test. A tick that heartbeats
    is alive, however long it has been running; a tick that stopped
    heartbeating is wedged.
    """
    age = _heartbeat_age_seconds(last_poll_ok)
    return age is not None and age <= HEARTBEAT_STALE_SECONDS


# Load persisted tick on module import (process startup)
_last_successful_tick = _load_last_successful_tick()
_news_since_update = 0
_running = False
# Monotonic stamp of the last freshness heartbeat (see _refresh_tick_stamp).
_last_heartbeat_mono: float = 0.0
# Monotonic id of the current tick. A tick that is released as wedged and
# later unwinds must not clear the running flag of the tick that replaced
# it - the generation counter is what keeps the two apart.
_tick_generation: int = 0
# Operator pause state, mirrored from the API layer. A deliberately paused
# engine is NOT a stale engine: the dashboard shows the PAUSED banner, so the
# freshness check must not also raise the red STALE banner over it.
_engine_paused: bool = False
_last_regime = None  # RegimeAssessment from the most recent recompute
# Economic-calendar signals from the most recent fetch, shared with the API.
_calendar_signals: list[CalendarSignal] = []
# The raw calendar events behind those signals. Kept separately because the
# imminent-release EVENT RISK warning must see un-released events too, even
# though only today's prints become signals.
_calendar_events: list = []

# When the calendar was last fetched. The FinancialJuice render is expensive
# (10-20s) and Cloudflare-sensitive, so it is polled on ITS OWN cadence
# (FJ_CALENDAR_POLL_SECONDS) rather than on every pipeline tick.
_calendar_last_fetch_utc = None

# Down-weight news that duplicates a calendar release.
CALENDAR_NEWS_DUP_FACTOR = 0.35
# Session-aware global equity card from the most recent recompute (NOW-only).
_last_equity_card: dict | None = None
# The weighted signals that produced it, kept so the API can re-score the
# equity card the instant a session opens without waiting for the next tick.
# A session boundary must not cost a full poll interval (180s) of a stale
# region/title/bias on the card.
_last_equity_signals: list = []

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

# No successful tick in this many minutes => the API reports the pipeline as
# STALE (degraded). Lives next to the state it evaluates so /api/health and
# the /api/dashboard payload builder can never drift apart on freshness.
HEALTH_STALE_MINUTES = 15

# How often a running tick refreshes its freshness stamp. A tick that is
# alive and working must never look stale just because its LLM phase is slow,
# so the stamp is heartbeated at least this often while the tick progresses.
TICK_HEARTBEAT_SECONDS = 60

# A tick is only considered HUNG when it has stopped making progress.
#
# The freshness stamp is heartbeated while a tick works, so a STALE
# HEARTBEAT - not the elapsed time alone - is the signal that the tick is
# wedged. The old rule ("running for more than MAX_TICK_DURATION_SECONDS
# => hung") declared a healthy tick hung whenever the LLM provider was
# slow: a boot tick with a degraded provider legitimately runs for 15+
# minutes, and the release that followed let a SECOND tick start while the
# first was still writing - two writers on one SQLite file, which is where
# the "database is locked" failures and the lost articles came from.
HEARTBEAT_STALE_SECONDS = 420

# Hard cap on LLM classifications started within a single tick. Beyond this the
# classifier falls back to deterministic heuristics, keeping every tick under
# the ~120s budget even when the LLM endpoint is degraded.
#
# The LIVE value is ``settings.llm_batch_limit`` (env: LLM_BATCH_LIMIT), read
# once per tick so the operator can tune it without a code change. This
# constant is the fallback for a settings object that does not carry the
# field.
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


def _safe_int(value, default: int = 0) -> int:
    """int() that never raises - settings may be a mock in tests."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    """float() that never raises - settings may be a mock in tests."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_enum(enum_cls, value, default):
    """
    Enum coercion that never raises.

    A stored row can carry a string the CURRENT enum no longer accepts - the
    pre-2026-10 asset_impact rows, for instance, wrote ``strength='STRONG'``
    while the enum only ever had VERY_HIGH / HIGH / MEDIUM / LOW. ``Enum(...)``
    raised ValueError on those rows and killed the whole recompute tick, which
    is exactly the failure the unroutable-asset guard was written to stop. The
    row is degraded to ``default`` instead: one stale row must never be able
    to take the engine down.
    """
    try:
        return enum_cls(value)
    except (TypeError, ValueError):
        return default


def _llm_priority_indices(
    items: list[tuple[str, float | None, str | None]],
    known_guids: set | None = None,
) -> set[int]:
    """
    Indices of the batch items that get FIRST pick of the tick's LLM budget.

    ``items`` is a list of (source_name, published_epoch_seconds, guid) tuples
    in the order the batch is to be sorted. The return value is the set of
    indices to promote ahead of everything else.

    WHY THIS EXISTS
    ---------------
    The LLM budget is small and strictly first-come-first-served over the
    sorted batch. Measured on the live endpoint (2026-10-21): one model call
    costs 20-40s, so the 90s tick budget buys only 2-5 verdicts per tick and
    the batch COUNT cap of 15 is never reached. The order therefore decides who
    is judged AT ALL - and the order is tier-then-recency, so a source whose
    headlines sit 10-30 minutes behind the freshest wire copy never wins a
    model call, however much it contributes. TradingView was exactly that
    source: 26 rows ingested, 0 model verdicts, every headline scored by the
    heuristic instead.

    The reservation is deliberately small and age-bounded:
      * at most ``llm_priority_slots`` items are promoted, so the sources that
        normally win the queue keep the rest of the budget;
      * only items younger than ``llm_priority_max_age_hours`` qualify, so a
        backlog after a restart cannot spend the whole budget on stale rows;
      * an item whose guid is already stored does NOT qualify (when
        ``known_guids`` is supplied). A source that re-serves the same
        headlines every tick would otherwise spend the whole reservation on
        rows the tick skips before the LLM phase - the reservation would be
        gone before a genuinely new headline ever reached the model.

    With no sources configured (the default) this returns an empty set and the
    sort behaves exactly as it did before.
    """
    raw_names = getattr(settings, "llm_priority_sources", "") or ""
    if not isinstance(raw_names, str):
        raw_names = ""
    names = {n.strip().lower() for n in raw_names.split(",") if n.strip()}
    slots = _safe_int(getattr(settings, "llm_priority_slots", 0), 0)
    max_age_h = _safe_float(
        getattr(settings, "llm_priority_max_age_hours", 0.0), 0.0)
    if not names or slots <= 0 or max_age_h <= 0:
        return set()

    now = time.time()
    candidates: list[tuple[float, int]] = []
    for index, (source, published_epoch, guid) in enumerate(items):
        # The contract is epoch seconds; anything else is ignored rather than
        # allowed to raise inside a tick.
        if not isinstance(published_epoch, (int, float)):
            continue
        if not published_epoch or published_epoch <= 0:
            continue
        if (source or "").strip().lower() not in names:
            continue
        if known_guids is not None and guid and guid in known_guids:
            # Already stored: the tick skips it before the LLM phase, so
            # promoting it would waste the reservation.
            continue
        age_h = (now - published_epoch) / 3600.0
        if age_h < 0 or age_h > max_age_h:
            continue
        candidates.append((published_epoch, index))

    # Newest first, so the reservation always goes to the FRESHEST items of
    # the listed sources.
    candidates.sort(reverse=True)
    return {index for _epoch, index in candidates[:slots]}


def _priority_sort_articles(articles: list, known_guids: set | None = None) -> list:
    """
    Sort a fetched batch so the highest-priority articles reach the LLM cap.

    Order, in priority:
      1. LLM reservation  - items from a source listed in
         ``settings.llm_priority_sources`` that are fresh enough to be
         promoted (see ``_llm_priority_indices``). Empty by default, so this
         tier is inert unless the operator configures it.
      2. source_quality  - TIER_1_OFFICIAL first, then TIER_2_MAJOR, etc.
      3. published_at    - newest first (parsed from published_raw).
      4. title           - stable tiebreaker so the sort is deterministic.

    Why this matters: LLM_BATCH_LIMIT caps model calls per tick, and without a
    sort the cap is hit in arrival order - a rush of Tier 4 social-media noise
    can consume every slot and leave a Tier 1 central-bank release to heuristics.
    Sorting first means the 15 most important articles are the ones classified
    by the model; the remainder are still ingested and scored, just deterministically.

    ``known_guids`` is the set of guids already stored. The tick skips those
    BEFORE the LLM phase, so they must not be allowed to consume the
    reservation - otherwise a source that re-serves the same headlines every
    tick would spend the whole reservation on rows the tick never judges.

    Note on importance: RawArticle carries no importance field - importance is a
    CLASSIFICATION output (see cue_rules.detect_importance), so it cannot be a
    sort input here without classifying first, which defeats the purpose of the
    cap. Source quality is the pre-classification proxy that best predicts it:
    official statistical releases and central-bank statements outrank wire
    copy, which outranks aggregator chatter.
    """
    indexed = list(enumerate(articles))
    reserved = _llm_priority_indices([
        (getattr(art, "source", "") or "", _article_epoch(art),
         getattr(art, "guid", None))
        for _index, art in indexed
    ], known_guids=known_guids)

    def _key(pair):
        index, art = pair
        group = 0 if index in reserved else 1
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
        return (group, quality_rank, ts_rank, (getattr(art, "title", "") or "").lower())

    return [art for _index, art in sorted(indexed, key=_key)]


def _article_epoch(art) -> float | None:
    """Publication time of a RawArticle as epoch seconds, or None."""
    try:
        rank = _parse_timestamp_rank(getattr(art, "published_raw", None))
    except Exception:
        return None
    return rank if rank and rank > 0 else None


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

# ONE-SHOT LLM POLICY: articles are classified at MOST ONCE.
#
# The backlog re-classification loop that used to live here (re-sending up to
# 5 previously-heuristic articles per tick) is GONE. It re-sent rows the
# ingest path had already judged, and it re-classified them WITHOUT the
# one-shot fate rule - a row it called LOW would have stayed in the feed
# instead of being deleted and suppressed. The single LLM call an article
# gets is the one at ingest (see ``_ingest_article``), where the verdict also
# decides whether the row is stored in Section B or deleted.
#
# ``overnight.classify_articles`` keeps its catch-up pass for rows the
# pipeline never got a model verdict for (batch cap / circuit breaker), and
# applies the same keep-or-delete rule when it judges them.


def _current_market_day_key() -> str:
    """The market-local trading day as the suppression table's key."""
    return T.market_today().isoformat()


def _final_weight_key(signal) -> tuple:
    """Content identity of a signal for post-cluster final_weight routing."""
    return (
        signal.asset,
        signal.title,
        signal.direction,
        signal.published_at,
        signal.event_key,
    )


def seed_dedup_index_from_db(session) -> int:
    """
    Rebuild the in-process dedup index from stored headlines (STEP 9.2).

    A restart starts with an empty ``EventDeduplicator``, which would admit
    today's headlines as brand-new events and double-count them. This reseed is
    latched by ``_dedup_seeded`` so it runs exactly once per process.

    Every stored headline from today is seeded - including rows whose
    ``published_at_utc`` is NULL (they fall back to the tick clock) - so that a
    later identical-title article matches an existing event and shares one
    ``event_key`` instead of being counted again as a fresh event.
    Returns the number of titles seeded.
    """
    global _dedup_seeded
    if _dedup_seeded:
        return 0

    rows = session.execute(
        select(NewsArticle.title, NewsArticle.published_at_utc)
        .where(
            NewsArticle.published_at_utc.is_not(None),
            NewsArticle.is_today.is_(True),
        )
        .order_by(NewsArticle.id)
    ).all()

    if rows:
        titles = [t for t, _ in rows if t]
        # Per-row timestamps; naive DB values are treated as UTC. A row whose
        # timestamp predates the prune window is still seeded so a later
        # identical-title article matches it and shares one ``event_key``.
        timestamps = [
            p if (p is not None and p.tzinfo is not None)
            else (p.replace(tzinfo=timezone.utc) if p is not None else None)
            for t, p in rows if t
        ]
        _dedup.seed_from_titles(titles, timestamps, now=T.now_utc())

    _dedup_seeded = True
    return len(titles) if rows else 0


def _guid_suppressed(session, guid: str) -> bool:
    """
    True when this guid was deleted earlier TODAY under the one-shot policy.

    The check that makes "never comes back" real. ``news_article`` is deduped
    on ``guid``, so once a dropped article's row is gone the guid looks brand
    new on the next feed poll; without this the engine would pay for the same
    headline and delete it again on every tick.

    Scoped to the current trading day, so a new day re-admits the story.
    """
    found = session.scalar(
        select(DroppedArticle.dropped_day).where(
            DroppedArticle.guid == guid,
            DroppedArticle.dropped_day == _current_market_day_key(),
        )
    )
    return found is not None


def _suppressed_guids(session) -> set:
    """
    Every guid deleted under the one-shot policy for the CURRENT trading day.

    The bulk form of ``_guid_suppressed``: one query for the whole set instead
    of one per headline. The LLM pre-classification phase needs to know which
    guids are already dead BEFORE it spends a model call on them, and the
    batch is ~700 articles per tick.
    """
    try:
        rows = session.scalars(
            select(DroppedArticle.guid).where(
                DroppedArticle.dropped_day == _current_market_day_key(),
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 - a miss must not break the tick
        log.warning("Suppressed-guid preload failed (%s) - continuing", exc)
        return set()
    return set(rows)


def _drop_low_impact_article(session, row, analysis,
                             stats: "IngestStats | None" = None) -> None:
    """
    Delete one low-impact article and suppress its guid for the trading day.

    Called from every path that has just obtained a MEDIUM/LOW verdict from a
    real model call (the ingest path in ``_ingest_article`` and the overnight
    catch-up pass). Deleting the ``NewsArticle`` row cascades its
    ``asset_impact`` and ``article_event`` links (both declare
    ``cascade="all, delete-orphan"``), and no ``NewsEvent`` cluster is created
    for it here at all.

    The suppression row is flushed in the same transaction, so a second copy of
    the same story inside the same tick is already covered.

    ``stats`` may be None for callers that keep their own tally (the overnight
    pass); the row is deleted and suppressed either way.
    """
    if stats is not None:
        stats.llm_new_dropped_low += 1
        # A deleted article was still processed: new_today counts articles the
        # pipeline judged this tick, not only the ones it kept.
        stats.new_today += 1
    log.info(
        "ONE-SHOT DROP: %r guid=%s importance=%s mode=%s - deleted and "
        "suppressed for trading day %s",
        (row.title or "")[:80], row.guid, analysis.importance.value,
        analysis.mode, _current_market_day_key(),
    )
    session.delete(row)
    # MERGE, not add: the same guid can be condemned on two different trading
    # days while its first suppression row is still inside the retention
    # window. A plain INSERT would then break the guid primary key; a merge
    # simply refreshes the row to the new day.
    session.merge(DroppedArticle(
        guid=row.guid,
        dropped_day=_current_market_day_key(),
        title=(row.title or "")[:300],
        source=row.source or "",
        importance=analysis.importance.value,
        llm_used=bool(row.llm_used),
    ))
    session.flush()


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


def compute_stale(
    stale_after_minutes: int = HEALTH_STALE_MINUTES,
) -> tuple[bool, str | None]:
    """Single source of truth for pipeline freshness (STALE / DEGRADED).

    Reads only in-memory state (no DB, no lock), so it is cheap enough for
    ``/api/health`` to call while a tick holds the SQLite write lock.

    A freshly restarted process has ``last_successful_tick = None`` until
    its first tick completes. ``/api/health`` has always treated "a tick is
    running and is not hung" as healthy in that window; the dashboard
    payload builder used its own inline rule that did NOT, so every restart
    flashed a red STALE banner for the first few minutes of legitimate
    work. Both callers now share this helper, so they can never disagree.

    A deliberately PAUSED engine is also not stale. The operator stopping
    the engine is a decision, not a failure, and the dashboard already
    shows a PAUSED banner for it - raising STALE on top of that would only
    be noise (and, on a machine that is paused for the night, it would be
    a permanent false alarm).

    Returns ``(stale, stale_reason)`` where reason is one of
    ``tick_hung`` / ``no_successful_tick`` / ``no_tick_completed`` /
    ``parse_error`` (stale) or ``None`` (healthy).
    """
    if _engine_paused:
        return False, None

    state = runtime_state()
    tick_running = state.get("running", False)
    tick_started = state.get("tick_started_at")
    last_poll_ok = state.get("last_successful_tick")

    # A tick running past the hard cap is only hung if it also stopped
    # making progress. The scheduler has max_instances=1, so a genuinely
    # hung tick blocks every future tick - it is stale no matter how fresh
    # the last completed tick was. But elapsed time alone is NOT enough:
    # with a degraded LLM provider a healthy tick legitimately runs for
    # 15+ minutes, and calling that hung is what raised the false STALE
    # banner every morning. The heartbeat is the liveness signal.
    if tick_running and tick_started:
        try:
            tick_start_dt = datetime.fromisoformat(tick_started)
            tick_age = datetime.now(timezone.utc) - tick_start_dt
            if tick_age > timedelta(seconds=MAX_TICK_DURATION_SECONDS):
                if not _heartbeat_is_fresh(last_poll_ok):
                    return True, "tick_hung"
        except (ValueError, TypeError):
            pass

    if last_poll_ok:
        try:
            last_dt = datetime.fromisoformat(last_poll_ok)
            age = datetime.now(timezone.utc) - last_dt
            if age > timedelta(minutes=stale_after_minutes):
                return True, "no_successful_tick"
            return False, None
        except (ValueError, TypeError):
            return True, "parse_error"

    # No tick has ever completed in this process. If one is running right now
    # (and is not hung, per the check above) the startup tick is still in
    # flight - that is normal startup, not staleness.
    if tick_running:
        return False, None
    return True, "no_tick_completed"


def last_successful_tick() -> str | None:
    """ISO timestamp of the last time the pipeline did work.

    Heartbeated while a tick runs (see ``_refresh_tick_stamp``), so this is
    "the pipeline last did something at" - not "the last tick that finished".
    A tick that dies stops refreshing it and the timestamp ages into
    staleness on its own.
    """
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
    Release the running flag when the current tick is genuinely WEDGED.

    A tick is wedged when it has been running past MAX_TICK_DURATION_SECONDS
    AND it has stopped heartbeating (see HEARTBEAT_STALE_SECONDS). Elapsed
    time alone is NOT enough: with a degraded LLM provider a healthy tick
    legitimately runs for 15+ minutes, and releasing it let a second tick
    start while the first was still writing - two writers on one SQLite
    file, which is where the "database is locked" failures and the lost
    articles came from.

    Returns False if no tick is running, the tick is within the time limit,
    or the tick is still making progress.
    """
    global _running, _tick_started_at, _tick_generation
    if not _running or _tick_started_at is None:
        return False
    elapsed = (T.now_utc() - _tick_started_at).total_seconds()
    if elapsed <= MAX_TICK_DURATION_SECONDS:
        return False
    if _heartbeat_is_fresh(_last_successful_tick):
        # Still working - a slow LLM phase is not a hang. Leave it alone.
        return False
    log.warning(
        "Tick wedged for %.0fs (limit %ds) with no heartbeat - releasing "
        "running flag", elapsed, MAX_TICK_DURATION_SECONDS)
    _running = False
    _tick_started_at = None
    # Invalidate the wedged tick: when it finally unwinds it must not clear
    # the flag of the tick that replaces it.
    _tick_generation += 1
    return True


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
    # New articles that fell back to heuristics because the tick's LLM TIME
    # budget was spent. This is the third reason a model call is skipped, and
    # the one that actually bounds a slow provider.
    llm_new_skipped_budget: int = 0
    # One-shot policy: the model DID judge it and the verdict was MEDIUM /
    # LOW, so the article was deleted and suppressed for the trading day.
    llm_new_dropped_low: int = 0
    # Articles skipped without any model call because their guid was already
    # suppressed earlier today (a source re-serving a dropped story).
    skipped_dropped: int = 0
    # Backlog re-classification - RETIRED with the one-shot policy. The fields
    # stay (schema back-compat, always 0) so no reader has to change today.
    llm_backlog_ok: int = 0
    llm_backlog_failed: int = 0
    llm_backlog_reclassified: int = 0
    calendar_events: int = 0
    calendar_signals: int = 0
    skipped_unroutable: int = 0
    # FJ session-report impacts dropped at signal build because the asset is
    # not on the allowlist (gold / silver / bitcoin). The article row stays in
    # the database for the FinancialJuice five-box section - only its effect on
    # those cards is removed.
    skipped_fj_session: int = 0
    errors: list[str] = field(default_factory=list)
    purged: dict = field(default_factory=dict)

    def verify_new_article_counters(self) -> bool:
        """Assert that the new-article LLM buckets sum to new_today.

        The buckets are mutually exclusive: each new article is counted in
        exactly one of llm_new_ok / llm_new_failed / llm_new_skipped_breaker /
        llm_new_skipped_batch / llm_new_skipped_budget / llm_new_dropped_low.
        Their sum must therefore equal new_today exactly.

        ``llm_new_dropped_low`` is the one-shot fate rule: a model verdict of
        MEDIUM/LOW impact is still a verdict, and the article still counts -
        it just counts as DELETED.
        """
        total = (self.llm_new_ok + self.llm_new_failed +
                 self.llm_new_skipped_batch +
                 self.llm_new_skipped_breaker +
                 self.llm_new_skipped_budget +
                 self.llm_new_dropped_low)
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


async def _preclassify_for_llm(
    articles: list,
    session,
    existing: set,
    batch_limit: int,
    budget_seconds: float,
) -> dict:
    """
    PHASE A of a tick: classify the LLM-bound articles CONCURRENTLY.

    WHY
    ---
    The tick's model allowance is wall-clock (``LLM_TICK_BUDGET_SECONDS``),
    not a count, and one call costs 20-40s on the live endpoint. A sequential
    loop therefore spends the whole allowance on 2-5 articles even though
    ``LLM_MAX_CONCURRENT`` slots are configured and never used - the
    classifier's semaphore existed with exactly one caller at a time, so it
    was a no-op. Classifying the batch concurrently multiplies the verdicts
    per tick by the concurrency factor WITHOUT lengthening the tick: the
    calls overlap instead of queueing behind each other.

    MEASURED on the live endpoint (2026-10-21): 90s of budget at ~30s a call
    bought 3 verdicts per tick, and the count cap of 15 was never reached.
    With three calls in flight the same 90s buys about nine.

    HOW
    ---
    The candidates are the first ``batch_limit`` NEW articles in the already
    priority-sorted batch - the same articles the sequential path would have
    sent to the model, in the same order. The cheap filters that would have
    rejected them anyway (already stored, suppressed by the one-shot policy,
    outside the rolling window) are applied first, so a wasted call is rare.

    Returns ``{guid: (analysis, llm_ok, skip_reason)}`` for every candidate.
    A candidate whose call was cancelled by the deadline is present with a
    ``None`` value, which tells phase B not to call the provider again for
    it - that is the difference between "the model said no" and "we ran out
    of time", and the second must not be spent twice.
    """
    global _llm_cooldown_until

    results: dict = {}
    if not settings.has_llm or batch_limit <= 0 or budget_seconds <= 0:
        return results

    if time.monotonic() < _llm_cooldown_until:
        # The provider answered NOTHING last tick. Spending another full
        # budget on it would make the tick 90s long for zero verdicts, and
        # would hammer an endpoint that is already down. The heuristics
        # still classify every article; the model is simply not asked.
        log.info("LLM cooldown active for %.0fs more - provider answered "
                 "nothing last tick, articles use heuristics",
                 _llm_cooldown_until - time.monotonic())
        return results

    from app.analysis.news_classifier import analyze_article

    suppressed = _suppressed_guids(session)
    candidates: list[RawArticle] = []
    for art in articles:
        if len(candidates) >= batch_limit:
            break
        if art.guid in existing or art.guid in suppressed:
            continue
        published, _updated, _tz = _parse_article_time(art)
        if published is None:
            continue
        if not within_rolling_window(published) or not within_max_age(published):
            continue
        if _is_fj_session_report_sync(art) and not _fj_session_report_is_admissible(
                published):
            continue
        candidates.append(art)

    if not candidates:
        return results

    log.info("LLM phase A: %d candidate(s) in flight, budget=%.0fs",
             len(candidates), budget_seconds)

    async def _classify(art: RawArticle):
        try:
            verdict = await analyze_article(art.title, art.body)
        except Exception as exc:  # noqa: BLE001 - one row never kills the phase
            log.warning("LLM phase A failed for %r: %s",
                        (art.title or "")[:70], exc)
            return art.guid, (None, False, "error")
        return art.guid, verdict

    tasks = []
    task_guids: dict = {}
    for art in candidates:
        task = asyncio.create_task(_classify(art))
        tasks.append(task)
        task_guids[task] = art.guid

    started = time.monotonic()
    done, pending = await asyncio.wait(tasks, timeout=budget_seconds)
    for task in done:
        guid, verdict = task.result()
        results[guid] = verdict
    if pending:
        for task in pending:
            task.cancel()
        # Let the cancellations settle so no request outlives the tick.
        await asyncio.gather(*pending, return_exceptions=True)
        # A cancelled candidate has NO verdict: mark it so phase B does not
        # spend another call on it.
        for task in pending:
            results[task_guids[task]] = None

    elapsed = time.monotonic() - started
    judged = sum(1 for v in results.values()
                 if v is not None and len(v) >= 2 and v[1])
    log.info("LLM phase A: %d verdict(s) from %d candidate(s) in %.0fs "
             "(%d cut off by the deadline)",
             judged, len(candidates), elapsed, len(pending))

    if judged == 0:
        # Nothing came back. Back off for a tick so the engine stays
        # responsive and the endpoint is not hammered while it is down.
        _llm_cooldown_until = time.monotonic() + LLM_COOLDOWN_SECONDS
        log.warning("LLM provider returned no verdict this tick - cooling "
                    "down for %.0fs", LLM_COOLDOWN_SECONDS)
    return results


async def _ingest_article(art: RawArticle, session, stats: IngestStats,
                          use_llm: bool = True, precomputed=None):
    """Apply the TODAY filter, classify, cluster and store one article.

    ONE-SHOT: this is the only place an article is ever classified, and the
    verdict also decides its fate - HIGH/VERY_HIGH impact is stored in the
    Section B feed, MEDIUM/LOW is deleted here and suppressed for the rest of
    the trading day (``_drop_low_impact_article`` / ``_guid_suppressed``).
    """
    published, updated, tz_label = _parse_article_time(art)
    is_fj = _is_fj_session_report_sync(art)

    # --- ONE-SHOT SUPPRESSION ---
    # A guid deleted earlier today is skipped before ANY work is done: no
    # model call, no row, no counters. FinancialJuice session reports are
    # never dropped by the policy, so they are never suppressed either.
    if not is_fj and _guid_suppressed(session, art.guid):
        stats.skipped_dropped += 1
        log.debug("ONE-SHOT SKIP (suppressed today): %r guid=%s",
                  (art.title or "")[:80], art.guid)
        return None

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
    # STEP 9.1: pass the tick's clock, so the prune window is absolute rather
    # than the incoming article's own timestamp drifting it forward.
    now_utc = T.now_utc()
    dedupe = _dedup.match(art.title, published, now=now_utc)
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
    if precomputed is not None:
        # Phase A already asked the model about this article (concurrently).
        # Use that verdict; never call the provider twice for one article.
        analysis, llm_ok, skip_reason = precomputed
    elif use_llm:
        analysis, llm_ok, skip_reason = await analyze_article(art.title, art.body)
    else:
        from app.analysis.news_classifier import heuristic_analyze

        analysis = heuristic_analyze(art.title, art.body)
        analysis.note = "LLM batch cap reached; heuristic used for this article."
        llm_ok = False
        skip_reason = "batch_cap"

    # --- CHART SCOPE ---
    # A story listed on the gold chart is the gold card's news, and the gold
    # card's news only. The scope is resolved at the fetch layer (the chart
    # queries that returned this story) and applied HERE, before the fate
    # decision and before any impact row is written - this is the single
    # choke point where an article's target cards are decided.
    chart_scope = _chart_scope_for(art)
    if chart_scope:
        from app.analysis.news_classifier import apply_chart_scope

        analysis = apply_chart_scope(analysis, chart_scope, art.title, art.body)

    # --- ONE-SHOT FATE: keep in Section B, or delete and suppress ---
    # Decided BEFORE the outcome bucket is chosen, so the buckets stay
    # mutually exclusive: a dropped article is counted in exactly one of them
    # (llm_new_dropped_low), never also in llm_new_ok / llm_new_failed.
    # Runs before any event/impact row is created, so a dropped article leaves
    # no cluster behind. Exempt: the FinancialJuice session reports (Section A)
    # are the prep/wrap boxes, not news, and the policy does not apply.
    if not is_fj and not is_section_b_verdict(
            analysis, llm_used=llm_ok, chart_scoped=bool(chart_scope),
            title=art.title):
        _drop_low_impact_article(session, row, analysis, stats)
        return None

    if use_llm:
        # The new-article buckets are MUTUALLY EXCLUSIVE: every new article
        # lands in exactly one of llm_new_ok / llm_new_failed /
        # llm_new_skipped_breaker / llm_new_skipped_batch (or, under the
        # one-shot policy, llm_new_dropped_low above), and their sum must
        # equal new_today. A circuit-breaker skip is its own bucket, NOT a
        # member of llm_new_failed - counting it in both double-counted the
        # article and broke the invariant.
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
    # The chart that listed this story, and the story's own TradingView tags.
    # Both are audit fields: the CARDS this story reached are the impact rows
    # written below, and they can only be the scoped ones.
    row.chart_scope = scope_label(chart_scope)
    row.tv_symbols = json.dumps(list(getattr(art, "symbols", None) or []))

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
                speaker_tier=asset_analysis.speaker_tier,
                session=current_session.value,
                importance_weight=_importance_weight(analysis.importance),
            )
        )

    stats.new_today += 1
    return row


def _chart_scope_for(art: RawArticle) -> list[Asset]:
    """
    The cards whose CHART listed this story.

    Empty for every source without a chart - the RSS feeds, FinancialJuice,
    Kitco and the rest - and then the classifier's routing decides the cards
    exactly as before. Only the TradingView feed carries a chart.

    The scope was resolved at the fetch layer (the chart queries that
    returned the story), so a story listed on two charts arrives with two
    cards and reaches both.
    """
    if not settings.chart_scope_isolation:
        return []
    out: list[Asset] = []
    for value in (getattr(art, "chart_assets", None) or []):
        try:
            asset = Asset(str(value).strip().upper())
        except ValueError:
            continue
        if asset not in out:
            out.append(asset)
    return out


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


def _downweight_news_duplicating_calendar(signals: list[WeightedSignal]) -> list[WeightedSignal]:
    """
    Down-weight news signals that duplicate a calendar release.

    Calendar signals have a non-empty event_key and published_at <= now.
    For each other signal s with empty event_key, same asset, same direction
    (BULLISH or BEARISH only), same indicator family (and != "unknown"),
    and published within 60 minutes of the calendar signal:
    apply s.relevance *= CALENDAR_NEWS_DUP_FACTOR (once per signal, even
    if several calendar signals match).
    """
    now = datetime.now(timezone.utc)
    from app.analysis.release_surprise import indicator_family

    def _as_aware_utc(dt: datetime) -> datetime:
        """Ensure datetime is timezone-aware UTC for safe comparison."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # Collect calendar signals: non-empty event_key, released (published_at <= now)
    calendar_signals = [
        s for s in signals
        if s.event_key and s.published_at and _as_aware_utc(s.published_at) <= now
    ]

    if not calendar_signals:
        return signals

    # Group calendar signals by (asset, family, time window)
    # For each calendar signal, find matching news signals
    downweighted: set[int] = set()

    for cal in calendar_signals:
        cal_family = indicator_family(cal.title)
        if cal_family == "unknown":
            continue

        cal_asset = cal.asset
        cal_direction = cal.direction
        cal_time = cal.published_at

        for i, s in enumerate(signals):
            if i in downweighted:
                continue
            # Skip calendar signals themselves
            if s.event_key:
                continue
            # Same asset
            if s.asset != cal_asset:
                continue
            # Same direction (only BULLISH or BEARISH)
            if s.direction not in (Direction.BULLISH, Direction.BEARISH):
                continue
            if s.direction != cal_direction:
                continue
            # Same indicator family
            s_family = indicator_family(s.title)
            if s_family == "unknown" or s_family != cal_family:
                continue
            # Within 60 minutes
            if s.published_at is None:
                continue
            s_pub = _as_aware_utc(s.published_at)
            cal_time_aware = _as_aware_utc(cal_time)
            delta = abs(s_pub - cal_time_aware)
            if delta.total_seconds() <= 3600:  # 60 minutes
                s.relevance *= CALENDAR_NEWS_DUP_FACTOR
                downweighted.add(i)

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


def current_calendar_events() -> list[CalendarEvent]:
    """
    Read-only view of the most recent RAW calendar events.

    The signals are the engine's own reading of those events; this is the
    event list itself, which is what the regional macro bias panel scores.
    Returned as a copy so a caller cannot mutate the scheduler's state.
    """
    return list(_calendar_events)


def regional_bias_payload() -> dict:
    """
    The REGIONAL MACRO BIAS panel payload.

    Scored from the SAME events the verdict uses - the five-region calendar
    view with its own per-asset score, its own region breakdown and its own
    upcoming-event list. It never raises: a failure here must not take the
    dashboard down, so the panel reports the error instead.
    """
    from app.analysis.regional_bias import compute_regional_bias, to_payload

    try:
        return to_payload(compute_regional_bias(current_calendar_events()))
    except Exception as exc:  # noqa: BLE001 - a panel must never break a request
        log.warning("Regional macro bias panel failed: %s: %s",
                    type(exc).__name__, exc)
        return {
            "generated_at_utc": None,
            "generated_at_local": "",
            "display_timezone": settings.display_timezone,
            "last_updated_utc": None,
            "last_updated_local": "",
            "stale": True,
            "stale_reason": f"the regional bias panel failed: {exc}",
            "error": f"{type(exc).__name__}: {exc}",
            "regions": [],
            "assets": [],
            "upcoming": [],
            "stats": {},
            "now_local": "",
        }


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
    from app.analysis.sentiment_engine import _signal_weight

    rows = session.scalars(
        select(NewsArticle).where(news_day_predicate())
    ).all()

    # Overnight admission: rows the morning brief judged while the laptop was
    # off are in the day's rows, but their age must be measured from the
    # moment the engine came back (the window's end), not from a publication
    # the operator could not act on - otherwise TRADING_DAY_CUTOFF_HOURS
    # zeroes the entire batch and it never reaches a score. Live rows keep
    # publication-time ageing (age_ref is None for them).
    from app.services.overnight import admit_window

    window = admit_window()
    overnight_age_ref = (window.end
                         if window is not None
                         and settings.overnight_counts_as_fresh
                         else None)

    signals: list[WeightedSignal] = []
    category_weights: dict = {}
    # TASK 1: remember which AssetImpact row each signal was built from, so
    # final_weight can be persisted AFTER clustering from the signal's actual
    # weight rather than before it.
    _weight_targets: dict[int, object] = {}
    _weight_targets_by_content: dict[tuple, list] = {}
    # Phase 2: verdicts collected per asset, logged once after the loop so the
    # snapshot rows are written after every asset's verdict is persisted.
    verdicts: list = []

    for row in rows:
        # HEARTBEAT: the recompute re-scores every stored row of the day, so
        # on a busy news day this loop is long enough to matter. Refreshing
        # the stamp here keeps a working tick from ever looking stale.
        _refresh_tick_stamp()
        if row.published_at_utc is None:
            continue
        event = row.event_links[0].event if row.event_links else None
        is_primary = (event.article_count <= 1) if event else True

        try:
            cat_enum = NewsCategory(row.category)
        except ValueError:
            cat_enum = NewsCategory.OTHER

        # Overnight admission: this row was published inside the covered
        # off-window, so age it from the boot (the window's end) instead of
        # its publication stamp. Anything else keeps None = live ageing.
        age_reference_utc = (
            overnight_age_ref
            if overnight_age_ref is not None
            and row.published_at_utc is not None
            and window.contains(row.published_at_utc)
            else None
        )

        # Task 3: an ACTUAL scheduled-data report also carries a surprise
        # weight - its direction was already fixed at ingest from the
        # surprise sign, this is only how LARGE the surprise was.
        kind = ReportKind(row.report_kind)
        release_text = f"{row.title} {row.body}"
        printed = (release_surprise.parse(release_text)
                   if kind is ReportKind.ACTUAL else None)

        # One classification per article (not per impact): true for the five
        # FinancialJuice session reports - the prep/wrap summaries. Their home
        # is the FJ five-box section; the only multi-source card they may shape
        # is the one on SESSION_REPORT_ALLOWED_ASSETS (today: EQUITY, the
        # region's own tape).
        is_session_report = is_fj_session_report_row(row)

        # Map every impact row on this article by asset so a signal's bleed
        # copy (same headline, other half of a precious-metals pair) still
        # knows which AssetImpact row it must publish its final weight to.
        impacts_by_asset = {}
        for _imp in row.impacts:
            try:
                impacts_by_asset.setdefault(Asset(_imp.asset), _imp)
            except ValueError:
                continue

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
            # FJ SESSION REPORTS: a session prep/wrap is a SUMMARY, not a
            # trade signal, so it may only shape the cards on
            # fj_sessions.SESSION_REPORT_ALLOWED_ASSETS (today: EQUITY, the
            # region's own tape). Dropping the impact here removes it from
            # gold / silver / bitcoin at the ONE choke point every verdict
            # passes through: no bullish/bearish weight, no TOP DRIVERS or
            # strongest-conflict box, no burst bonus, no binary tie-break.
            # The article row itself is untouched - it still feeds the
            # FinancialJuice five-box section, which reads rows directly.
            if is_session_report and not session_report_asset_allowed(asset_enum):
                stats.skipped_fj_session += 1
                log.debug(
                    "Skipping FJ session report impact for %s on article %d "
                    "(%s) - session reports stay in their five boxes",
                    asset_enum.value, row.id, row.title[:80],
                )
                continue
            signal = build_signal(
                asset=asset_enum,
                title=row.title,
                # A legacy row can carry a string the current enum no longer
                # accepts (e.g. strength='STRONG'). Degrade it instead of
                # raising - one stale row must never kill the tick.
                direction=_safe_enum(Direction, impact.direction,
                                     Direction.NEUTRAL),
                strength=_safe_enum(ImpactStrength, impact.strength,
                                    ImpactStrength.MEDIUM),
                importance=_safe_enum(EconomicImportance, row.importance,
                                      EconomicImportance.MEDIUM),
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
                # Step 3: carry the article's own analysis mode so the card
                # reads "llm" / "mixed" instead of always "heuristic".
                mode=(row.analysis_mode or "heuristic"),
                surprise_weight=release_surprise.surprise_weight(
                    printed, release_text),
                session_multiplier=asset_session_multiplier(
                    asset_enum.value, detect_session(), cat_enum.value),
                # Section 7: silver's dual-nature character, carried from
                # the AssetImpact row so the weight path can reward a
                # characterised XAGUSD story on every recompute.
                metal_character=impact.metal_character,
                # Central bank speaker tier multiplier.
                speaker_tier=getattr(impact, "speaker_tier", 1.0),
                # Overnight admission freshness (None for live rows).
                age_reference_utc=age_reference_utc,
                # The chart that listed this story ("XAUUSD", "XAUUSD,XAGUSD",
                # ...). Empty for a story with no chart. Carried so the
                # cross-asset bleed can leave a chart-scoped story on its own
                # card: gold chart news must not bleed onto the silver card.
                chart_scope=row.chart_scope or "",
            )
            signals.append(signal)
            _weight_targets[id(signal)] = impacts_by_asset.get(signal.asset, impact)
            _weight_targets_by_content.setdefault(
                _final_weight_key(signal), []
            ).append(impacts_by_asset.get(signal.asset, impact))

            # IMPROVEMENT 5: gold/silver cross-asset bleed. A high-relevance,
            # high-strength macro story lands on both precious metals; the
            # secondary copy is auto-filtered to XAUUSD/XAGUSD only, so
            # Bitcoin and the equity proxy (Asset.BITCOIN) never reach it.
            bleed = build_bleed_signal(signal)
            if bleed is not None:
                signals.append(bleed)
                # A bleed copy has its own asset. If this article did not store
                # an impact row for that asset, it must NOT be routed back to
                # the primary impact row: it would overwrite the primary's
                # final_weight with the bleed's (different) weight.
                bleed_target = impacts_by_asset.get(bleed.asset)
                if bleed_target is not None:
                    _weight_targets[id(bleed)] = bleed_target
                    _weight_targets_by_content.setdefault(
                        _final_weight_key(bleed), []
                    ).append(bleed_target)
            category_weights[cat_enum] = (
                category_weights.get(cat_enum, 0.0) + impact.relevance
            )

    assessment = regime_mod.detect_regime(category_weights)

    # Economic calendar joins the same aggregation as the news flow, so the
    # bullish/bearish verdict reflects scheduled macro risk too.
    #
    # PHASE 7: A SCHEDULED PRINT IS NOT EVIDENCE.
    #
    # Only a RELEASED print - one with a real ``actual`` - may add bull/bear
    # weight to a card. An UPCOMING print (no actual yet) is a forward-looking
    # catalyst: it belongs in the economic-calendar panel and in the EVENT
    # RISK note, and NOWHERE in the score. Before this gate the card moved on
    # a CONSENSUS that nobody had published yet, and it moved on the
    # CONSENSUS DIRECTION, which is a guess about a print that has not
    # happened. ``calendar_upcoming_contributes`` restores the old behaviour
    # for anyone who wants it.
    for sig in _calendar_signals:
        if (sig.mode != "RELEASED"
                and not settings.calendar_upcoming_contributes):
            continue
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
            new_mult = asset_session_multiplier(s.asset.value, current_session, s.category.value)
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

    # Down-weight news that duplicates a calendar release
    signals = _downweight_news_duplicating_calendar(signals)

    # Near-duplicate clustering at scoring time (item 1 fix)
    # Collapse near-identical headlines within a 30-minute window across sources
    from app.analysis.event_clustering import cluster_signals
    signals = cluster_signals(signals, window_hours=0.5)

    # TASK 1: persist the weight the aggregation actually summed. Writing this
    # before cluster_signals() would publish the pre-cluster weight, so the
    # feed and the score could disagree when a near-duplicate dominated the
    # raw list. Now every surviving impact row receives its own post-cluster
    # _signal_weight; recency_weight mirrors it because the score path
    # recomputes the same single weight and nothing else is stored for it.
    recompute_now = T.now_utc()
    for surviving in signals:
        # The surviving object is usually the mapped signal itself, but
        # cluster_signals() may return a freshly constructed representative
        # when a cluster had several copies. Fall back to the signal's content
        # so the impact row the score actually used still receives the weight.
        targets = []
        direct = _weight_targets.get(id(surviving))
        if direct is not None:
            targets.append(direct)
        else:
            targets.extend(_weight_targets_by_content.get(
                _final_weight_key(surviving), ()))
        if not targets:
            continue
        final_weight = _signal_weight(surviving, recompute_now)
        if final_weight and final_weight > 0.0:
            for impact in targets:
                impact.final_weight = final_weight
                impact.recency_weight = final_weight

    # Item 8: an imminent high-impact release makes any standing bias unreliable.
    # Read from the RAW event list, not from the signals: only RELEASED prints
    # become signals, but an unreleased high-impact print is exactly the thing
    # the trader must be warned about.
    #
    # A release from a region OUTSIDE the whitelist is passed separately: it
    # scores nothing (it is not evidence for a dollar) but it still moves the
    # market, so it raises the same warning, labelled "other region".
    at_risk, risk_note = (False, "")
    try:
        from app.analysis.calendar_risk import event_risk_check
        from app.ingestion.economic_calendar import filtered_out_events
        at_risk, risk_note = event_risk_check(
            _calendar_events,
            other_events=[e for e, _reason in filtered_out_events()])
    except Exception as exc:  # noqa: BLE001 - never break the recompute
        log.debug("event_risk_check failed: %s", exc)

    # PER-ASSET BASELINE. Computed ONCE per tick, before the equity card and
    # before the asset loop, from each asset's own persisted history.
    #
    # Two things matter here:
    #   1. ONE baseline for the whole tick. The verdict, the published score,
    #      the sentiment_snapshot row and the bias_snapshots row must all be
    #      built on the SAME baseline, or the audit trail would show two
    #      different numbers for the same tick.
    #   2. NO LOOK-AHEAD. The history is read before this tick's rows are
    #      written, so a tick can never influence its own baseline.
    #
    # The baseline is the asset's trailing median of its UNADJUSTED raw
    # scores - so the history is a series of levels, never a series of
    # already-centred values. That is what stops the adjustment from
    # feeding back on itself and converging to zero.
    baselines: dict[str, float | None] = {}
    try:
        from app.analysis.bias_baseline import baseline_score

        for asset in Asset:
            try:
                baselines[asset.value] = baseline_score(asset.value)
            except Exception as exc:  # noqa: BLE001 - baseline is optional
                log.debug("baseline query failed for %s: %s", asset.value, exc)
                baselines[asset.value] = None
    except Exception as exc:  # noqa: BLE001 - baseline is optional
        log.debug("baseline stage skipped: %s", exc)

    # Session-aware global equity card: NOW-only, in-memory, never persisted.
    # No SentimentSnapshot / BiasSnapshot rows - the frontend keys flip state
    # by the region field in the payload.
    #
    # BUILT HERE, BEFORE THE ASSET LOOP, and that position is the point.
    # The pipeline used to score a SECOND, region-blind equity proxy
    # (``compute_sentiment(asset_signals, Asset.EQUITY)`` with the default
    # history key) and publish THAT to the decision log and to
    # bias_snapshots, while the dashboard showed the region-filtered card
    # computed under its own ``EQUITY:<region>`` history key. Two different
    # equity verdicts were published for the same tick, and every hit-rate
    # number was measured against the one the trader never saw.
    #
    # Building the card first means the EQUITY verdict the pipeline logs and
    # scores IS the verdict the dashboard shows.
    global _last_equity_card, _last_equity_signals
    equity_card_result = None
    try:
        from app.analysis.equity_card import build_equity_card, to_payload
        _last_equity_signals = list(signals)
        equity_card_result = build_equity_card(
            signals, now=recompute_now,
            baseline=baselines.get(Asset.EQUITY.value))
        _last_equity_card = to_payload(equity_card_result)
    except Exception as exc:  # noqa: BLE001 - equity card never breaks a poll
        log.debug("equity card build failed: %s", exc)
        _last_equity_card = None

    for asset in Asset:
        if asset is Asset.EQUITY:
            # Equity is served by the session-aware card
            # (build_equity_card / top-level "equity" key), not by
            # SentimentSnapshot rows - so there is no snapshot to write here.
            #
            # What IS written is the card's own verdict, into the decision log
            # and into bias_snapshots, so the equity history that the hit-rate
            # and calibration tooling read is the one a trader acted on.
            #
            # The card owns its carry-forward state under "EQUITY:<region>",
            # so the pipeline must NOT also call remember_bias("EQUITY", ...)
            # here: that would be a second, competing equity history.
            if equity_card_result is None:
                continue
            try:
                _decision_log().record_bias(
                    _EquityCardVerdict(equity_card_result))
            except Exception as exc:  # noqa: BLE001 - auditing is best-effort
                log.debug("decision log write failed: %s", exc)
            verdicts.append(AssetVerdict(
                asset=asset,
                score=float(equity_card_result.score),
                confidence=float(equity_card_result.confidence),
                bias=str(equity_card_result.bias),
                article_count=int(equity_card_result.contributing_articles),
                last_signal_age_min=_last_signal_age_min(
                    [s for s in signals if s.asset is asset], T.now_utc()),
                # The equity card's drivers are plain DICTS; bias_snapshots
                # and the decision log both read them as objects with
                # ``.title`` / ``.contribution`` / ``.direction``. Passing the
                # dicts straight through raised AttributeError inside
                # write_bias_snapshots, which swallowed it and wrote NOTHING
                # for ANY asset - silently killing the whole audit trail.
                bull_drivers=[_DriverView(d) for d in
                              (equity_card_result.bull_drivers or [])],
                bear_drivers=[_DriverView(d) for d in
                              (equity_card_result.bear_drivers or [])],
                # The two aggregate totals: the logged split is their ratio, so
                # the row has to carry the numbers the ratio was made from.
                bull=float(equity_card_result.bull),
                bear=float(equity_card_result.bear),
            ))
            log.info(
                "EQUITY: %s score=%+.1f (n=%d conf=%.2f)",
                equity_card_result.bias, equity_card_result.score,
                equity_card_result.contributing_articles,
                equity_card_result.confidence,
            )
            continue

        asset_signals = [s for s in signals if s.asset is asset]

        current_result = compute_sentiment(
            asset_signals, asset, event_risk=at_risk,
            event_risk_note=risk_note,
            now=recompute_now,
            baseline=baselines.get(asset.value))

        # Item 11: persist every published bias so it can be scored later.
        try:
            _decision_log().record_bias(current_result)
        except Exception as exc:  # noqa: BLE001 - auditing is best-effort
            log.debug("decision log write failed: %s", exc)

        # Binary bias: carry-forward history must equal the PUBLISHED (CURRENT)
        # verdict. Only CURRENT is written below, so there is no stale prep/wrap
        # write to the shared per-asset state.
        if (not current_result.stale
                and current_result.confidence > 0.0
                and current_result.confidence_label != "LOW"):
            remember_bias(asset.value, current_result.bias)

        for scope, result in (
            ("CURRENT", current_result),
        ):
            # TASK 2: collect bull/bear on every snapshot row for every asset,
            # so historical r is reconstructable from the persisted totals.
            # EQUITY is no longer here: it is served by the session-aware card
            # and was already handled above.
            session.add(
                SentimentSnapshot(
                    asset=asset.value,
                    scope=scope,
                    score=result.score,
                    raw_score=result.raw_score,
                    baseline_score=result.baseline_score,
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
                    contributing_clusters=result.contributing_clusters,
                      cluster_conflict=result.cluster_conflict,
                      evidence_split=result.evidence_split,
                      # Deprecated alias: old consumers read contradiction_detected.
                      contradiction_detected=result.cluster_conflict,
                      burst_bull=result.burst_bull,
                      burst_bear=result.burst_bear,
                      stale=result.stale,
                    confidence_label=result.confidence_label,
                    price_note=_price_note(asset),
                    bull=result.bull_weight,
                    bear=result.bear_weight,
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
            # The UNADJUSTED level goes into the audit trail as raw_score.
            # The baseline history is a series of levels, so it must never be
            # built from already-centred values - otherwise the adjustment
            # would feed back on itself and the skew would never come out.
            raw_score=current_result.raw_score,
            baseline_score=current_result.baseline_score,
            adjusted=(current_result.baseline_score is not None),
            # The two aggregate totals: the logged split is their ratio, so
            # the row has to carry the numbers the ratio was made from.
            bull=float(current_result.bull_weight),
            bear=float(current_result.bear_weight),
        ))

    # Phase 2: append-only validation log, written AFTER every verdict is
    # already persisted. Wrapped so it can never break or slow a poll.
    try:
        from app.services.bias_snapshots import write_bias_snapshots

        written = write_bias_snapshots(session, verdicts)
        if written:
            log.debug("bias_snapshots: %d row(s) appended", written)
            # THE MEASUREMENT LOOP. Every published bias CHANGE is recorded as
            # a PENDING outcome so the engine can be scored against what the
            # price actually did. Before this, ``bias_outcome`` could only be
            # filled by hand - one click per call - so it stayed empty and the
            # confidence coefficients were never checked against anything.
            #
            # It runs only when the snapshot rows were actually written: the
            # outcome rows reference a bias_snapshots row by foreign key, so
            # attaching a call to a stale snapshot would corrupt the join.
            try:
                from app.services.outcomes import record_new_calls

                record_new_calls(session, verdicts)
            except Exception as exc:  # noqa: BLE001 - measurement is optional
                log.debug("outcome capture skipped: %s", exc)
    except Exception as exc:  # noqa: BLE001 - diagnostics never break a poll
        log.warning("bias snapshot stage skipped: %s", exc)

    # The session-aware equity card is built ABOVE, before the asset loop, so
    # that the EQUITY verdict recorded here and in the decision log is the same
    # one the dashboard shows. Nothing more to do at the end of the recompute.

    return signals, assessment


def current_equity_signals() -> list:
    """Copy of the signal list behind the most recent equity card."""
    return list(_last_equity_signals)


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

    OVERNIGHT ADMISSION: a laptop that was powered off over the ET midnight
    boundary leaves its last headlines stamped on the PREVIOUS market day, so
    the plain ``published_at >= day_start`` test would drop every one of them
    from the morning's boxes - the model may have just judged them in the
    morning brief and they would still never reach a score. When the brief has
    covered an off-window today, rows published inside that window are
    admitted too (``overnight.admit_window``); the window lapses at the next
    market midnight, so it can never re-serve yesterday as today.
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

    # Best-effort: an unreadable/expired admission simply yields None and the
    # predicate degrades to the plain market-day filter.
    from app.services.overnight import admit_window

    window = admit_window()

    clauses = [
        NewsArticle.published_at_utc >= day_start,
        _is_fj_source(NewsArticle.source),
    ]
    if window is not None:
        clauses.append(and_(
            NewsArticle.published_at_utc >= window.start.replace(tzinfo=None),
            NewsArticle.published_at_utc <= window.end.replace(tzinfo=None),
        ))

    return and_(
        NewsArticle.is_today.is_(True),
        or_(*clauses),
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

    ``dropped_article`` (the one-shot suppression log) is trimmed on the same
    window: a suppression only has to outlive the trading day that wrote it.

    Never raises: a retention failure must not take the pipeline down.
    """
    keep = settings.news_retention_days if keep_days is None else keep_days
    result = {"articles": 0, "events": 0, "dropped_articles": 0, "cutoff": None}
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

        # One-shot suppression rows follow the same window. A suppression only
        # has to outlive the trading day that wrote it; keeping them past the
        # retention window would let the table grow without bound for no gain.
        stale_drops = session.execute(
            delete(DroppedArticle).where(DroppedArticle.created_at < cutoff_utc)
        )
        result["dropped_articles"] = stale_drops.rowcount or 0

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


async def _record_price_points() -> int:
    """
    Store one spot price per asset that has a reachable feed.

    Best-effort and never fatal: an asset with no feed, or a feed that is
    down, simply produces no row. The engine never invents a price.
    """
    from app.market.price_data import get_spot_price
    from app.services.outcomes import record_price_point

    quotes: list[tuple[Asset, float, str]] = []
    for asset in Asset:
        if asset is Asset.EQUITY:
            continue
        try:
            price, source = await get_spot_price(asset)
        except Exception as exc:  # noqa: BLE001 - a missing price is normal
            log.debug("spot price failed for %s: %s", asset.value, exc)
            continue
        if price is not None:
            quotes.append((asset, price, source))
    if not quotes:
        return 0
    try:
        with session_scope() as session:
            written = 0
            for asset, price, source in quotes:
                if record_price_point(session, asset, price, source):
                    written += 1
            return written
    except Exception as exc:  # noqa: BLE001 - measurement never breaks a tick
        log.debug("price point write skipped: %s", exc)
        return 0


def _calendar_release_window(now=None) -> bool:
    """
    Is a high-impact release inside its burst window?

    The window runs from ``FJ_CALENDAR_PRE_RELEASE_MINUTES`` before the
    release until ``FJ_CALENDAR_POST_RELEASE_MINUTES`` after it. Inside it the
    calendar is polled at the faster ``FJ_CALENDAR_BURST_SECONDS`` cadence, so
    the actual number is picked up within a tick of the print instead of
    waiting out the full poll interval.
    """
    burst = int(settings.fj_calendar_burst_seconds or 0)
    if burst <= 0 or not _calendar_events:
        return False
    now = now or T.now_utc()
    pre = timedelta(minutes=int(settings.fj_calendar_pre_release_minutes or 0))
    post = timedelta(minutes=int(settings.fj_calendar_post_release_minutes or 0))
    for event in _calendar_events:
        when = getattr(event, "when_utc", None)
        if when is None:
            continue
        if str(getattr(event, "impact", "") or "").strip().capitalize() != "High":
            continue
        if when - pre <= now <= when + post:
            return True
    return False


def _calendar_poll_due(now) -> tuple[bool, float, float]:
    """
    Is the calendar due for a refresh?

    Returns ``(due, seconds_until_due, cadence_seconds)``. The cadence is
    ``FJ_CALENDAR_POLL_SECONDS``, tightened to ``FJ_CALENDAR_BURST_SECONDS``
    while a high-impact release sits inside its window. A cadence of 0 means
    "every tick", which is the pre-existing behaviour.
    """
    cadence = float(settings.fj_calendar_poll_seconds or 0)
    if cadence <= 0:
        return True, 0.0, 0.0
    burst = float(settings.fj_calendar_burst_seconds or 0)
    if burst > 0 and _calendar_release_window(now):
        cadence = burst
    last = _calendar_last_fetch_utc
    if last is None or not _calendar_events:
        return True, 0.0, cadence
    elapsed = (now - last).total_seconds()
    if elapsed >= cadence:
        return True, 0.0, cadence
    return False, max(0.0, cadence - elapsed), cadence


async def run_once() -> IngestStats:
    """
    Execute one full pipeline pass.

    Never raises - errors are logged and returned in ``stats.errors`` so the
    scheduler keeps running.
    """
    global _running, _last_update, _last_successful_tick, _news_since_update, _calendar_signals, _calendar_events, _calendar_last_fetch_utc, _tick_started_at, _tick_generation

    if _running:
        # A tick is already in flight. If it is genuinely WEDGED (no
        # heartbeat, past the hard cap) release it and take over - otherwise
        # the engine is stuck forever and the dashboard never gets a tick.
        # A merely SLOW tick is left alone: releasing it would start a
        # second tick while the first is still writing to the same SQLite
        # file, which is how the "database is locked" errors and the lost
        # articles happened.
        if not check_and_release_hung_tick():
            log.debug("Pipeline already running; skipping this tick")
            return IngestStats(errors=["pipeline already running"])
        log.warning("Released wedged tick from previous run; proceeding with new tick")

    # Reset LLM circuit breaker at the start of each tick so a degraded
    # endpoint gets a fresh chance on the next poll.
    await llm_client.reset_circuit_breaker()

    _running = True
    _tick_generation += 1
    my_generation = _tick_generation
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
        #
        # The calendar has ITS OWN cadence (FJ_CALENDAR_POLL_SECONDS): the
        # FinancialJuice render is slow and Cloudflare-sensitive, so it is not
        # re-rendered on every tick. Inside a high-impact release's window
        # the cadence tightens to FJ_CALENDAR_BURST_SECONDS.
        due, wait, cadence = _calendar_poll_due(T.now_utc())
        if due:
            events, calendar_errors = await fetch_calendar()
            _calendar_last_fetch_utc = T.now_utc()
            stats.errors.extend(calendar_errors)
        else:
            events, calendar_errors = list(_calendar_events), []
            log.info("Economic calendar: next poll in %.0fs (cadence %.0fs) - "
                     "reusing %d event(s)", wait, cadence, len(events))
        _calendar_events = events
        _calendar_signals = signals_for_events(
            events, horizon_hours=settings.economic_calendar_horizon_hours
        )
        stats.calendar_events = len(events)
        stats.calendar_signals = len(_calendar_signals)

        # Stamp the tick as soon as ingestion+calendar succeed so a later
        # failure in ingestion/classification/sentiment doesn't leave us at
        # NO TICK forever. The stamp is then HEARTBEATED as the tick
        # progresses (see _refresh_tick_stamp), so a slow-but-alive tick
        # never ages past HEALTH_STALE_MINUTES and paints a false STALE
        # banner over live data.
        _refresh_tick_stamp(force=True)

        with session_scope() as session:
            # STEP 9.2: reseed the dedup index from stored headlines once per
            # process so a restart does not re-admit today's events.
            seed_dedup_index_from_db(session)
            # Bookkeeping row for the dashboard's last-run panel. It is
            # best-effort: a transient SQLite write lock (e.g. the morning
            # brief holding an open write transaction) must never abort the
            # whole pass. When this insert failed in the past, the exception
            # unwound run_once() before _last_successful_tick was stamped, so
            # /api/health stayed 503 no_tick_completed and the dashboard pill
            # read "NO TICK" forever.
            run_row: PipelineRun | None = None
            run_id = None
            try:
                run_row = PipelineRun(
                    started_at=started, status="running",
                    articles_fetched=stats.fetched,
                    detail={"sources_polled": report.sources_polled,
                            "sources_ok": report.sources_ok},
                )
                session.add(run_row)
                session.flush()
                run_id = run_row.id
            except Exception as exc:  # noqa: BLE001 - bookkeeping is best-effort
                log.warning("Pipeline run row skipped (best-effort): %s", exc)
                run_row = None
                run_id = None

            existing = _load_existing_guids(session)
            new_articles: list[NewsArticle] = []

            # CHANGE 7: rank the fetched batch BEFORE the LLM cap is applied,
            # so the 15 highest-priority articles get model attention. Without
            # this the cap is hit in arrival order and a rush of low-quality
            # noise can consume every slot, leaving a Tier 1 release to
            # heuristics. Sort is by source_quality then published_at (newest
            # first); every article is still ingested, only the classification
            # mode differs. The guids already stored are passed in so the LLM
            # reservation is not spent on rows the tick skips anyway.
            report.articles = _priority_sort_articles(
                report.articles, known_guids=existing)

            llm_calls = 0
            # THE LLM TIME BUDGET.
            #
            # The per-tick call COUNT cap (LLM_BATCH_LIMIT) is not enough to
            # keep a tick inside its interval, because a slow provider burns
            # the full 60s timeout on every call. The circuit breaker does not
            # cover that case either: it counts CONSECUTIVE failures, so a
            # provider that alternates success and timeout never trips it.
            #
            # MEASURED on the live database (last 80 ticks): the tick
            # duration was p50 121s, p90 589s, max 946s (15.8 minutes), and
            # 27 of 80 ticks ran past the 180s tick interval. The worst tick
            # did 15 LLM calls that each timed out: 15 x 60s.
            #
            # A tick that takes ten minutes is a card that is ten minutes
            # stale, which defeats the point of an intraday engine. So the
            # tick now has a TIME allowance for model work: once it is spent,
            # the remaining articles are classified deterministically and the
            # tick finishes on time.
            llm_started = time.monotonic()
            llm_budget_seconds = float(settings.llm_tick_budget_seconds)
            llm_batch_limit = int(getattr(settings, "llm_batch_limit",
                                          LLM_BATCH_LIMIT))
            budget_logged = False

            # PHASE A - classify the LLM-bound articles CONCURRENTLY.
            #
            # The tick's model allowance is wall-clock, and one call costs
            # 20-40s on the live endpoint, so a sequential loop spends the
            # whole allowance on 2-5 articles even though LLM_MAX_CONCURRENT
            # slots are configured and never used (the classifier's semaphore
            # had exactly one caller at a time - it was a no-op). Overlapping
            # the calls multiplies the verdicts per tick by the concurrency
            # factor without lengthening the tick.
            #
            # The candidates are the first llm_batch_limit NEW articles of the
            # already priority-sorted batch - the same articles, in the same
            # order, that the sequential path would have sent to the model.
            precomputed = await _preclassify_for_llm(
                report.articles, session, existing, llm_batch_limit,
                llm_budget_seconds)

            for art in report.articles:
                # HEARTBEAT: the LLM classification of one article can take a
                # full timeout budget (60s) when the provider is slow, and a
                # batch of them can outlast the whole freshness window.
                # Refreshing the stamp per article keeps /api/health honest
                # about a tick that is alive and working.
                _refresh_tick_stamp()
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
                llm_time_used = time.monotonic() - llm_started
                budget_spent = llm_time_used >= llm_budget_seconds
                verdict = None
                if art.guid in precomputed:
                    # Phase A already handled this one. A None verdict means
                    # the deadline cut the call off - do not spend another one.
                    verdict = precomputed[art.guid]
                    use_llm = verdict is not None
                    if not use_llm:
                        stats.llm_new_skipped_budget += 1
                else:
                    use_llm = (settings.has_llm
                               and llm_calls < llm_batch_limit
                               and not budget_spent)
                if (settings.has_llm and llm_calls < llm_batch_limit
                        and budget_spent and not budget_logged):
                    budget_logged = True
                    stats.llm_new_skipped_budget += 1
                    log.info(
                        "LLM time budget spent (%.0fs of %.0fs) - remaining "
                        "articles classified by heuristic",
                        llm_time_used, llm_budget_seconds)
                try:
                    row = await _ingest_article(art, session, stats, use_llm,
                                                precomputed=verdict)
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
                                _sel(_fn.count()).where(_NA.guid == art.guid))
                            log.error(
                                "GUID-COLLISION DIAG: guid=%s in_existing=%s "
                                "db_count=%s source=%s",
                                art.guid, art.guid in existing, cnt, art.source,
                            )

            _news_since_update += len(new_articles)
            stats.articles_new = len(new_articles)

            # 1b) ONE-SHOT POLICY ----------------------------------------------
            # Nothing to do here: the re-classification loop that used to run
            # at this point (up to 5 previously-heuristic articles per tick) is
            # retired. An article is classified exactly once, in the loop
            # above, and that verdict also decides whether it is stored or
            # deleted - so there is no second pass to make. The same rule is
            # applied by the overnight catch-up pass in services/overnight.py.

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
            # Heartbeat across the recompute too: it re-scores every stored
            # article, which is the other slow phase of a tick.
            _refresh_tick_stamp()
            _recompute_sentiment(session, stats)
            _refresh_tick_stamp()

            if run_row is not None:
                run_row.articles_new = stats.new_today
                run_row.articles_rejected = stats.rejected
                run_row.llm_errors = stats.llm_new_failed + stats.llm_backlog_failed
                run_row.status = "ok"
                run_row.finished_at = T.now_utc()
                # Record the tick's errors on the run row. The detail was
                # written when the run STARTED, so it only carried the
                # source counts - the errors the dashboard reads were
                # always empty, and a tick failing every time showed
                # nothing but a stale clock 15 minutes later.
                run_row.detail = {
                    **(run_row.detail or {}),
                    "errors": list(stats.errors[:20]),
                }

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

        # 5) OUTCOME LOOP --------------------------------------------------
        # Record what the price actually was, then resolve every published
        # call whose horizon has elapsed. This is the loop that lets the
        # engine be measured against reality instead of against opinion.
        #
        # It runs OUTSIDE the main session: the price fetch is a network
        # call, and holding a write transaction open across one is how the
        # "database is locked" errors happen. Both steps are best-effort and
        # can never break a tick.
        try:
            quoted = await _record_price_points()
            if quoted:
                log.debug("outcome loop: %d price point(s) recorded", quoted)
            with session_scope() as session:
                from app.services.outcomes import (
                    resolve_pending,
                    trim_price_points,
                )

                resolve_pending(session)
                trim_price_points(session)
        except Exception as exc:  # noqa: BLE001 - measurement never breaks a tick
            log.debug("outcome loop skipped: %s", exc)

        log.info(
            "Pipeline complete: fetched=%d new=%d rejected=%d dup=%d "
            "llm_new_ok=%d llm_new_fail=%d llm_new_skip_batch=%d llm_new_skip_breaker=%d "
            "dropped_low=%d skipped_dropped=%d purged=%d unroutable=%d",
            stats.fetched, stats.new_today, stats.rejected, stats.duplicates,
            stats.llm_new_ok, stats.llm_new_failed, stats.llm_new_skipped_batch,
            stats.llm_new_skipped_breaker,
            stats.llm_new_dropped_low, stats.skipped_dropped,
            stats.purged.get("articles", 0),
            stats.skipped_unroutable,
        )
        return stats
    except Exception as exc:
        log.exception("Pipeline run failed")
        stats.errors.append(f"pipeline: {type(exc).__name__}: {exc}")
        # The running flag is cleared in the finally block below - a failed
        # tick must release it unconditionally, heartbeat or not, or the
        # next scheduler tick would be blocked forever.
        try:
            with session_scope() as session:
                row = session.get(PipelineRun, run_id) if "run_id" in dir() else None
                if row is not None:
                    row.status = "error"
                    row.finished_at = T.now_utc()
                    # Merge, don't replace: the source counts written at
                    # the start of the run are still useful, and the
                    # dashboard reads both from this blob.
                    row.detail = {
                        **(row.detail or {}),
                        "error": str(exc),
                        "errors": list(stats.errors[:20]),
                    }
        except Exception:  # noqa: BLE001
            pass
        return stats
    finally:
        # Only clear the running flag if this tick still owns it. A tick
        # that was released as wedged must not clear the flag of the tick
        # that replaced it.
        if my_generation == _tick_generation:
            _running = False
            _tick_started_at = None

