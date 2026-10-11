"""
SENTIMENT AGGREGATOR (items 2 + 6 + 7 + 10).

WHAT THIS REPLACES
------------------
The old engine ended with:

    label = Direction.BULLISH if net >= 0 else Direction.BEARISH

That forced a directional answer out of EVERY input, including an empty one,
and the forced 100% bull/bear split made a coin-flip look confident.

THE NEW CONTRACT
----------------
    score      -100 .. +100   signed, continuous, honest about magnitude
    confidence   0 .. 1       how much the evidence actually supports it
    bias        BULLISH | BEARISH         (binary - never NO_BIAS / NEUTRAL)

BINARY BIAS CONTRACT
---------------------
The label is the SIGN of the net news score: positive -> BULLISH, negative ->
BEARISH. A score inside +/- EPS (one named constant) has no sign worth
trusting, so the side of the highest-weight, newest directional signal breaks
the tie. When an asset has NO signals in the window the last published bias is
carried forward with ``stale=True`` (history is kept per asset, and per region
for the equity card); with no history at all the newest article's direction is
used, and failing that the documented ``DEFAULT_BIAS`` constant. Confidence is
still computed and reported, but it no longer gates the label: the engine
always names a side, because a day trader cannot act on "I do not know".

FINAL WEIGHT FORMULA (item 6 - one magnitude, one event weight)
-------------------------------------------------------------
    magnitude    = ONE measured cue score + ONE event-type weight
                   (previously counted three times: strength, persistence and
                   importance were all derived from the same cue total)

    decay        = 0.5 ** (age_hours / half_life(category))      <- item 7

    weight       = decay
                 x relevance          (already independent of the cue score)
                 x persistence        (derived from EVENT TYPE, not the cue
                                      total, so it is not a second copy of
                                      magnitude)
                 x source_quality
                 x session_multiplier
                 x corroboration      (one cluster = one vote, item 4)

    contribution = weight x strength_multiplier x direction_sign

    score        = 100 * (bull - bear) / (bull + bear)        -> -100..+100

    confidence   = f(conviction, breadth, agreement, certainty), gated so a
                   thin or conflicted read can never report high confidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import re

from app.analysis.text_match import contains_cue
from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
    ReportKind,
    Verification,
)
from app.utils import timeutils as T
from app.utils.timestamp_parse import age_hours

log = get_logger(__name__)


def _current_market_day_key() -> str:
    """Return the current trading day as a string key (e.g., '2026-10-07')."""
    return T.market_today().isoformat()


def _maybe_reset_bias_history() -> None:
    """Reset _LAST_BIAS and _LAST_FLIP_TIME if the trading day has changed."""
    global _LAST_BIAS_DAY_KEY
    current_day = _current_market_day_key()
    if _LAST_BIAS_DAY_KEY is None:
        _LAST_BIAS_DAY_KEY = current_day
        return
    if _LAST_BIAS_DAY_KEY != current_day:
        log.info("Trading day changed from %s to %s - resetting bias history",
                 _LAST_BIAS_DAY_KEY, current_day)
        _LAST_BIAS.clear()
        _LAST_FLIP_TIME.clear()
        _LAST_BIAS_DAY_KEY = current_day


class Bias(str, Enum):
    """BULLISH/BEARISH are the only verdicts compute() returns (binary bias).

    NO_BIAS is kept strictly for wire- and storage-compatibility (display
    fail-safe mapping, legacy rows, schema defaults) - compute() never
    returns it any more.
    """

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NO_BIAS = "NO_BIAS"


STRENGTH_MULT = {
    ImpactStrength.VERY_HIGH: 1.00,
    ImpactStrength.HIGH: 0.72,
    ImpactStrength.MEDIUM: 0.45,
    ImpactStrength.LOW: 0.20,
}
QUALITY_MULT = {
    "TIER_1_OFFICIAL": 1.00,
    "TIER_2_MAJOR": 0.97,
    "TIER_3_CRYPTO": 0.94,
    "TIER_4_OTHER": 0.90,
}

# Earnings mega-cap name patterns (Item 6).
# Compiled once at module load from settings.
_earnings_mega_cap_names = [n.strip().lower() for n in settings.earnings_mega_cap_names.split(",")]
_EARNINGS_MEGA_CAP_PATTERNS = [
    re.compile(rf"(?<!\w){re.escape(name)}(?!\w)") for name in _earnings_mega_cap_names
]

# ---------------------------------------------------------------------------
# BINARY BIAS (label = sign of the net news score)
# ---------------------------------------------------------------------------
# The ONE named constant for "no trustworthy sign". Inside +/- EPS the tie is
# broken by the side of the highest-weight, newest directional signal instead
# of withholding a verdict. Mirrors the old settings.bias_min_abs_score floor
# (12.0), so borderline calls flip exactly where the NO_BIAS gate used to
# withhold them - the value is owned here, not re-read per call.
EPS = 12.0

# Used only when there are no signals, no history AND no newest article:
# a documented constant, never a silent coin flip. Strictly directional.
DEFAULT_BIAS = Bias.BULLISH

# The ONE lean a tie-break verdict is allowed to show: +/- TIEBREAK_LEAN maps
# to a 51/49 split (bull_pct = round(50 + score / 2)), so a call the engine
# could only reach by breaking a tie reads as a weak call, not a strong one.
TIEBREAK_LEAN = 2.0

# A tie-break is the weakest call the engine can make, so its confidence is
# capped low: the payload keeps saying "weak" next to the label.
TIEBREAK_MAX_CONFIDENCE = 0.30

# Minimum total directional weight (bull + bear) to produce a directional
# verdict. Below this the evidence is too thin and the engine returns NO_BIAS.
MIN_TOTAL_WEIGHT = 0.15

# Minimum confidence before a computed flip is allowed (Item 5).
# When confidence is below this, a flip is refused and the previous bias is held.
FLIP_MIN_CONFIDENCE = 0.35


def confidence_label_for(conf: float, low_quality: bool) -> str:
    """
    Map confidence to a label.

    LOW   if low_quality (bias held or tie-broken) or conf < 0.35
    MEDIUM if conf < 0.55
    HIGH  otherwise
    """
    if low_quality or conf < 0.35:
        return "LOW"
    if conf < 0.55:
        return "MEDIUM"
    return "HIGH"


def payload_score(raw_score: float, bias) -> float:
    """
    The score a payload may show for ``bias``.

    Normally that is the raw net score, unchanged. The exception is the
    tie-break case (|raw_score| < EPS) where the label was taken from the
    heaviest, newest signal and therefore DISAGREES with the raw sign:
    publishing that raw score would draw a bar leaning one way under a badge
    naming the other (score +2.8 / BEARISH / bullish_pct 51). The score is
    then replaced by a small lean of the label's sign, which renders about
    51/49 toward the label. A score whose sign already agrees is left alone
    however small it is, and a score at or beyond EPS is never touched. The
    raw value survives on ``raw_score`` for logging only, never in the payload.
    """
    name = getattr(bias, "value", bias)
    if abs(raw_score) >= EPS:
        return float(raw_score)
    if name == "BULLISH" and raw_score < 0:
        return TIEBREAK_LEAN
    if name == "BEARISH" and raw_score > 0:
        return -TIEBREAK_LEAN
    return float(raw_score)

# Task 8: stability (no whipsaw) constants.
from app.config import FLIP_HOLD_MINUTES, FLIP_MARGIN

# IMPROVEMENT 6: anti-whipsaw cooling period after a flip.
# After a bias flips, the market needs COOLING_MINUTES before another flip is
# allowed, and during that window the score must clear by COOLING_MARGIN (25,
# wider than the normal FLIP_MARGIN of 15) so a re-test of the old level does
# not immediately flip it back. The Tier-1 override still bypasses this
# entirely - it is checked first in _should_flip() and returns immediately.
COOLING_MINUTES = 30
COOLING_MARGIN = 25

# Last published bias per state key: the asset symbol ("GOLD" ...) or
# "EQUITY:<region>" for the session-aware equity card, so a signal-free poll
# can carry the previous verdict forward instead of inventing one.
_LAST_BIAS: dict[str, Bias] = {}

# Last flip timestamp per state key (for FLIP_HOLD_MINUTES enforcement).
_LAST_FLIP_TIME: dict[str, datetime] = {}

# Day key for the current trading day (market timezone date).
# Used to auto-reset bias history at the start of each new trading day.
_LAST_BIAS_DAY_KEY: str | None = None


def remember_bias(key: str, bias: Bias) -> None:
    """Record a published bias under ``key`` for later carry-forward."""
    _LAST_BIAS[key] = bias


def recall_bias(key: str) -> Bias | None:
    """The last bias published under ``key``; None when there is no history."""
    return _LAST_BIAS.get(key)


def _recall_flip_time(key: str) -> datetime | None:
    """The last time a bias flip occurred for this key."""
    return _LAST_FLIP_TIME.get(key)


def _record_flip_time(key: str, now: datetime) -> None:
    """Record a bias flip time for this key."""
    _LAST_FLIP_TIME[key] = now


def clear_bias_history() -> None:
    """Drop all carry-forward history (test isolation / cold restarts)."""
    _LAST_BIAS.clear()
    _LAST_FLIP_TIME.clear()


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """Force a datetime to aware UTC.

    SQLite strips ``tzinfo`` on write and read, so a ``published_at`` that was
    stored aware comes back naive. Subtracting a naive value from an aware
    ``now`` raises TypeError. This helper makes every comparison/subtraction
    safe regardless of what the database handed back.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _has_fresh_tier1_opposite(relevant: Sequence[WeightedSignal],
                               current_bias: Bias,
                               now: datetime | None) -> bool:
    """
    Check if there's a fresh Tier 1 signal pointing opposite to current bias.

    A "fresh" Tier 1 signal is one published within the last hour.
    """
    if now is None:
        return False
    cutoff = now - timedelta(hours=1)
    for s in relevant:
        if s.published_at is None or _ensure_aware(s.published_at) < cutoff:
            continue
        if event_tier(s) != "TIER_1":
            continue
        # Check if this signal points opposite to current bias
        signal_bias = _directional_bias(s.direction)
        if signal_bias is not None and signal_bias != current_bias:
            return True
    return False


