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

    decay        = 0.5 ** (age_hours / half_life(tier, asset))   <- item 7
                  (tier half-lives from config.TIER_HALF_LIVES, scaled by
                   config.ASSET_DECAY_MULTIPLIER - see decay_weight())

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

   IMPACT GATE (config.only_high_impact_counts, default on)
     A signal whose impact band is not VERY_HIGH/HIGH gets weight 0 at the ONE
     choke point (_signal_weight), so it never enters bull/bear, never appears
     in the driver boxes, never feeds the burst bonus or the tie-break. The
     bullish/bearish split and the drivers on a card therefore come from
     high-impact news alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import re

from app.analysis.policy_scope import foreign_policy_scope
from app.analysis.text_match import contains_cue
from app.config import (
    CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT,
    CLUSTER_CONFLICT_WEIGHT_SHARE,
    settings,
)
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

# --- Step 4f: asset-sensitivity fallbacks ---------------------------------
# Used by _signal_weight when a headline's event types are known but tell us
# nothing about how strongly this asset transmits.
#
# ASSET_SENSITIVITY_UNKNOWN is the value applied when detect_event_types
# returns NOTHING. The old code used 1.0 - full transmission weight - so a
# headline whose mechanism could not be identified scored exactly like one the
# map records as a direct, high-magnitude driver of that asset.
# 0.5 is the "no opinion" default: enough to carry a genuine lean, not enough
# to amplify unidentifiable noise.
ASSET_SENSITIVITY_UNKNOWN = 0.5

# Event types exempt from the asset-sensitivity scaling, because they describe
# money moving into or out of the instrument ITSELF rather than a macro
# transmission channel. A gold-ETF flow headline should not be discounted by
# gold's real-yield sensitivity: that magnitude describes a different
# mechanism. Step 4f widened this from the previous {BTC_ETF_FLOW,
# GOLD_ETF_FLOW} to cover every *_ETF_FLOW*, *_ETF_FLOW_IN, *_ETF_FLOW_OUT and
# INDEX_FLOW variant.
_FLOW_SENSITIVITY_EXEMPT = {
    "BTC_ETF_FLOW", "GOLD_ETF_FLOW",
    "GOLD_ETF_FLOW_IN", "GOLD_ETF_FLOW_OUT",
    "BTC_ETF_FLOW_IN", "BTC_ETF_FLOW_OUT",
    "INDEX_FLOW",
}

# --- impact gate (config.only_high_impact_counts) -------------------------
# The ONE rule for which news may reach a card: with the gate on, only the two
# top bands are admissible. MEDIUM (0.45) and LOW (0.20) used to contribute at
# a reduced scale; they are now dropped at the weight choke point, so they can
# never enter the bullish/bearish totals, the driver boxes, the news-velocity
# burst bonus or the tie-break. Set the flag false to restore the old scaled
# behaviour everywhere without touching this module again.
STRENGTH_GATE = frozenset({ImpactStrength.VERY_HIGH, ImpactStrength.HIGH})


def _passes_impact_gate(s: WeightedSignal) -> bool:
    """False when a signal's impact band may not reach any card."""
    if not settings.only_high_impact_counts:
        return True
    return s.strength in STRENGTH_GATE


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
EPS = 25.0

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

# Minimum total directional weight (bull + bear) to run the FULL verdict path
# (sign-of-score bias plus the flip / stability machinery). Below this the
# evidence is too thin for that path, so compute() takes the weak-evidence
# branch instead: a binary bias chosen from the same totals with confidence
# capped low. The weighted lean is still PUBLISHED as the score there (see the
# branch), so the gate selects the bias path - it never zeroes the score.
MIN_TOTAL_WEIGHT = 0.15

# Minimum confidence before a computed flip is allowed (Item 5).
# When confidence is below this, a flip is refused and the previous bias is held.
# DAY TRADING: lower threshold for faster flips (was 0.35)
#
# THE SOURCE OF TRUTH IS ``settings.flip_min_confidence``. This used to be a
# hardcoded 0.30 here while config.py declared ``flip_min_confidence = 0.50``
# and nothing read it - so the knob in the config file was dead, and the only
# number that ever existed was this literal. It is now read from settings, and
# it is actually USED as a flip gate (see ``_should_flip``).
FLIP_MIN_CONFIDENCE = float(settings.flip_min_confidence)


def confidence_label_for(conf: float, low_quality: bool) -> str:
    """
    Map confidence to a label.

    LOW   if low_quality (bias held or tie-broken) or conf < 0.30
    MEDIUM if conf < 0.55
    HIGH  otherwise
    """
    if low_quality or conf < 0.30:
        return "LOW"
    if conf < 0.55:
        return "MEDIUM"
    return "HIGH"


def payload_score(raw_score: float, bias) -> float:
    """
    The score a payload may show for ``bias``.

    THE PUBLISHED SCORE NEVER CONTRADICTS THE PUBLISHED LABEL.

    ``raw_score`` is the aggregate net lean - the honest answer to "what do
    the day's headlines add up to". ``bias`` is the verdict the engine
    actually published, and the two can legitimately differ: the stability
    gates may HOLD the previous bias while the aggregate has already turned,
    and a fresh Tier-1 release may FLIP the badge while the aggregate (one
    print against a hundred headlines) has not caught up yet.

    Before this rule, those ticks published a badge and its opposite number
    side by side - "BEARISH +40.0" - because the score was the aggregate and
    the label was the verdict. Measured on the live database, 24% of ticks
    carried that contradiction, and 86% of those were Tier-1 override flips.

    So: whenever the label names the side the raw lean does not, the
    published score becomes the small lean of the LABEL's sign
    (``+/- TIEBREAK_LEAN``), which renders as a 51/49 read - a weak call
    that is at least pointing the same way as the badge. The aggregate lean
    is not lost: it stays on ``raw_score`` for the audit trail, and the card
    note records WHY the two differ.

    A score whose sign already agrees with the label is left alone, however
    small it is.
    """
    name = getattr(bias, "value", bias)
    if name == "BULLISH" and raw_score < 0:
        return TIEBREAK_LEAN
    if name == "BEARISH" and raw_score > 0:
        return -TIEBREAK_LEAN
    return float(raw_score)

