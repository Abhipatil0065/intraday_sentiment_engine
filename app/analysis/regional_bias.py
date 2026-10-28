"""
REGIONAL MACRO BIAS ENGINE.

The news-weighted verdict answers "what does the whole feed say about gold?".
This engine answers a different, narrower question: "what do the scheduled
macro releases of FIVE REGIONS say about each asset?", on its own scale, with
its own arithmetic, auditable line by line.

It reads the SAME FinancialJuice events the rest of the engine already
fetches - no new provider, no second scrape - and adds one thing the existing
pipeline does not have: a signed, per-asset, region-attributed score with an
explicit time decay.

THE ARITHMETIC
--------------
For every released print that survives the region whitelist:

    contribution(asset) = |surprise|
                         x impact_weight          (High 3 / Medium 2 / Low 1)
                         x region_weight          (USA 1.0, ECB 0.6, ...)
                         x asset_sensitivity      (signed, -1 .. +1)
                         x time_decay             (1.0 for 4h, 0 at 48h)

The SIGN lives in ``asset_sensitivity``: the row for the event's scenario
already says whether that scenario is good or bad for the asset. The
"direction flag" of the brief is what SELECTS that row:

    a US CPI beat            -> hot  -> USD_HAWKISH -> gold NEGATIVE
    a US CPI miss            -> cold -> USD_DOVISH  -> gold POSITIVE
    a US unemployment beat   -> WEAK (inverted)    -> gold POSITIVE
    a hawkish ECB decision   -> ECB_HAWKISH        -> gold POSITIVE (weaker USD)

The surprise MAGNITUDE is |z|, the standardised miss:
``(actual - forecast) / std`` using the engine's own std table, with a
percent-deviation fallback for an indicator that has no std of its own.

Then, per asset:

    score = 100 * raw / scale, clipped to [-100, +100]

and a label from the configured bands.

WHAT IS DELIBERATELY NOT SCORED
-------------------------------
* An UPCOMING print adds NO score. Nobody has published the number yet; a
  card that moves on a guess is moving on nothing. The print is listed as an
  EVENT RISK instead, and inside its risk window the neutral zone is widened
  so a thin score is not presented as a call.
* A SPEECH with no direction tag scores NOTHING. It has no numbers, and an
  untagged speech is not a hawkish speech.
* An event with no actual or no forecast scores NOTHING.
* An event older than the decay window scores NOTHING.

Every event that DOES contribute is logged with its region, weights, surprise
and final contribution, so any number on the dashboard can be traced back to
the release that produced it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.analysis.calendar_config import (
    CalendarBiasConfig,
    CalendarConfigError,
    Region,
    apply_impact_overrides,
    classify_region,
    load_calendar_config,
    title_matches,
)
from app.analysis.calendar_risk import matched_std
from app.analysis.economic_impact import to_float
from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import Asset

log = get_logger(__name__)

#: The five instruments this engine scores. The brief's "global equities" is
#: the engine's existing EQUITY card; OIL is WTI crude.
PANEL_ASSETS: tuple[Asset, ...] = (
    Asset.GOLD, Asset.SILVER, Asset.BITCOIN, Asset.EQUITY, Asset.OIL,
)

#: Panel labels. Asset.EQUITY is labelled "S&P 500" everywhere else in the
#: engine; on this panel it is the global equities basket.
PANEL_LABELS: dict[Asset, str] = {
    Asset.GOLD: "GOLD",
    Asset.SILVER: "SILVER",
    Asset.BITCOIN: "BITCOIN",
    Asset.EQUITY: "EQUITIES",
    Asset.OIL: "OIL",
}

DEFAULT_LABEL_NAMES = {
    "strong_bearish": "Strong Bearish",
    "bearish": "Bearish",
    "neutral": "Neutral",
    "bullish": "Bullish",
    "strong_bullish": "Strong Bullish",
}


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class EventRead:
    """How one released event was read: the numbers and the verdict."""

    title: str
    country: str
    when_utc: datetime
    impact: str
    region_code: str
    region_label: str
    scenario: str
    stance: str
    surprise: float              # signed z
    surprise_pct: float | None
    std_source: str              # "standardised" | "percent"
    is_inverted: bool
    is_speech: bool
    is_decision: bool
    reason: str


@dataclass(frozen=True)
class Contribution:
    """One event's contribution to one asset."""

    asset: Asset
    title: str
    country: str
    when_utc: datetime
    impact: str
    region_code: str
    region_label: str
    scenario: str
    stance: str
    surprise: float
    impact_weight: float
    region_weight: float
    sensitivity: float
    time_decay: float
    contribution: float
    reason: str