def _should_flip(bull: float, bear: float, current_bias: Bias,
                 relevant: Sequence[WeightedSignal],
                 now: datetime | None,
                 key: str,
                 flip_margin_override: float | None = None) -> tuple[bool, Bias]:
    """
    Determine if bias should flip based on stability rules.

    ``flip_margin_override`` widens the required score margin when a
    contradiction has been detected (both sides heavily weighted): the
    caller passes ``FLIP_MARGIN + 10`` in that case so a genuinely torn
    market cannot be flipped on noise. When None the module FLIP_MARGIN is
    used, unless a cooling period is active (IMPROVEMENT 6) in which case
    COOLING_MARGIN applies instead.

    Order of gates, earliest first:
      1. No current bias (first run) -> take the computed bias.
      2. No weight at all -> hold.
      3. Fresh Tier 1 opposite signal -> flip immediately, bypasses
         everything below including the cooling period.
      4. Cooling period after a recent flip -> wider effective margin.
      5. Hold-time gate -> refuse to flip again inside FLIP_HOLD_MINUTES.
      6. Margin gate -> flip only when the score cleared by the margin.
    """
    # No current bias (first run) - just use the computed bias
    if current_bias is None:
        return True, Bias.BULLISH if bull > bear else Bias.BEARISH

    # Compute the score from current bull/bear weights
    total = bull + bear
    if total <= 0:
        return False, current_bias
    current_score = 100.0 * (bull - bear) / total

    # Check for fresh Tier 1 opposite signal (forces flip regardless of
    # margin/hold). This bypasses the cooling period below on purpose: a
    # confirmed official release is the one event that can flip immediately.
    if _has_fresh_tier1_opposite(relevant, current_bias, now):
        return True, Bias.BEARISH if current_bias is Bias.BULLISH else Bias.BULLISH

    # IMPROVEMENT 6: anti-whipsaw cooling period after a flip.
    # After a bias flips, the market needs COOLING_MINUTES before another
    # flip is allowed, and during that window the score must clear by
    # COOLING_MARGIN (25, wider than the normal FLIP_MARGIN of 15) so a
    # re-test of the old level does not immediately flip it back. The
    # Tier-1 override above still bypasses this entirely - it returned
    # before reaching this point.
    last_flip = _recall_flip_time(key)
    in_cooling = (
        last_flip is not None and
        now is not None and
        (now - last_flip).total_seconds() < COOLING_MINUTES * 60
    )

    if in_cooling:
        effective_margin = COOLING_MARGIN
    else:
        effective_margin = (flip_margin_override if flip_margin_override
                            is not None else FLIP_MARGIN)

    # Check hold time (skip for Tier 1 - already handled above)
    if last_flip is not None and now is not None:
        hold_elapsed = (now - last_flip).total_seconds() / 60.0
        if hold_elapsed < FLIP_HOLD_MINUTES:
            return False, current_bias

    # Check margin: score must have crossed zero by at least the margin.
    # The margin is on the score scale (-100..+100).
    if current_bias is Bias.BULLISH:
        if current_score <= -effective_margin:
            return True, Bias.BEARISH
    else:
        if current_score >= effective_margin:
            return True, Bias.BULLISH

    return False, current_bias


