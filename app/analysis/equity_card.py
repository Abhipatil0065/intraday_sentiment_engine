"""SESSION-AWARE GLOBAL EQUITY CARD (one card, NOW-only, never persisted).

Takes equity-relevant news signals for today, filters them to the active
session's region via EQUITY_SESSION_MAP keywords (index names, regional
central banks, regional macro/earnings terms), and scores them with the
SAME sentiment_engine.compute() contract the metal cards use - BINARY BIAS:
the label is the sign of the net news score, never NO_BIAS / NEUTRAL.

Zero regional matches -> 50/50 payload (no_data=True) that CARRIES FORWARD
the last bias published for that region (stale=True), falling back to the
newest article's direction and then DEFAULT_BIAS - never an error, never a
withheld verdict. State is keyed per region ("EQUITY:<region>") so a session
switch cannot reuse another region's history. No DB rows are written; the
pipeline keeps the result in memory and main.py serves it inside
/api/dashboard.

Task 7: equity card has its OWN relevance table (EQUITY_RELEVANCE in config),
separate from BITCOIN's slot. Inputs: EQUITIES + central bank + macro data +
earnings + geopolitical + banking stress + risk sentiment categories,
filtered by active region keywords. Direction: hawkish policy, hot inflation,
rising yields, escalation -> bearish; cuts, easing, earnings beats, strong
growth data -> bullish. Surprise, tier, decay and repeat rules from earlier
tasks apply.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from app.analysis.cue_rules import detect_direction
from app.analysis.display import to_display
from app.analysis.event_clustering import normalise_tokens
from app.analysis.release_surprise import (
    combine_prints_for_asset,
    parse_all,
)
from app.analysis.sentiment_engine import (
    DEFAULT_BIAS,
    Bias,
    WeightedSignal,
    payload_score,
    recall_bias,
    EPS,
)
from app.analysis.sentiment_engine import (
    compute as compute_sentiment,
)
from app.config import EQUITY_RELEVANCE
from app.logging_conf import get_logger
from app.market.session_detector import (
    EQUITY_SESSION_MAP,
    SessionName,
    equity_keywords,
    equity_session,
    equity_title,
)
from app.models.enums import Asset, Direction, NewsCategory, ReportKind
from app.utils import timeutils as T

log = get_logger(__name__)


@dataclass
class EquityCardResult:
    """In-memory verdict for the one global equity card (always directional)."""

    region: str
    title: str
    symbol: str
    score: float = 0.0
    # The pre-clamp net score, for logging only: `score` is what the payload
    # shows, so a tie-break can never contradict its badge.
    raw_score: float = 0.0
    # TASK 2: the aggregate totals, so the session card reports the same r as
    # the asset cards. Zero (not None) because this object is constructed
    # in-memory rather than read back from a nullable column.
    bull: float = 0.0
    bear: float = 0.0
    confidence: float = 0.0
    bias: str = Bias.BULLISH.value
    mode: str = "heuristic"
    drivers: list = field(default_factory=list)
    # Both sides, each capped at 3, for the dashboard's side-by-side boxes.
    bull_drivers: list = field(default_factory=list)
    bear_drivers: list = field(default_factory=list)
    contributing_articles: int = 0
    signal_count: int = 0       # signals that reached this region in the window
    no_data: bool = True
    # True when the region had no signals and the bias was carried forward
    # from region history (or the newest article / DEFAULT_BIAS).
    stale: bool = False
    note: str = ""
    last_updated: str = ""
    # Same four fields the asset cards expose after the contradiction split:
    # deprecated alias, cluster-level conflict, evidence split, and the
    # 15-minute same-direction burst counts behind the score.
    contradiction_detected: bool = False
    cluster_conflict: bool = False
    evidence_split: bool = False
    burst_bull: int = 0
    burst_bear: int = 0


def _region_of(session: SessionName) -> str:
    if session in (SessionName.ASIA, SessionName.EUROPE, SessionName.US):
        return session.value
    return "US"


def _state_key(region: str) -> str:
    """Carry-forward namespace for one region (mirrors the frontend flip key)."""
    return f"EQUITY:{region}"

# Global macro keywords that apply to ALL equity sessions. A headline matching
# only these (and no session-specific keyword) reaches every session card at
# partial weight, so global evidence (Fed, CPI, NFP, yields, risk appetite)
# is not dropped just because it names no regional index.
GLOBAL_MACRO_KEYWORDS = {
    "fed", "fomc", "federal reserve", "powell",
    "cpi", "ppi", "inflation", "deflation",
    "nfp", "payrolls", "unemployment", "jobless",
    "gdp", "recession", "growth",
    "pmi", "manufacturing", "services",
    "rate cut", "rate hike", "hawkish", "dovish",
    "treasury yield", "bond yield", "yields",
    "tariff", "trade war", "sanctions",
    "risk-on", "risk-off", "risk appetite",
    "s&p 500", "s&p", "nasdaq", "dow", "equities",
    "stocks rally", "stocks fall", "market selloff", "market rally",
}

# Weight applied to a signal whose headline matches only a global-macro
# keyword (no session-specific keyword). Session-specific matches keep 1.0.
GLOBAL_MACRO_WEIGHT = 0.65


def _matches_region(text: str, keywords: Sequence[str]) -> float:
    """Match weight for one headline against a session.

    Returns 1.0 when the headline matches a session-specific keyword,
    GLOBAL_MACRO_WEIGHT (0.65) when it matches a global-macro keyword only,
    and 0.0 otherwise. Session-specific keywords always win over global ones,
    so a Nikkei headline scores full weight in ASIA and is absent elsewhere.
    """
    lowered = (text or "").lower()
    if any(k and k in lowered for k in keywords):
        return 1.0
    if any(k and k in lowered for k in GLOBAL_MACRO_KEYWORDS):
        return GLOBAL_MACRO_WEIGHT
    return 0.0


def _aware_utc(dt: datetime | None) -> datetime | None:
    """Aware UTC copy of a published_at, or None (naive -> UTC, like clustering)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# Same 30-minute window ``cluster_signals`` uses for near-duplicate joins, so