# Task 8: stability (no whipsaw) constants.
#
# THE hasattrs ARE GONE - AND THAT IS THE POINT.
#
# These four used to read:
#
#     COOLING_MINUTES = settings.COOLING_MINUTES if hasattr(settings, 'COOLING_MINUTES') else 10
#
# ``settings`` is a pydantic-settings model whose FIELDS ARE LOWERCASE
# (``cooling_minutes``, not ``COOLING_MINUTES``), so ``hasattr`` was ALWAYS
# False and the hardcoded fallback on the right always won. The operator's
# ``.env`` values were read by nothing: the engine ran with FLIP_MARGIN 8.0 /
# COOLING_MINUTES 10 / COOLING_MARGIN 12 / FLIP_HOLD_MINUTES 5 while .env said
# 5.0 / 5 / 8 / 2. Every stability knob the operator tuned was inert, and
# because the engine still produced cards nobody could tell - so nobody could
# tell that the tuning had never taken effect.
#
# The fields are now read directly by their real names. If a field is renamed
# in config.py this raises at import time (a loud failure) instead of silently
# reverting to a default (a quiet one).
#
# IMPROVEMENT 6: anti-whipsaw cooling period after a flip.
# After a bias flips, the market needs COOLING_MINUTES before another flip is
# allowed, and during that window the score must clear by COOLING_MARGIN
# (wider than the normal FLIP_MARGIN) so a re-test of the old level does not
# immediately flip it back. The Tier-1 override still bypasses this entirely -
# it is checked first in _should_flip() and returns immediately.
COOLING_MINUTES = int(settings.cooling_minutes)
COOLING_MARGIN = float(settings.cooling_margin)

FLIP_MARGIN = float(settings.flip_margin)
FLIP_HOLD_MINUTES = int(settings.flip_hold_minutes)

# CONTRADICTION SPLIT - placeholder, NOT tuned.
# ``evidence_split`` is the ratio-based meaning of the old contradiction flag.
# ``cluster_conflict`` is the cluster-level meaning. The threshold below is a
# placeholder for the tuning phase and must not be adjusted here.
EVIDENCE_SPLIT_RATIO = 0.7
CONTRADICTION_FLIP_MARGIN_ADJUSTMENT = 10.0


def evidence_split_from_totals(bull: float, bear: float) -> bool:
    """Whether the two aggregate evidence totals are balanced enough to count."""
    larger = max(bull, bear)
    if larger <= 0.0:
        return False
    return min(bull, bear) / larger > EVIDENCE_SPLIT_RATIO


def effective_flip_margin(cluster_conflict: bool) -> float:
    """Widen the flip margin only on a cluster-level directional conflict."""
    return FLIP_MARGIN + (
        CONTRADICTION_FLIP_MARGIN_ADJUSTMENT if cluster_conflict else 0.0
    )


def cap_confidence_for_evidence_split(confidence: float) -> float:
    """Cap confidence when the aggregate evidence is genuinely split."""
    return min(confidence, TIEBREAK_MAX_CONFIDENCE)

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
    """The last time a bias flip occurred for this key (aware UTC)."""
    dt = _LAST_FLIP_TIME.get(key)
    return _to_aware_utc(dt) if dt is not None else None


def _record_flip_time(key: str, now: datetime) -> None:
    """Record a bias flip time for this key (always aware UTC)."""
    _LAST_FLIP_TIME[key] = _to_aware_utc(now)


def clear_bias_history() -> None:
    """Drop all carry-forward history (test isolation / cold restarts)."""
    _LAST_BIAS.clear()
    _LAST_FLIP_TIME.clear()


def warm_bias_history(rows: dict[str, str]) -> None:
    """Load last real biases (asset -> 'BULLISH'/'BEARISH') into
    memory, without overwriting any existing entry."""
    for asset, bias_str in rows.items():
        if bias_str not in ("BULLISH", "BEARISH"):
            continue
        _LAST_BIAS.setdefault(asset, Bias(bias_str))


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