@dataclass
class WeightedSignal:
    """One event's contribution to one asset."""

    asset: Asset
    title: str
    direction: Direction
    strength: ImpactStrength = ImpactStrength.MEDIUM
    importance: EconomicImportance = EconomicImportance.MEDIUM
    published_at: datetime | None = None
    relevance: float = 0.0
    persistence: float = 0.5
    source_quality: str = "TIER_2_MAJOR"
    session_multiplier: float = 1.0
    is_primary: bool = True
    event_key: str = ""
    cluster_id: str = ""
    corroboration: float = 1.0
    category: NewsCategory = NewsCategory.OTHER
    reason: str = ""
    transmission: str = ""
    report_kind: ReportKind = ReportKind.GENERAL
    mode: str = "heuristic"
    # Task 3: surprise-size multiplier for ACTUAL scheduled-data reports
    # (release_surprise.surprise_weight). Constant 1.0 for every other
    # signal, so no existing behaviour changes.
    surprise_weight: float = 1.0
    # Section 7: silver's dual nature. PRECIOUS/INDUSTRIAL/BOTH/null, carried
    # through so the weight path can reward a characterised silver story.
    metal_character: str | None = None

    # filled by compute()
    recency: float = 0.0
    final_weight: float = 0.0
    magnitude: float = 0.0
    contribution: float = 0.0


@dataclass
class Driver:
    """One headline's contribution, for the manual trader's audit trail."""

    title: str
    contribution: float
    direction: str
    weight: float


@dataclass
class SentimentResult:
    """One asset's verdict. Always directional (BULLISH or BEARISH)."""

    asset: Asset
    score: float = 0.0
    # The pre-clamp net score, for logging/audit only: `score` is what the
    # payload shows, so a tie-break can never contradict its own badge.
    raw_score: float = 0.0
    # Dampened score for display/cue strength. Computed as
    # raw * total_weight / (total_weight + k). Raw score is used for
    # bias labels, flip margin and stability-hold logic.
    damped_score: float = 0.0
    confidence: float = 0.0
    bias: Bias = Bias.BULLISH
    mode: str = "heuristic"
    drivers: list[Driver] = field(default_factory=list)
    conflict: Driver | None = None
    # Both sides, each capped at 3, for the dashboard's side-by-side boxes.
    # Additive only: `drivers` above still carries the single leading side and
    # `conflict` the single strongest opposing headline, so the decision log
    # and hit-rate tooling are unaffected.
    bull_drivers: list[Driver] = field(default_factory=list)
    bear_drivers: list[Driver] = field(default_factory=list)
    delta_60m: float = 0.0
    event_risk: bool = False
    event_risk_note: str = ""
    contributing_articles: int = 0
    contributing_clusters: int = 0
    # True when there were no signals and the bias was carried forward from
    # history (or from the newest article / DEFAULT_BIAS when there is none).
    stale: bool = False
    # Number of signals that reached this asset in the window. 0 on a stale
    # (carried-forward) verdict - the point of stale is that nothing new was
    # seen. Mirrors contributing_articles on a fresh verdict.
    signal_count: int = 0
    # True when both sides are heavily weighted (bull > 80 and bear > 80):
    # the news is genuinely torn, so a flip is held to a wider margin.
    contradiction_detected: bool = False
    # News-velocity burst counts in the last 15 minutes: how many same-side
    # headlines landed together. 0 on a stale verdict. Exposed on the
    # dashboard so a reader can see the momentum behind the score.
    burst_bull: int = 0
    burst_bear: int = 0
    note: str = ""
    confidence_label: str = "LOW"


