"""
OVERNIGHT CATCH-UP ("MORNING BRIEF").

WHY THIS EXISTS
===============
The engine ingests and classifies news only while the laptop is on. When the
machine is switched off overnight, every headline published in that gap is

  (a) fetched on the first tick after boot, or
  (b) was fetched the previous evening but classified with the deterministic
      heuristic engine (``llm_used = False``) because the per-tick LLM batch
      cap (``LLM_BATCH_LIMIT``) or the circuit breaker was in force.

Either way the model never judged those headlines. This module turns the
shutdown gap into ONE explicit catch-up pass:

  1. Resolve the off-window: a shutdown marker written on graceful shutdown
     (FastAPI lifespan shutdown + atexit), falling back to the database
     (last completed pipeline run -> newest stored article -> newest
     snapshot) when the machine was powered off without a clean exit.
  2. Collect every article published (or stored) inside that window.
  3. Send each one through the SAME ``analyze_article()`` LLM path the live
     pipeline uses, banded into HIGH / MEDIUM / LOW impact, and write the
     verdict back to ``news_article`` + ``asset_impact``.
  4. Run one final pipeline pass so every sentiment snapshot is rebuilt from
     the refreshed rows, and write a dated morning brief report.

Runs automatically on boot from ``app.main`` (see ``run_boot_catchup``), is
idempotent per window (a state file records what it already covered), and is
guarded by a file lock so a scheduled task and a running server never spend
the same API budget twice.

  python run.py --morning-brief            # catch-up pass + report
  python run.py --morning-brief --dry-run  # report only, no writes
  python run.py --morning-brief --all      # re-judge even LLM rows
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Self

from sqlalchemy import and_, or_, select

from app.analysis.news_classifier import analyze_article
from app.analysis.recency_engine import (
    compute_final_weight,
    signed_contribution,
)
from app.config import BASE_DIR, settings
from app.database import session_scope
from app.logging_conf import get_logger
from app.market.session_detector import detect_session, session_relevance
from app.models.db_models import (
    AssetImpact,
    NewsArticle,
    PipelineRun,
)
from app.models.enums import EconomicImportance
from app.utils import timeutils as T

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
STATE_DIR = BASE_DIR / "data" / "state"
# Best-effort exit stamp, written when the engine shuts down cleanly.
SHUTDOWN_MARKER = STATE_DIR / "overnight_boundary.json"
# Which window the last completed brief covered (idempotency).
BRIEF_STATE = STATE_DIR / "morning_brief_state.json"
# Cross-process guard so the scheduled task and a running server never both
# pay for the same catch-up.
BRIEF_LOCK = STATE_DIR / "morning_brief.lock"
REPORT_DIR = BASE_DIR / "data" / "reports"

# A lock older than this is treated as abandoned (stale process) and stolen.
_LOCK_STALE_SECONDS = 30 * 60
# Commit cadence while rewriting classified rows.
_COMMIT_EVERY = 25

_IMPORTANCE_RANK = {
    EconomicImportance.LOW: 0,
    EconomicImportance.MEDIUM: 1,
    EconomicImportance.HIGH: 2,
    EconomicImportance.VERY_HIGH: 3,
}

# The user-facing three-band impact the brief reports. VERY_HIGH and HIGH are
# both "HIGH" for a morning read; the four-level value stays on the row.
_BAND = {
    "VERY_HIGH": "HIGH",
    "HIGH": "HIGH",
    "MEDIUM": "MEDIUM",
    "LOW": "LOW",
}
_BANDS = ("HIGH", "MEDIUM", "LOW")


def impact_band(importance: str) -> str:
    """Four-level importance -> the three-band impact shown in the brief."""
    return _BAND.get(str(importance or "").upper(), "LOW")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _aware(dt: datetime | None) -> datetime | None:
    """SQLite strips tzinfo; force naive values to be read as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_iso(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(dt)


def _fmt_hours(hours: float) -> str:
    total_min = round(max(0.0, hours) * 60)
    return f"{total_min // 60}h{total_min % 60:02d}m"


# ---------------------------------------------------------------------------
# Shutdown marker (best effort - a hard power-off leaves the DB fallback)
# ---------------------------------------------------------------------------
def record_shutdown(now: datetime | None = None) -> None:
    """Stamp the engine's exit time so the next boot can size the gap.

    Called from the FastAPI lifespan shutdown hook and from atexit. Never
    raises: shutdown bookkeeping must not block process exit, and a missing
    marker is covered by the database fallback in ``resolve_window``.
    """
    stamp = _aware(now or T.now_utc())
    try:
        _write_json_atomic(SHUTDOWN_MARKER, {
            "recorded_at_utc": stamp.isoformat(),
            "pid": os.getpid(),
        })
        log.debug("Overnight boundary recorded: %s", stamp.isoformat())
    except Exception as exc:  # noqa: BLE001 - never block shutdown
        log.debug("Shutdown marker skipped: %s", exc)


def read_shutdown_marker(now: datetime | None = None) -> datetime | None:
    """
    The last clean-shutdown time, or None when absent/unreadable.

    ``now`` is injectable so a skewed marker can be judged against a pinned
    clock in tests instead of the wall clock.
    """
    data = _read_json(SHUTDOWN_MARKER)
    if not data:
        return None
    stamp = _parse_iso(data.get("recorded_at_utc"))
    if stamp is None:
        return None
    reference = _aware(now or T.now_utc())
    if stamp > reference + timedelta(minutes=5):
        return None  # garbage or clock skew - trust the database instead
    return stamp


atexit.register(record_shutdown)


# ---------------------------------------------------------------------------
# Window resolution
# ---------------------------------------------------------------------------
@dataclass
class OvernightWindow:
    """The off-period the brief must cover, plus how it was derived."""

    start: datetime
    end: datetime
    source: str
    gap_hours: float
    capped: bool = False

    def contains(self, moment: datetime | None) -> bool:
        moment = _aware(moment)
        return moment is not None and self.start <= moment <= self.end

    def to_dict(self) -> dict:
        return {
            "start_utc": self.start.isoformat(),
            "end_utc": self.end.isoformat(),
            "gap_hours": round(self.gap_hours, 3),
            "gap_human": _fmt_hours(self.gap_hours),
            "source": self.source,
            "capped": self.capped,
        }


def _last_db_activity(session) -> tuple[datetime | None, str]:
    """Newest proof the engine was alive, straight from the database.

    A clean shutdown marker is preferred, but a machine that was simply
    powered off still leaves this trail, so the catch-up window can never be
    lost. Order: last completed pipeline run -> newest stored article ->
    newest sentiment snapshot.
    """
    last_run = session.scalar(
        select(PipelineRun.finished_at)
        .where(PipelineRun.finished_at.isnot(None))
        .order_by(PipelineRun.finished_at.desc())
        .limit(1)
    )
    if last_run is not None:
        return _aware(last_run), "last_pipeline_run"

    last_article = session.scalar(
        select(NewsArticle.published_at_utc)
        .where(NewsArticle.published_at_utc.isnot(None))
        .order_by(NewsArticle.published_at_utc.desc())
        .limit(1)
    )
    if last_article is not None:
        return _aware(last_article), "newest_article"

    from app.models.db_models import SentimentSnapshot

    last_snapshot = session.scalar(
        select(SentimentSnapshot.computed_at)
        .order_by(SentimentSnapshot.computed_at.desc())
        .limit(1)
    )
    if last_snapshot is not None:
        return _aware(last_snapshot), "newest_snapshot"
    return None, "empty_database"


def resolve_window(session, now: datetime | None = None) -> OvernightWindow | None:
    """
    The off-window to catch up on, or None when there is no meaningful gap.

    "Meaningful" is ``settings.morning_brief_min_gap_minutes`` (default 90):
    a machine that was merely restarted for a few minutes has no overnight
    backlog worth a catch-up pass, and burning LLM calls on it is waste.
    """
    end = _aware(now or T.now_utc())

    marker = read_shutdown_marker(now=end)
    if marker is not None:
        start, source = marker, "shutdown_marker"
    else:
        start, source = _last_db_activity(session)
        if start is None:
            return None

    gap_hours = (end - start).total_seconds() / 3600.0
    if gap_hours * 60 < settings.morning_brief_min_gap_minutes:
        return None

    capped = False
    cap = float(settings.overnight_max_gap_hours)
    if cap > 0 and gap_hours > cap:
        # Never reach further back than one plausible off-period (and never
        # past the retention window): the cap keeps the pass bounded even if
        # the marker is stale or the DB is old.
        start = end - timedelta(hours=cap)
        gap_hours = cap
        capped = True

    return OvernightWindow(start=start, end=end, source=source,
                           gap_hours=gap_hours, capped=capped)


def _window_from_override(session, now: datetime | None,
                          since: datetime | None,
                          until: datetime | None) -> OvernightWindow | None:
    """Explicit --since/--until window (operator override, no gap test)."""
    end = _aware(now or T.now_utc())
    start = _aware(since)
    if start is None:
        resolved = resolve_window(session, now=end)
        if resolved is None:
            return None
        start = resolved.start
    if start > end:
        return None
    gap = (end - start).total_seconds() / 3600.0
    return OvernightWindow(start=start, end=end, source="manual", gap_hours=gap)


def collect_articles(session, window: OvernightWindow) -> list[NewsArticle]:
    """Every article inside the window, newest first.

    Articles with no usable publication time are placed by their ingest time
    instead of being silently dropped - they were stored in the gap, so the
    brief must still account for them.
    """
    start_naive = _naive(window.start)
    end_naive = _naive(window.end)
    rows = session.scalars(
        select(NewsArticle)
        .where(or_(
            and_(
                NewsArticle.published_at_utc.isnot(None),
                NewsArticle.published_at_utc >= start_naive,
                NewsArticle.published_at_utc <= end_naive,
            ),
            and_(
                NewsArticle.published_at_utc.is_(None),
                NewsArticle.created_at >= start_naive,
                NewsArticle.created_at <= end_naive,
            ),
        ))
        .order_by(NewsArticle.published_at_utc.desc(), NewsArticle.id.desc())
    ).all()
    return list(rows)


def _article_priority_key(article: NewsArticle) -> tuple:
    """
    Tier first, newest second - the same ordering the live pipeline uses to
    spend its LLM budget on the most trustworthy batch members, so the
    morning pass sees the Tier 1 releases even when the batch is large.
    """
    from app.services.pipeline import _SOURCE_QUALITY_RANK

    quality_rank = _SOURCE_QUALITY_RANK.get(
        (getattr(article, "source_quality", "") or "").upper(), 99)
    published = article.published_at_utc
    try:
        ts_rank = -(_naive(published).replace(tzinfo=timezone.utc).timestamp()
                    if published else 0.0)
    except (TypeError, ValueError, OSError):
        ts_rank = 999
    return (quality_rank, ts_rank, (article.title or "").lower())


# ---------------------------------------------------------------------------
# Calendar context (scheduled releases inside the window)
# ---------------------------------------------------------------------------
def calendar_in_window(window: OvernightWindow) -> list[dict]:
    """
    Scheduled macro releases that landed in the window (plus what is due
    within the next few hours), from the persisted calendar cache.

    Read-only and best-effort: the cache is the last good payload, so it also
    answers after an offline night. A missing cache simply omits the section.
    """
    try:
        from app.ingestion.calendar_cache import load_cache

        events = load_cache()
    except Exception as exc:  # noqa: BLE001 - context only, never fatal
        log.debug("Calendar cache unavailable for morning brief: %s", exc)
        return []

    out: list[dict] = []
    for ev in events:
        when = _aware(getattr(ev, "when_utc", None))
        if when is None:
            continue
        # Releases in the window plus the ones still ahead this morning.
        if not (window.start <= when <= window.end + timedelta(hours=12)):
            continue
        out.append({
            "title": (ev.title or "").strip(),
            "country": (ev.country or "").strip().upper(),
            "when_utc": when.isoformat(),
            "display_time": T.local_clock(when),
            "impact": (ev.impact or "Medium"),
            "released": getattr(ev, "actual", None) is not None,
            "forecast": getattr(ev, "forecast", None),
            "previous": getattr(ev, "previous", None),
            "actual": getattr(ev, "actual", None),
        })
    out.sort(key=lambda r: r["when_utc"])
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@dataclass
class ClassificationOutcome:
    """What the LLM pass did to the stored rows."""

    attempted: int = 0
    llm_judged: int = 0
    llm_failed: int = 0
    kept_stored: int = 0       # already LLM-judged previously (or skipped)
    breaker_stopped: bool = False
    errors: list[str] = field(default_factory=list)
    rewrites: int = 0
    # False when the pass could not or should not contact the provider (no key
    # configured, or --dry-run previewing the window). The report then bands
    # stored verdicts and says so, instead of claiming model output.
    llm_called: bool = True

    def to_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "llm_judged": self.llm_judged,
            "llm_failed": self.llm_failed,
            "kept_stored": self.kept_stored,
            "breaker_stopped": self.breaker_stopped,
            "rewrites": self.rewrites,
            "llm_called": self.llm_called,
            "errors": self.errors[:10],
        }


