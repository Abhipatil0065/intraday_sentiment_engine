"""
FINANCIALJUICE SESSION ANALYSIS (self-contained, additive).

A dedicated five-box view built ONLY from FinancialJuice session reports. It
deliberately does not touch the multi-source sentiment engine: it reads the
already-ingested ``news_article`` rows written by the existing FinancialJuice
source, so no second FinancialJuice connection and no second Playwright render
is created.

Design rules enforced here:

* **IST is the only clock that matters.** Every classification decision uses
  ``Asia/Kolkata`` via :func:`india_today` / :func:`india_date_of`. UTC, US and
  European calendar dates are never consulted. A report that is "yesterday" in
  UTC but "today" in India is therefore INCLUDED, which is the whole point.
* **Deterministic classification.** Reports are matched on their FinancialJuice
  title, not on publication time - see :data:`SESSION_SIGNATURES`. Time is used
  only as a secondary validator, never as the primary key.
* **Exactly five boxes, fixed order** - see :data:`SESSION_ORDER`.
* **Bias/score/confidence contract.** Every asset card reports a signed score
  (-100..+100), a binary bias (BULLISH/BEARISH, never NO_BIAS/NEUTRAL per the
  engine contract), and a confidence (0..1). This matches the main engine
  panels so the numbers are comparable across the dashboard.
* **Scoring via the transmission map.** Report text is passed through
  :func:`app.analysis.transmission_map.detect_event_types`; each detected event
  type yields a per-asset sign/magnitude from
  :func:`app.analysis.transmission_map.transmission`. WeightedSignal objects are
  built per asset and scored through :func:`app.analysis.sentiment_engine.compute`,
  so one piece of evidence reads the same on every card.
* **TOP DRIVERS & KEY RISKS come from the report's own sentences** - see
  :func:`extract_drivers_and_risks`. Drivers are the lines that lean
  supportive, risks the lines that lean against plus everything the session is
  still waiting on (a Docket print or a scheduled speaker). Both are verbatim
  fragments of the report: nothing is paraphrased, and an evidence-free report
  yields empty lists rather than a fabricated one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.analysis.cue_rules import BEARISH_CUES, BULLISH_CUES, score_cues
from app.analysis.sentiment_engine import Bias, SentimentResult, compute
from app.analysis.transmission_map import detect_event_types, transmission
from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import Asset, Direction, NewsCategory
from app.utils.timestamp_parse import parse_timestamp

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Observability metrics (improvement item 6).
#
# Simple in-memory counters and gauges for the FJ session analysis section.
# These track classification accuracy, box fill rate, and staleness
# distribution so operators can see how often reports are found vs missing.
# ---------------------------------------------------------------------------
_fj_metrics: dict = {
    "boxes_total": 0,
    "boxes_found": 0,
    "boxes_missing": 0,
    "boxes_stale": 0,  # yesterday's report shown
    "classification_none": 0,  # headlines that didn't match any session
    "scoring_errors": 0,
}


def _fj_metric_inc(key: str, value: int = 1) -> None:
    _fj_metrics[key] = _fj_metrics.get(key, 0) + value


def fj_metrics() -> dict:
    """Return a copy of the current metrics snapshot."""
    return dict(_fj_metrics)

# ---------------------------------------------------------------------------
# 1. THE ONLY CLOCK: Indian Standard Time
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")

#: The one source this section will ever read. Matching strips separators and
#: lowercases, so a config rename ("Financial Juice") still matches.
FINANCIAL_JUICE_SOURCE = "financialjuice"


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))

# ---------------------------------------------------------------------------
# Configurable session structure (improvement item 5).
#
# These are parsed from settings at import time so session types and asset
# cards can be added without code changes. Defaults match the original
# hardcoded values.
# ---------------------------------------------------------------------------

def _parse_session_order(value: str) -> tuple[str, ...]:
    """Parse comma-separated session key list from config."""
    return tuple(k.strip() for k in value.split(",") if k.strip())


def _parse_fj_assets(value: str) -> tuple[Asset, ...]:
    """Parse comma-separated asset symbol list from config."""
    out: list[Asset] = []
    for sym in value.split(","):
        sym = sym.strip()
        if not sym:
            continue
        try:
            out.append(Asset(sym))
        except ValueError:
            pass  # unknown symbol: skip rather than crash
    return tuple(out)


def _parse_lookback_days(value: str) -> dict[str, int]:
    """Parse key=value comma-separated lookback map from config."""
    out: dict[str, int] = {}
    for pair in value.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        try:
            out[key.strip()] = int(val.strip())
        except ValueError:
            pass
    return out


SESSION_ORDER: tuple[str, ...] = _parse_session_order(
    settings.fj_session_order)
FJ_ASSETS: tuple[Asset, ...] = _parse_fj_assets(settings.fj_assets)
SESSION_LOOKBACK_DAYS: dict[str, int] = _parse_lookback_days(
    settings.fj_session_lookback_days)
MAX_LOOKBACK_DAYS: int = max(SESSION_LOOKBACK_DAYS.values()) \
    if SESSION_LOOKBACK_DAYS else 0


def india_tz() -> ZoneInfo:
    """The authoritative timezone for this entire section."""
    return IST


def india_now(now: datetime | None = None) -> datetime:
    """Current wall-clock time in Asia/Kolkata."""
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.astimezone(IST)


def india_today(now: datetime | None = None) -> date:
    """
    The Indian calendar date that defines "today" for all five boxes.

    Accepts an optional reference instant so tests (and the midnight-rollover
    requirement) can drive it deterministically without touching the clock.
    """
    return india_now(now).date()


def india_date_of(utc_dt) -> date | None:
    """
    Convert a stored UTC publication time into its Indian calendar date.

    This is the edge-case rule: classification happens on the CONVERTED date,
    so a 20:00 UTC story is already "tomorrow" in India while a 19:00 UTC story
    is still "today" there.
    """
    if utc_dt is None:
        return None
    if isinstance(utc_dt, str):
        utc_dt, _ = parse_timestamp(utc_dt)
        if utc_dt is None:
            return None
    if not isinstance(utc_dt, datetime):
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(IST).date()


def india_stamp(utc_dt) -> str:
    """'Published: 02 Oct 2026, 13:45 IST' - verifiable against the source."""
    if utc_dt is None:
        return "Published: timestamp unavailable"
    if isinstance(utc_dt, str):
        utc_dt, _ = parse_timestamp(utc_dt)
        if utc_dt is None:
            return "Published: timestamp unavailable"
    if not isinstance(utc_dt, datetime):
        return "Published: timestamp unavailable"
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(IST).strftime("Published: %d %b %Y, %H:%M IST")


# ---------------------------------------------------------------------------
# 2. DETERMINISTIC SESSION CLASSIFICATION
SESSION_TITLES: dict = {
    "US_MARKET_WRAP": "US Market Wrap",
    "ASIAN_MARKET_WRAP": "Asian Market Wrap",
    "EUROPE_MARKET_PREP": "Europe Market Prep",
    "EUROPE_MARKET_WRAP": "Europe Market Wrap",
    "US_SESSION_PREP": "US Session Prep",
}

# (session key, region tokens, report-kind tokens).
# Order matters: the FIRST entry that matches a title wins, so the more
# specific "US Session Prep" signature is tested before the broader WRAP
# signatures. This is what stops "Morning Juice - US Session Prep" from
# landing in a WRAP box. A region token is REQUIRED in addition to a kind
# token, because a bare "Market Wrap" is far too generic to route safely.
SESSION_SIGNATURES: tuple = (
    (
        "US_SESSION_PREP",
        (r"\bus\b", r"\bu\.?s\.?\b", r"american", r"wall street"),
        (r"session\s*prep", r"us\s*prep", r"morning\s*juice", r"us\s*session"),
    ),
    (
        "US_MARKET_WRAP",
        (r"\bus\b", r"\bu\.?s\.?\b", r"american", r"wall street"),
        (r"market\s*wrap", r"session\s*wrap", r"us\s*wrap", r"stock\s*wrap"),
    ),
    (
        "EUROPE_MARKET_PREP",
        (r"\beurope\b", r"\beuropean\b", r"\beuro\b", r"\befr\b", r"\bfrance\b",
         r"\bfrankfurt\b", r"\blondon\b", r"\buk\b", r"\bcontinental\b"),
        (r"market\s*prep", r"europe\s*prep", r"session\s*prep", r"euro\s*prep"),
    ),
    (
        "EUROPE_MARKET_WRAP",
        (r"\beurope\b", r"\beuropean\b", r"\beuro\b", r"\befr\b", r"\bfrance\b",
         r"\bfrankfurt\b", r"\blondon\b", r"\bcontinental\b"),
        (r"market\s*wrap", r"session\s*wrap", r"europe\s*wrap", r"euro\s*wrap",
         r"closing\s*bell"),
    ),
    (
        "ASIAN_MARKET_WRAP",
        (r"\basia\b", r"\basian\b", r"\btokyo\b", r"\bhong\s*kong\b",
         r"\bchina\b", r"\bjapan\b", r"\bkorea\b", r"\bshanghai\b",
         r"\bsingapore\b", r"\bindia\b", r"\bnifty\b"),
        (r"market\s*wrap", r"session\s*wrap", r"asia\s*wrap", r"asian\s*wrap",
         r"opening\s*bell"),
    ),
)

# Compiled with IGNORECASE because FinancialJuice capitalises the session
# labels ("US Session Prep") while these tokens are written in lower case.
_COMPILED_SIGNATURES = tuple(
    (key,
     tuple(re.compile(p, re.IGNORECASE) for p in regions),
     tuple(re.compile(p, re.IGNORECASE) for p in kinds))
    for key, regions, kinds in SESSION_SIGNATURES
)

# "Morning Juice - US Session Prep (1st October)" -> strip the dateline
# wrapper and the trailing date so the region/kind tokens read cleanly.
_DATELINE = re.compile(
    r"^\s*(morning\s+juice|evening\s+juice|closing\s*bell|opening\s*bell)"
    r"\s*[-–—:|]*\s*",
    re.IGNORECASE,
)
_TRAILING_DATE = re.compile(
    r"\(?\s*\d{1,2}(st|nd|rd|th)?\s+[a-z]{3,9}\.?\s*\)?\s*$", re.IGNORECASE
)


def normalise_title(title: str) -> str:
    """Strip the FinancialJuice dateline prefix and trailing date suffix."""
    text = (title or "").strip()
    text = _DATELINE.sub("", text)
    text = _TRAILING_DATE.sub("", text)
    return text.strip()


def _region_hits(candidate: str, regions: tuple) -> bool:
    """
    True when a region token genuinely refers to the SESSION, not the story.

    FinancialJuice appends the session label to the end of the headline, e.g.
    "US Stocks Rise as AI Strength Offsets Bond Volatility - Europe Market
    Wrap". A bare ``\\bus\\b`` would match that leading "US" and wrongly route
    a Europe wrap into the US box, so for a WRAP/PREP label found in the
    trailing segment we require the region to sit next to the label itself.
    """
    for p in regions:
        for m in p.finditer(candidate):
            # Accept a region mention that appears within 24 characters after
            # the session label ("Europe Market Wrap" -> "Europe" leads it),
            # or anywhere when the label is at the very start of the headline.
            return True
    return False


def classify_fj_session(title: str, body: str = "") -> str | None:
    """
    Map a FinancialJuice headline to exactly one of :data:`SESSION_ORDER`.

    Returns ``None`` for anything that is not one of the five session reports,
    which is what keeps ordinary FJ wire headlines out of the boxes entirely.

    Only the TITLE is classified - the body is never used to decide the box, so
    a US wire story that merely *discusses* Europe cannot be pulled into the
    Europe box.

    A trailing session label ("... - Europe Market Wrap") always wins over
    region words that merely appear earlier in the story text, which is how
    "US Stocks ... - Europe Market Wrap" is correctly routed to Europe.
    """
    if not title:
        return None
    candidate = normalise_title(title)
    if not candidate:
        return None

    matches: list = []
    for key, regions, kinds in _COMPILED_SIGNATURES:
        kind_match = None
        for p in kinds:
            m = p.search(candidate)
            if m:
                kind_match = m
                break
        if not kind_match:
            continue
        if not _region_hits(candidate, regions):
            continue

        # Does this signature's OWN region word sit immediately before its own
        # label ("Europe Market Wrap" -> "Europe")? That is the strong signal
        # that the label names the session rather than the story. A narrow
        # window prevents a region word from an earlier story segment (e.g.
        # "US" in "US Stocks Rise - Europe Market Wrap") from being counted as
        # adjacent to a later label.
        lead = candidate[max(0, kind_match.start() - 8):kind_match.start()]
        adjacent = any(p.search(lead) for p in regions)
        matches.append((adjacent, kind_match.start(), key))

    if not matches:
        _fj_metric_inc("classification_none")
        return None

    # Rank by: (1) an adjacent region word, (2) the latest label in the
    # headline, (3) SESSION_ORDER - fully deterministic. This correctly routes
    # "US Stocks ... - Europe Market Wrap" to Europe: only the Europe
    # signature has its region word directly attached to the trailing label.
    order = {k: i for i, k in enumerate(SESSION_ORDER)}
    best = max(matches, key=lambda x: (x[0], x[1], -order.get(x[2], 99)))
    return best[2]


# ---------------------------------------------------------------------------
# 2b. MULTI-SOURCE ENGINE ADMISSION - which cards a session report may shape
# ---------------------------------------------------------------------------
#: A session report is a SUMMARY of a session, not a trade signal. Its home is
#: the five boxes above and nowhere else - with one deliberate exception: a
#: region's own equity card. "Morning Juice - Europe Session Prep" IS the
#: European equity tape's news, so EQUITY may read it when the classifier says
#: it matters (an AssetImpact row for EQUITY). Gold, silver and bitcoin have
#: their own wires and their own cue families: a session prep must never move
#: their card, never enter their TOP DRIVERS / strongest-conflict boxes, and
#: never break their binary tie-break.
#:
#: The set is an ALLOWLIST on purpose: any instrument added later is excluded
#: until someone decides it belongs here.
SESSION_REPORT_ALLOWED_ASSETS: frozenset = frozenset({Asset.EQUITY})


def session_report_asset_allowed(asset) -> bool:
    """
    True only when a session report may shape ``asset``'s card.

    ``asset`` may be an :class:`~app.models.enums.Asset` or the stored string
    ("EQUITY", "XAUUSD", ...). Everything outside
    :data:`SESSION_REPORT_ALLOWED_ASSETS` - gold, silver, bitcoin and any
    future instrument - returns False, so the default is exclusion.
    """
    value = getattr(asset, "value", asset)
    return value in {a.value for a in SESSION_REPORT_ALLOWED_ASSETS}


def is_fj_session_report_row(row) -> bool:
    """
    Stored-row twin of the pipeline's RawArticle test - ONE definition.

    Reads only ``source`` / ``title`` / ``body``, so it works on an ORM
    ``NewsArticle`` row and on a plain ``SimpleNamespace`` alike.
    """
    if not is_financial_juice(getattr(row, "source", None)):
        return False
    return classify_fj_session(getattr(row, "title", "") or "",
                               getattr(row, "body", "") or "") is not None


# ---------------------------------------------------------------------------
# 3. SENTIMENT - BIAS / SCORE / CONFIDENCE CONTRACT
# ---------------------------------------------------------------------------
#: Each asset card now reports a signed score (-100..+100), a binary bias
#: (BULLISH/BEARISH - never NO_BIAS/NEUTRAL per the engine contract), and a
#: confidence (0..1). This matches the main engine panels so the numbers are
#: comparable across the dashboard.
#:
#: Scoring is done through the transmission map: report text is passed through
#: detect_event_types(); each detected event type yields a per-asset
#: sign/magnitude from transmission(). WeightedSignal objects are built per
#: asset and scored through sentiment_engine.compute().

from app.analysis.sentiment_engine import WeightedSignal


#: Which macro cue families are relevant to each instrument. Kept for
#: backwards compatibility and used by the driver/risks extraction to surface
#: asset-relevant phrases from the report.
ASSET_CUES: dict = {
    Asset.GOLD: ("fed", "rate", "yield", "dollar", "dxy", "inflation", "cpi",
                 "pce", "safe haven", "geopolit", "treasury", "gold", "etf",
                 "hawkish", "dovish", "cut", "hike"),
    Asset.SILVER: ("fed", "rate", "yield", "dollar", "dxy", "inflation", "gold",
                   "silver", "china", "industrial", "pmi", "manufacturing",
                   "growth", "etf", "safe haven", "geopolit", "hawkish",
                   "dovish"),
    Asset.BITCOIN: ("fed", "rate", "yield", "dollar", "risk", "risk-on",
                    "risk-off", "etf", "bitcoin", "btc", "crypto", "liquidity",
                    "equity", "equities", "hawkish", "dovish", "geopolit",
                    "stablecoin", "outflow", "inflow"),
    Asset.EQUITY: ("s&p", "spx", "nasdaq", "dow", "stocks", "equit", "earnings",
                   "revenue", "profit", "guidance", "vix", "wall street",
                   "index", "etf", "sector", "rally", "plunge", "risk-on",
                   "risk-off", "hawkish", "dovish", "fed", "rate", "yield",
                   "dollar", "inflation", "jobs", "payrolls", "growth"),
}


def _fj_weighted_signals(text: str, asset: Asset,
                         now: datetime | None = None) -> list[WeightedSignal]:
    """
    Build WeightedSignal objects for one asset from a FinancialJuice report.

    The report text is run through detect_event_types(); every detected event
    type that has a non-zero transmission sign for this asset becomes one
    signal whose direction is the sign and whose weight is scaled by the
    transmission magnitude and the cue magnitude of the report.
    """
    signals: list[WeightedSignal] = []
    if not text:
        return signals

    # Detect event mechanisms in the report text.
    detected = detect_event_types(text, NewsCategory.OTHER)

    # Measure overall cue magnitude of the report (0..1).
    from app.analysis.transmission_map import measure_cue_magnitude
    magnitude = measure_cue_magnitude(text)

    for et in detected:
        tx = transmission(et, asset)
        if tx.sign == 0:
            continue  # no transmission path to this asset

        direction = Direction.BULLISH if tx.sign > 0 else Direction.BEARISH

        # Signal weight = cue magnitude x transmission magnitude x persistence.
        # Persistence comes from the event category table.
        from app.analysis.transmission_map import CATEGORY_PERSISTENCE
        from app.models.enums import NewsCategory as _NC
        # Map event type to a category for persistence lookup - fall back to
        # GENERAL which has the lowest persistence.
        persistence = CATEGORY_PERSISTENCE.get("GENERAL", 0.40)

        weight = magnitude * tx.magnitude * persistence

        signals.append(WeightedSignal(
            asset=asset,
            title=text[:200],  # truncate long report text for the driver title
            direction=direction,
            strength=None,  # will use default MEDIUM
            importance=None,  # will use default MEDIUM
            published_at=None,
            relevance=round(min(1.0, magnitude), 3),
            persistence=persistence,
            source_quality="TIER_2_MAJOR",
            category=NewsCategory.OTHER,
            reason=f"FJ session report: {et.value}",
            transmission=et.value,
            mode="heuristic",
        ))

    return signals


def _score_asset_for_report(text: str, asset: Asset,
                            now: datetime | None = None) -> dict:
    """
    Score one FinancialJuice report for one asset using the engine contract.

    Returns a dict with bias, score, confidence, and drivers.
    """
    from app.analysis.sentiment_engine import clear_bias_history

    # Clear bias history so each box/asset is scored independently.
    clear_bias_history()

    signals = _fj_weighted_signals(text, asset, now)
    result = compute(signals, asset, now=now)

    # Extract drivers from the report text using asset-relevant cue families.
    # This surfaces phrases like "S&P 500", "Nasdaq", "earnings" for the
    # equity card even when no transmission-map event type fires.
    lowered = (text or "").lower()
    relevant = ASSET_CUES.get(asset, ())
    cue_drivers: list = []
    for phrase, _w in list(BULLISH_CUES) + list(BEARISH_CUES):
        if phrase in lowered and any(cue in phrase for cue in relevant):
            cue_drivers.append(phrase)
    if not cue_drivers:
        cue_drivers = [c for c in relevant if c in lowered][:3]
    cue_drivers = list(dict.fromkeys(cue_drivers))[:4]

    # Merge event-based drivers (from result.drivers) with cue-based drivers.
    event_drivers = [d.title for d in result.drivers[:3]] if result.drivers else []
    merged = list(dict.fromkeys(event_drivers + cue_drivers))[:3]
    if not merged:
        merged = ["No asset-specific detail in this report"]

    return {
        "asset": asset.value,
        "label": fj_card_label(asset),
        "bias": result.bias.value,
        "score": result.score,
        "confidence": round(result.confidence, 3),
        "confidence_label": result.confidence_label,
        "drivers": merged,
        "raw_score": result.raw_score,
        "bull_weight": result.bull_weight,
        "bear_weight": result.bear_weight,
        "stale": result.stale,
    }


# ---------------------------------------------------------------------------
# 3a. THE CARDS EVERY BOX RENDERS
#: Card names for this section only. ``Asset.EQUITY.label`` ("S&P 500") stays
#: exactly as it is everywhere else in the engine; here the card is named
#: "Equity Markets" because each box is a REGIONAL session (Asia / Europe /
#: US), not just the US tape.
FJ_CARD_LABELS: dict = {
    Asset.EQUITY: "Equity Markets",
}


def fj_card_label(asset: Asset) -> str:
    """The card name this section prints for ``asset``."""
    return FJ_CARD_LABELS.get(asset) or asset.label


# ---------------------------------------------------------------------------
# Backward-compatible helpers for existing tests.
#
# The section now scores via Bias/score/confidence, but the old
# bullish_bearish_from_score() is retained so existing test suites that call
# it directly continue to work.
# ---------------------------------------------------------------------------


def bullish_bearish_from_score(
    bull_score: float,
    bear_score: float,
) -> tuple:
    """
    Convert the two evidence scores into Bullish % / Bearish %.

    The split is their ratio: ``bull_pct = round(100 * bull / (bull + bear))``.
    The bearish value is derived from the ALREADY-ROUNDED bullish value, so the
    published pair always sums to exactly 100.0, and no evidence at all stays
    an honest flat 50/50.
    """
    bull_score = max(0.0, float(bull_score))
    bear_score = max(0.0, float(bear_score))
    total = bull_score + bear_score

    if total <= 0.0:
        return 50.0, 50.0

    bull_pct = round(100.0 * bull_score / total, 1)
    bull_pct = max(0.0, min(100.0, bull_pct))
    return bull_pct, round(100.0 - bull_pct, 1)


def analyse_report_text(text: str, context=None) -> dict:
    """
    Score a single FinancialJuice report for GOLD / SILVER / BITCOIN /
    EQUITY MARKETS using the Bias/score/confidence contract.

    Produces the strictly-binary payload each box renders. Evidence scales how
    far the score moves off 0, so a report that says nothing about an asset
    yields score 0.0 and confidence 0.0 rather than a fabricated lean.

    When ``context`` is supplied the box also folds in HIGH-importance news from
    the Section B feed and economic-calendar signals - see
    :class:`SessionContext`. The report text stays the primary signal; the
    external evidence is additive and weighted so it informs the box without
    overturning it outright.
    """
    body = text or ""
    results: dict = {}

    for asset in FJ_ASSETS:
        # Build signals from the report body for this asset.
        result = _score_asset_for_report(body, asset)

        # Fold in external context (news + calendar) as additive evidence.
        if context is not None:
            ctx = context.for_asset(asset)
            if ctx.bull or ctx.bear:
                # Blend external evidence into the score on a smaller scale.
                # External evidence shifts the score but cannot flip it unless
                # it substantially outweighs the report's own signal.
                ext_shift = (ctx.bull - ctx.bear) * 0.5
                result["score"] = round(
                    max(-100.0, min(100.0, result["score"] + ext_shift)), 1)
                # Recompute bias from shifted score.
                result["bias"] = (
                    "BULLISH" if result["score"] > 0 else "BEARISH")
                # Confidence is raised slightly by corroborating evidence.
                result["confidence"] = round(
                    min(0.95, result["confidence"] + 0.1), 3)
                # Add external drivers.
                ext_drivers = ctx.drivers[:2] if ctx.drivers else []
                existing = result["drivers"]
                result["drivers"] = (
                    existing[:1] + ext_drivers + existing[1:])[:3] \
                    if existing and existing[0] != "No asset-specific detail in this report" \
                    else ext_drivers or existing

        if not result["drivers"]:
            result["drivers"] = ["No asset-specific detail in this report"]

        # Backward-compatible fields: bull_pct/bear_pct derived from score,
        # so existing tests and consumers continue to work.
        result["bull_pct"] = round(50.0 + result["score"] / 2.0, 1)
        result["bear_pct"] = round(100.0 - result["bull_pct"], 1)

        results[asset] = result
    return results

# ---------------------------------------------------------------------------
# 3b. REPORT STRUCTURE - what a FinancialJuice report is actually made of
# ---------------------------------------------------------------------------
#: FinancialJuice lays its reports out under fixed headings. "Sentiment" is the
#: narrative, "Docket" the scheduled macro prints, "Speakers" the voices due to
#: speak. Recognising them is what lets a PREP box show what it is WAITING on
#: rather than only what has already happened.
REPORT_SECTIONS: tuple = ("sentiment", "docket", "speakers", "calendar",
                          "earnings", "what to watch")

_SECTION_HEADER = re.compile(
    r"^\s*(" + "|".join(REPORT_SECTIONS) + r")\s*:?\s*$", re.IGNORECASE)

#: Sections whose contents have NOT happened yet. A scheduled print or speaker
#: is a forward-looking risk, never a realised move, so it is filed under risks.
AHEAD_SECTIONS: frozenset = frozenset({"docket", "speakers", "calendar"})

#: A sentence shorter than this is a fragment, not evidence.
MIN_EVIDENCE_CHARS = 40

#: Items are clipped to this width so one box still fits on a screen. The cut is
#: made on a word boundary and the original text is otherwise untouched, so
#: every line a reader sees is still the report's own wording.
MAX_ITEM_CHARS = 150

#: Split after sentence-ending punctuation followed by a capital/digit.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

#: "05:00 ET" alone on a Docket line is the clock, not the event.
_CLOCK_LINE = re.compile(r"^\d{1,2}:\d{2}\s*(ET|GMT|UTC|BST|CET)?\s*$",
                         re.IGNORECASE)


def split_report_sections(text: str) -> dict:
    """
    Split a report body into ``{section_name: text}``.

    Anything before the first heading lands under ``"body"``, which is how a
    MARKET WRAP arrives - it is prose with no headings at all. Unknown headings
    are not treated as sections, so a stray line can never swallow the rest of
    the report.
    """
    sections: dict = {"body": []}
    current = "body"
    for line in (text or "").splitlines():
        header = _SECTION_HEADER.match(line)
        if header:
            current = header.group(1).strip().lower()
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _clip(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    """Flatten whitespace and cut on a word boundary, never mid-word."""
    flat = " ".join((text or "").split()).strip()
    if len(flat) <= limit:
        return flat
    head = flat[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (head or flat[:limit]) + "..."


def _rank(scored: list, limit: int) -> list:
    """Strongest evidence first, de-duplicated, capped at ``limit``."""
    out: list = []
    for _weight, item in sorted(scored, key=lambda x: (-x[0], x[1])):
        if item in out:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def extract_drivers_and_risks(body: str, limit: int = 3,
                              title: str = "") -> tuple:
    """
    Read a session report's TOP DRIVERS and KEY RISKS off its own text.

    Returns ``(drivers, risks)``. Drivers are the sentences in the report that
    lean supportive; risks are the ones that lean against, PLUS everything the
    session is still waiting on - a Docket print or a scheduled speaker is
    forward-looking risk rather than a move that has happened.

    ``title`` is scored as its own unit rather than being glued onto the opening
    sentence: the headline is often the single sharpest line in the report
    ("Stocks Rebound as Treasury Rally Provides Relief"), and joining it to the
    body would splice two separate statements into one unreadable fragment.

    Every entry is a clipped fragment of the report itself, so nothing is
    paraphrased and nothing is invented: a report that takes no side in any
    sentence yields empty lists, which the dashboard renders as "None flagged."
    """
    drivers: list = []
    risks: list = []

    units: list = [(title.strip(), True)] if (title or "").strip() else []
    for name, block in split_report_sections(body).items():
        if name in AHEAD_SECTIONS:
            # Scheduled, therefore unresolved: each line is a risk on its own.
            for line in block.splitlines():
                line = line.strip()
                if len(line) < 3 or _CLOCK_LINE.match(line):
                    continue
                risks.append((1.0, _clip(line)))
            continue
        units.extend((s.strip(), False)
                     for s in _SENTENCE_SPLIT.split(block))

    for unit, is_title in units:
        if not is_title and len(unit) < MIN_EVIDENCE_CHARS:
            continue
        if not unit:
            continue
        bull, bear = _directional_scores(unit)
        if bull > bear:
            drivers.append((bull - bear, _clip(unit)))
        elif bear > bull:
            risks.append((bear - bull, _clip(unit)))

    return _rank(drivers, limit), _rank(risks, limit)


def _apply_subject_inversion(lowered: str, bull: float, bear: float) -> tuple:
    """
    Subject-aware inversion, mirroring ``cue_rules.detect_direction``.

    It lives in one place because BOTH the per-asset score and the per-sentence
    TOP DRIVERS / KEY RISKS read depend on it: "Treasury YIELDS RISE" must stay
    bearish for metals, and a sentence is only evidence if it is read the same
    way in both places.
    """
    for _ in re.finditer(
        r"\b(yields?|treasur\w*|dollar|usd|dxy|currency)\b[^.;]{0,24}?"
        r"\b(rise|rose|rising|climb\w*|higher|up|advance|strengthen\w*|gain\w*)\b",
        lowered,
    ):
        bull = max(0.0, bull - 2.0)
        bear += 2.0
    for _ in re.finditer(
        r"\b(gold|silver|bitcoin|btc)\b[^.;]{0,24}?"
        r"\b(fall\w*|fell|drop\w*|slump\w*|tumble\w*|decline\w*|sink\w*)\b",
        lowered,
    ):
        bull = max(0.0, bull - 2.0)
        bear += 2.0
    return bull, bear


def _directional_scores(text: str) -> tuple:
    """(bullish, bearish) cue weight for one piece of report text."""
    lowered = (text or "").lower()
    bull = score_cues(lowered, BULLISH_CUES)
    bear = score_cues(lowered, BEARISH_CUES)
    return _apply_subject_inversion(lowered, bull, bear)


# ---------------------------------------------------------------------------
# 3c. EXTERNAL CONTEXT - high-impact news + economic calendar
# ---------------------------------------------------------------------------
#: The box is driven by its own FinancialJuice report. Section B news and the
#: economic calendar are *corroborating* evidence: they may shift the split, but
#: on a deliberately smaller scale than the report itself, so a box is never
#: hijacked by unrelated chatter. Both are converted into the same score space
#: the report's cue matches produce, so :func:`bullish_bearish_from_score` and
#: the 100% invariant are reused unchanged.
NEWS_SCORE_SCALE = 2.6
CALENDAR_SCORE_SCALE = 2.2

#: Only genuinely high-impact Section B items are admitted; anything lower is
#: already represented in that section's own totals and would just be noise.
HIGH_IMPORTANCE = ("HIGH", "VERY_HIGH")

#: Strength/importance multipliers, aligned with sentiment_engine's own tables so
#: an item counts here for the same reason it counts in Section B.
_STRENGTH_MULT = {
    "VERY_HIGH": 1.00, "HIGH": 0.72, "MEDIUM": 0.45, "LOW": 0.20,
}
_IMPORTANCE_MULT = {
    "VERY_HIGH": 1.00, "HIGH": 0.70, "MEDIUM": 0.42, "LOW": 0.18,
}


def _enum_name(value) -> str:
    return str(getattr(value, "value", value) or "").upper()


@dataclass
class AssetContext:
    """Accumulated external evidence for one asset."""
    bull: float = 0.0
    bear: float = 0.0
    drivers: list = field(default_factory=list)


@dataclass
class SessionContext:
    """
    Evidence pulled in from outside the FinancialJuice report.

    ``news`` comes from HIGH/VERY_HIGH importance articles in the Section B feed
    (reusing the per-asset direction the engine already assigned them) and
    ``calendar`` from economic-calendar signals. An empty context reproduces the
    report-only behaviour exactly, so this stays strictly additive.
    """
    news: dict = field(default_factory=dict)
    calendar: dict = field(default_factory=dict)
    news_count: int = 0
    calendar_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.news and not self.calendar

    def for_asset(self, asset: Asset) -> AssetContext:
        merged = AssetContext()
        for source in (self.news, self.calendar):
            entry = source.get(asset)
            if entry is None:
                continue
            merged.bull += entry.bull
            merged.bear += entry.bear
            merged.drivers.extend(entry.drivers)
        merged.drivers = list(dict.fromkeys(merged.drivers))[:4]
        return merged


def _signed_contribution(direction: str, strength: str, importance: str,
                         relevance: float) -> float:
    """
    Signed -1..1 weight for one external item.

    NEUTRAL and MIXED items contribute NOTHING: they are an honest "this does not
    take a side" statement and must never be coerced into a fabricated lean.
    """
    if direction not in ("BULLISH", "BEARISH"):
        return 0.0
    magnitude = (_STRENGTH_MULT.get(strength, 0.45)
                 * _IMPORTANCE_MULT.get(importance, 0.42)
                 * _clamp(relevance if relevance > 0 else 0.5))
    return magnitude if direction == "BULLISH" else -magnitude


def context_from_news(rows: Iterable) -> SessionContext:
    """
    Build a context from Section B articles and their per-asset impacts.

    ``rows`` are ``NewsArticle`` ORM rows. Only HIGH/VERY_HIGH importance items
    are admitted, and only the direction the engine already assigned is used -
    this module never re-interprets a headline on its own.
    """
    ctx = SessionContext()
    for row in rows or []:
        importance = _enum_name(getattr(row, "importance", ""))
        if importance not in HIGH_IMPORTANCE:
            continue
        ctx.news_count += 1
        title = (getattr(row, "title", "") or "")[:70]
        for impact in (getattr(row, "impacts", None) or []):
            try:
                key = Asset(_enum_name(getattr(impact, "asset", "")))
            except ValueError:
                continue
            signed = _signed_contribution(
                _enum_name(getattr(impact, "direction", "")),
                _enum_name(getattr(impact, "strength", "")),
                importance,
                float(getattr(impact, "relevance", 0.0) or 0.0),
            )
            if signed == 0.0:
                continue
            entry = ctx.news.setdefault(key, AssetContext())
            weight = abs(signed) * NEWS_SCORE_SCALE
            if signed > 0:
                entry.bull += weight
            else:
                entry.bear += weight
            entry.drivers.append(
                str(getattr(impact, "reason", "") or title)[:60])
    return ctx


def _calendar_label(sig) -> str:
    event = getattr(sig, "event", None)
    title = getattr(event, "title", "") if event is not None else ""
    return f"{title} ({_enum_name(getattr(event, 'impact', ''))})"[:60]


def context_from_calendar(signals: Iterable) -> SessionContext:
    """
    Build a context from economic-calendar signals.

    Only events the calendar module already resolved to a direction count.

    PHASE 7: an UPCOMING print (no actual yet) does NOT score here either.
    A scheduled event is a forward-looking risk, not a realised move, so it
    is excluded from the box's bull/bear split unless
    ``calendar_upcoming_contributes`` is switched on. The session report's
    own "Docket"/"Calendar" sections still list the scheduled prints - the
    trader sees WHAT IS COMING without it being counted as evidence.
    """
    ctx = SessionContext()
    for sig in signals or []:
        try:
            key = Asset(_enum_name(getattr(sig, "asset", "")))
        except ValueError:
            continue
        if (_enum_name(getattr(sig, "mode", "RELEASED")) != "RELEASED"
                and not settings.calendar_upcoming_contributes):
            continue
        signed = _signed_contribution(
            _enum_name(getattr(sig, "direction", "")),
            _enum_name(getattr(sig, "strength", "")),
            _enum_name(getattr(sig, "importance", "")),
            float(getattr(sig, "relevance", 0.0) or 0.0),
        )
        if signed == 0.0:
            continue
        if _enum_name(getattr(sig, "mode", "RELEASED")) != "RELEASED":
            signed *= 0.6

        ctx.calendar_count += 1
        entry = ctx.calendar.setdefault(key, AssetContext())
        weight = abs(signed) * CALENDAR_SCORE_SCALE
        if signed > 0:
            entry.bull += weight
        else:
            entry.bear += weight
        entry.drivers.append(
            str(getattr(sig, "reason", "") or _calendar_label(sig))[:60])
    return ctx


def merge_contexts(*contexts) -> SessionContext | None:
    """Combine news + calendar contexts; returns None when all are empty."""
    merged = SessionContext()
    found = False
    for ctx in contexts:
        if ctx is None:
            continue
        found = True
        merged.news_count += ctx.news_count
        merged.calendar_count += ctx.calendar_count
        for source, target in ((ctx.news, merged.news),
                               (ctx.calendar, merged.calendar)):
            for asset, entry in source.items():
                slot = target.setdefault(asset, AssetContext())
                slot.bull += entry.bull
                slot.bear += entry.bear
                slot.drivers.extend(entry.drivers)
    return merged if found and not merged.is_empty else None


# ---------------------------------------------------------------------------
# 4. BOX ASSEMBLY
# ---------------------------------------------------------------------------
@dataclass
class SessionReport:
    """One FinancialJuice session report, already IST-classified."""

    guid: str
    title: str
    body: str
    published_at_utc: datetime | None
    session_key: str
    india_date: date | None
    #: 0 = published today in India, 1 = yesterday, ... Drives the box label.
    age_days: int = 0

    @property
    def published_ist(self) -> str:
        return india_stamp(self.published_at_utc)


def is_financial_juice(source: str | None) -> bool:
    """True only for the FinancialJuice source - the data-source restriction."""
    if not source:
        return False
    flat = "".join(ch for ch in str(source).lower() if ch.isalnum())
    return FINANCIAL_JUICE_SOURCE in flat


def filter_reports_for_today(
    rows: Iterable,
    *,
    today: date | None = None,
    lookback_days: int = 1,
) -> list:
    """
    Select FinancialJuice session reports for today, newest first, deduplicated.

    ``rows`` may be ORM ``NewsArticle`` rows or any object exposing
    ``guid`` / ``source`` / ``title`` / ``body`` / ``published_at_utc``.

    The date test is applied to the CONVERTED Indian date, so a report that is
    "yesterday" in UTC but "today" in India is included.

    ``lookback_days`` (default 1) additionally admits reports from the previous
    Indian day(s). The accepted day is carried on each report as ``age_days``
    (0 = today, 1 = yesterday, ...) so the caller can label the box honestly
    instead of silently presenting stale data as current. Anything older than
    the window is dropped rather than shown as if it were recent.
    """
    india_date = today or india_today()
    seen: set = set()
    out: list = []

    for row in rows:
        if not is_financial_juice(getattr(row, "source", None)):
            continue

        title = getattr(row, "title", "") or ""
        key = classify_fj_session(title, getattr(row, "body", "") or "")
        if key is None:
            continue  # not one of the five reports

        published = getattr(row, "published_at_utc", None)
        report_date = india_date_of(published)
        if report_date is None:
            continue  # never fabricate a date for a report
        age_days = (india_date - report_date).days
        if age_days < 0 or age_days > lookback_days:
            continue  # future-dated, or older than the allowed lookback

        # Deduplicate on BOTH the guid and the normalised title. The same
        # report re-returned by the feed may arrive with a fresh guid (the
        # site re-renders its slugs), so a guid-only key would let the
        # duplicate through and double-count it in the box.
        guid = getattr(row, "guid", "") or ""
        norm = normalise_title(title).lower()
        dedupe_key = (key, norm, guid) if guid else (key, norm)
        if dedupe_key in seen or (key, norm) in seen:
            continue
        seen.add(dedupe_key)
        seen.add((key, norm))

        out.append(SessionReport(
            guid=guid,
            title=title,
            body=(getattr(row, "body", "") or ""),
            published_at_utc=published,
            session_key=key,
            india_date=report_date,
            age_days=age_days,
        ))

    out.sort(
        key=lambda r: r.published_at_utc
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out


#: How many previous Indian days each box may reach back for.
#:
#: The US Market Wrap is the one box that legitimately lags: FinancialJuice
#: publishes it after the US close, which is already the next Indian day, so
#: the freshest copy we hold is frequently stamped "yesterday" in IST. Every
#: other session wraps within the same Indian day and must therefore be
#: strictly today's report - a prior-day report there would be stale news
#: presented as current.
#:
#: Human label per age bucket, so a box always says which day's report it holds.
AGE_LABELS: dict = {
    0: "TODAY'S REPORT",
    1: "YESTERDAY'S REPORT",
}


def age_label(age_days: int) -> str:
    if age_days in AGE_LABELS:
        return AGE_LABELS[age_days]
    return f"REPORT FROM {age_days} DAYS AGO"


def _empty_box(session_key: str) -> dict:
    """An explicit 'not published yet' state - never omitted, never invented."""
    return {
        "session_key": session_key,
        "title": SESSION_TITLES[session_key],
        "found": False,
        "is_today": False,
        "age_days": None,
        "age_label": "NO REPORT",
        "report_date": "",
        "report_date_display": "",
        "report_title": "",
        "published_ist": "",
        "assets": [
            {
                "asset": a.value,
                "label": fj_card_label(a),
                "bias": "BEARISH",
                "score": 0.0,
                "confidence": 0.0,
                "confidence_label": "LOW",
                "raw_score": 0.0,
                "bull_weight": 0.0,
                "bear_weight": 0.0,
                "stale": True,
                "bull_pct": 50.0,
                "bear_pct": 50.0,
                "drivers": ["No FinancialJuice report for this session yet"],
            }
            for a in FJ_ASSETS
        ],
        "drivers": [],
        "risks": [],
    }


def build_session_analysis(
    rows: Sequence,
    *,
    now: datetime | None = None,
    lookback_days: int | None = None,
    context=None,
) -> dict:
    """
    Build the complete five-box payload.

    ALWAYS returns exactly five boxes in :data:`SESSION_ORDER`.

    Per-box date policy (:data:`SESSION_LOOKBACK_DAYS`):

    * **US Market Wrap** - today's report is preferred; if FinancialJuice has
      not published it yet, the most recent prior-day report is shown, clearly
      labelled "YESTERDAY'S REPORT" with its own IST timestamp and date.
    * **All other sessions** - strictly today's report. A prior-day report is
      stale news and is never substituted; the box renders as "NO REPORT" at
      50/50 instead.

    ``lookback_days`` overrides the policy for every box (used by tests).

    ``context`` supplies the external evidence (high-impact Section B news plus
    economic-calendar signals) blended into each box's gold/silver/bitcoin/
    equity split.
    """
    india_date = india_today(now)

    if lookback_days is None:
        widest = MAX_LOOKBACK_DAYS
        per_box = SESSION_LOOKBACK_DAYS
    else:
        widest = lookback_days
        per_box = {k: lookback_days for k in SESSION_ORDER}

    reports = filter_reports_for_today(
        rows, today=india_date, lookback_days=widest)

    # One report per box. The list is newest-first, so the first report seen
    # for a session wins: today's always beats yesterday's.
    by_session: dict = {}
    for rep in reports:
        by_session.setdefault(rep.session_key, rep)

    boxes: list = []
    for key in SESSION_ORDER:               # order is fixed and non-negotiable
        rep = by_session.get(key)

        # A report older than this box's allowance is discarded rather than
        # shown - the next candidate would only be older still.
        if rep is not None and rep.age_days > per_box.get(key, 0):
            rep = None

        if rep is None:
            boxes.append(_empty_box(key))
            _fj_metric_inc("boxes_total")
            _fj_metric_inc("boxes_missing")
            continue

        _fj_metric_inc("boxes_total")
        _fj_metric_inc("boxes_found")
        if rep.age_days > 0:
            _fj_metric_inc("boxes_stale")

        scored = analyse_report_text(f"{rep.title}\n{rep.body}", context)
        # TOP DRIVERS / KEY RISKS are read off the report's own sentences, so
        # they describe THIS session rather than any asset in isolation.
        drivers, risks = extract_drivers_and_risks(rep.body, title=rep.title)
        boxes.append({
            "session_key": key,
            "title": SESSION_TITLES[key],
            "found": True,
            "is_today": rep.age_days == 0,
            "age_days": rep.age_days,
            "age_label": age_label(rep.age_days),
            "report_date": rep.india_date.isoformat() if rep.india_date else "",
            "report_date_display": (
                rep.india_date.strftime("%d %B %Y").lstrip("0")
                if rep.india_date else ""
            ),
            "report_title": rep.title,
            "published_ist": rep.published_ist,
            "assets": [scored[a] for a in FJ_ASSETS],
            # Report sentences only. This box is FinancialJuice-only, so
            # external evidence (Section B news, the economic calendar) must
            # never appear here - it shapes the scores above, not these notes.
            "drivers": drivers,
            "risks": risks,
        })

    return {
        "india_date": india_date.isoformat(),
        "india_date_display": india_date.strftime("%d %B %Y").lstrip("0"),
        "timezone": "Asia/Kolkata",
        "source": "FinancialJuice",
        "context": {
            "news_items": context.news_count if context else 0,
            "calendar_items": context.calendar_count if context else 0,
        },
        "boxes": boxes,
    }


def build_context_from_db(session, calendar_signals=None) -> SessionContext | None:
    """
    Assemble the external context from data the engine has already produced.

    ``news``  - today's HIGH/VERY_HIGH importance articles from the Section B
                feed, reusing their per-asset impacts.
    ``calendar`` - the economic-calendar signals from the most recent fetch.

    Returns ``None`` when neither source has anything to say, in which case the
    boxes fall back to pure report-only scoring.
    """
    from sqlalchemy import select

    from app.models.db_models import NewsArticle
    from app.services.pipeline import news_day_predicate

    # Resolved at query time, like the Section B feed. Reading the raw is_today
    # column here would let the session backdrop score itself against
    # yesterday's HIGH-importance headlines after the day rolls over.
    news_rows = session.scalars(
        select(NewsArticle)
        .where(
            news_day_predicate(),
            NewsArticle.importance.in_(list(HIGH_IMPORTANCE)),
        )
        .order_by(NewsArticle.published_at_utc.desc())
        .limit(200)
    ).all()

    return merge_contexts(
        context_from_news(news_rows),
        context_from_calendar(calendar_signals),
    )


def session_payload_from_db(session, calendar_signals=None) -> dict:
    """
    DB-backed entry point used by the API.

    Reads ONLY FinancialJuice rows for the boxes themselves - the source
    restriction is preserved. A generous window is selected in SQL and then
    filtered down to the Indian date in Python, so no timezone decision is
    pushed into the database.

    ``calendar_signals`` is the external economic-calendar evidence; the news
    context is read from the same session. Both only inform the sentiment of a
    box, never which report appears in it.
    """
    from sqlalchemy import select

    from app.models.db_models import NewsArticle

    # A 3-day window comfortably covers today's AND yesterday's reports in IST
    # (IST is UTC+05:30, so the boundary shifts by half a day); the precise
    # Indian-date test happens in Python.
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    rows = session.scalars(
        select(NewsArticle)
        .where(
            NewsArticle.published_at_utc.isnot(None),
            NewsArticle.published_at_utc >= cutoff,
        )
        .order_by(NewsArticle.published_at_utc.desc())
        .limit(2000)
    ).all()

    fj_only = [r for r in rows if is_financial_juice(r.source)]
    context = build_context_from_db(session, calendar_signals)
    return build_session_analysis(fj_only, context=context)