def decay_weight(published_at: datetime | None, tier: str,
                 now: datetime | None = None) -> float:
    """
    Exponential decay with a per-event-tier half-life (Task 5a).

    Replaces the per-category half-life. Tier 1 ~6h, Tier 2 ~2h, Tier 3 ~1h.
    ``weight = 0.5 ** (age_hours / half_life)``.
    Items older than TRADING_DAY_CUTOFF_HOURS get weight 0.
    """
    from app.config import TIER_HALF_LIVES, TRADING_DAY_CUTOFF_HOURS

    half_life = TIER_HALF_LIVES.get(tier, 2.0)
    half_life = max(0.25, float(half_life))

    if published_at is None:
        return 0.15
    hours = _age_hours(published_at, now)
    if hours < 0:
        return 1.0  # within clock skew: treat as brand new
    if hours >= TRADING_DAY_CUTOFF_HOURS:
        return 0.0
    return max(0.0, min(1.0, 0.5 ** (hours / half_life)))


def _age_hours(published_at: datetime, now: datetime | None = None) -> float:
    # Coerce a naive ``now`` (the in-process default from ``datetime.utcnow()``)
    # to aware UTC before subtracting: ``published_at`` is stored as aware, so
    # subtracting two naive values raised TypeError whenever a caller passed
    # an aware clock and the default kicked in.
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return age_hours(published_at, now=now) or 0.0


def _now_aware(now: datetime | None) -> datetime:
    """The effective clock for this tick, always aware UTC.

    When ``now`` is not supplied the default is taken from
    ``app.utils.timestamp_parse.datetime`` rather than this module's own
    ``datetime``: the test suite monkeypatches that module's clock, and the
    old ``datetime.utcnow()`` default bypassed it, which aged every test
    signal past the trading-day cutoff and silently zeroed the score.
    """
    if now is None:
        from app.utils import timestamp_parse as tp
        now = tp.datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def event_tier(s: WeightedSignal) -> str:
    """Tier key from the EVENT (importance + category + report_kind).

    TIER_1: VERY_HIGH importance, ACTUAL_DATA for CPI/PPI/PCE/NFP/FOMC/rate
      decisions, confirmed geopolitical shocks, ETF flow data.
    TIER_2: central-bank speeches / secondary data / official statements.
    TIER_3: analyst opinion / previews / commentary / forecasts.
    Reads existing fields only; no new classifier lives here.
    """
    if s.importance is EconomicImportance.VERY_HIGH:
        return "TIER_1"
    if s.report_kind is ReportKind.ACTUAL:
        return "TIER_1"
    if s.category in (NewsCategory.BITCOIN_ETF_FLOWS, NewsCategory.ETF_FLOWS):
        return "TIER_1"
    if s.category is NewsCategory.GEOPOLITICS:
        return ("TIER_1" if s.importance is EconomicImportance.HIGH
                else "TIER_2")
    title = (s.title or "").lower()
    
    # Result words that indicate a factual data release (beats/misses) - if present,
    # "forecast"/"forecasts" should NOT be treated as opinion words.
    result_words = ("beats", "beat", "misses", "missed", "miss", "tops", "topped",
                    "exceeds", "exceeded", "falls short", "fall short", "below", "above",
                    "than expected")
    has_result_word = any(contains_cue(title, w) for w in result_words)
    
    opinion_words = ("analyst", "opinion", "preview", "commentary",
                     "forecast", "comment", "outlook")
    if has_result_word:
        # Ignore "forecast"/"forecasts" for is_opinion test only
        opinion_words = tuple(w for w in opinion_words if w not in ("forecast", "forecasts"))
    
    is_opinion = any(contains_cue(title, k) for k in opinion_words)
    if is_opinion or s.report_kind is ReportKind.PREP:
        return "TIER_3"
    if s.importance in (EconomicImportance.HIGH, EconomicImportance.MEDIUM):
        return "TIER_2"
    return "TIER_3"