def _apply_analysis(article: NewsArticle, analysis) -> None:
    """Write a fresh LLM verdict onto a stored article row.

    Mirrors ``pipeline._ingest_article`` field-for-field so a morning-judged
    row is indistinguishable from one judged live at ingest time.
    """
    article.category = analysis.category.value
    article.importance = analysis.importance.value
    article.report_kind = analysis.report_kind.value
    article.verification = analysis.verification.value
    article.relevance_score = analysis.relevance_score
    article.analysis_mode = analysis.mode
    article.analysis_note = analysis.note
    article.llm_used = True


def _rewrite_impacts(session, article: NewsArticle, analysis) -> None:
    """Replace the article's asset_impact rows with the fresh verdict.

    Deletes first, then recreates (asset_impact carries
    UNIQUE(article_id, asset)), recomputing the recency/importance weights
    exactly like the backfill tool does - the same maths the engine uses at
    ingest time, so the rebuilt snapshots are consistent.
    """
    from app.services.pipeline import _importance_weight

    old = session.scalars(
        select(AssetImpact).where(AssetImpact.article_id == article.id)
    ).all()
    event_id = old[0].event_id if old else None
    for row in old:
        session.delete(row)
    session.flush()

    cur_session = detect_session()
    for aa in analysis.assets:
        weights = compute_final_weight(
            published_at=article.published_at_utc,
            importance=analysis.importance,
            strength=aa.strength,
            asset_relevance=aa.relevance,
            persistence=aa.persistence,
            source_quality=article.source_quality,
            session_multiplier=session_relevance(
                cur_session, analysis.category.value),
        )
        session.add(AssetImpact(
            article_id=article.id,
            event_id=event_id,
            asset=aa.asset.value,
            direction=aa.direction.value,
            strength=aa.strength.value,
            relevance=aa.relevance,
            persistence=aa.persistence,
            reason=aa.reason,
            transmission=aa.transmission,
            metal_character=aa.metal_character,
            speaker_tier=aa.speaker_tier,
            recency_weight=weights["recency"],
            importance_weight=_importance_weight(analysis.importance),
            final_weight=weights["final_weight"],
            signed_contribution=signed_contribution(
                direction=aa.direction, strength=aa.strength,
                final_weight=weights["final_weight"]),
            session=cur_session.value,
        ))

    # A stronger verdict on an existing event upgrades the cluster row, the
    # same rank rule the pipeline applies when articles pile onto an event.
    event = next((link.event for link in article.event_links), None)
    if event is not None:
        old_rank = _IMPORTANCE_RANK.get(
            EconomicImportance(event.importance), 0)
        new_rank = _IMPORTANCE_RANK.get(analysis.importance, 0)
        if new_rank > old_rank:
            event.importance = analysis.importance.value