@dataclass(frozen=True)
class UpcomingEvent:
    """A scheduled release that has not happened yet."""

    title: str
    country: str
    when_utc: datetime
    impact: str
    region_code: str
    region_label: str
    minutes_away: int


@dataclass(frozen=True)
class AssetBias:
    """The verdict for one asset."""

    asset: Asset
    label: str
    score: float
    bias: str
    raw_score: float
    contributions: tuple[Contribution, ...] = ()
    region_breakdown: tuple[tuple[str, str, float], ...] = ()
    top_drivers: tuple[Contribution, ...] = ()
    event_risk: dict | None = None
    events_considered: int = 0
    events_scored: int = 0


@dataclass(frozen=True)
class RegionalBias:
    """The whole panel: four asset verdicts plus the calendar context."""

    generated_at_utc: datetime
    assets: tuple[AssetBias, ...] = ()
    upcoming: tuple[UpcomingEvent, ...] = ()
    regions: tuple[str, ...] = ()
    stats: dict = field(default_factory=dict)
    error: str = ""
    stale: bool = False
    stale_reason: str = ""
    last_updated_utc: datetime | None = None

    def asset(self, asset: Asset) -> AssetBias | None:
        for row in self.assets:
            if row.asset is asset:
                return row
        return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _impact_rank(impact: str) -> int:
    return {"Holiday": 0, "Low": 1, "Medium": 2, "High": 3}.get(
        (impact or "").strip().capitalize(), 0)


def time_decay(when_utc: datetime, now: datetime, cfg: CalendarBiasConfig
                ) -> float:
    """
    How much influence a release still has.

    Full weight for ``full_weight_hours`` after the release, then a linear
    decay to zero at ``decay_to_zero_hours``. An event that has not happened
    yet has NO influence (0.0) - it is a risk, not evidence.
    """
    if when_utc is None:
        return 0.0
    age_hours = (now - when_utc).total_seconds() / 3600.0
    if age_hours < 0:
        return 0.0
    full = float(cfg.full_weight_hours)
    horizon = float(cfg.decay_to_zero_hours)
    if age_hours <= full:
        return 1.0
    if age_hours >= horizon:
        return 0.0
    if horizon <= full:
        return 0.0
    return max(0.0, 1.0 - (age_hours - full) / (horizon - full))


def surprise_for(event, cfg: CalendarBiasConfig
                 ) -> tuple[float | None, float | None, str]:
    """
    The surprise of one release: ``(z, percent_deviation, source)``.

    ``z`` is ``(actual - forecast) / std`` for an indicator the engine has a
    std for, and ``((actual - forecast) / |forecast|) * 100 / percent_divisor``
    for one it does not. Both are None when the print cannot be compared with
    anything - a missing or unparseable actual or forecast SKIPS the event,
    it never guesses.

    The magnitude is capped at ``surprise.max_sigma``: the std table holds
    rule-of-thumb estimates, and one indicator whose std is too tight must
    not be able to swamp every other release in the panel.
    """
    actual = to_float(getattr(event, "actual", None))
    forecast = to_float(getattr(event, "forecast", None))
    if actual is None or forecast is None:
        return None, None, "none"

    pct = None
    if forecast != 0:
        pct = (actual - forecast) / abs(forecast) * 100.0

    title = str(getattr(event, "title", "") or "")
    if cfg.use_standardised:
        std = matched_std(title)
        if std:
            return _cap_sigma((actual - forecast) / std, cfg), pct, \
                "standardised"
    if pct is None:
        return None, None, "none"
    divisor = float(cfg.percent_divisor) or 1.0
    return _cap_sigma(pct / divisor, cfg), pct, "percent"