def _to_aware_utc(dt: datetime | None) -> datetime | None:
    """Normalize any datetime to aware UTC.

    Naive values are treated as UTC (matching ``_ensure_aware``); aware values
    are converted to UTC. Aware and naive values are never mixed in
    :data:`_LAST_FLIP_TIME`.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def _apply_baseline(raw_score: float, baseline: float | None) -> tuple[float, float | None]:
    """
    Centre one raw score against the asset's trailing baseline.

    Returns ``(lean, baseline_used)``. ``baseline_used`` is None when no
    baseline was available, and then the lean IS the raw score - so with no
    baseline the engine behaves exactly as it always did.

    The rule itself lives in ``bias_baseline.adjusted_score`` so there is ONE
    implementation of it: the audit log, the snapshot writer and the scoring
    path cannot disagree about what "adjusted" means.
    """
    if baseline is None:
        return float(raw_score), None
    try:
        from app.analysis.bias_baseline import adjusted_score

        lean, used = adjusted_score(raw_score, baseline)
        return float(lean), (None if used is None else float(used))
    except Exception:  # noqa: BLE001 - a missing baseline must not break a tick
        return float(raw_score), None


def _should_flip(bull: float, bear: float, current_bias: Bias,
                 relevant: Sequence[WeightedSignal],
                 now: datetime | None,
                 key: str,
                 flip_margin_override: float | None = None,
                 confidence: float | None = None) -> tuple[bool, Bias, str]:
    """
    Determine if bias should flip based on stability rules.

    ``flip_margin_override`` widens the required score margin when a
    contradiction has been detected (both sides heavily weighted): the
    caller passes ``FLIP_MARGIN + 10`` in that case so a genuinely torn
    market cannot be flipped on noise. When None the module FLIP_MARGIN is
    used, unless a cooling period is active (IMPROVEMENT 6) in which case
    COOLING_MARGIN applies instead.

    ``confidence`` is the tick's own confidence. It gates the MARGIN flip
    only: a reading too thin to trust must not move a published verdict. It
    deliberately does NOT gate the fresh-Tier-1 override (a confirmed
    official release flips on its own, however thin the surrounding
    evidence) and it does NOT gate the first-run verdict (there is no
    previous bias to hold).

    Returns ``(should_flip, bias, reason)``. ``reason`` names the gate
    that produced the answer so the card's note can tell the reader WHY a
    verdict was held or moved, instead of every hold reading the same.

    Order of gates, earliest first:
      1. No current bias (first run) -> take the computed bias.
      2. No weight at all -> hold.
      3. Fresh Tier 1 opposite signal -> flip immediately, bypasses
         everything below including the cooling period.
      4. Cooling period after a recent flip -> wider effective margin.
      5. Hold-time gate -> refuse to flip again inside FLIP_HOLD_MINUTES.
      6. Confidence floor -> refuse to flip on a thin reading.
      7. Margin gate -> flip only when the score cleared by the margin.
    """
    # No current bias (first run) - just use the computed bias
    if current_bias is None:
        return True, Bias.BULLISH if bull > bear else Bias.BEARISH, "first_run"

    # Compute the score from current bull/bear weights
    total = bull + bear
    if total <= 0:
        return False, current_bias, "no_weight"
    current_score = 100.0 * (bull - bear) / total

    # Check for fresh Tier 1 opposite signal (forces flip regardless of
    # margin/hold). This bypasses the cooling period below on purpose: a
    # confirmed official release is the one event that can flip immediately.
    if _has_fresh_tier1_opposite(relevant, current_bias, now):
        return (True,
                Bias.BEARISH if current_bias is Bias.BULLISH else Bias.BULLISH,
                "tier1_override")

    # IMPROVEMENT 6: anti-whipsaw cooling period after a flip.
    # After a bias flips, the market needs COOLING_MINUTES before another
    # flip is allowed, and during that window the score must clear by
    # COOLING_MARGIN (wider than the normal FLIP_MARGIN) so a re-test of the
    # old level does not immediately flip it back. The Tier-1 override above
    # still bypasses this entirely - it returned before reaching this point.
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
            return False, current_bias, "hold_window"

    # Confidence floor. A reading the engine itself calls LOW must not move a
    # published verdict - it is the same thin-evidence signal that produces the
    # tie-break verdicts, and letting it flip the badge is how a card ends up
    # naming a side its own drivers contradict. The fresh-Tier-1 override
    # above already returned, so a confirmed release is unaffected.
    if confidence is not None and confidence < FLIP_MIN_CONFIDENCE:
        return False, current_bias, "low_confidence"

    # Check margin: score must have crossed zero by at least the margin.
    # The margin is on the score scale (-100..+100).
    if current_bias is Bias.BULLISH:
        if current_score <= -effective_margin:
            return True, Bias.BEARISH, "margin"
    else:
        if current_score >= effective_margin:
            return True, Bias.BULLISH, "margin"

    return False, current_bias, "margin_not_cleared"


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
    # STEP 9.3: set by event_clustering._collapse_members when two readings of
    # the SAME event (one event_key) pointed in opposite directions and were
    # collapsed to a single vote. The signal survives with the dominant
    # direction, and this flag tells compute() the evidence behind it had a
    # cluster-level directional conflict, so the verdict can be reported with
    # a wider flip margin.
    cluster_conflict: bool = False
    # STEP 9.3: when this signal is the survivor of a cluster whose members
    # carried opposing directions, this holds the summed magnitude of the
    # losing side(s) - the evidence that genuinely conflicted with the
    # dominant reading. Zero on every other signal. compute() uses this as
    # the conflicted share of the tick's evidence: a lopsided collapse
    # (ten-to-one) contributes only the minority side's weight instead of
    # counting the dominant survivor's full weight as a torn market.
    conflict_weight: float = 0.0
    # Task 3: surprise-size multiplier for ACTUAL scheduled-data reports
    # (release_surprise.surprise_weight). Constant 1.0 for every other
    # signal, so no existing behaviour changes.
    surprise_weight: float = 1.0
    # Section 7: silver's dual nature. PRECIOUS/INDUSTRIAL/BOTH/null, carried
    # through so the weight path can reward a characterised silver story.
    metal_character: str | None = None
    # Central bank speaker tier multiplier.
    speaker_tier: float = 1.0
    # Overnight admission: the instant this signal's FRESHNESS is measured
    # from, when that differs from publication time. Set for headlines the
    # morning brief judged while the laptop was off - the engine could not act
    # on them at publication, so age is measured from the moment it came back
    # (the covered window's end) instead. None for every live signal, which
    # keeps publication-time ageing exactly as before. Display, burst and
    # delta-60m logic still reads ``published_at``.
    age_reference_utc: datetime | None = None

    # filled by compute()
    recency: float = 0.0
    final_weight: float = 0.0
    magnitude: float = 0.0
    contribution: float = 0.0

    @property
    def contradiction_detected(self) -> bool:
        """
        Deprecated read-only alias for ``cluster_conflict``.

        The old name covered two meanings; the cluster-level meaning now lives
        in ``cluster_conflict`` and the ratio-based meaning lives in the
        SentimentResult's ``evidence_split``.
        """
        return self.cluster_conflict


@dataclass
class Driver:
    """One headline's contribution, for the manual trader's audit trail."""

    title: str
    contribution: float
    direction: str
    weight: float


@dataclass
class SentimentResult:
    """One asset's verdict. Always directional (BULLISH or BEARISH).

    bull_weight / bear_weight (TASK 2) carry the aggregate totals, so the
    split of the evidence can be reconstructed after the fact. They are
    never read by the scoring path.
    """

    asset: Asset
    score: float = 0.0
    # The pre-clamp net score, for logging/audit only: `score` is what the
    # payload shows, so a tie-break can never contradict its own badge.
    raw_score: float = 0.0
    # The per-asset baseline that was subtracted from ``raw_score`` to produce
    # the centred lean the verdict is actually made on. None when no baseline
    # was available (warm-up, or the feature switched off) - in which case
    # the verdict is made on the raw lean exactly as before.
    baseline_score: float | None = None
    # TASK 2: the two aggregate totals, persisted so
    # r = min(bull,bear)/max(bull,bear) is recoverable from history. A net
    # score cannot be decomposed into its two sides, so without this the ratio
    # is unrecoverable for any past tick. DATA CAPTURE ONLY - nothing in the
    # scoring path reads these, so they cannot change a verdict.
    bull_weight: float = 0.0
    bear_weight: float = 0.0
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
    # TASK 3/SPLIT: two separate flags, replacing the old contradiction_detected.
    # ``cluster_conflict`` means a cluster collapsed opposing readings.
    cluster_conflict: bool = False
    # ``evidence_split`` means the two aggregate totals are balanced above a
    # ratio threshold. Placeholder threshold, not tuned.
    evidence_split: bool = False
    # News-velocity burst counts in the last 15 minutes: how many same-side
    # headlines landed together. 0 on a stale verdict. Exposed on the
    # dashboard so a reader can see the momentum behind the score.
    burst_bull: int = 0
    burst_bear: int = 0
    note: str = ""
    confidence_label: str = "LOW"

    @property
    def contradiction_detected(self) -> bool:
        """
        Deprecated read-only alias for ``cluster_conflict``.

        Payload consumers should migrate to ``cluster_conflict`` and
        ``evidence_split``. The alias is retained only for existing payload
        readers and is documented as deprecated.
        """
        return self.cluster_conflict


def decay_weight(published_at: datetime | None, tier: str,
                 now: datetime | None = None, asset: "Asset | None" = None,
                 event_type: str | None = None) -> float:
    """
    Exponential decay with a per-event-tier half-life (Task 5a + Steps 7/11).

    Tier 1 = 1.5h, Tier 2 = 1.25h, Tier 3 = 1.0h (the live values are
    ``config.TIER_HALF_LIVES``); the per-asset multiplier in
    config.ASSET_DECAY_MULTIPLIER is applied on top (bitcoin 1.25 for its 24/7
    clock). EQUITY is absent from the multiplier table on purpose - Step 6's
    Option A removed it, because the session-aware equity card applies its own
    multipliers and no longer routes through this function.

    Explicit half-life overrides per EventType (config.EVENT_HALF_LIFE_OVERRIDE)
    let FOMC, NFP, CPI, geopolitical escalation and similar events carry
    custom persistence independent of their tier.

        weight = 0.5 ** (age_hours / half_life)

    Items older than settings.trading_day_cutoff_hours get weight = DECAY_FLOOR
    (0.05 = 5% residual), not zero, so stale news fades instead of vanishing.

    ``published_at=None`` also returns DECAY_FLOOR; those rows are never
    admitted by ingest, so they should not reach this function at all.

    THE STALE MASS THAT FLOOR CREATES IS CAPPED ELSEWHERE. The floor is per
    SIGNAL, so with 130+ signals a tick the accumulated shelf of hours-old
    headlines becomes the majority of the evidence - measured on the live
    database, 56-69% of the day's weight mass by mid-afternoon. The cap in
    ``_aggregate`` (``settings.stale_mass_cap_ratio``) is what stops that
    shelf from outvoting the news that just landed.

    STEP 11: this docstring used to say "Tier 1 ~90min, Tier 2 ~45min,
    Tier 3 ~15min". Those were the DAY-TRADING values from before the
    2026-10-13 retune. The live values are 1.5 / 1.25 / 1.0h - and an earlier
    version of this docstring said 1.5/0.75/0.25h, which was wrong about
    Tier 2 and Tier 3. Reading the docstring instead of config would have
    mis-dimensioned every decay calculation.
    """
    from app.config import (ASSET_DECAY_MULTIPLIER, TIER_HALF_LIVES,
                            EVENT_HALF_LIFE_OVERRIDE, DECAY_FLOOR,
                            TRADING_DAY_CUTOFF_HOURS, settings)

    # Step 7: Check for event-specific half-life override first
    if event_type is not None:
        half_life = EVENT_HALF_LIFE_OVERRIDE.get(event_type)
        if half_life is None:
            half_life = TIER_HALF_LIVES.get(tier, 2.0)
    else:
        half_life = TIER_HALF_LIVES.get(tier, 2.0)
    if asset is not None:
        half_life *= ASSET_DECAY_MULTIPLIER.get(
            getattr(asset, "name", str(asset)), 1.0)
    half_life = max(0.1, float(half_life))

    if published_at is None:
        # Step 7: Missing timestamp -> flag as UNVERIFIED, return floor weight
        return DECAY_FLOOR
    hours = _age_hours(published_at, now)
    if hours < 0:
        return 1.0  # within clock skew: treat as brand new
    cutoff = getattr(settings, 'trading_day_cutoff_hours', 2.0)
    if hours >= cutoff:
        return DECAY_FLOOR  # Step 7: floor instead of hard zero
    return max(DECAY_FLOOR, min(1.0, 0.5 ** (hours / half_life)))


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

    POLICY SCOPE: TIER_1 means "this event reprices these assets". A
    central-bank stance that belongs to a FOREIGN central bank is not such
    an event for gold, silver, bitcoin or the S&P - it does not move US
    rates. Such a headline is capped at TIER_2, which also removes it from
    the fresh-Tier-1 flip override (``_has_fresh_tier1_opposite``) that let
    "hawkish RBA" flip the gold badge on its own. The US central bank keeps
    the full TIER_1 treatment. See app/analysis/policy_scope.py.
    """
    if s.importance is EconomicImportance.VERY_HIGH:
        if foreign_policy_scope(s.title or ""):
            return "TIER_2"
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
        decay = 0.5 ** (age_hours / (TIER_HALF_LIVES[tier] * ASSET_DECAY_MULTIPLIER[asset])),
        tier = event_tier(s) in {TIER_1, TIER_2, TIER_3},
        age_hours >= TRADING_DAY_CUTOFF_HOURS -> weight = 0.
    Importance feeds the tier once via event_tier() and is never
    multiplied separately here. Source quality (QUALITY_MULT, 0.9..1.0)
    is an independent trust factor, never the tier.
    """
    # Impact gate, BEFORE any other factor: a MEDIUM/LOW impact signal may not
    # reach a card at all. Returning 0.0 makes every caller drop the signal -
    # _aggregate() skips weight <= 0, so it never enters bull/bear, never
    # lands in bull_items/bear_items (hence no driver), and neither
    # _tie_break_bias() nor _delta_60m() can resurrect it.
    if not _passes_impact_gate(s):
        return 0.0

    # Coerce a naive ``now`` to aware UTC: ``published_at`` is stored aware,
    # so a naive clock made the age subtraction raise TypeError and silently
    # skipped the freshness weighting below.
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    from app.analysis.cue_rules import detect_verification
    from app.analysis.policy_scope import policy_event_scale
    from app.analysis.release_surprise import MAX_SURPRISE_WEIGHT
    from app.analysis.transmission_map import detect_event_types, transmission
    from app.config import (
        CONFIRMED_BOOST,
        RUMOR_MULTIPLIER,
        TIER_MULTIPLIERS,
    )

    # Task 5a: decay uses the event-tier half-life (TIER_1~1.5h, TIER_2~0.75h,
    # TIER_3~0.25h) scaled by the per-asset multiplier, with
    # TRADING_DAY_CUTOFF_HOURS giving weight 0. An overnight-admitted signal
    # ages from its reference (the brief's judgment moment), not from the
    # publication time the laptop slept through.
    tier_key = event_tier(s)
    published_ref = s.age_reference_utc or s.published_at
    decay = decay_weight(published_ref, tier_key, now, asset=s.asset)
    rel = max(0.0, min(1.0, s.relevance))
    per = max(0.05, min(1.0, s.persistence))
    qual = QUALITY_MULT.get((s.source_quality or "").upper(), 0.90)
    if s.category is NewsCategory.BITCOIN_ETF_FLOWS:
        qual = 1.0
    sess = max(0.5, min(1.5, s.session_multiplier))
    # BTC-specific session multiplier (24/7 volume awareness)
    if s.asset is Asset.BITCOIN and s.published_at is not None:
        hour_utc = _ensure_aware(s.published_at).hour
        # Asian session (00-08 UTC): lower vol, fade moves
        if 0 <= hour_utc < 8:
            sess *= 0.85
        # London open (08-12): high vol, respect moves
        elif 8 <= hour_utc < 12:
            sess *= 1.15
        # NY open (13-21): highest vol, strongest signals
        elif 13 <= hour_utc < 21:
            sess *= 1.25
        # NY close/Asian pre-open (21-24): fading vol
        else:
            sess *= 0.95
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
    flow_event_types = _FLOW_SENSITIVITY_EXEMPT
    detected = detect_event_types(s.title, s.category)
    earnings_mult = 1.0
    if any(et.value in earnings_event_types for et in detected):
        lowered = (s.title or "").lower()
        if any(p.search(lowered) for p in _EARNINGS_MEGA_CAP_PATTERNS):
            earnings_mult = settings.earnings_mega_cap_mult
        else:
            earnings_mult = settings.earnings_unnamed_mult

    # Asset sensitivity based on transmission map magnitude.
    #
    # STEP 4f (second half): when NO usable event type is detected, the old
    # code fell back to 1.0 - FULL transmission weight. A headline whose
    # mechanism could not be identified therefore scored exactly like one the
    # map knows is a direct, high-magnitude driver of that asset. It now falls
    # back to ASSET_SENSITIVITY_UNKNOWN (0.5), which is the "no opinion"
    # default: enough to carry a genuine lean, not enough to amplify noise.
    #
    # STEP 4f (first half): the skip list is widened from
    # {BTC_ETF_FLOW, GOLD_ETF_FLOW} to EVERY *_ETF_FLOW* and *_ETF_FLOW_IN /
    # *_ETF_FLOW_OUT plus INDEX_FLOW. A direct flow headline is a statement
    # about money into or out of the instrument itself - it should not be
    # discounted by the asset's macro sensitivity, which for gold is driven by
    # real yields. Before this, a gold-ETF flow headline was penalised by the
    # very factor that describes a different transmission channel.
    #
    # Events with sign 0 (GENERAL_MACRO and similar) must never be scaled:
    # they are directionless by construction, and max_m only ever considers
    # non-zero-sign entries for exactly that reason.
    flow_event_types = _FLOW_SENSITIVITY_EXEMPT
    asset_sensitivity = ASSET_SENSITIVITY_UNKNOWN
    if detected:
        # Separate earnings, flow, and nonzero non-earnings/non-flow events
        has_earnings = any(et.value in earnings_event_types for et in detected)
        has_flow = any(et.value in flow_event_types for et in detected)
        if has_earnings or has_flow:
            # earnings or flow detected -> skip transmission map, asset_sensitivity = 1.0
            asset_sensitivity = 1.0
        else:
            # Look at nonzero events only (sign != 0)
            nonzero = [et for et in detected if transmission(et, s.asset).sign != 0]
            if nonzero:
                # compute max magnitude among nonzero events
                #
                # POLICY SCOPE: a central-bank stance that belongs to a
                # FOREIGN central bank does not travel the US rate channel
                # the map is written for, so its magnitude is scaled down
                # before the sensitivity is read. A hawkish Fed keeps the
                # full channel; a hawkish RBA keeps a weak one. Without this
                # the word "hawkish" alone gave any central bank the top
                # driver slot on the gold card.
                max_m = 0.0
                for et in nonzero:
                    entry = transmission(et, s.asset)
                    # entry.sign != 0 guaranteed by filter
                    magnitude = entry.magnitude * policy_event_scale(et.value, s.title)
                    if magnitude > max_m:
                        max_m = magnitude
                if max_m > 0:
                    asset_sensitivity = min(1.0, max(0.3, max_m))
                else:
                    # All nonzero events have zero magnitude? Should not happen if magnitude >0 for sign!=0, but just in case.
                    asset_sensitivity = 1.0
            else:
                # All detected events are sign-zero (e.g., GENERAL_MACRO) -> treat as no usable event type
                asset_sensitivity = ASSET_SENSITIVITY_UNKNOWN
    # else: no detected events, leave asset_sensitivity as ASSET_SENSITIVITY_UNKNOWN (0.5)

    return decay * rel * per * qual * sess * corr * surprise * tier * metal_mult * earnings_mult * asset_sensitivity * s.speaker_tier


def _mode_for(signals: Sequence[WeightedSignal]) -> str:
    """
    How these signals were classified, reported truthfully (Step 3).

        every contributing signal is "llm"        -> "llm"
        every contributing signal is "heuristic"  -> "heuristic"
        a blend of the two (or any other value)   -> "mixed"
        nothing contributed at all                -> "heuristic"

    ``WeightedSignal.mode`` is populated from ``NewsArticle.analysis_mode`` by
    ``build_signal``. Before Step 3 the pipeline never passed it, so every
    signal defaulted to "heuristic" and the card claimed a rule engine even
    when the LLM had classified every headline behind it. The footer now shows
    the real mode, and a card that reads "mixed" is a genuine blend -
    typically LLM-classified news alongside rule-based calendar prints, which
    are always computed and therefore always heuristic.
    """
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

    # How much of each side's total comes from news OLDER than the trading-day
    # cutoff. Tracked separately so the stale mass can be capped below.
    cutoff_hours = float(getattr(settings, "trading_day_cutoff_hours", 8.0))
    bull_fresh = bear_fresh = 0.0
    stale_items: list[tuple[float, WeightedSignal, int]] = []

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

        age_hours = None
        if s.published_at is not None:
            age_hours = (now - _ensure_aware(s.published_at)).total_seconds() / 3600.0
        is_stale = age_hours is not None and age_hours >= cutoff_hours

        if sign > 0:
            bull += magnitude
            bull_items.append((magnitude, s))
            if is_stale:
                stale_items.append((magnitude, s, 1))
            else:
                bull_fresh += magnitude
        elif sign < 0:
            bear += magnitude
            bear_items.append((magnitude, s))
            if is_stale:
                stale_items.append((magnitude, s, -1))
            else:
                bear_fresh += magnitude
        else:
            # An item that takes no side is never forced onto one. It counts
            # against conviction instead; MIXED counts double.
            directionless += magnitude * (1.4 if s.direction is Direction.MIXED
                                          else 1.0)
        if s.cluster_id:
            clusters.add(s.cluster_id)
        used += 1

    # --- STALE MASS CAP ---------------------------------------------------
    # WHY: the per-signal decay floors at DECAY_FLOOR (0.05) past the
    # trading-day cutoff, so old news fades but never vanishes. That contract
    # is deliberate and tested. The problem is that the floor is PER SIGNAL:
    # with 130+ signals a tick, hundreds of hours-old headlines each
    # contribute 5% of their weight, and the accumulated shelf becomes the
    # majority of the evidence.
    #
    # MEASURED on the live database (the day's surviving HIGH/VERY_HIGH
    # weight mass, by hour of the session):
    #
    #     hour  09  10  11  12  13  14  15  16  17  18  19  20  23
    #     stale 20% 29% 29% 31% 31% 56% 54% 68% 66% 69% 67% 65% 57%
    #
    # So through the US session the card is majority-driven by news that is
    # already hours old and has been priced in. That is the opposite of what
    # a DAY-TRADING tool should be: the headline that just landed cannot move
    # a card that is 68% yesterday's news.
    #
    # WHAT: each side's stale mass is capped at
    # ``stale_mass_cap_ratio`` times the TOTAL fresh mass. The cap is
    # measured against the total fresh mass, not against the same side's
    # fresh mass, so a side with no fresh news of its own cannot be drowned
    # by the other side's stale news either. With the default 0.5, at least
    # half of the evidence behind every card is news from the last 8 hours.
    #
    # The floor itself is untouched: an old headline still contributes, it
    # just cannot outvote the fresh news as a block. When there is NO fresh
    # news at all the cap does not apply - a quiet morning with only old
    # headlines is better served by those headlines than by nothing.
    cap = settings.stale_mass_cap_ratio
    if cap is not None and cap > 0.0 and stale_items:
        fresh_total = bull_fresh + bear_fresh
        if fresh_total > 0:
            budget = cap * fresh_total
            stale_bull = sum(m for m, _, side in stale_items if side > 0)
            stale_bear = sum(m for m, _, side in stale_items if side < 0)
            scale_bull = (budget / stale_bull) if stale_bull > budget else 1.0
            scale_bear = (budget / stale_bear) if stale_bear > budget else 1.0
            if scale_bull < 1.0 or scale_bear < 1.0:
                rescaled = {id(signal): factor
                            for (_, signal, side), factor in zip(
                                stale_items,
                                [scale_bull if side > 0 else scale_bear
                                 for _, _, side in stale_items])
                            if factor < 1.0}
                for magnitude, signal, _side in stale_items:
                    factor = rescaled.get(id(signal))
                    if factor is None:
                        continue
                    signal.magnitude = magnitude * factor
                    signal.final_weight = signal.final_weight * factor
                    signal.contribution = signal.contribution * factor
                # Rebuild both totals from the (possibly rescaled) items, and
                # keep the item tuples in step with the signal objects.
                bull = 0.0
                bear = 0.0
                rebuilt_bull: list[tuple[float, WeightedSignal]] = []
                rebuilt_bear: list[tuple[float, WeightedSignal]] = []
                for magnitude, signal in bull_items:
                    value = signal.magnitude
                    bull += value
                    rebuilt_bull.append((value, signal))
                for magnitude, signal in bear_items:
                    value = signal.magnitude
                    bear += value
                    rebuilt_bear.append((value, signal))
                bull_items = rebuilt_bull
                bear_items = rebuilt_bear

    # --- News velocity burst bonus (IMPROVEMENT 1) ---
    # A cluster of same-direction headlines in the last 15 minutes is a
    # momentum cue: multiply the side's total so it can punch through a
    # marginal FLIP_MARGIN. Conflicting bursts (both sides >= 2 in the
    # window) cancel the bonus entirely - that is a tug-of-war, not a trend.
    burst_signals = [s for s in signals
                     if _passes_impact_gate(s)
                     and s.published_at is not None
                     and (now - _ensure_aware(s.published_at)).total_seconds() <= 900]
    burst_bull = sum(1 for s in burst_signals
                     if s.direction is Direction.BULLISH)
    burst_bear = sum(1 for s in burst_signals
                     if s.direction is Direction.BEARISH)
    conflicting_burst = burst_bull >= 2 and burst_bear >= 2

    if not conflicting_burst:
        if burst_bull >= 5 and burst_bull > burst_bear:
            bull *= 1.05
        elif burst_bull >= 3 and burst_bull > burst_bear:
            bull *= 1.02
        elif burst_bear >= 5 and burst_bear > burst_bull:
            bear *= 1.05
        elif burst_bear >= 3 and burst_bear > burst_bull:
            bear *= 1.02

    return {"bull": bull, "bear": bear, "directionless": directionless,
            "bull_items": bull_items, "bear_items": bear_items,
            "used": used, "clusters": len(clusters),
            "burst_bull": burst_bull, "burst_bear": burst_bear,
            "conflicting_burst": conflicting_burst}


def _score_from(bull: float, bear: float) -> float:
    """Map the two weighted totals onto a signed -100..+100 score."""
    from app.config import (CONTRADICTION_DAMPENER_ENABLED,
                            CONTRADICTION_DAMPENER_FACTOR,
                            CONTRADICTION_RATIO)

    total = bull + bear
    if total <= 0:
        return 0.0
    score = round(100.0 * (bull - bear) / total, 1)
    # STEP 8: contradiction dampener.
    #
    # When the two sides are genuinely close, the news is fighting itself and
    # the difference is mostly which side happened to land the heavier single
    # headline. Projecting that to 67/33 published a conviction the evidence
    # did not support. Halving the score here means the split (and therefore
    # the displayed percentage) shows a lean, not a verdict.
    #
    # The 0.6 threshold is the same ratio the confidence blend uses for
    # "agreement": min/max > 0.6 is the point at which the weaker side is at
    # least 60% of the stronger, i.e. the fight is real rather than noise.
    if CONTRADICTION_DAMPENER_ENABLED:
        smaller = min(bull, bear)
        larger = max(bull, bear)
        if larger > 0 and smaller / larger > CONTRADICTION_RATIO:
            score = round(
                score * CONTRADICTION_DAMPENER_FACTOR, 1)
    return score


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

    # STEP 10: remap onto the observed hit rates. Identity until a fit exists,
    # so this is a no-op today - see confidence_calibration for why any number
    # invented here would be an unfounded formula.
    try:
        from app.analysis.confidence_calibration import calibrate_confidence
        confidence = calibrate_confidence(confidence)
    except Exception:  # noqa: BLE001 - calibration must never break scoring
        pass

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
            history_key: str | None = None,
            baseline: float | None = None) -> SentimentResult:
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

    ``baseline`` is the asset's trailing baseline (its own recent median raw
    score). When supplied, the verdict is made on the CENTRED lean - the raw
    score minus that baseline - so "how negative is today" becomes "how
    different is today from this asset's own recent history". The unadjusted
    level survives on ``raw_score`` for the audit trail. None (the default)
    means no baseline is available and the verdict is made on the raw lean,
    exactly as before.

    The baseline is NOT applied to the weak-evidence branch: that branch
    means "there is not enough evidence for a verdict", and normalising a
    non-verdict would only manufacture one.
    """
    _maybe_reset_bias_history()
    key = history_key or asset.value
    relevant = [s for s in signals if s.asset is asset]
    mode = _mode_for(relevant)

    if not relevant:
        bias, note = _carry_forward_bias(signals, key)
        remember_bias(key, bias)
        # bull_weight/bear_weight stay at their dataclass default of 0.0: this
        # branch is reached when `not relevant`, so there is no aggregate to
        # report. (They are computed further down, so passing them here would
        # raise UnboundLocalError.)
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
    # The un-capped formula value: used below so a tie-break that confirms the
    # raw score's lean is not forced down to TIEBREAK_MAX_CONFIDENCE.
    raw_confidence = confidence

    # --- CONTRADICTION SPLIT ---
    # ``cluster_conflict`` widens the flip margin when the conflicted clusters
    # are a meaningful minority of the evidence, not a long-tail sliver. The
    # observed baseline was 75 conflicted signals carrying 2.8-2.9% of total
    # weight, which is exactly the case this gate must not flag.
    #
    # Weight with the same scoring factor _aggregate applies, i.e. the
    # signal's magnitude, not merely its pre-strength _signal_weight. The score
    # is driven by magnitude, so the conflict share below must be a share of
    # the same evidence the score sums.
    cluster_conflicted_weight = 0.0
    cluster_total_weight = 0.0
    for s in relevant:
        signal_weight = (
            _signal_weight(s, now) * STRENGTH_MULT.get(s.strength, 0.45)
        )
        cluster_total_weight += signal_weight
        if getattr(s, "cluster_conflict", False):
            # Only the evidence that actually disagreed - the losing side of
            # the cluster collapse - counts as conflicted. The survivor's own
            # weight is dominant evidence, not a torn market; counting it in
            # full made this gate fire on nearly every tick.
            cluster_conflicted_weight += getattr(s, "conflict_weight", 0.0)

    cluster_share = (
        cluster_conflicted_weight / cluster_total_weight
        if cluster_total_weight > 0.0
        else 0.0
    )
    # ``>=`` is the intended comparison. The tie tolerance only keeps a
    # nominally exact 5% boundary from being a false negative through binary
    # floating-point division.
    cluster_conflict = (
        cluster_total_weight >= CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT
        and (
            cluster_share >= CLUSTER_CONFLICT_WEIGHT_SHARE
            or abs(cluster_share - CLUSTER_CONFLICT_WEIGHT_SHARE) < 1e-12
        )
    )
    flip_margin = effective_flip_margin(cluster_conflict)

    # ``evidence_split`` caps confidence: the aggregate bull/bear totals are
    # balanced above the placeholder ratio. The confidence cap reads this flag
    # only.
    evidence_split = evidence_split_from_totals(bull, bear)

    # STEP 8: a detected contradiction must not be reported as a call.
    #
    # Both sides are heavily weighted, so whatever number the engine prints the
    # split is a coin toss with a thumb on the scale. Cap the confidence at
    # TIEBREAK_MAX_CONFIDENCE (0.30) and force the LOW label, so a card cannot
    # show "BEARISH 67%" alongside confidence 0.62 and be read as conviction.
    # The flip-margin widening above already handles the "should we flip" half;
    # this handles the "how loudly do we speak" half.
    if evidence_split:
        confidence = cap_confidence_for_evidence_split(confidence)

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
        # The weighted lean is PUBLISHED, not discarded. This branch used to
        # return score / raw_score / damped_score = 0.0, and the dashboard
        # projects the score straight onto the card
        # (bull_pct = round(50 + score / 2), display.to_display). Every poll
        # whose evidence sat under the weight floor therefore froze every card
        # on "BULLISH 50% / BEARISH 50%" even though the lean was computable
        # from the same two totals that picked the label.
        #
        # The lean is scaled by how much of the floor the evidence reached, so
        # a card fades toward an even split as the evidence thins (a lone
        # stale headline can no longer draw a near-100% bar) and the published
        # number is continuous across the gate: at exactly MIN_TOTAL_WEIGHT
        # the scale is 1.0, and this branch is not taken past it. raw_score
        # keeps the UNSCALED ratio so the audit trail is unaffected.
        weak_raw = _score_from(bull, bear)
        weak_score = payload_score(
            round(weak_raw * min(1.0, total_weight / MIN_TOTAL_WEIGHT), 1),
            weak_bias)
        weak_confidence = min(
            _confidence_from(bull, bear, agg["directionless"], agg["used"]),
            TIEBREAK_MAX_CONFIDENCE)
        return SentimentResult(
            asset=asset, score=weak_score, raw_score=weak_raw,
            bull_weight=bull, bear_weight=bear,
            damped_score=weak_score, confidence=weak_confidence,
            bias=weak_bias, mode=mode,
            drivers=drivers_early, conflict=conflict_early,
            bull_drivers=bull_drivers_early, bear_drivers=bear_drivers_early,
            delta_60m=_delta_60m(list(signals), asset, now),
            event_risk=event_risk, event_risk_note=event_risk_note,
            contributing_articles=agg["used"], contributing_clusters=agg["clusters"],
            stale=is_stale, signal_count=len(relevant),
            burst_bull=agg["burst_bull"], burst_bear=agg["burst_bear"],
            note=stale_note,
            cluster_conflict=cluster_conflict,
            evidence_split=evidence_split,
            confidence_label=confidence_label_for(weak_confidence, True),
        )

    # BINARY BIAS: label = sign of the net news score. Inside EPS the sign is
    # not trustworthy, so the heaviest, newest directional signal breaks the
    # tie. Confidence is reported but never gates the label.
    #
    # BASELINE: CONTEXT, NOT A TRANSFORM.
    #
    # The baseline is the asset's trailing median of its own recent raw
    # scores. The obvious use - subtract it and publish the centred value -
    # was implemented and then MEASURED against the live history, and it does
    # not do what it was built to do:
    #
    #   * It barely changes the bias mix. The live database ran BTCUSD at
    #     86.6% BEARISH and EQUITY at 90.8% BEARISH with ZERO bull-dominant
    #     ticks; with the baseline applied the label changes on only 0.7-3.4%
    #     of ticks, because the baseline IS the trailing level and the day's
    #     level IS the trailing level.
    #   * It costs the card its information. The sign rule fires on only
    #     9.7-25.4% of ticks (window 20) instead of the current ~66%, so
    #     75-90% of every card would become a tie-break verdict: LOW
    #     confidence, a 50/50 bar, and a label chosen by the single heaviest
    #     headline. "Always bearish" would become "always uninformative".
    #
    # The reason is structural, not a tuning mistake: a trailing baseline is a
    # CHANGE detector, so it removes the change and leaves the level. A
    # STRUCTURAL skew - a corpus whose vocabulary is negative - lives in the
    # level, and no amount of centring will remove it.
    #
    # So the baseline is published as CONTEXT: the card carries both "how
    # negative is today" (the level, which is what the label and the bar
    # show) and "how that compares with this asset's own recent norm". When
    # the two agree, the reading is in line with the asset's norm and the
    # note says so - which is the actionable half of the idea, and the half
    # that survives the measurement.
    raw = score
    centred, baseline_used = _apply_baseline(raw, baseline)
    in_line_with_norm = (
        baseline_used is not None and abs(centred) < EPS)
    if abs(raw) >= EPS:
        computed_bias = Bias.BULLISH if raw > 0 else Bias.BEARISH
    else:
        # Item 5: low confidence - if confidence is too low, we still produce
        # a binary call via tie-break but cap confidence and mark LOW quality.
        if confidence < FLIP_MIN_CONFIDENCE:
            confidence = min(confidence, TIEBREAK_MAX_CONFIDENCE)
        computed_bias = _tie_break_bias(relevant, history_key=key, now=now)
        # The cap applies only when the tie-break contradicts the raw score's
        # lean: the tie-break names the opposite side of the lean. When the
        # tie-break confirms the lean (or there is no lean to contradict,
        # raw == 0), the cap does not apply - the tie-break is corroborating,
        # not overriding, the verdict.
        disagrees = (
            (raw > 0 and computed_bias is Bias.BEARISH)
            or (raw < 0 and computed_bias is Bias.BULLISH)
        )
        if disagrees:
            # Tie-break names the opposite side of the raw score's lean: cap
            # confidence low so the payload reads as weak once a side is named.
            confidence = min(confidence, TIEBREAK_MAX_CONFIDENCE)
        else:
            # Tie-break confirms the lean (or there is no lean to contradict,
            # raw == 0): the cap does not apply, confidence stays at the
            # formula value.
            confidence = raw_confidence

    # Dampened score for display/cue strength (raw used for bias/flip/stability)
    damped = _damped_score(raw, total_weight)

    # STABILITY (Task 8): check if we should flip from the last published bias.
    #
    # ``gate_confidence`` is the confidence the flip gates are evaluated
    # against - the tick's own reading, captured BEFORE any hold-branch cap
    # is applied below, so the number in the note is the number the gate saw.
    last_bias = recall_bias(key)
    gate_confidence = confidence
    should_flip, flipped_bias, flip_reason = _should_flip(
        bull, bear, last_bias, relevant, now, key,
        flip_margin_override=flip_margin,
        confidence=gate_confidence)

    low_quality = False

    if not should_flip and last_bias is not None:
        # Hold the previous bias - payload score must match the held label
        final_bias = last_bias
        # Use payload_score to ensure score sign matches the held label
        score = payload_score(raw, final_bias)
        # If raw agrees with held bias (same sign, outside EPS), confirm without capping
        agrees = (abs(raw) >= EPS) and ((raw > 0) == (last_bias is Bias.BULLISH))
        if agrees:
            note = f"Bias confirmed: {last_bias.value} maintained with raw score {raw:.1f}."
            low_quality = False
        elif flip_reason == "low_confidence":
            # The reading was too thin to move a published verdict. Say so -
            # a reader who sees a held badge needs to know the evidence never
            # cleared the bar, not that the evidence disagreed.
            confidence = min(confidence, TIEBREAK_MAX_CONFIDENCE)
            note = (f"Bias held (confidence {gate_confidence:.2f} below the "
                    f"flip floor): {last_bias.value} maintained despite raw "
                    f"score {raw:.1f}.")
            low_quality = True
        else:
            # Held-but-weak call: cap confidence low
            confidence = min(confidence, TIEBREAK_MAX_CONFIDENCE)
            note = f"Bias held (stability): {last_bias.value} maintained despite raw score {raw:.1f}."
            low_quality = True
    else:
        # Flip to computed bias (or first-run bias), or use flipped_bias from Tier-1 bypass
        final_bias = flipped_bias if last_bias is not None else computed_bias
        score = payload_score(raw, final_bias)
        if last_bias is not None and final_bias != last_bias:
            _record_flip_time(key, _now_aware(now))
            if flip_reason == "tier1_override":
                # The badge moved on a fresh Tier-1 release while the day's
                # aggregate has not caught up. Say so - the published score is
                # the label's lean, so the note is where the aggregate lives.
                note = (f"Bias flipped: {last_bias.value} -> {final_bias.value} "
                        f"on a fresh Tier-1 release (day aggregate still "
                        f"{raw:+.1f}).")
            else:
                note = f"Bias flipped: {last_bias.value} -> {final_bias.value} (raw {raw:.1f})."
        else:
            note = ""
        # Only remember non-tie-break verdicts (abs(raw) >= EPS)
        if abs(raw) >= EPS:
            remember_bias(key, final_bias)
        # Tie-break case (raw inside EPS) is low quality
        low_quality = abs(raw) < EPS

        # CONTRADICTION SPLIT: an evidence-split verdict is low quality too,
        # however the score landed. The two sides are balanced, so the label
        # names which side happened to be heavier - not a conviction. This
        # forces confidence_label to LOW regardless of the numeric confidence.
        if evidence_split:
            low_quality = True

    # Drivers follow the PUBLISHED LABEL, not the computed one.
    #
    # This used to be ``computed_bias``: the side the CURRENT SIGNALS point to.
    # That produced two different stories on one card. When the stability
    # gates hold the previous bias, or when a fresh Tier-1 signal flips it,
    # ``final_bias`` and ``computed_bias`` disagree - and the driver box
    # followed the other one. Measured on the live database, 39.7% of ticks
    # published a badge whose #1 driver pointed the OPPOSITE way: the single
    # headline a day trader would act on, arguing against the verdict above
    # it.
    #
    # The card must be ONE story, so the drivers follow ``final_bias`` - the
    # side the badge names. Nothing is lost: ``bull_drivers`` and
    # ``bear_drivers`` still carry BOTH sides, and ``conflict`` still names
    # the strongest opposing headline.
    leading = bull_items if final_bias is Bias.BULLISH else bear_items
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

    # Baseline context. The reading is IN LINE with this asset's own recent
    # norm when the level and the trailing baseline agree - which is the
    # actionable half of the baseline idea: a -42 that is BTC's normal is not
    # the same information as a -42 that is far outside its norm.
    if baseline_used is not None:
        if in_line_with_norm:
            context = (f" In line with this asset's recent norm "
                       f"(baseline {baseline_used:+.1f}).")
            note = (note + context) if note else context.strip()
        else:
            deviation = centred - 0.0
            direction_word = ("more bullish" if deviation > 0
                              else "more bearish")
            context = (f" {abs(deviation):.1f} points {direction_word} than "
                       f"this asset's recent norm (baseline "
                       f"{baseline_used:+.1f}).")
            note = (note + context) if note else context.strip()

    return SentimentResult(
        asset=asset,
        score=score,
        raw_score=raw,
        baseline_score=baseline_used,
        bull_weight=bull,
        bear_weight=bear,
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
        cluster_conflict=cluster_conflict,
        evidence_split=evidence_split,
        confidence_label=confidence_label_for(confidence, low_quality),
    )


def score_from_signals(signals: Sequence[WeightedSignal], asset: Asset,
                       now: datetime | None = None) -> float:
    """Convenience wrapper returning only the signed score."""
    return compute(signals, asset, now=now).score