async def classify_articles(session, articles: list[NewsArticle], *,
                            dry_run: bool = False,
                            rejudge_all: bool = False,
                            now: datetime | None = None) -> ClassificationOutcome:
    """
    Send the window's articles through the LLM, newest/tier-first.

    Default scope is every row the model has NOT already judged
    (``llm_used = False``): those are the headlines the live pipeline had to
    fall back to heuristics for (batch cap, circuit breaker, missing key at
    the time). ``rejudge_all`` spends calls on rows the model already saw.

    The circuit breaker is checked between articles: once it opens the pass
    stops paying for failures and bands the rest from the stored verdict.
    """
    from app.analysis import llm_client

    outcome = ClassificationOutcome()
    if not settings.has_llm or dry_run:
        # Heuristic mode, or a --dry-run preview of the window: no provider
        # request is spent. The report still bands what is stored, so the
        # morning read is useful either way.
        outcome.kept_stored = len(articles)
        outcome.llm_called = False
        return outcome

    ordered = sorted(articles, key=_article_priority_key)
    since_commit = 0
    for index, article in enumerate(ordered, start=1):
        if not rejudge_all and article.llm_used:
            outcome.kept_stored += 1
            continue
        if llm_client.circuit_breaker().is_open:
            outcome.breaker_stopped = True
            log.warning("Morning brief: LLM circuit breaker open after %d "
                        "article(s) - remaining rows keep stored verdicts",
                        outcome.attempted)
            outcome.kept_stored += len(ordered) - index + 1
            break

        outcome.attempted += 1
        try:
            analysis, llm_ok, skip_reason = await analyze_article(
                article.title or "", article.body or "")
        except Exception as exc:  # noqa: BLE001 - one row never kills the pass
            outcome.llm_failed += 1
            outcome.errors.append(f"{article.id}: {type(exc).__name__}: {exc}")
            continue

        if llm_ok:
            outcome.llm_judged += 1
            if not dry_run:
                _apply_analysis(article, analysis)
                _rewrite_impacts(session, article, analysis)
                outcome.rewrites += 1
                since_commit += 1
        else:
            outcome.llm_failed += 1
            log.debug("Morning brief: LLM unavailable (%s) for %r - stored "
                      "verdict kept", skip_reason, (article.title or "")[:80])

        if not dry_run and since_commit >= _COMMIT_EVERY:
            session.commit()
            since_commit = 0
        if index % 10 == 0:
            log.info("Morning brief progress: %d/%d articles (judged=%d "
                     "failed=%d)", index, len(ordered), outcome.llm_judged,
                     outcome.llm_failed)

    if not dry_run and outcome.rewrites:
        session.commit()
    return outcome


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _item_from_row(article: NewsArticle) -> dict:
    impacts = [
        {
            "asset": imp.asset,
            "direction": imp.direction,
            "strength": imp.strength,
            "relevance": imp.relevance,
            "final_weight": imp.final_weight,
        }
        for imp in article.impacts
    ]
    published = _aware(article.published_at_utc)
    return {
        "id": article.id,
        "title": (article.title or "").strip(),
        "source": article.source,
        "source_quality": article.source_quality,
        "published_at_utc": published.isoformat() if published else None,
        "display_time": T.local_clock(published),
        "category": article.category,
        "importance": article.importance,
        "impact": impact_band(article.importance),
        "llm_used": bool(article.llm_used),
        "analysis_mode": article.analysis_mode,
        "verification": article.verification,
        "assets": impacts,
    }