def _cap_sigma(value: float, cfg: CalendarBiasConfig) -> float:
    """Clip a surprise to the configured maximum number of sigma."""
    cap = float(cfg.surprise_max_sigma or 0.0)
    if cap <= 0:
        return value
    return max(-cap, min(cap, value))


def is_inverted(title: str, cfg: CalendarBiasConfig) -> bool:
    """True when a HIGHER-than-forecast print of this title is a WEAK economy."""
    return title_matches(cfg.inverted_keywords, title)


def speech_stance(title: str, cfg: CalendarBiasConfig
                  ) -> tuple[str | None, tuple[str, ...]]:
    """
    The stance of a speech or press conference, from its keyword tags.

    Returns ``(stance, matched_keywords)``. The stance is None when the title
    carries no tag - an untagged speech scores NOTHING, because a speech
    without a directional word in its title is not evidence of a direction.

    A title that matches BOTH a hawkish and a dovish tag is treated as
    untagged: an ambiguous speech is not a direction.
    """
    stances: set[str] = set()
    matched: list[str] = []
    for stance, keywords in cfg.speech_tags:
        for keyword in keywords:
            if title_matches((keyword,), title):
                stances.add(stance)
                matched.append(keyword)
    if len(stances) != 1:
        return None, tuple(matched)
    return next(iter(stances)), tuple(matched)


def resolve_stance(event, cfg: CalendarBiasConfig, region: Region,
                   z: float) -> tuple[str | None, str, bool, bool, bool]:
    """
    The stance of one event: ``(stance, reason, inverted, is_speech, is_decision)``.

    Three kinds of event, three different questions:

      SPEECH    it has no numbers, so its direction comes from its keyword
                tags. No tag -> no stance -> no score.
      DECISION  a rate decision: above expectation is the region's TIGHT
                stance, below it is the LOOSE stance.
      DATA      above expectation is HOT (strong) unless the indicator is
                INVERTED - the unemployment rate and jobless claims, where
                higher means a weaker economy.
    """
    title = str(getattr(event, "title", "") or "")
    inverted = is_inverted(title, cfg)
    is_speech = title_matches(cfg.speech_keywords, title)
    is_decision = title_matches(cfg.decision_keywords, title)

    if is_speech:
        stance, hits = speech_stance(title, cfg)
        if stance is None:
            return None, ("speech with no direction tag"
                          + (f" (matched {', '.join(hits)})" if hits else "")
                          ), inverted, True, False
        # The matched keywords are logged: a direction nobody can trace is a
        # direction nobody should trust.
        log.info("Regional bias: speech tag - %s matched %s -> %s",
                 title[:60], ", ".join(hits) or "-", stance)
        return (stance, f"speech tagged {stance.lower()} "
                f"via '{', '.join(hits)}'"), inverted, True, False

    if is_decision:
        stances = cfg.stance_map.get(region.code) or {}
        tight = str(stances.get("tight") or "HAWKISH")
        loose = str(stances.get("loose") or "DOVISH")
        if z > 0:
            return tight, f"policy decision above expectation ({tight})", \
                inverted, False, True
        return loose, f"policy decision below expectation ({loose})", \
            inverted, False, True

    if inverted:
        if z > 0:
            return "WEAK", "above forecast, but this indicator is inverted", \
                True, False, False
        return "STRONG", "below forecast on an inverted indicator", \
            True, False, False

    if z > 0:
        return "STRONG", "above forecast (hot print)", False, False, False
    return "WEAK", "below forecast (cold print)", False, False, False