# the title fallback below can never merge stories that clustering itself
# would keep apart. The event_key / cluster_id identity has no window: one
# event is one story for the whole day, that is what the key MEANS.
_TITLE_WINDOW = timedelta(minutes=30)


def _collapse_cross_asset(signals: list[WeightedSignal],
                          now: datetime | None = None) -> list[WeightedSignal]:
    """
    ONE story, ONE vote in the equity aggregate (2026-10-13).

    One article is classified against every asset it could touch, so the
    pipeline built one WeightedSignal PER ASSET_IMPACT ROW - copies sharing a
    single title but a different asset (e.g. XAUUSD + XAGUSD + BTCUSD news on
    the same headline). ``cluster_signals`` groups by asset FIRST, so those
    copies never meet there, and ``region_signals`` then re-tags EVERY
    region-matching copy as Asset.EQUITY. Without this collapse the story
    scored once per impact row and the card printed the identical (same
    direction) headline several times in its BULLISH / BEARISH driver lists,
    each copy inflating the net score.

    Copies collapse by cluster identity - ``event_key`` (the ingestion-level
    event), then ``cluster_id``, then the exact normalised title INSIDE a
    30-minute window (which also merges the same headline stored twice under
    different GUIDs) - and by direction, so two genuinely opposing readings of
    one event stay two votes, exactly like ``cluster_signals`` does. The
    surviving member is the copy with the highest weight under the same
    formula the scoring engine uses, so the representative is the one that
    contributes most anyway, and it keeps the strongest corroboration in the
    group (a per-asset boost ``cluster_signals`` already granted is not thrown
    away).

    A signal with no usable identity passes through untouched. Input objects
    are never mutated: the winner is returned via ``dataclasses.replace``.
    """
    from app.analysis.sentiment_engine import _signal_weight

    def _title_key(s: WeightedSignal) -> str:
        return " ".join(sorted(normalise_tokens(s.title or "")))

    groups: dict[tuple, list[WeightedSignal]] = {}
    order: list[tuple] = []
    for idx, s in enumerate(signals):
        stamp = _aware_utc(s.published_at)
        title_key = _title_key(s)
        key: tuple | None = None

        # 1. Exact event identity: one event is one story, all day.
        event_id = s.event_key or s.cluster_id
        if event_id:
            candidate = ("event", event_id, s.direction)
            if candidate in groups:
                key = candidate

        # 2. Title fallback, inside the 30-minute window: the same headline
        #    stored twice (different GUIDs, so different event keys) or
        #    carried by sources with no event identity at all. May join a
        #    group opened under either identity.
        if key is None and title_key:
            for existing in order:
                if existing[0] == "raw":
                    continue
                rep = groups[existing][0]
                if rep.direction != s.direction:
                    continue
                if _title_key(rep) != title_key:
                    continue
                rep_stamp = _aware_utc(rep.published_at)
                if stamp is None or rep_stamp is None:
                    continue
                if abs((stamp - rep_stamp).total_seconds()) <= \
                        _TITLE_WINDOW.total_seconds():
                    key = existing
                    break

        # 3. Nothing to join: open a new group.
        if key is None:
            if event_id:
                key = ("event", event_id, s.direction)
            elif title_key:
                key = ("title", title_key, s.direction, idx)
            else:
                # No fingerprint at all: keep it as its own vote.
                key = ("raw", idx)

        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)

    out: list[WeightedSignal] = []
    for key in order:
        members = groups[key]
        if len(members) == 1:
            out.append(members[0])
            continue
        winner = max(members, key=lambda m: _signal_weight(m, now))
        best_corr = max(float(m.corroboration or 1.0) for m in members)
        if best_corr > float(winner.corroboration or 1.0):
            winner = replace(winner, corroboration=best_corr)
        out.append(winner)
    return out