def build_report(window: OvernightWindow, articles: list[NewsArticle],
                 outcome: ClassificationOutcome, *,
                 dry_run: bool, rejudge_all: bool,
                 calendar: list[dict], now: datetime) -> dict:
    """Band the window's flow into HIGH / MEDIUM / LOW, per asset and overall."""
    items = [_item_from_row(article) for article in articles]

    totals = {band: 0 for band in _BANDS}
    per_asset: dict[str, dict[str, Any]] = {}
    for item in items:
        totals[item["impact"]] = totals.get(item["impact"], 0) + 1
        for imp in item["assets"]:
            bucket = per_asset.setdefault(imp["asset"], {
                "high": 0, "medium": 0, "low": 0,
                "bullish": 0, "bearish": 0, "neutral": 0,
                "articles": 0,
            })
            bucket["articles"] += 1
            bucket[{"HIGH": "high", "MEDIUM": "medium"}.get(
                item["impact"], "low")] += 1
            if imp["direction"] == "BULLISH":
                bucket["bullish"] += 1
            elif imp["direction"] == "BEARISH":
                bucket["bearish"] += 1
            else:
                bucket["neutral"] += 1

    band_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda i: (band_order.get(i["impact"], 3),
                              i["published_at_utc"] or ""))

    return {
        "kind": "morning_brief",
        "generated_at_utc": now.isoformat(),
        "window": window.to_dict(),
        "dry_run": dry_run,
        "rejudge_all": rejudge_all,
        "llm": {
            "enabled": settings.has_llm,
            "model": settings.llm_model if settings.has_llm else None,
            **outcome.to_dict(),
        },
        "totals": {
            "articles": len(items),
            **totals,
        },
        "per_asset": per_asset,
        "calendar": calendar,
        "items": items,
        "disclaimer": (
            "News-based impact classification for the overnight window. "
            "HIGH / MEDIUM / LOW is an importance band for a morning read, "
            "not a price prediction or trading signal."
        ),
    }