def scenario_for(region_code: str, stance: str, cfg: CalendarBiasConfig
                 ) -> str | None:
    """The sensitivity row for this region and stance, or None."""
    return (cfg.scenario_map.get(region_code) or {}).get(stance)


def normalise_score(raw: float, cfg: CalendarBiasConfig) -> float:
    """The raw sum mapped onto -100 .. +100."""
    scale = float(cfg.normal_scale) or 1.0
    if cfg.normal_method == "tanh":
        value = 100.0 * math.tanh(raw / scale)
    else:
        value = 100.0 * raw / scale
    return max(float(cfg.normal_min), min(float(cfg.normal_max), value))


def label_for(score: float, cfg: CalendarBiasConfig,
              neutral_multiplier: float = 1.0) -> str:
    """
    The label for a score.

    ``neutral_multiplier`` WIDENS THE NEUTRAL ZONE only: the inner bands move
    out, the strong bands stay put. That is what an imminent release does to a
    score - it makes a thin reading less meaningful, it does not make a strong
    one impossible.
    """
    names = dict(DEFAULT_LABEL_NAMES)
    names.update(cfg.label_names or {})
    multiplier = max(1.0, float(neutral_multiplier or 1.0))
    negative_edge = abs(float(cfg.label_bearish)) * multiplier
    positive_edge = float(cfg.label_bullish) * multiplier

    if score <= float(cfg.label_strong_bearish):
        return names["strong_bearish"]
    if score <= -negative_edge:
        return names["bearish"]
    if score < positive_edge:
        return names["neutral"]
    if score < float(cfg.label_strong_bullish):
        return names["bullish"]
    return names["strong_bullish"]


# --------------------------------------------------------------------------
# Frequency-variant dedupe
# --------------------------------------------------------------------------
class _TitleHolder:
    """A stand-in for a CalendarEvent - release_family() only reads .title."""

    __slots__ = ("title",)

    def __init__(self, title: str) -> None:
        self.title = title


def _release_family(title: str) -> str:
    """
    The release a row belongs to, with its m/m and y/y variant stripped.

    Delegates to the engine's own ``release_family`` so there is ONE
    definition of "which rows are the same release" in the codebase.
    """
    from app.analysis.economic_impact import release_family

    return release_family(_TitleHolder(title))