# Categories the equity card listens to (Task 7).
EQUITY_CATEGORIES = {
    NewsCategory.EQUITIES,
    NewsCategory.CENTRAL_BANK,
    NewsCategory.INTEREST_RATES,
    NewsCategory.INFLATION,
    NewsCategory.EMPLOYMENT,
    NewsCategory.GDP,
    NewsCategory.TREASURY_YIELDS,
    NewsCategory.USD_DXY,
    NewsCategory.GEOPOLITICS,
    NewsCategory.RISK_SENTIMENT,
    NewsCategory.BANKING_FINANCIAL_STRESS,
}


def _equity_relevance(category: NewsCategory) -> float:
    """Equity card's own relevance table (config.EQUITY_RELEVANCE)."""
    return EQUITY_RELEVANCE.get(category.value, 0.10)


def region_signals(signals: Sequence[WeightedSignal],
                   session: SessionName) -> list[WeightedSignal]:
    """Equity-relevant signals whose headline matches this region's keywords.

    Session-specific keywords give full weight (1.0); global-macro keywords
    give partial weight (GLOBAL_MACRO_WEIGHT = 0.65) so global evidence
    reaches every session without drowning out session-specific news.
    """
    keywords = equity_keywords(session)
    if not keywords and not GLOBAL_MACRO_KEYWORDS:
        return []
    out = []
    for s in signals:
        if s.category not in EQUITY_CATEGORIES:
            continue
        match_weight = _matches_region(s.title, keywords)
        if match_weight <= 0.0:
            continue
        # Apply equity card's own relevance, scaled by session match weight.
        # Preserve 0.0 for suppressed signals (relevance 0.0 stays 0.0).
        equity_rel = _equity_relevance(s.category)
        if s.relevance == 0.0:
            equity_rel = 0.0
        equity_rel *= match_weight
        out.append(WeightedSignal(
            asset=Asset.EQUITY,  # proxy asset for scoring engine
            title=s.title,
            direction=s.direction,
            strength=s.strength,
            importance=s.importance,
            published_at=s.published_at,
            relevance=equity_rel,
            persistence=s.persistence,
            source_quality=s.source_quality,
            session_multiplier=s.session_multiplier,
            is_primary=s.is_primary,
            event_key=s.event_key,
            cluster_id=s.cluster_id,
            corroboration=s.corroboration,
            category=s.category,
            reason=s.reason,
            transmission=s.transmission,
            report_kind=s.report_kind,
            mode=s.mode,
            surprise_weight=s.surprise_weight,
            # Overnight admission: keep the proxy's freshness aligned with
            # the signal it copies, so an off-window headline admitted this
            # morning is not aged out of the equity card.
            age_reference_utc=getattr(s, "age_reference_utc", None),
        ))
    return out