def render_text(report: dict) -> str:
    """Compact console rendering of the morning brief."""
    if report.get("status") not in ("completed", "dry_run"):
        return (f"MORNING BRIEF: {report.get('status', 'unknown')} - "
                f"{report.get('reason', '')}".rstrip(" -"))

    window = report["window"]
    llm = report["llm"]
    totals = report["totals"]
    out: list[str] = []
    out.append("=" * 74)
    out.append("MORNING BRIEF - overnight news impact (LLM-judged)"
               + ("  [DRY RUN - nothing written]" if report.get("dry_run")
                  else ""))
    out.append("=" * 74)
    out.append(f"Window : {window['start_utc'][:16].replace('T', ' ')} UTC -> "
               f"{window['end_utc'][:16].replace('T', ' ')} UTC "
               f"({window['gap_human']}, source={window['source']}"
               f"{', capped' if window['capped'] else ''})")
    if llm["enabled"] and llm.get("llm_called", True):
        out.append(f"LLM    : {llm['model']} - judged {llm['llm_judged']} of "
                   f"{totals['articles']} headline(s), kept stored "
                   f"{llm['kept_stored']}, failed {llm['llm_failed']}")
        if llm.get("breaker_stopped"):
            out.append("         circuit breaker opened mid-pass - the rest "
                       "kept their stored verdicts")
    else:
        out.append("LLM    : not contacted (dry run, or not configured) - "
                   "bands come from stored verdicts")
    out.append(f"Totals : {totals['articles']} headline(s) -> "
               f"HIGH {totals['HIGH']} | MEDIUM {totals['MEDIUM']} | "
               f"LOW {totals['LOW']}")

    for asset, bucket in sorted(report["per_asset"].items()):
        out.append(f"  {asset:<7} H{bucket['high']} M{bucket['medium']} "
                   f"L{bucket['low']}  bull {bucket['bullish']} / "
                   f"bear {bucket['bearish']} / neutral {bucket['neutral']}")

    for band in _BANDS:
        rows = [i for i in report["items"] if i["impact"] == band]
        if not rows:
            continue
        out.append("")
        out.append("-" * 74)
        out.append(f"{band} IMPACT ({len(rows)})")
        out.append("-" * 74)
        for item in rows[:12]:
            assets = ", ".join(
                f"{a['asset']} {a['direction']}({a['strength']})"
                for a in item["assets"]) or "no routed asset"
            out.append(f"[{item['display_time']}] {item['title'][:88]}")
            out.append(f"    {item['source']} | {item['category']} | "
                       f"importance={item['importance']} | {assets}")
        if len(rows) > 12:
            out.append(f"    ... {len(rows) - 12} more (see the JSON report)")

    if report.get("calendar"):
        out.append("")
        out.append("-" * 74)
        out.append("SCHEDULED RELEASES IN THE WINDOW")
        out.append("-" * 74)
        for ev in report["calendar"][:15]:
            state = "released" if ev["released"] else "scheduled"
            out.append(f"[{ev['display_time']}] {ev['title'][:60]} "
                       f"({ev['country']}, {ev['impact']}) - {state}"
                       + (f", actual={ev['actual']}"
                          if ev["released"] and ev.get("actual") is not None
                          else ""))

    if report.get("final_pass"):
        fp = report["final_pass"]
        out.append("")
        out.append("-" * 74)
        out.append(f"Final pass: fetched={fp.get('fetched')} "
                   f"new={fp.get('new_today')} errors={fp.get('errors', 0)}")
    out.append("")
    out.append(report.get("disclaimer", ""))
    return "\n".join(out)