def dedupe_frequency_variants(reads: list[EventRead],
                              cfg: CalendarBiasConfig) -> list[EventRead]:
    """
    Keep ONE row per release when a print publishes an m/m AND a y/y figure.

    They are the same macro moment published twice, and counting both would
    double the move it implies. The row kept is the one with the larger
    surprise - the more informative half of the same print.
    """
    if not cfg.dedupe_frequency_variants:
        return list(reads)
    groups: dict[tuple[str, str, str], list[EventRead]] = {}
    for read in reads:
        key = (read.region_code, _release_family(read.title),
               read.when_utc.strftime("%Y%m%dT%H%M"))
        groups.setdefault(key, []).append(read)
    kept: list[EventRead] = []
    for bucket in groups.values():
        if len(bucket) == 1:
            kept.extend(bucket)
            continue
        kept.append(max(bucket, key=lambda r: abs(r.surprise)))
    kept.sort(key=lambda r: r.when_utc)
    return kept


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------
def calendar_freshness(cfg: CalendarBiasConfig | None = None,
                        now: datetime | None = None) -> dict:
    """
    When was the calendar last updated, and is it stale?

    The provider's own health is the source of truth. The events themselves
    are checked too: a payload served from the stale cache says so in its
    provider field.
    """
    cfg = cfg or load_calendar_config()
    reference = _now(now)
    last_updated: datetime | None = None
    failures = 0
    last_error = ""
    try:
        from app.ingestion import fj_calendar

        health = fj_calendar.provider_health()
        entry = health.get("financialjuice") or {}
        raw = entry.get("last_success_utc")
        if raw:
            try:
                parsed = datetime.fromisoformat(str(raw))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                last_updated = parsed.astimezone(timezone.utc)
            except ValueError:
                last_updated = None
        failures = int(entry.get("consecutive_failures") or 0)
        last_error = str(entry.get("last_error") or "")
    except Exception as exc:  # noqa: BLE001 - health must never break a panel
        log.debug("calendar provider health unavailable: %s", exc)

    stale = False
    reason = ""
    if last_updated is None:
        stale = True
        reason = ("the calendar provider has not completed a successful poll "
                  "yet")
    else:
        age_minutes = (reference - last_updated).total_seconds() / 60.0
        if age_minutes > float(cfg.stale_minutes):
            stale = True
            reason = (f"the calendar was last updated {int(age_minutes)} min "
                      f"ago (stale after {cfg.stale_minutes} min)")

    return {
        "last_updated_utc": last_updated,
        "age_minutes": (None if last_updated is None else
                        (reference - last_updated).total_seconds() / 60.0),
        "stale": stale,
        "stale_reason": reason,
        "consecutive_failures": failures,
        "last_error": last_error,
    }


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------
def _read_events(events, cfg: CalendarBiasConfig, now: datetime
                 ) -> tuple[list[EventRead], int, int]:
    """
    Turn released events into reads.

    Returns ``(reads, considered, skipped)``. An event that cannot be read -
    no numbers, no direction, no scenario - is skipped and counted, never
    guessed at.
    """
    reads: list[EventRead] = []
    considered = 0
    skipped = 0
    min_rank = _impact_rank(cfg.impact_min)

    # The forced impact is applied BEFORE any impact weighting, here as well
    # as at the calendar input: the weight must follow the forced value
    # wherever the scoring happens, not only where the poll happened.
    try:
        events, _changes = apply_impact_overrides(events or [], cfg)
    except Exception as exc:  # noqa: BLE001 - an override must never break a score
        log.debug("Regional bias: impact override skipped - %s", exc)
        events = list(events or [])

    for event in (events or []):
        when = getattr(event, "when_utc", None)
        title = str(getattr(event, "title", "") or "")
        if when is None:
            skipped += 1
            continue
        impact = str(getattr(event, "impact", "") or "")
        if _impact_rank(impact) < min_rank:
            skipped += 1
            continue
        # Not released yet, or released without a published number: no score.
        if when > now:
            skipped += 1
            continue
        if time_decay(when, now, cfg) <= 0.0:
            skipped += 1
            continue

        region, reason = classify_region(event, cfg)
        if region is None:
            skipped += 1
            log.debug("Regional bias: skipping %r - %s",
                      str(getattr(event, "title", ""))[:60], reason)
            continue

        considered += 1

        # A SPEECH carries no number, so it never has a surprise. It is read
        # from its keyword tags instead, and an untagged speech scores
        # NOTHING. Its magnitude is a configured number of sigma - how much a
        # tagged speech is worth - not an invented surprise.
        if title_matches(cfg.speech_keywords, title):
            stance, hits = speech_stance(title, cfg)
            if stance is None:
                skipped += 1
                log.info("Regional bias: %s (%s) skipped - untagged speech, "
                         "scores nothing", title[:60], region.code)
                continue
            magnitude = float(cfg.speech_magnitude)
            z = magnitude
            source = "speech-tag"
            inverted = False
            is_decision = False
            reason = (f"speech tagged {stance.lower()} via "
                      f"'{', '.join(hits)}'")
            log.info("Regional bias: speech tag - %s matched %s -> %s",
                     title[:60], ", ".join(hits) or "-", stance)
        else:
            z, pct, source = surprise_for(event, cfg)
            if z is None:
                skipped += 1
                log.info("Regional bias: %s (%s) has no usable actual/forecast - "
                         "skipped, no surprise invented", title[:60], region.code)
                continue
            if abs(z) < float(cfg.surprise_min_magnitude):
                skipped += 1
                continue
            magnitude = abs(z)
            stance, reason, inverted, is_speech, is_decision = resolve_stance(
                event, cfg, region, z)
            if stance is None:
                skipped += 1
                log.info("Regional bias: %s (%s) skipped - %s", title[:60],
                         region.code, reason)
                continue

        scenario = scenario_for(region.code, stance, cfg)
        if scenario is None:
            skipped += 1
            log.info("Regional bias: %s (%s) skipped - no sensitivity row for "
                     "stance %s", title[:60], region.code, stance)
            continue

        reads.append(EventRead(
            title=title,
            country=region.code,
            when_utc=when,
            impact=impact,
            region_code=region.code,
            region_label=region.label,
            scenario=scenario,
            stance=stance,
            surprise=z,
            surprise_pct=None,
            std_source=source,
            is_inverted=inverted,
            is_speech=(source == "speech-tag"),
            is_decision=is_decision,
            reason=reason,
        ))
    return reads, considered, skipped


