"""
BACKFILL: re-classify already-stored articles with the corrected classifier.

Needed after any change to the classification rules. The normal pipeline pass
does NOT refresh existing rows (ingestion dedups on guid and skips stored
articles), so a classifier fix only affects NEW articles unless the stored rows
are explicitly rewritten.

Per article: re-runs the real analyze_article() path, updates news_article,
DELETEs and recreates asset_impact (this removes stale BULLISH rows), and
recomputes derived sentiment_snapshot rows.

Safety: takes a SQLite backup before writing; --dry-run changes nothing.

  python tools/backfill_reclassify.py --dry-run
  python tools/backfill_reclassify.py
  python tools/backfill_reclassify.py --heuristic
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.analysis.news_classifier import (  # noqa: E402
    analyze_article, heuristic_analyze,
)
from app.analysis.recency_engine import (  # noqa: E402
    compute_final_weight, signed_contribution,
)
from app.database import SessionLocal, init_db  # noqa: E402
from app.logging_conf import get_logger, setup_logging  # noqa: E402
from app.market.session_detector import (  # noqa: E402
    detect_session, session_relevance,
)
from app.models.db_models import AssetImpact, NewsArticle  # noqa: E402
from app.services.pipeline import _importance_weight  # noqa: E402

log = get_logger("backfill")
DB_PATH = BASE_DIR / "data" / "intraday_sentiment.db"


def backup() -> Path:
    """Consistent copy of the DB (via the SQLite backup API, WAL included)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = DB_PATH.with_name(f"{DB_PATH.stem}.backup_{stamp}.db")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    log.info("Backup written: %s", dest)
    return dest