def report_path_for(now: datetime) -> Path:
    """Dated on the DISPLAY date: that is the date on the reader's calendar
    when they open the morning brief."""
    local = _aware(now).astimezone(T.display_tz())
    return REPORT_DIR / f"morning_brief_{local.date().isoformat()}.json"


def write_report(report: dict, path: Path | None = None) -> Path:
    target = path or report_path_for(_parse_iso(report["generated_at_utc"])
                                     or T.now_utc())
    _write_json_atomic(target, report)
    report["report_path"] = str(target)
    return target


def summarise(report: dict) -> str:
    """One-line log rendering."""
    if report.get("status") not in ("completed", "dry_run"):
        return f"skipped ({report.get('reason', 'unknown')})"
    totals = report["totals"]
    llm = report["llm"]
    judged = (llm.get("llm_judged", 0)
              if (llm.get("enabled") and llm.get("llm_called", True)) else 0)
    prefix = "dry run - " if report.get("status") == "dry_run" else ""
    return (prefix +
            f"{report['window']['gap_human']} gap via "
            f"{report['window']['source']}: {totals['articles']} headlines, "
            f"HIGH {totals['HIGH']} / MEDIUM {totals['MEDIUM']} / "
            f"LOW {totals['LOW']} ({judged} LLM-judged)")