def _contributions(reads: list[EventRead], cfg: CalendarBiasConfig,
                   now: datetime) -> list[Contribution]:
    """Expand every read into its per-asset contributions."""
    out: list[Contribution] = []
    for read in reads:
        decay = time_decay(read.when_utc, now, cfg)
        impact_weight = cfg.impact_weight(read.impact)
        region = cfg.region_by_code(read.region_code)
        region_weight = region.weight if region is not None else 1.0
        magnitude = abs(read.surprise)
        for asset in PANEL_ASSETS:
            sensitivity = cfg.sensitivity(read.scenario, asset.value)
            if sensitivity == 0.0:
                continue
            contribution = (magnitude * impact_weight * region_weight
                            * sensitivity * decay)
            # AUDIT LINE. Every event that contributes to a score is logged
            # with its region, its weights, its surprise and the number it
            # produced, so any figure on the dashboard can be traced back to
            # the release that caused it.
            log.info(
                "Regional bias: %s [%s] %s -> %s %s | surprise %+.3f (%s) "
                "x impact %.1f x region %.2f x sensitivity %+.2f x decay %.2f "
                "= %+.4f",
                read.title[:60], read.region_code, asset.value, read.scenario,
                read.stance, read.surprise, read.std_source, impact_weight,
                region_weight, sensitivity, decay, contribution)
            out.append(Contribution(
                asset=asset,
                title=read.title,
                country=read.country,
                when_utc=read.when_utc,
                impact=read.impact,
                region_code=read.region_code,
                region_label=read.region_label,
                scenario=read.scenario,
                stance=read.stance,
                surprise=read.surprise,
                impact_weight=impact_weight,
                region_weight=region_weight,
                sensitivity=sensitivity,
                time_decay=decay,
                contribution=contribution,
                reason=read.reason,
            ))
    return out


def _event_risk(upcoming: list[UpcomingEvent], cfg: CalendarBiasConfig,
                now: datetime) -> dict | None:
    """
    The imminent-release flag for the panel.

    An upcoming high-impact print inside its risk window (before_minutes
    before the release until after_minutes after it) means any score is
    provisional, so the neutral zone is widened.
    """
    for item in upcoming:
        start = item.when_utc - timedelta(minutes=cfg.risk_before_minutes)
        end = item.when_utc + timedelta(minutes=cfg.risk_after_minutes)
        if start <= now <= end:
            return {
                "active": True,
                "title": item.title,
                "country": item.country,
                "region_code": item.region_code,
                "when_utc": item.when_utc,
                "minutes_away": item.minutes_away,
                "impact": item.impact,
                "note": (f"EVENT RISK: {item.title} releases in "
                         f"~{item.minutes_away} min - the score below is "
                         f"provisional until the print is published."),
            }
    return None