def _signal_weight(s: WeightedSignal, now: datetime | None) -> float:
    """The single final weight for one signal.

    FINAL WEIGHT FORMULA (Task 4b + 5a):
        weight = decay * relevance * persistence * source_quality
               * session * corroboration * surprise * tier(event)
        where tier(event) = TIER_MULTIPLIERS[event_tier(s)]
        (possibly x RUMOR_MULTIPLIER / CONFIRMED_BOOST).
        decay = 0.5 ** (age_hours / TIER_HALF_LIVES[tier]),
        tier = event_tier(s) in {TIER_1, TIER_2, TIER_3},
        age_hours >= TRADING_DAY_CUTOFF_HOURS -> weight = 0.
    Importance feeds the tier once via event_tier() and is never
    multiplied separately here. Source quality (QUALITY_MULT, 0.9..1.0)
    is an independent trust factor, never the tier.
    """
    # Coerce a naive ``now`` to aware UTC: ``published_at`` is stored aware,
    # so a naive clock made the age subtraction raise TypeError and silently
    # skipped the freshness weighting below.
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    from app.analysis.cue_rules import detect_verification
    from app.analysis.release_surprise import MAX_SURPRISE_WEIGHT
    from app.analysis.transmission_map import detect_event_types, transmission
    from app.config import (
        CONFIRMED_BOOST,
        RUMOR_MULTIPLIER,
        TIER_MULTIPLIERS,
    )

    # Task 5a: decay uses event tier half-life (TIER_1~6h, TIER_2~2h, TIER_3~1h),
    # with TRADING_DAY_CUTOFF_HOURS (16h) giving weight 0.
    tier_key = event_tier(s)
    decay = decay_weight(s.published_at, tier_key, now)
    rel = max(0.0, min(1.0, s.relevance))
    per = max(0.05, min(1.0, s.persistence))
    qual = QUALITY_MULT.get((s.source_quality or "").upper(), 0.90)
    if s.category is NewsCategory.BITCOIN_ETF_FLOWS:
        qual = 1.0
    sess = max(0.5, min(1.5, s.session_multiplier))
    corr = max(1.0, float(s.corroboration or 1.0))
    surprise = max(0.0, min(MAX_SURPRISE_WEIGHT, float(s.surprise_weight or 0.0)))

    # Section 7: silver's dual nature. A characterised XAGUSD story earns a
    # small boost on top of everything else: PRECIOUS and INDUSTRIAL each
    # multiply by 1.10, BOTH (the headline drives both channels at once)
    # stays at 1.0, and an uncharacterised item (null) is unchanged.
    metal_mult = 1.0
    if s.asset is Asset.SILVER and s.metal_character:
        metal_mult = 1.10 if s.metal_character in ("PRECIOUS", "INDUSTRIAL") else 1.0

    # Task 4b: the tier comes from the EVENT (event_tier above), never from
    # the outlet. QUALITY_TO_TIER is gone; importance feeds the tier once.
    tier = TIER_MULTIPLIERS.get(event_tier(s), 0.45)
    # Task 3: verification comes from the headline. Unverified/rumor items
    # (e.g. "may", "reportedly", "sources say") weigh less; confirmed items
    # are slightly boosted. Named multipliers, one application each.
    if detect_verification(s.title) is Verification.UNVERIFIED:
        tier *= RUMOR_MULTIPLIER
    elif detect_verification(s.title) is Verification.CONFIRMED:
        tier *= CONFIRMED_BOOST

    # Earnings mega-cap / unnamed multiplier (Item 6).
    # Detect event types from the headline and category.
    earnings_event_types = {"EARNINGS_BEAT", "EARNINGS_MISS", "GUIDANCE_RAISED", "GUIDANCE_CUT"}
    flow_event_types = {"BTC_ETF_FLOW", "GOLD_ETF_FLOW"}
    detected = detect_event_types(s.title, s.category)
    earnings_mult = 1.0
    if any(et.value in earnings_event_types for et in detected):
        lowered = (s.title or "").lower()
        if any(p.search(lowered) for p in _EARNINGS_MEGA_CAP_PATTERNS):
            earnings_mult = settings.earnings_mega_cap_mult
        else:
            earnings_mult = settings.earnings_unnamed_mult

    # Asset sensitivity based on transmission map magnitude.
    # ev = detect_event_types(s.title, s.category); if empty -> 1.0.
    # m = transmission(ev[0], s.asset).magnitude.
    # asset_sensitivity = min(1.0, max(0.3, m)).
    # Skip (use 1.0) ONLY for EARNINGS_BEAT, EARNINGS_MISS,
    # GUIDANCE_RAISED, GUIDANCE_CUT, BTC_ETF_FLOW and GOLD_ETF_FLOW.
    # Events with sign 0 (GENERAL_MACRO and similar) must never be scaled.
    flow_event_types = {"BTC_ETF_FLOW", "GOLD_ETF_FLOW"}
    asset_sensitivity = 1.0
    if not any(et.value in earnings_event_types for et in detected) \
       and not any(et.value in flow_event_types for et in detected) \
       and detected:
        # Look at all detected events with non-zero sign, take max magnitude
        max_m = 0.0
        for et in detected:
            entry = transmission(et, s.asset)
            if entry.sign != 0 and entry.magnitude > 0:
                if entry.magnitude > max_m:
                    max_m = entry.magnitude
        if max_m > 0:
            asset_sensitivity = min(1.0, max(0.3, max_m))

    return decay * rel * per * qual * sess * corr * surprise * tier * metal_mult * earnings_mult * asset_sensitivity


def _mode_for(signals: Sequence[WeightedSignal]) -> str:
    modes = {s.mode for s in signals if s.mode}
    if not modes:
        return "heuristic"
    if len(modes) == 1:
        return modes.pop()
    return "mixed"


def _aggregate(signals: Sequence[WeightedSignal],
               now: datetime | None = None) -> dict:
    """Shared weighted-sum core. Returns raw totals and per-side items."""
    bull = bear = directionless = 0.0
    bull_items: list[tuple[float, WeightedSignal]] = []
    bear_items: list[tuple[float, WeightedSignal]] = []
    used = 0
    clusters: set[str] = set()

    if now is None:
        now = _now_aware(None)

    for s in signals:
        weight = _signal_weight(s, now)
        s.recency = weight
        s.final_weight = weight
        if weight <= 0:
            continue

        magnitude = weight * STRENGTH_MULT.get(s.strength, 0.45)
        s.magnitude = magnitude
        if s.direction is Direction.BULLISH:
            sign = 1.0
        elif s.direction is Direction.BEARISH:
            sign = -1.0
        else:
            sign = 0.0
        s.contribution = sign * magnitude

        if sign > 0:
            bull += magnitude
            bull_items.append((magnitude, s))
        elif sign < 0:
            bear += magnitude
            bear_items.append((magnitude, s))
        else:
            # An item that takes no side is never forced onto one. It counts
            # against conviction instead; MIXED counts double.
            directionless += magnitude * (1.4 if s.direction is Direction.MIXED
                                          else 1.0)
        if s.cluster_id:
            clusters.add(s.cluster_id)
        used += 1

    # --- News velocity burst bonus (IMPROVEMENT 1) ---
    # A cluster of same-direction headlines in the last 15 minutes is a
    # momentum cue: multiply the side's total so it can punch through a
    # marginal FLIP_MARGIN. Conflicting bursts (both sides >= 2 in the
    # window) cancel the bonus entirely - that is a tug-of-war, not a trend.
    burst_signals = [s for s in signals
                     if s.published_at is not None
                     and (now - _ensure_aware(s.published_at)).total_seconds() <= 900]
    burst_bull = sum(1 for s in burst_signals
                     if s.direction is Direction.BULLISH)
    burst_bear = sum(1 for s in burst_signals
                     if s.direction is Direction.BEARISH)
    conflicting_burst = burst_bull >= 2 and burst_bear >= 2

    if not conflicting_burst:
        if burst_bull >= 5 and burst_bull > burst_bear:
            bull *= 1.30
        elif burst_bull >= 3 and burst_bull > burst_bear:
            bull *= 1.20
        elif burst_bear >= 5 and burst_bear > burst_bull:
            bear *= 1.30
        elif burst_bear >= 3 and burst_bear > burst_bull:
            bear *= 1.20

    return {"bull": bull, "bear": bear, "directionless": directionless,
            "bull_items": bull_items, "bear_items": bear_items,
            "used": used, "clusters": len(clusters),
            "burst_bull": burst_bull, "burst_bear": burst_bear,
            "conflicting_burst": conflicting_burst}