# ---------------------------------------------------------------------------
# State + cross-process lock
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    return _read_json(BRIEF_STATE) or {}


def _save_state(report: dict) -> None:
    _write_json_atomic(BRIEF_STATE, {
        "last_run_utc": report["generated_at_utc"],
        "last_window_start_utc": report["window"]["start_utc"],
        "last_window_end_utc": report["window"]["end_utc"],
        "articles": report["totals"]["articles"],
        "llm_judged": report["llm"].get("llm_judged", 0),
    })


def _already_covered(window: OvernightWindow) -> bool:
    state = _load_state()
    return state.get("last_window_end_utc") == window.end.isoformat()


class _Lock:
    """Exclusive file lock so two processes never run the same pass."""

    def __init__(self, path: Path | None = None) -> None:
        # Resolved at call time, not at import: tests retarget the module
        # constant and the lock must follow it, never the repo's data dir.
        self.path = Path(path) if path is not None else BRIEF_LOCK
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return True  # best effort - a missing dir must not block the brief
        for attempt in range(2):
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return True
            except FileExistsError:
                if attempt == 0 and self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        return False
                    continue
                return False
            except OSError:
                return True
        return False

    def _stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        return age > _LOCK_STALE_SECONDS

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def run_morning_brief(*, force: bool = False, dry_run: bool = False,
                            rejudge_all: bool = False,
                            now: datetime | None = None,
                            since: datetime | None = None,
                            until: datetime | None = None,
                            session=None) -> dict:
    """
    One catch-up pass. Returns the report dict (status: completed / skipped /
    dry_run / locked / empty).
    """
    stamp = _aware(now or T.now_utc())

    def _resolve(s):
        if since is not None or until is not None:
            return _window_from_override(s, stamp, since, until)
        return resolve_window(s, now=stamp)

    if session is not None:
        return await _run_pass(session, stamp, _resolve, force=force,
                               dry_run=dry_run, rejudge_all=rejudge_all)

    with session_scope() as s:
        return await _run_pass(s, stamp, _resolve, force=force,
                               dry_run=dry_run, rejudge_all=rejudge_all)


async def _run_pass(session, stamp: datetime, resolver, *, force: bool,
                    dry_run: bool, rejudge_all: bool) -> dict:
    window = resolver(session)
    if window is None:
        return {
            "kind": "morning_brief", "status": "skipped",
            "reason": "no_overnight_gap",
            "generated_at_utc": stamp.isoformat(),
            "min_gap_minutes": settings.morning_brief_min_gap_minutes,
        }

    if not force and _already_covered(window):
        return {
            "kind": "morning_brief", "status": "skipped",
            "reason": "already_covered",
            "window": window.to_dict(),
            "generated_at_utc": stamp.isoformat(),
        }

    articles = collect_articles(session, window)
    outcome = await classify_articles(
        session, articles, dry_run=dry_run, rejudge_all=rejudge_all, now=stamp)

    report = build_report(
        window, articles, outcome, dry_run=dry_run,
        rejudge_all=rejudge_all, calendar=calendar_in_window(window), now=stamp)
    report["status"] = "dry_run" if dry_run else "completed"
    if not dry_run:
        _save_state(report)
    write_report(report)
    log.info("Morning brief %s: %s", report["status"], summarise(report))
    return report


