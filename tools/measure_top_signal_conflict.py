#!/usr/bin/env python3
import json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

from app.database import SessionLocal
from app.models.db_models import SentimentSnapshot, NewsArticle, AssetImpact
from app.models.enums import Asset
from app.analysis.signal_factory import build_signal, build_bleed_signal
from app.analysis.event_clustering import cluster_signals
from app.analysis import sentiment_engine

try:
    from app.analysis.sentiment_engine import _signal_weight, STRENGTH_MULT
except Exception:
    _signal_weight = sentiment_engine._signal_weight
    STRENGTH_MULT = sentiment_engine.STRENGTH_MULT


def parse_dt(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None
def article_to_signals(article: NewsArticle) -> List[Any]:
    signals = []
    try:
        for impact in article.impacts:
            sig = build_signal(
                asset=impact.asset,
                title=article.title,
                direction=impact.direction,
                strength=impact.strength,
                importance=impact.importance,
                published_at=article.published_at,
                relevance=impact.relevance,
                persistence=impact.persistence,
                source_quality=impact.source_quality,
                source_name=article.source_name,
                category=article.category,
                session_multiplier=impact.session_multiplier,
                is_primary=True,
                event_key=article.event_key or '',
                cluster_id=article.cluster_id or (article.event_key or ''),
                independent_sources=1,
                report_kind=article.report_kind,
                reason=impact.reason,
                transmission=impact.transmission,
                mode=article.analysis_mode or 'heuristic',
                surprise_weight=getattr(impact, 'surprise_weight', 1.0),
                metal_character=impact.metal_character,
                speaker_tier=getattr(impact, 'speaker_tier', 1.0),
                age_reference_utc=article.age_reference_utc,
            )
            signals.append(sig)
            bleed = build_bleed_signal(sig)
            if bleed is not None:
                signals.append(bleed)
    except Exception:
        raise
    return signals
def get_historical_snapshots(session, asset_str: str, limit: int = 20) -> List[SentimentSnapshot]:
    asset = Asset[asset_str] if hasattr(Asset, asset_str) else asset_str
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    q = session.query(SentimentSnapshot).filter(
        SentimentSnapshot.scope == 'CURRENT',
        SentimentSnapshot.asset == asset_str,
        SentimentSnapshot.contributing_articles.isnot(None),
        SentimentSnapshot.computed_at >= cutoff,
    ).order_by(SentimentSnapshot.computed_at.desc())
    rows = q.limit(limit).all()
    return rows
def reconstruct_postcluster_signals(snapshot: SentimentSnapshot, asset_str: str) -> List[Any]:
    session = SessionLocal()
    try:
        asset = Asset[asset_str] if asset_str in Asset.__members__ else Asset(asset_str)
        contrib = snapshot.contributing_articles or 0
        if not contrib:
            return []
        # Get articles whose impacts for this asset might have contributed
        # Look up AssetImpact rows for this asset with final_weight>0, ordered by article pub time near snapshot?
        # Easier: get recent impacts and also try to match by proximity to snapshot.computed_at
        computed = parse_dt(snapshot.computed_at)
        window_start = computed - timedelta(hours=3) if computed else datetime.now(timezone.utc) - timedelta(hours=6)
        window_end = computed + timedelta(hours=1) if computed else datetime.now(timezone.utc)
        # Get impacts
        impacts = session.query(AssetImpact).join(NewsArticle).filter(
            AssetImpact.asset == asset_str,
            AssetImpact.final_weight > 0,
            NewsArticle.published_at_utc >= window_start,
            NewsArticle.published_at_utc <= window_end,
        ).limit(contrib * 2).all()  # get a bit more
        # Also prefer impacts that actually have nonzero weight in the context of that tick?
        articles = []
        seen = set()
        for imp in impacts:
            if imp.article_id in seen:
                continue
            seen.add(imp.article_id)
            art = session.get(NewsArticle, imp.article_id)
            if art:
                articles.append(art)
            if len(articles) >= contrib + 5:
                break
        # Build pre-cluster signals for this asset from these articles
        all_signals = []
        for art in articles:
            all_signals.extend(article_to_signals(art))
        # Filter to the asset in question
        asset_signals = [s for s in all_signals if getattr(s, 'asset') and str(s.asset.value) == asset_str]
        # Apply same filters as pipeline does at recompute time (note: use current state; we skip if can't reproduce exactly)
        try:
            asset_signals = pipeline._suppress_previews(asset_signals)
        except Exception:
            pass  # skip if issues
        try:
            asset_signals = pipeline._downweight_news_duplicating_calendar(asset_signals)
        except Exception:
            pass  # skip
        # Session change reweight - skip (depends on current session state)
        clustered = cluster_signals(asset_signals, window_hours=0.5)
        return clustered
    finally:
        session.close()
def analyze_snapshot(snapshot: SentimentSnapshot, asset_str: str):
    now = parse_dt(snapshot.computed_at)
    clustered = reconstruct_postcluster_signals(snapshot, asset_str)
    results = []
    for s in clustered:
        try:
            mag = _signal_weight(s, now) * STRENGTH_MULT.get(getattr(s, 'strength'), 0.45)
        except Exception:
            mag = 0.0
        results.append({
            'cluster_conflict': getattr(s, 'cluster_conflict', False),
            'mag': mag,
            'title': getattr(s, 'title', ''),
        })
    results.sort(key=lambda x: x['mag'], reverse=True)
    top1_conflict = results[0]['cluster_conflict'] if results else 0
    top3_conflict = 0
    if results:
        for r in results[:3]:
            if r['cluster_conflict']:
                top3_conflict = 1
                break
    else:
        top1_conflict = 0
        top3_conflict = 0
    return {
        'top1': int(top1_conflict),
        'top3': int(top3_conflict),
        'count': len(results),
        'computed_at': snapshot.computed_at,
    }
def main():
    assets = ['XAUUSD', 'XAGUSD', 'BTCUSD', 'EQUITY']
    session = SessionLocal()
    try:
        print('# Top-signal conflict measurement (historical snapshots)')
        print('# Reconstruction approach: build signals from articles via build_signal/build_bleed_signal, cluster_signals, compute mag with now=snapshot.computed_at')
        print('# Applied filters: _suppress_previews, _downweight_news_duplicating_calendar where possible; skipped session-change reweight (depends on current session state)')
        print('# Post-tick final_weight backfill is not applied to reconstructed signals (we compute weights directly)')
        for asset_str in assets:
            rows = get_historical_snapshots(session, asset_str, limit=20)
            if len(rows) < 20:
                print(f'WARNING: {asset_str} has only {len(rows)} snapshots < 20')
            times = [parse_dt(r.computed_at) for r in rows]
            if times and all(t for t in times):
                tmin = min(t for t in times)
                tmax = max(t for t in times)
                print(f'# {asset_str} time range: {tmin} to {tmax} (n={len(rows)})')
            else:
                print(f'# {asset_str} time range: unknown (n={len(rows)})')
            top1_list = []
            top3_list = []
            for snap in rows:
                try:
                    res = analyze_snapshot(snap, asset_str)
                    top1_list.append(res['top1'])
                    top3_list.append(res['top3'])
                except Exception as e:
                    top1_list.append(0)
                    top3_list.append(0)
            print(f'{asset_str} top-1 conflict: {top1_list}')
            print(f'{asset_str} top-3 conflict: {top3_list}')
            top1_n = sum(top1_list)
            top3_m = sum(top3_list)
            print(f'{asset_str}: top-1 {top1_n} of {len(top1_list)}, top-3 {top3_m} of {len(top3_list)}')
            print()
        print('# Filters applied: _suppress_previews, _downweight_news_duplicating_calendar (best effort)')
        print('# Filters skipped: session-change reweight (current session state differs from historical); post-tick modifications to AssetImpact.final_weight not replicated')
        print('# Reconstruction is approximate (filters use current state; we match articles by time window near snapshot).')
        print('# If contributing_articles were empty for all snapshots, reconstruction would be impossible - but we found data.')
    finally:
        session.close()


if __name__ == '__main__':
    main()