def _score_from(bull: float, bear: float) -> float:
    """Map the two weighted totals onto a signed -100..+100 score."""
    total = bull + bear
    if total <= 0:
        return 0.0
    return round(100.0 * (bull - bear) / total, 1)


def _damped_score(raw_score: float, total_weight: float) -> float:
    """
    Apply dampening to the raw score based on total evidence weight.

    Formula: damped = raw * total_weight / (total_weight + k)
    
    When total_weight is 0, returns 0.
    When k=0, returns raw (no dampening).
    As total_weight increases, damped approaches raw.
    
    The raw score is preserved for bias labels, flip margin and stability-hold
    logic. The damped score is for display and cue strength only.
    """
    k = getattr(settings, "score_damping_k", 0.5)
    if total_weight <= 0:
        return 0.0
    if k <= 0:
        return raw_score
    return round(raw_score * total_weight / (total_weight + k), 1)


def _confidence_from(bull: float, bear: float, directionless: float,
                     used: int) -> float:
    """
    How much the evidence supports the reading.

    Blends conviction (how one-sided), breadth (how many distinct events),
    agreement (how much the two sides fight) and certainty (how much of the
    input was directional at all), then clamps.
    """
    directional = bull + bear
    if directional <= 0:
        return 0.0

    net = abs(bull - bear) / directional
    conviction = min(1.0, net / 0.6)
    breadth = min(1.0, used / 8.0)
    agreement = 1.0 - (2.0 * min(bull, bear) / directional)
    total_flow = directional + directionless
    certainty = directional / total_flow if total_flow > 0 else 0.0

    confidence = (0.40 * conviction + 0.28 * breadth + 0.22 * agreement) * certainty
    return round(max(0.0, min(0.95, confidence)), 3)


def _directional_bias(direction: Direction | None) -> Bias | None:
    """BULLISH/BEARISH map to themselves; MIXED/NEUTRAL/None have no side."""
    if direction is Direction.BULLISH:
        return Bias.BULLISH
    if direction is Direction.BEARISH:
        return Bias.BEARISH
    return None


def _tie_break_bias(signals: Sequence[WeightedSignal],
                    history_key: str | None = None,
                    now: datetime | None = None) -> Bias:
    """
    The |score| < EPS tie-break: the side of the highest-weight, NEWEST signal.

    "Weight" is the signal's magnitude (final weight x strength) - how much it
    actually moved the net score - and publication time breaks equal magnitudes
    in favour of the fresher headline.

    ALWAYS returns BULLISH or BEARISH - never None, NEUTRAL, or MIXED:
      1. Directional signals exist -> highest weight wins; equal weight is
         broken by the newest published_at.
      2. No directional signals  -> carry forward recall_bias(history_key).
      3. No history at all       -> DEFAULT_BIAS (BULLISH).
    """
    best: WeightedSignal | None = None
    best_key: tuple[float, float] | None = None
    for s in signals:
        if _directional_bias(s.direction) is None:
            continue
        weight = _signal_weight(s, now)
        magnitude = weight * STRENGTH_MULT.get(s.strength, 0.45)
        ts = _ensure_aware(s.published_at).timestamp() if s.published_at else 0.0
        key = (magnitude, ts)
        if best_key is None or key > best_key:
            best, best_key = s, key
    if best is not None:
        return _directional_bias(best.direction) or DEFAULT_BIAS
    # No directional signal: carry forward the last published bias for this
    # key instead of inventing one. DEFAULT_BIAS when there is no history.
    if history_key is not None:
        carried = recall_bias(history_key)
        if carried is not None:
            return carried
    return DEFAULT_BIAS


def _carry_forward_bias(signals: Sequence[WeightedSignal],
                        history_key: str) -> tuple[Bias, str]:
    """
    No signals for this asset: reuse the last published bias (stale=True).

    History first - that is the verdict the trader last saw. With no history
    at all, the newest article's direction; failing that, DEFAULT_BIAS.
    """
    last = recall_bias(history_key)
    if last is not None:
        return (last,
                "No news in the window; carrying forward the last published "
                "bias (stale).")
    newest: WeightedSignal | None = None
    newest_ts = 0.0
    for s in signals:
        if _directional_bias(s.direction) is None:
            continue
        ts = _ensure_aware(s.published_at).timestamp() if s.published_at else 0.0
        if newest is None or ts > newest_ts:
            newest, newest_ts = s, ts
    if newest is not None:
        return (_directional_bias(newest.direction) or DEFAULT_BIAS,
                "No history for this asset; direction taken from the newest "
                "article (stale).")
    return (DEFAULT_BIAS,
            "No news and no history; using DEFAULT_BIAS (stale).")


def _top_drivers(items: Sequence[tuple[float, WeightedSignal]],
                 limit: int = 3) -> list[Driver]:
    """Top N drivers, strongest first, each with its signed contribution."""
    out = [Driver(title=s.title, contribution=round(magnitude, 4),
                  direction=s.direction.value, weight=round(s.final_weight, 4))
           for magnitude, s in items]
    out.sort(key=lambda d: abs(d.contribution), reverse=True)
    return out[:limit]