async def _await_pipeline_idle(timeout_seconds: int | None = None) -> bool:
    """Wait for any in-flight tick so the final rebuild pass is not skipped."""
    from app.services import pipeline

    timeout = (timeout_seconds if timeout_seconds is not None
               else settings.morning_brief_wait_idle_seconds)
    waited = 0.0
    while pipeline.runtime_state().get("running") and waited < timeout:
        await asyncio.sleep(2)
        waited += 2
    return not pipeline.runtime_state().get("running")


async def run_boot_catchup(*, force: bool = False, dry_run: bool = False,
                           rejudge_all: bool = False,
                           now: datetime | None = None,
                           startup_grace_seconds: int | None = None) -> dict:
    """
    Boot-time entry point: classify the overnight window, then run one final
    pipeline pass so sentiment snapshots are rebuilt from the refreshed rows.

    Safe to call from the web process (main.lifespan) and from a scheduled
    task: a cross-process lock keeps the two from duplicating the work, and a
    skipped/locked run is reported, never raised.
    """
    with _Lock() as guard:
        if not guard.acquire():
            return {
                "kind": "morning_brief", "status": "locked",
                "reason": "another morning brief is already running",
                "generated_at_utc": T.now_utc().isoformat(),
            }
        grace = (settings.morning_brief_startup_grace_seconds
                 if startup_grace_seconds is None else startup_grace_seconds)
        if grace > 0:
            # A web process booting at this same moment primes its first
            # ingest tick right now. Give that tick a head start so the
            # catch-up does not interleave with the boot ingest.
            await asyncio.sleep(grace)
        report = await run_morning_brief(force=force, dry_run=dry_run,
                                         rejudge_all=rejudge_all, now=now)
        if report.get("status") != "completed":
            return report

        # One more full pass: ingest anything published since the window was
        # resolved, then rebuild every snapshot from the refreshed verdicts.
        from app.services import pipeline

        idle = await _await_pipeline_idle()
        stats = await pipeline.run_once()
        report["final_pass"] = {
            "waited_for_idle": idle,
            "fetched": stats.fetched,
            "new_today": stats.new_today,
            "errors": len(stats.errors),
            "llm_new_ok": stats.llm_new_ok,
            "llm_backlog_ok": stats.llm_backlog_ok,
        }
        if stats.errors:
            report["final_pass"]["error_list"] = stats.errors[:5]
        write_report(report)
        log.info("Morning brief final pass: %s", report["final_pass"])
        return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_cli_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d"):
        try:
            naive = datetime.strptime(raw, fmt)  # noqa: DTZ007 - localised below
            break
        except ValueError:
            continue
    else:
        raise SystemExit(f"Unparseable datetime: {raw!r} "
                         "(use 'YYYY-MM-DD HH:MM')")
    # Operator input is display-local wall clock; store and compare in UTC.
    return naive.replace(tzinfo=T.display_tz()).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Overnight catch-up: LLM-judge every headline published "
                    "while the laptop was off and write the morning brief.")
    parser.add_argument("--force", action="store_true",
                        help="run even if this window was already covered")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; no database writes")
    parser.add_argument("--all", dest="rejudge_all", action="store_true",
                        help="re-judge every article in the window, "
                             "including rows the LLM already classified")
    parser.add_argument("--since", default=None,
                        help="window start override (display timezone), "
                             "e.g. '2026-10-11 22:30'")
    parser.add_argument("--until", default=None,
                        help="window end override (display timezone)")
    parser.add_argument("--gap-hours", type=float, default=None,
                        help="shortcut for --since: window = now - N hours")
    args = parser.parse_args(argv)

    from app.database import init_db
    from app.logging_conf import setup_logging

    setup_logging()
    init_db()

    since = _parse_cli_datetime(args.since)
    if args.gap_hours and since is None:
        since = T.now_utc() - timedelta(hours=args.gap_hours)
    until = _parse_cli_datetime(args.until)

    report = asyncio.run(run_morning_brief(
        force=args.force, dry_run=args.dry_run,
        rejudge_all=args.rejudge_all, since=since, until=until))

    print()
    print(render_text(report))
    if report.get("report_path"):
        print(f"report: {report['report_path']}")
    return 0 if report.get("status") in ("completed", "dry_run") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