def neutral_card(session: SessionName, reason: str = "") -> EquityCardResult:
    """
    Directional fallback for an empty region - never a 50/50 card, never NO_BIAS.

    The bias ALWAYS carries forward the last verdict published for THIS region
    (stale=True). If no region history exists yet, the answer is NO_BIAS
    (a documented withheld verdict), which to_display() renders as NEUTRAL
    with a 50/50 split.
    """
    info = EQUITY_SESSION_MAP.get(session, {})
    region = _region_of(session)
    carried = recall_bias(_state_key(region))
    if carried is not None and carried in (Bias.BULLISH, Bias.BEARISH):
        bias = carried
    else:
        bias = Bias.NO_BIAS
    return EquityCardResult(
        region=region,
        title=str(info.get("title", region + " EQUITY")),
        symbol=str(info.get("symbol", "")),
        score=0.0,
        confidence=0.0,
        bias=bias.value,
        mode="heuristic",
        drivers=[],
        bull_drivers=[],
        bear_drivers=[],
        contributing_articles=0,
        signal_count=0,
        no_data=True,
        stale=True,
        note=reason or ("No " + region + " equities news in the window; "
                        "carrying forward the last published bias."),
        last_updated=T.stamp(),
    )


def build_equity_card(all_signals: Sequence[WeightedSignal],
                      now: datetime | None = None,
                      session: SessionName | None = None,
                      baseline: float | None = None) -> EquityCardResult:
    """
    Score the active region's equity-relevant signals into one card verdict.

    ``baseline`` is the equity asset's trailing baseline, applied exactly as
    it is for the three market assets: the verdict is made on the centred
    lean, so the card cannot publish an uncentred LEVEL under a badge that
    was decided on a centred value. None means no baseline is available, and
    the card behaves exactly as it always did.
    """
    active = session or equity_session()
    if active is SessionName.OFF_HOURS:
        active = equity_session()
    matched = region_signals(all_signals, active)
    if not matched:
        return neutral_card(active)

    proxy = []
    for s in matched:
        # Task 3b: an ACTUAL scheduled-data print is read with the equity
        # card's OWN direction column (equity_direction) - hot inflation/yields
        # bearish, strong growth/jobs/PMI bullish, higher unemployment/claims
        # bearish - never with the metals/bitcoin column the asset cards use.
        if s.report_kind is ReportKind.ACTUAL:
            text = f"{s.title} {s.reason or ''}"
            prints = parse_all(text)
            if prints:
                direction, _ = combine_prints_for_asset(
                    Asset.EQUITY, prints, s.direction,
                    use_equity_column=True, text=text)
            else:
                direction = s.direction
        else:
            # For non-ACTUAL signals, re-derive an equity-specific direction
            # from headline cues: metals and equities can disagree (e.g.
            # geopolitical safe-haven is bullish for gold but bearish for
            # equities; strong NFP is bearish for gold but bullish for
            # equities). If the cues return NEUTRAL or MIXED (no clear
            # directional signal in the headline), fall back to the signal's
            # own direction as a sensible default.
            equity_dir = detect_direction(s.title, equity=True)
            if equity_dir in (Direction.NEUTRAL, Direction.MIXED):
                direction = s.direction
            else:
                direction = equity_dir
        proxy.append(WeightedSignal(
            asset=Asset.EQUITY, title=s.title, direction=direction,
            strength=s.strength, importance=s.importance,
            published_at=s.published_at, relevance=s.relevance,
            persistence=s.persistence, source_quality=s.source_quality,
            session_multiplier=s.session_multiplier, is_primary=s.is_primary,
            event_key=s.event_key, cluster_id=s.cluster_id,
            corroboration=s.corroboration, category=s.category,
            reason=s.reason, transmission=s.transmission,
            report_kind=s.report_kind, mode=s.mode,
            surprise_weight=s.surprise_weight,
            age_reference_utc=getattr(s, "age_reference_utc", None)))
    # One story, one vote: drop the per-asset copies of the same headline
    # BEFORE scoring, otherwise a macro story that routed to gold, silver and
    # bitcoin at ingest is counted (and displayed) once per impact row.
    proxy = _collapse_cross_asset(proxy, now)
    # Task 4: the equity card reuses the same single tier multiplier table
    # and applies it once per signal, via sentiment_engine._signal_weight
    # (compute). No duplicate tier logic here, so importance and tier never
    # double-count.
    result = compute_sentiment(proxy, Asset.EQUITY, now=now,
                               history_key=_state_key(_region_of(active)),
                               baseline=baseline)
    info = EQUITY_SESSION_MAP.get(active, {})
    region = _region_of(active)
    drivers = []
    for d in (result.drivers or [])[:3]:
        direc = d.direction.value if isinstance(d.direction, Direction) else str(d.direction)
        drivers.append({"title": d.title,
                        "contribution": round(float(d.contribution), 4),
                        "direction": direc,
                        "weight": round(float(d.weight), 4)})
    bull_drivers = []
    for d in (result.bull_drivers or [])[:3]:
        direc = d.direction.value if isinstance(d.direction, Direction) else str(d.direction)
        bull_drivers.append({"title": d.title,
                             "contribution": round(float(d.contribution), 4),
                             "direction": direc,
                             "weight": round(float(d.weight), 4)})
    bear_drivers = []
    for d in (result.bear_drivers or [])[:3]:
        direc = d.direction.value if isinstance(d.direction, Direction) else str(d.direction)
        bear_drivers.append({"title": d.title,
                             "contribution": round(float(d.contribution), 4),
                             "direction": direc,
                             "weight": round(float(d.weight), 4)})
    bias = result.bias.value if isinstance(result.bias, Bias) else str(result.bias)
    # Same rule as the asset cards (label/percentage consistency): the payload
    # score must sit on the label's side, so a tie-break that named the other
    # side publishes the small lean instead of the raw (contradicting) score.
    # The raw value is kept alongside it for logging only. Idempotent - compute
    # applies the same rule - so this is the card's own copy of the guarantee.
    #
    # The baseline is CONTEXT, not a transform - see the long note in
    # sentiment_engine.compute for the measurement that settled it.
    score = payload_score(result.raw_score, result.bias)
    return EquityCardResult(
        region=region,
        title=str(info.get("title", equity_title(active))),
        symbol=str(info.get("symbol", "")),
        score=float(score),
        raw_score=float(result.raw_score),
        bull=float(getattr(result, "bull_weight", 0.0)),
        bear=float(getattr(result, "bear_weight", 0.0)),
        confidence=float(result.confidence),
        bias=bias,
        mode=getattr(result, "mode", "heuristic"),
        drivers=drivers,
        bull_drivers=bull_drivers,
        bear_drivers=bear_drivers,
        contributing_articles=int(result.contributing_articles),
        signal_count=int(getattr(result, "signal_count", 0)),
        no_data=False,
        stale=bool(getattr(result, "stale", False)),
        note="",
        last_updated=T.stamp(),
        contradiction_detected=bool(getattr(
            result, "contradiction_detected", False)),
        cluster_conflict=bool(getattr(
            result, "cluster_conflict", False)),
        evidence_split=bool(getattr(
            result, "evidence_split", False)),
        burst_bull=int(getattr(result, "burst_bull", 0) or 0),
        burst_bear=int(getattr(result, "burst_bear", 0) or 0),
    )