def _strongest_conflict(bull_items: Sequence[tuple[float, WeightedSignal]],
                        bear_items: Sequence[tuple[float, WeightedSignal]]) -> Driver | None:
    """The single opposing headline the trader most needs to know about."""
    if not bull_items or not bear_items:
        return None
    # Whichever side is currently losing, name its biggest driver. Compare on
    # magnitude only - a tie must fall back to the signal, never to the tuple
    # comparison, which would try to order the dataclasses themselves.
    if bull_items[0][0] <= bear_items[0][0]:
        candidates = bull_items
    else:
        candidates = bear_items
    magnitude, s = max(candidates, key=lambda x: x[0])
    return Driver(title=s.title, contribution=round(-magnitude, 4),
                  direction=s.direction.value, weight=round(s.final_weight, 4))


def _delta_60m(signals: Sequence[WeightedSignal], asset: Asset,
               now: datetime | None) -> float:
    """
    How much the score MOVED because of the last hour of news (item 10).

    The score an hour ago is the reading computed from every signal EXCEPT the
    ones published inside the recent window. The delta is the difference, so a
    trader can see at a glance whether the current bias was just made or has
    been standing all session.
    """
    from app.config import settings

    if now is None:
        return 0.0
    cutoff = now - timedelta(minutes=int(settings.delta_window_minutes))
    older = [s for s in signals
             if s.asset is asset and not (s.published_at is not None
                                         and _ensure_aware(s.published_at) >= cutoff)]

    prior = _aggregate(older, now)
    prior_score = _score_from(prior["bull"], prior["bear"])
    current = _aggregate([s for s in signals if s.asset is asset], now)
    current_score = _score_from(current["bull"], current["bear"])
    if prior["used"] == 0:
        # Nothing outside the window means there is no earlier reading at all:
        # the whole current score was made in the last hour.
        return current_score
    return round(current_score - prior_score, 1)