def compute_regional_bias(events, now: datetime | None = None,
                          config: CalendarBiasConfig | None = None
                          ) -> RegionalBias:
    """
    Score every panel asset from the whitelisted calendar regions.

    Never raises: a broken config produces an error result that the dashboard
    renders as an error, not a crash.
    """
    reference = _now(now)
    try:
        cfg = config or load_calendar_config()
    except CalendarConfigError as exc:
        log.error("Regional bias: config unavailable - %s", exc)
        return RegionalBias(generated_at_utc=reference, error=str(exc),
                            stale=True,
                            stale_reason="the calendar config could not be read")

    try:
        reads, considered, skipped = _read_events(events, cfg, reference)
        reads = dedupe_frequency_variants(reads, cfg)
        contributions = _contributions(reads, cfg, reference)
    except Exception as exc:  # noqa: BLE001 - a panel must never break a poll
        log.error("Regional bias: scoring failed - %s: %s",
                  type(exc).__name__, exc, exc_info=True)
        return RegionalBias(generated_at_utc=reference,
                            error=f"{type(exc).__name__}: {exc}",
                            stale=True, stale_reason="the bias engine failed")

    # --- UPCOMING high-impact releases (no score, a risk flag) -------------
    upcoming: list[UpcomingEvent] = []
    horizon = reference + timedelta(hours=float(cfg.upcoming_horizon_hours))
    min_upcoming_rank = _impact_rank(cfg.upcoming_min_impact)
    for event in (events or []):
        when = getattr(event, "when_utc", None)
        if when is None or when <= reference or when > horizon:
            continue
        if _impact_rank(str(getattr(event, "impact", "") or "")) < min_upcoming_rank:
            continue
        region, _reason = classify_region(event, cfg)
        if region is None:
            continue
        upcoming.append(UpcomingEvent(
            title=str(getattr(event, "title", "")),
            country=region.code,
            when_utc=when,
            impact=str(getattr(event, "impact", "") or ""),
            region_code=region.code,
            region_label=region.label,
            minutes_away=int((when - reference).total_seconds() // 60),
        ))
    upcoming.sort(key=lambda u: u.when_utc)
    upcoming = upcoming[: max(0, int(cfg.upcoming_max_items))]

    risk = _event_risk(upcoming, cfg, reference)
    neutral_multiplier = (float(cfg.risk_neutral_multiplier)
                          if risk and risk.get("active") else 1.0)

    # --- per-asset scores --------------------------------------------------
    assets_out: list[AssetBias] = []
    for asset in PANEL_ASSETS:
        rows = [c for c in contributions if c.asset is asset]
        if cfg.max_events_per_asset > 0 and len(rows) > cfg.max_events_per_asset:
            rows = sorted(rows, key=lambda c: abs(c.contribution),
                          reverse=True)[:cfg.max_events_per_asset]
        raw = sum(c.contribution for c in rows)
        score = normalise_score(raw, cfg)
        bias = label_for(score, cfg, neutral_multiplier)

        breakdown: dict[str, tuple[str, float]] = {}
        for row in rows:
            code, label, total = breakdown.get(
                row.region_code, (row.region_code, row.region_label, 0.0))
            breakdown[row.region_code] = (code, label, total + row.contribution)
        # A region whose releases all landed on forecast contributes nothing
        # and is left off the card: "GBP +0.00" is noise, not information.
        region_rows = tuple(
            (code, label, round(total, 4))
            for code, label, total in sorted(
                breakdown.values(), key=lambda v: -abs(v[2]))
            if round(total, 6) != 0.0)

        drivers = tuple(sorted(rows, key=lambda c: abs(c.contribution),
                               reverse=True)[:max(0, int(cfg.top_drivers))])

        assets_out.append(AssetBias(
            asset=asset,
            label=PANEL_LABELS.get(asset, asset.label),
            score=round(score, 2),
            bias=bias,
            raw_score=round(raw, 4),
            contributions=tuple(rows),
            region_breakdown=region_rows,
            top_drivers=drivers,
            event_risk=risk,
            events_considered=considered,
            events_scored=len({(c.title, c.when_utc) for c in rows}),
        ))

    freshness = calendar_freshness(cfg, reference)
    stale = bool(freshness.get("stale"))
    stale_reason = str(freshness.get("stale_reason") or "")
    if not any(a.contributions for a in assets_out) and not stale:
        stale = True
        stale_reason = ("no whitelisted macro release in the scoring window "
                        "- the calendar is empty, not neutral")

    return RegionalBias(
        generated_at_utc=reference,
        assets=tuple(assets_out),
        upcoming=tuple(upcoming),
        regions=tuple(r.code for r in cfg.regions),
        stats={
            "events_total": len(list(events or [])),
            "events_read": len(reads),
            "events_considered": considered,
            "events_skipped": skipped,
            "events_scored": len(reads),
            "contributions": len(contributions),
            "neutral_multiplier": neutral_multiplier,
        },
        error="",
        stale=stale,
        stale_reason=stale_reason,
        last_updated_utc=freshness.get("last_updated_utc"),
    )


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------
def _local_time(value: datetime | None) -> str:
    """A UTC instant rendered in the engine's DISPLAY_TIMEZONE."""
    if value is None:
        return ""
    from app.utils import timeutils as T

    return value.astimezone(T.display_tz()).strftime("%Y-%m-%d %H:%M")


def _countdown(minutes_away: int) -> str:
    if minutes_away <= 0:
        return "now"
    if minutes_away < 60:
        return f"in {minutes_away} min"
    hours, rest = divmod(minutes_away, 60)
    if hours < 24:
        return f"in {hours}h {rest:02d}m"
    days, hours = divmod(hours, 24)
    return f"in {days}d {hours}h"


def to_payload(result: RegionalBias) -> dict:
    """Dashboard-ready payload for the REGIONAL MACRO BIAS panel."""
    assets: list[dict] = []
    for row in result.assets:
        assets.append({
            "asset": row.asset.value,
            "label": row.label,
            "score": row.score,
            "bias": row.bias,
            "raw_score": row.raw_score,
            "events_considered": row.events_considered,
            "events_scored": row.events_scored,
            "region_breakdown": [
                {"code": code, "label": label, "contribution": total}
                for code, label, total in row.region_breakdown
            ],
            "top_drivers": [
                {
                    "title": d.title,
                    "country": d.country,
                    "when_utc": d.when_utc.isoformat(),
                    "when_local": _local_time(d.when_utc),
                    "impact": d.impact,
                    "region_code": d.region_code,
                    "region_label": d.region_label,
                    "scenario": d.scenario,
                    "stance": d.stance,
                    "surprise": round(d.surprise, 3),
                    "contribution": round(d.contribution, 3),
                    "reason": d.reason,
                }
                for d in row.top_drivers
            ],
            "event_risk": row.event_risk,
        })

    upcoming = [
        {
            "title": u.title,
            "country": u.country,
            "region_code": u.region_code,
            "region_label": u.region_label,
            "when_utc": u.when_utc.isoformat(),
            "when_local": _local_time(u.when_utc),
            "impact": u.impact,
            "minutes_away": u.minutes_away,
            "countdown": _countdown(u.minutes_away),
        }
        for u in result.upcoming
    ]

    from app.utils import timeutils as T

    return {
        "generated_at_utc": result.generated_at_utc.isoformat(),
        "generated_at_local": _local_time(result.generated_at_utc),
        "display_timezone": settings.display_timezone,
        "last_updated_utc": (result.last_updated_utc.isoformat()
                             if result.last_updated_utc else None),
        "last_updated_local": _local_time(result.last_updated_utc),
        "stale": result.stale,
        "stale_reason": result.stale_reason,
        "error": result.error,
        "regions": list(result.regions),
        "assets": assets,
        "upcoming": upcoming,
        "stats": result.stats,
        "now_local": T.local_clock(result.generated_at_utc),
    }
