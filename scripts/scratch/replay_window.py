"""
Replay a UTC window of real stored articles through the SAME
signal-building path as pipeline._recompute_sentiment, then score
the three macro assets with scoring-time clustering ON and OFF.

Usage:
    .venv/Scripts/python.exe scripts/scratch/replay_window.py 2026-10-07T17:30 2026-10-07T19:30
    .venv/Scripts/python.exe scripts/scratch/replay_window.py 2026-10-07T12:00 2026-10-07T16:00

Read-only: no rows are added/updated/committed.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, r"C:\Users\HP\.vscode\intraday_sentiment_engine")

from sqlalchemy import select

from app.database import init_db, session_scope
from app.models.db_models import NewsArticle
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
    ReportKind,
)
from app.analysis.signal_factory import build_signal
from app.analysis.event_clustering import (
    cluster_signals, event_anchor, normalise_tokens, extract_entities,
)
from app.analysis.sentiment_engine import (
    compute, _aggregate, _signal_weight, clear_bias_history,
)
from app.analysis import release_surprise
from app.market.session_detector import detect_session, session_relevance
from app.services.pipeline import _suppress_previews

ASSETS = (Asset.GOLD, Asset.SILVER, Asset.BITCOIN)


def parse_window(start: str, end: str):
    fmt = "%Y-%m-%dT%H:%M"
    return (datetime.strptime(start, fmt).replace(tzinfo=timezone.utc),
            datetime.strptime(end, fmt).replace(tzinfo=timezone.utc))


def build_signals(session, window_start, window_end):
    """The _recompute_sentiment signal path, restricted to the window."""
    rows = session.scalars(
        select(NewsArticle).where(
            NewsArticle.published_at_utc >= window_start.replace(tzinfo=None),
            NewsArticle.published_at_utc <= window_end.replace(tzinfo=None),
        )
    ).all()
    sess = detect_session(now=window_end)
    signals = []
    for row in rows:
        if row.published_at_utc is None:
            continue
        event = row.event_links[0].event if row.event_links else None
        is_primary = (event.article_count <= 1) if event else True
        try:
            cat_enum = NewsCategory(row.category)
        except ValueError:
            cat_enum = NewsCategory.OTHER
        kind = ReportKind(row.report_kind)
        release_text = f"{row.title} {row.body}"
        printed = (release_surprise.parse(release_text)
                   if kind is ReportKind.ACTUAL else None)
        for impact in row.impacts:
            try:
                asset_enum = Asset(impact.asset)
            except ValueError:
                continue
            signals.append(build_signal(
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
                independent_sources=(event.article_count if event else 1),
                reason=impact.reason,
                transmission=impact.transmission,
                report_kind=kind,
                surprise_weight=release_surprise.surprise_weight(
                    printed, release_text),
                session_multiplier=session_relevance(
                    sess, cat_enum.value),
            ))
    # Same pre-cluster step as the pipeline: preview suppression.
    signals = _suppress_previews(signals)
    return rows, signals


def cluster_memberships(signals, window_hours=0.5):
    """Replicate cluster_signals' matching loop, keeping memberships.

    Mirrors cluster_signals exactly (both rules: 0.72 title
    similarity and the event-anchored join), so the reported
    structure is what the scorer actually sees.
    """
    window = timedelta(hours=window_hours)
    out = {}
    why_by_asset = {}
    by_asset = {}
    for s in signals:
        by_asset.setdefault(s.asset, []).append(s)
    for asset, asset_signals in by_asset.items():
        def get_sort_key(s):
            dt = s.published_at
            if dt is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        asset_signals.sort(key=get_sort_key)
        # Clusters hold WeightedSignal objects exactly like
        # cluster_signals; the join reason per signal is kept
        # in ``why`` (founders carry (0.0, False)).
        clusters = []
        why: dict = {}
        for s in asset_signals:
            if s.published_at is None:
                clusters.append([s])
                continue
            matched = False
            for cluster in clusters:
                rep = cluster[0]
                if rep.published_at is None:
                    continue
                s_dt = s.published_at
                rep_dt = rep.published_at
                if s_dt.tzinfo is None:
                    s_dt = s_dt.replace(tzinfo=timezone.utc)
                if rep_dt.tzinfo is None:
                    rep_dt = rep_dt.replace(tzinfo=timezone.utc)
                if abs((s_dt - rep_dt).total_seconds()) > window.total_seconds():
                    continue
                s_tokens = normalise_tokens(s.title)
                rep_tokens = normalise_tokens(rep.title)
                if not s_tokens or not rep_tokens:
                    continue
                union = len(s_tokens | rep_tokens)
                inter = len(s_tokens & rep_tokens)
                jaccard = inter / union if union else 0.0
                s_entities = extract_entities(s.title)
                rep_entities = extract_entities(rep.title)
                entity_boost = 0.0
                if s_entities and rep_entities:
                    shared = len(s_entities & rep_entities)
                    if shared:
                        entity_boost = 0.15 * shared
                similarity = min(1.0, jaccard + entity_boost)
                event_matched = False
                if s.direction == rep.direction:
                    s_key = event_anchor(s.title)
                    if s_key is not None and s_key == event_anchor(rep.title):
                        event_matched = True
                if similarity >= 0.72 or event_matched:
                    cluster.append(s)
                    why[id(s)] = (similarity, event_matched)
                    matched = True
                    break
            if not matched:
                clusters.append([s])
        out[asset] = clusters
        why_by_asset[asset] = why
    return out, why_by_asset


def main():
    start_s, end_s = sys.argv[1], sys.argv[2]
    window_start, window_end = parse_window(start_s, end_s)

    init_db()
    with session_scope() as session:
        rows, signals = build_signals(session, window_start, window_end)

    print("=" * 78)
    print(f"REPLAY WINDOW {window_start.isoformat()} .. {window_end.isoformat()}")
    print("=" * 78)
    print(f"articles in window: {len(rows)}  signals built: {len(signals)}")
    for asset in ASSETS:
        n = len([s for s in signals if s.asset is asset])
        print(f"  {asset.value}: {n} signals")

    clustered = cluster_signals(list(signals), window_hours=0.5)
    memberships, join_why = cluster_memberships(list(signals))

    print("\n" + "=" * 78)
    print("SCORING: clustering OFF vs ON")
    print("=" * 78)
    for asset in ASSETS:
        off_sigs = [s for s in signals if s.asset is asset]
        on_sigs = [s for s in clustered if s.asset is asset]
        clear_bias_history()
        agg_off = _aggregate(off_sigs)
        res_off = compute(off_sigs, asset)
        clear_bias_history()
        agg_on = _aggregate(on_sigs)
        res_on = compute(on_sigs, asset)
        print(f"\n--- {asset.value} ---")
        print(f"  OFF: score={res_off.score:+7.1f} raw={res_off.raw_score:+7.1f} "
              f"bias={res_off.bias.value:7s} conf={res_off.confidence:.3f} "
              f"articles={res_off.contributing_articles} "
              f"clusters={res_off.contributing_clusters}")
        print(f"       bull={agg_off['bull']:.6f} bear={agg_off['bear']:.6f} "
              f"directionless={agg_off['directionless']:.6f}")
        print(f"  ON : score={res_on.score:+7.1f} raw={res_on.raw_score:+7.1f} "
              f"bias={res_on.bias.value:7s} conf={res_on.confidence:.3f} "
              f"articles={res_on.contributing_articles} "
              f"clusters={res_on.contributing_clusters}")
        print(f"       bull={agg_on['bull']:.6f} bear={agg_on['bear']:.6f} "
              f"directionless={agg_on['directionless']:.6f}")
        print(f"  DELTA (ON - OFF): {res_on.score - res_off.score:+.1f}")

    print("\n" + "=" * 78)
    print("CLUSTER STRUCTURE (scoring-time, 30-min window, "
          "title>=0.72 OR event anchor)")
    print("=" * 78)
    for asset in ASSETS:
        clusters = memberships[asset]
        sizes = [len(c) for c in clusters]
        print(f"\n{asset.value}: {len(clusters)} cluster(s), "
              f"member counts={sizes}")
        why = join_why[asset]
        for c in clusters:
            for s in c:
                sim, ev = why.get(id(s), (0.0, False))
                w = _signal_weight(s, None)
                tag = "EVENT-ANCHORED" if ev else (
                    "title>=0.72" if sim >= 0.72 else "singleton")
                print(f"    {s.published_at} {s.direction.value:7s} "
                      f"{s.source_quality:15s} str={s.strength.value:9s} "
                      f"rel={s.relevance:.2f} per={s.persistence:.2f} "
                      f"corr={s.corroboration:.4f} wt={w:.6f} "
                      f"anchor={str(event_anchor(s.title)):18s} "
                      f"[{tag}] | {s.title[:44]}")

    print("\n" + "=" * 78)
    print("MERGE AUDIT - pairs joined by the EVENT-ANCHOR rule alone")
    print("(title similarity < 0.72; these merges are the new rule)")
    print("=" * 78)
    any_audit = False
    for asset in ASSETS:
        for c in memberships[asset]:
            if len(c) < 2:
                continue
            for i in range(len(c)):
                for j in range(i + 1, len(c)):
                    a = c[i]
                    b = c[j]
                    ta, tb = normalise_tokens(a.title), normalise_tokens(b.title)
                    if not ta or not tb:
                        continue
                    union = len(ta | tb)
                    inter = len(ta & tb)
                    jaccard = inter / union if union else 0.0
                    ea, eb = extract_entities(a.title), extract_entities(b.title)
                    boost = 0.0
                    if ea and eb:
                        shared = len(ea & eb)
                        if shared:
                            boost = 0.15 * shared
                    sim = min(1.0, jaccard + boost)
                    if sim < 0.72:
                        any_audit = True
                        print(f"  {asset.value}  sim={sim:.4f} "
                              f"anchor={event_anchor(a.title)}  "
                              f"gap={abs((b.published_at - a.published_at).total_seconds() / 60):.1f}m")
                        print(f"      A: {a.title[:60]}")
                        print(f"      B: {b.title[:60]}")
    if not any_audit:
        print("  (none - every multi-member cluster joined on "
              "title similarity >= 0.72)")


if __name__ == "__main__":
    main()