def compute(signals: Sequence[WeightedSignal], asset: Asset,
            now: datetime | None = None,
            event_risk: bool = False,
            event_risk_note: str = "",
            history_key: str | None = None) -> SentimentResult:
    """
    Aggregate every weighted signal for one asset into a verdict.

    BINARY BIAS: the label is the sign of the net news score - positive
    BULLISH, negative BEARISH - with the |score| < EPS tie broken by the
    heaviest, newest directional signal. No signals at all carries the last
    published bias forward (stale=True), falling back to the newest article's
    direction and then DEFAULT_BIAS. Never returns NO_BIAS / NEUTRAL.

    STABILITY (Task 8): bias only flips if (a) opposite side exceeds current
    by FLIP_MARGIN, OR (b) a fresh Tier 1 item points the other way.
    Minimum hold time FLIP_HOLD_MINUTES between flips (except Tier 1).
    When bias is held, payload score sign matches label (via payload_score),
    confidence stays low for held-but-weak calls.

    ``history_key`` namespaces the carry-forward state; it defaults to the
    asset symbol and is set to "EQUITY:<region>" by the equity card so a
    proxy scoring run cannot pollute an asset's own history.
    """
    _maybe_reset_bias_history()
    key = history_key or asset.value
    relevant = [s for s in signals if s.asset is asset]
    mode = _mode_for(relevant)

    if not relevant:
        bias, note = _carry_forward_bias(signals, key)
        remember_bias(key, bias)
        return SentimentResult(
            asset=asset, score=0.0, confidence=0.0, bias=bias,
            mode=mode, contributing_articles=0, contributing_clusters=0,
            event_risk=event_risk, event_risk_note=event_risk_note,
            stale=True, signal_count=0,
            burst_bull=0, burst_bear=0,
            note=note, damped_score=0.0,
            confidence_label="LOW",
        )

    agg = _aggregate(relevant, now)
    bull, bear = agg["bull"], agg["bear"]
    bull_items, bear_items = agg["bull_items"], agg["bear_items"]

    score = _score_from(bull, bear)
    confidence = _confidence_from(bull, bear, agg["directionless"], agg["used"])

    # --- High contradiction holds bias stronger (IMPROVEMENT 2) ---
    # When both sides are heavily weighted the news is genuinely torn, so a
    # flip should be harder: the score must cross by an extra 10 points.
    contradiction_detected = (bull > 80.0 and bear > 80.0)
    effective_flip_margin = FLIP_MARGIN + (10 if contradiction_detected else 0)

    # Total directional weight for dampening
    total_weight = bull + bear

    # NO_BIAS gate: total weight below minimum returns a binary bias without
    # writing to remember_bias or overwriting the last real bias.
    if total_weight < MIN_TOTAL_WEIGHT:
        # Compute drivers for the early return (same logic as below)
        leading_for_min = bull_items if (bull > bear) else bear_items
        drivers_early = _top_drivers(leading_for_min)
        conflict_early = _strongest_conflict(bull_items, bear_items)
        bull_drivers_early = _top_drivers(bull_items)
        bear_drivers_early = _top_drivers(bear_items)
        # Determine binary bias: stronger side wins; if tied, use history or tie-break
        if bull > bear:
            weak_bias = Bias.BULLISH
            is_stale = False
            stale_note = "Weak evidence: total weight below minimum"
        elif bear > bull:
            weak_bias = Bias.BEARISH
            is_stale = False
            stale_note = "Weak evidence: total weight below minimum"
        else:
            carried = recall_bias(key)
            weak_bias = carried if carried is not None else _tie_break_bias(relevant, history_key=key, now=now)
            is_stale = True
            stale_note = "No directional evidence; label carried over (stale)"
        return SentimentResult(
            asset=asset, score=0.0, raw_score=0.0, damped_score=0.0, confidence=0.0,
            bias=weak_bias, mode=mode,
            drivers=drivers_early, conflict=conflict_early,
            bull_drivers=bull_drivers_early, bear_drivers=bear_drivers_early,
            delta_60m=_delta_60m(list(signals), asset, now),
            event_risk=event_risk, event_risk_note=event_risk_note,
            contributing_articles=agg["used"], contributing_clusters=agg["clusters"],
            stale=is_stale, signal_count=len(relevant),
            burst_bull=agg["burst_bull"], burst_bear=agg["burst_bear"],
            note=stale_note,
            contradiction_detected=contradiction_detected,
            confidence_label="LOW",
        )

    # BINARY BIAS: label = sign of the net news score. Inside EPS the sign is
    # not trustworthy, so the heaviest, newest directional signal breaks the
    # tie. Confidence is reported but never gates the label.
    raw = score
    if abs(raw) >= EPS:
        computed_bias = Bias.BULLISH if raw > 0 else Bias.BEARISH
    else:
        # Item 5: low confidence - if confidence is too low, we still produce
        # a binary call via tie-break but cap confidence and mark LOW quality.
        if confidence < FLIP_MIN_CONFIDENCE:
            confidence = min(confidence, TIEBREAK_MAX_CONFIDENCE)
        computed_bias = _tie_break_bias(relevant, history_key=key, now=now)
        # A tie-break is a weak call: cap the confidence low so the payload
        # still reads as weak once the label is forced to name a side.
        confidence = min(confidence, TIEBREAK_MAX_CONFIDENCE)

    # Dampened score for display/cue strength (raw used for bias/flip/stability)
    damped = _damped_score(raw, total_weight)

    # Drivers are reported on the side the CURRENT SIGNALS point to (computed_bias),
    # not the held bias. This ensures drivers reflect what the news is saying now.
    # The bull_drivers/bear_drivers always show both sides for the dashboard.
    leading = bull_items if computed_bias is Bias.BULLISH else bear_items
    drivers = _top_drivers(leading)
    conflict = _strongest_conflict(bull_items, bear_items)
    # Both sides kept separately for the dashboard, which shows a bullish box
    # and a bearish box side by side rather than only the leading side.
    bull_drivers = _top_drivers(bull_items)
    bear_drivers = _top_drivers(bear_items)

    # STABILITY (Task 8): check if we should flip from the last published bias
    last_bias = recall_bias(key)
    should_flip, flipped_bias = _should_flip(
        bull, bear, last_bias, relevant, now, key,
        flip_margin_override=effective_flip_margin)

    low_quality = False

    if not should_flip and last_bias is not None:
        # Hold the previous bias - payload score must match the held label
        final_bias = last_bias
        # Use payload_score to ensure score sign matches the held label
        score = payload_score(raw, final_bias)
        # Held-but-weak call: cap confidence low
        confidence = min(confidence, TIEBREAK_MAX_CONFIDENCE)
        note = f"Bias held (stability): {last_bias.value} maintained despite raw score {raw:.1f}."
        low_quality = True
    else:
        # Flip to computed bias (or first-run bias), or use flipped_bias from Tier-1 bypass
        final_bias = flipped_bias if last_bias is not None else computed_bias
        score = payload_score(raw, final_bias)
        if last_bias is not None and final_bias != last_bias:
            _record_flip_time(key, now or datetime.now())
            note = f"Bias flipped: {last_bias.value} -> {final_bias.value} (raw {raw:.1f})."
        else:
            note = ""
        # Only remember non-tie-break verdicts (abs(raw) >= EPS)
        if abs(raw) >= EPS:
            remember_bias(key, final_bias)
        # Tie-break case (raw inside EPS) is low quality
        low_quality = abs(raw) < EPS

    # Dampened score for display/cue strength (raw used for bias/flip/stability)
    damped = _damped_score(raw, total_weight)

    # Drivers are reported on the side the CURRENT SIGNALS point to (computed_bias),
    # not the held bias. This ensures drivers reflect what the news is saying now.
    # The bull_drivers/bear_drivers always show both sides for the dashboard.
    leading = bull_items if computed_bias is Bias.BULLISH else bear_items
    drivers = _top_drivers(leading)
    conflict = _strongest_conflict(bull_items, bear_items)
    # Both sides kept separately for the dashboard, which shows a bullish box
    # and a bearish box side by side rather than only the leading side.
    bull_drivers = _top_drivers(bull_items)
    bear_drivers = _top_drivers(bear_items)

    if not note and abs(raw) < EPS:
        if bull + bear <= 0:
            note = ("No directional signal in the window; DEFAULT_BIAS taken "
                    "as the binary verdict.")
        else:
            note = ("Net score inside EPS; direction taken from the heaviest, "
                    "newest signal (binary tie-break).")

    return SentimentResult(
        asset=asset,
        score=score,
        raw_score=raw,
        damped_score=damped,
        confidence=confidence,
        bias=final_bias,
        mode=mode,
        drivers=drivers,
        conflict=conflict,
        bull_drivers=bull_drivers,
        bear_drivers=bear_drivers,
        delta_60m=_delta_60m(list(signals), asset, now),
        event_risk=event_risk,
        event_risk_note=event_risk_note,
        contributing_articles=agg["used"],
        contributing_clusters=agg["clusters"],
        stale=False,
        signal_count=len(relevant),
        burst_bull=agg["burst_bull"],
        burst_bear=agg["burst_bear"],
        note=note,
        contradiction_detected=contradiction_detected,
        confidence_label=confidence_label_for(confidence, low_quality),
    )


def score_from_signals(signals: Sequence[WeightedSignal], asset: Asset,
                       now: datetime | None = None) -> float:
    """Convenience wrapper returning only the signed score."""
    return compute(signals, asset, now=now).score