async def reclassify(session, *, use_llm: bool, dry_run: bool,
                     today_only: bool = True,
                     only_mode: Optional[str] = None,
                     only_directional: bool = False) -> dict:
    """
    only_mode: restrict to rows whose stored analysis_mode equals this value.

    This matters. The substring defect lives ONLY in the deterministic
    heuristic engine - the LLM path derives assets from the model, not from
    keyword tables. So on a mixed day the rows worth rewriting are the ones
    classified while the LLM was failing (analysis_mode='heuristic'), not the
    whole corpus. Re-classifying LLM rows would burn API calls and could
    overwrite better model output with worse heuristic output.

    only_directional: further restrict to heuristic rows that currently hold a
    BULLISH/BEARISH asset_impact. Those are the only rows that can actually
    distort a verdict - a neutral or impact-free heuristic row contributes
    nothing to the bull/bear split, so re-spending an LLM call on it is waste.
    """
    q = session.query(NewsArticle)
    if today_only:
        q = q.filter(NewsArticle.is_today.is_(True))
    if only_mode:
        q = q.filter(NewsArticle.analysis_mode == only_mode)
    if only_directional:
        directional = (
            session.query(AssetImpact.article_id)
            .filter(AssetImpact.direction.in_(["BULLISH", "BEARISH"]))
            .distinct()
        )
        q = q.filter(NewsArticle.id.in_(directional))
    arts = q.all()
    log.info("Re-classifying %d article(s) llm=%s dry_run=%s only_mode=%s "
             "only_directional=%s", len(arts), use_llm, dry_run, only_mode,
             only_directional)

    stats = {"articles": len(arts), "changed": 0, "impacts_deleted": 0,
             "impacts_created": 0, "llm_ok": 0, "llm_failed": 0,
             "lost_assets": 0, "gained_assets": 0}
    examples = []

    for art in arts:
        if use_llm:
            analysis, ok = await analyze_article(art.title or "", art.body or "")
        else:
            analysis, ok = heuristic_analyze(art.title or "", art.body or ""), False
        stats["llm_ok" if ok else "llm_failed"] += 1

        old_cat, old_imp = art.category, art.importance
        new_cat, new_imp = analysis.category.value, analysis.importance.value

        old_impacts = session.query(AssetImpact).filter(
            AssetImpact.article_id == art.id).all()
        old_n, new_n = len(old_impacts), len(analysis.assets)

        # Capture event_id BEFORE deleting: the column is a live FK, and
        # asset_impact also carries UNIQUE(article_id, asset), so the deletes
        # must be flushed before the replacements can be inserted.
        event_id = old_impacts[0].event_id if old_impacts else None

        if old_cat != new_cat or old_imp != new_imp or old_n != new_n:
            stats["changed"] += 1
            if old_n > new_n:
                stats["lost_assets"] += old_n - new_n
            elif new_n > old_n:
                stats["gained_assets"] += new_n - old_n
            if len(examples) < 25:
                examples.append((art.id, (art.title or "")[:54], old_cat, new_cat,
                                 old_imp, new_imp, old_n, new_n))

        if dry_run:
            continue

        art.category = new_cat
        art.importance = new_imp
        art.relevance_score = analysis.relevance_score
        art.analysis_mode = analysis.mode
        art.analysis_note = analysis.note

        for im in old_impacts:
            session.delete(im)
        stats["impacts_deleted"] += old_n
        # Release the UNIQUE(article_id, asset) rows before re-inserting.
        session.flush()

        cur_session = detect_session()
        for aa in analysis.assets:
            f = compute_final_weight(
                published_at=art.published_at_utc,
                importance=analysis.importance,
                strength=aa.strength,
                asset_relevance=aa.relevance,
                persistence=aa.persistence,
                source_quality=art.source_quality,
                session_multiplier=session_relevance(cur_session, new_cat),
            )
            signed = signed_contribution(
                direction=aa.direction, strength=aa.strength,
                final_weight=f["final_weight"])
            session.add(AssetImpact(
                article_id=art.id, event_id=event_id,
                asset=aa.asset.value,
                direction=aa.direction.value,
                strength=aa.strength.value,
                relevance=aa.relevance,
                persistence=aa.persistence,
                reason=aa.reason,
                transmission=aa.transmission,
                metal_character=aa.metal_character,
                recency_weight=f["recency"],
                importance_weight=_importance_weight(analysis.importance),
                final_weight=f["final_weight"],
                signed_contribution=signed,
                session=cur_session.value,
            ))
            stats["impacts_created"] += 1

    if not dry_run:
        session.commit()
        # Snapshots are a derived view, rebuilt from scratch on every pass.
        session.execute(text("DELETE FROM sentiment_snapshot"))
        session.commit()

    log.info("changed=%(changed)d assets -%(lost_assets)d/+%(gained_assets)d "
             "impacts -%(impacts_deleted)d/+%(impacts_created)d", stats)
    if examples:
        print("\n id    headline                                            "
              "category           -> category           importance        assets")
        for e in examples:
            print(f" {e[0]:<5} {e[1]:<54} {e[2]:<18} -> {e[3]:<18} "
                  f"{e[4]:<9} -> {e[5]:<9} {e[6]}->{e[7]}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report changes without writing")
    ap.add_argument("--heuristic", action="store_true",
                    help="skip the LLM entirely (fast, no API calls)")
    ap.add_argument("--all-days", action="store_true",
                    help="re-classify every article, not just is_today")
    ap.add_argument("--only-mode", choices=["heuristic", "llm"], default=None,
                    help="only rewrite rows stored with this analysis_mode "
                         "(default: all)")
    ap.add_argument("--only-directional", action="store_true",
                    help="with --only-mode heuristic: only rows currently "
                         "holding a BULLISH/BEARISH impact (the only ones "
                         "that can distort a verdict)")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    setup_logging()
    if not args.dry_run and not args.no_backup:
        b = backup()
        print(f"backup: {b}")

    init_db()
    session = SessionLocal()
    try:
        stats = asyncio.run(reclassify(session, use_llm=not args.heuristic,
                                       dry_run=args.dry_run,
                                       today_only=not args.all_days,
                                       only_mode=args.only_mode,
                                       only_directional=args.only_directional))
    finally:
        session.close()

    print(f"\n=== {'DRY RUN - nothing written' if args.dry_run else 'COMMITTED'} ===")
    for k, v in stats.items():
        print(f"  {k:<18} {v}")
    print("\nRun a normal pipeline pass (python run.py --once) to rebuild "
          "sentiment snapshots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())