def to_payload(card: EquityCardResult) -> dict:
    """Plain dict for /api/dashboard (matches schemas.EquityCard)."""
    result = {
        "region": card.region,
        "title": card.title,
        "symbol": card.symbol,
        "score": float(card.score),
        "confidence": float(card.confidence),
        "bias": card.bias,
        "mode": card.mode,
        "drivers": list(card.drivers),
        "bull_drivers": list(card.bull_drivers),
        "bear_drivers": list(card.bear_drivers),
        "contributing_articles": int(card.contributing_articles),
        "signal_count": int(getattr(card, "signal_count", 0)),
        "no_data": bool(card.no_data),
        "stale": bool(card.stale),
        "note": card.note,
        "last_updated": card.last_updated,
        "contradiction_detected": bool(card.contradiction_detected),
        "cluster_conflict": bool(card.cluster_conflict),
        "evidence_split": bool(card.evidence_split),
        "burst_bull": int(card.burst_bull),
        "burst_bear": int(card.burst_bear),
    }
    # Step 8: pass the same tie-break flag the asset cards use, derived from the
    # card's own raw score, so a balanced regional verdict reads NEUTRAL 50/50
    # instead of a forced 51/49 lean.
    result["bull"] = float(card.bull)
    result["bear"] = float(card.bear)
    result["display"] = to_display(
        card.score, card.bias, card.confidence,
        tie_break=abs(float(card.raw_score)) < EPS,
    )
    return result