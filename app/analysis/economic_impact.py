"""
ECONOMIC CALENDAR IMPACT ENGINE.

Turns scheduled macro releases into per-asset directional signals so the
economic calendar is weighed alongside the news feed when the final
bullish/bearish verdict is decided.

Two honest modes - the engine NEVER invents a print:

  RELEASED  an ``actual`` value exists. Direction comes from the realised
             surprise versus forecast (actual above forecast = "hot").
  EXPECTED  no ``actual`` yet. Direction comes only from the published
             consensus (forecast versus previous). This is labelled an
             expectation, never an outcome.

By default the PIPELINE uses RELEASED prints only
(``calendar_include_upcoming = False``): an un-released print is a consensus
expectation, not a realised move, so it does not reach the bullish/bearish
verdict, the economic-calendar cards, or the market-prep drivers until the
provider publishes the actual. The moment the same release reappears with an
``actual`` it is resolved through the RELEASED branch and counted then.

Every event carries a transmission chain explaining HOW it reaches the
asset (e.g. "hot CPI -> higher real yields -> bearish gold").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
)

log = get_logger(__name__)


# --------------------------------------------------------------------------
# Event taxonomy
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class EventProfile:
    """How one class of macro print transmits into the three assets."""

    category: NewsCategory
    # Relevance per asset (this is the ASSET RELEVANCE factor).
    relevance: dict[Asset, float]
    # What a "hot" (above-forecast) print does to each asset.
    hot_bias: dict[Asset, Direction]
    # Base importance of the class, before the provider's own impact tag.
    base_importance: EconomicImportance
    transmission: str


def _p(
    category: NewsCategory,
    gold: float,
    silver: float,
    btc: float,
    hot: tuple[Direction, Direction, Direction],
    importance: EconomicImportance,
    chain: str,
) -> EventProfile:
    return EventProfile(
        category=category,
        relevance={Asset.GOLD: gold, Asset.SILVER: silver,
                   Asset.BITCOIN: btc},
        hot_bias={Asset.GOLD: hot[0], Asset.SILVER: hot[1],
                  Asset.BITCOIN: hot[2]},
        base_importance=importance,
        transmission=chain,
    )

# "Hot" inflation / strong labour / hawkish policy = tighter policy
# expectations = higher real yields + stronger dollar = metals and crypto down.
PROFILES: tuple[EventProfile, ...] = (
    _p(NewsCategory.INFLATION, 0.86, 0.84, 0.76,
       (Direction.BEARISH, Direction.BEARISH, Direction.BEARISH),
       EconomicImportance.VERY_HIGH,
       "Hot inflation -> higher rate expectations -> stronger USD and real yields -> bearish for metals and crypto"),
    _p(NewsCategory.EMPLOYMENT, 0.80, 0.76, 0.78,
       (Direction.BEARISH, Direction.BEARISH, Direction.BEARISH),
       EconomicImportance.VERY_HIGH,
       "Strong payrolls -> hawkish Fed path -> higher yields and USD -> bearish for metals and crypto"),
    _p(NewsCategory.CENTRAL_BANK, 0.90, 0.88, 0.86,
       (Direction.BEARISH, Direction.BEARISH, Direction.BEARISH),
       EconomicImportance.VERY_HIGH,
       "Hawkish central bank -> tighter policy -> stronger USD and real yields -> bearish for metals and crypto"),
    _p(NewsCategory.INTEREST_RATES, 0.88, 0.84, 0.82,
       (Direction.BEARISH, Direction.BEARISH, Direction.BEARISH),
       EconomicImportance.HIGH,
       "Higher rates -> higher discount rate -> bearish for non-yielding assets"),
    _p(NewsCategory.TREASURY_YIELDS, 0.92, 0.86, 0.80,
       (Direction.BEARISH, Direction.BEARISH, Direction.BEARISH),
       EconomicImportance.VERY_HIGH,
       "Rising nominal and real yields -> opportunity cost rises -> bearish for metals"),
    _p(NewsCategory.USD_DXY, 0.88, 0.84, 0.70,
       (Direction.BEARISH, Direction.BEARISH, Direction.MIXED),
       EconomicImportance.HIGH,
       "Dollar strength pressures dollar-priced commodities and crypto"),
    _p(NewsCategory.GDP, 0.66, 0.70, 0.68,
       (Direction.MIXED, Direction.MIXED, Direction.MIXED),
       EconomicImportance.HIGH,
       "Growth data reprices the cycle - direction depends on the growth/inflation mix"),
    _p(NewsCategory.INDUSTRIAL_DEMAND, 0.30, 0.70, 0.22,
       (Direction.MIXED, Direction.BEARISH, Direction.MIXED),
       EconomicImportance.MEDIUM,
       "Strong industrial demand supports silver more than gold; gold barely reacts"),
    _p(NewsCategory.CHINA, 0.52, 0.80, 0.40,
       (Direction.MIXED, Direction.BULLISH, Direction.MIXED),
       EconomicImportance.MEDIUM,
       "Strong China data lifts silver industrial demand"),
)

# Title patterns -> profile index. Order matters: the first match wins, so the
# specific markers are tested before the generic ones.
TITLE_RULES: tuple[tuple[str, int], ...] = (
    ("core pce", 0), ("pce price index", 0), ("ppi", 0), ("producer price", 0),
    ("cpi", 0), ("consumer price", 0), ("inflation", 0), ("price index", 0),
    ("deflator", 0),
    ("nonfarm", 1), ("non-farm", 1), ("nfp", 1), ("payroll", 1),
    ("jobless claim", 1), ("unemployment rate", 1), ("employment change", 1),
    ("initial claim", 1), ("labour market", 1), ("labor market", 1),
    ("fomc", 2), ("federal reserve", 2), ("fed ", 2), ("powell", 2),
    ("ecb", 2), ("boe", 2), ("boj", 2), ("central bank", 2),
    ("rate decision", 3), ("policy rate", 3), ("interest rate", 3),
    ("cash rate", 3),
    ("treasury yield", 4), ("bond yield", 4), ("10-year", 4), ("auction", 4),
    ("dxy", 5), ("dollar index", 5),
    ("gdp", 6), ("pmi", 6), ("ism", 6), ("industrial production", 6),
    ("retail sales", 6), ("consumer confidence", 6),
    ("copper", 7), ("industrial", 7), ("manufacturing", 7), ("factory", 7),
    ("china", 8), ("chinese", 8), ("caixin", 8), ("pboc", 8),
)

# Countries whose currency is a first-order driver for all three assets.
USD_CURRENCY = {"USD", "US", "USA"}

# Impact labels the provider may send, and how strong they are.
_IMPACT_RANK = {"holiday": 0, "low": 1, "medium": 2, "high": 3}


# --------------------------------------------------------------------------
# Phase 3: per-indicator polarity
# --------------------------------------------------------------------------
# "Hot" is only hot for SOME indicators. A payrolls beat is a hot economy; an
# unemployment-rate beat is a COLD economy. Treating every indicator as
# "above forecast = hot" is a straight sign error on the two prints that carry
# the most labour-market information, and it points gold the wrong way.
#
# The map is CONFIGURABLE rather than hard-coded so the behaviour can be
# corrected without a release.
_INVERSE_KEYWORDS: tuple[str, ...] = ()
_CORE_KEYWORDS = (
    "core",          # core CPI / core PCE exclude food and energy
    "excl. food",
    "excluding food",
    "core pce",
)


def _inverse_indicators() -> tuple[str, ...]:
    """
    Keywords whose ABOVE-FORECAST print means a COLD (weak) economy.

    Parsed from ``calendar_inverse_indicators`` as "keyword=+1|-1" pairs; -1
    marks an inverse indicator. Longer keywords are tested first so "initial
    jobless claim" wins over the shorter "jobless claim" on an overlapping
    title - both mean the same thing, but matching the shorter needle first
    would make the intent depend on dict ordering.
    """
    global _INVERSE_KEYWORDS
    if _INVERSE_KEYWORDS:
        return _INVERSE_KEYWORDS
    parsed: list[str] = []
    for pair in (settings.calendar_inverse_indicators or "").split(","):
        keyword, _, sign = pair.partition("=")
        keyword = keyword.strip().lower()
        if keyword and sign.strip() == "-1":
            parsed.append(keyword)
    parsed.sort(key=len, reverse=True)
    _INVERSE_KEYWORDS = tuple(parsed)
    return _INVERSE_KEYWORDS


def indicator_polarity(event: CalendarEvent) -> int:
    """
    +1 when a higher-than-forecast print is HOT, -1 when it is COLD.

    Defaults to +1. Only a title that matches a configured inverse indicator
    flips, so an unrecognised print keeps the ordinary reading rather than
    being inverted by guesswork.
    """
    if not settings.calendar_polarity_enabled:
        return 1
    lowered = (event.title or "").lower()
    for keyword in _inverse_indicators():
        if keyword in lowered:
            return -1
    return 1


def is_core_release(event: CalendarEvent) -> bool:
    """True for the headline-measure variant of a release (core CPI, core PCE)."""
    lowered = (event.title or "").lower()
    return any(needle in lowered for needle in _CORE_KEYWORDS)


def _frequency_of(title: str) -> str:
    """
    Which frequency variant a row is, so m/m and y/y can be told apart.

    Returns "mom", "yoy" or "" (neither). Used to stop one release being
    counted twice: a single CPI print publishes both and they are the same
    macro moment, not two.
    """
    lowered = (title or "").lower()
    # Check the spelled forms first, then the terse ones providers also use.
    if any(n in lowered for n in ("y/y", "yy", "yoy", "year-on-year",
                                  "year over year", "12-month", "12 month")):
        return "yoy"
    if any(n in lowered for n in ("m/m", "mm", "mom", "month-on-month",
                                  "month over month")):
        return "mom"
    return ""


def release_family(event: CalendarEvent) -> str:
    """
    The release a row belongs to, with its frequency variant stripped.

    "US CPI MoM" and "US CPI YoY" both reduce to "us cpi", so a dedupe can
    tell they are one release rather than two unrelated events.
    """
    lowered = (event.title or "").lower()
    stripped = lowered
    for variant in ("month-on-month", "month over month", "year-on-year",
                    "year over year", "12-month", "12 month",
                    "y/y", "m/m", "yoy", "mom", "yy", "mm"):
        stripped = stripped.replace(variant, " ")
    # Collapse the punctuation left behind by the strip.
    cleaned = " ".join("".join(ch if ch.isalnum() else " "
                              for ch in stripped).split())
    return cleaned


# --------------------------------------------------------------------------
# Event + signal records
# --------------------------------------------------------------------------
@dataclass
class CalendarEvent:
    """One scheduled macro release, normalised from the provider feed."""

    title: str
    country: str
    when_utc: datetime
    impact: str = "Medium"          # High / Medium / Low / Holiday
    forecast: float | None = None
    previous: float | None = None
    actual: float | None = None   # None until the print is released
    provider: str = "forexfactory"

    @property
    def is_released(self) -> bool:
        return self.actual is not None

    @property
    def event_key(self) -> str:
        """Stable identity so repeated polls update rather than duplicate."""
        stamp = self.when_utc.strftime("%Y%m%dT%H%M")
        return f"{self.country}:{self.title.lower().strip()}:{stamp}"


@dataclass
class CalendarSignal:
    """A calendar event resolved into per-asset directional input."""

    event: CalendarEvent
    profile: EventProfile
    asset: Asset
    direction: Direction
    strength: ImpactStrength
    importance: EconomicImportance
    relevance: float
    persistence: float
    mode: str                      # RELEASED / EXPECTED
    reason: str
    transmission: str
    surprise: float | None = None


# --------------------------------------------------------------------------
# Parsing + classification
# --------------------------------------------------------------------------
def to_float(raw) -> float | None:
    """
    Parse a calendar value into a float, or None when it is not a number.

    Handles what real providers actually send:
        '3.4%'  '-0.2%'  '235K'  '1.2M'  '$74.2B'  '1,250'
    Returns None - never a guess - for anything it cannot read:
        None  ''  '-'  '--'  'N/A'  '<0.1%'  '>'  '1.5|2.1'  '?'  'TBC'
    '<0.1%' is a BOUND, not a measurement, so treating it as 0.1 would invent
    a number the provider never published.

    A pipe ('1.5|2.1') is a revised|original pair; it is ambiguous rather than
    a single value, so it yields None and the caller computes no surprise.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value == value and abs(value) != float("inf") else None

    text = str(raw).strip()
    if not text:
        return None

    # Reject anything that is not a single unambiguous number. Checked BEFORE
    # stripping so a bound or a revision pair is never coerced.
    lowered = text.lower()
    if lowered in {"-", "--", "---", "n/a", "na", "none", "null", "tbc",
                   "?", "tba", "unchanged", "no change"}:
        return None
    if "|" in text:                      # '1.5|2.1' revised|original
        return None
    if text.startswith(("<", ">", "≈", "~")):   # '<' '>' bounds
        return None

    text = text.replace(",", "").replace("$", "").replace(" ", " ")
    text = text.strip()
    if not text:
        return None

    # Trailing unit: % K M B T
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1]
    else:
        suffix = text[-1:].upper()
        for letter, factor in (("T", 1e12), ("B", 1e9),
                               ("M", 1e6), ("K", 1e3)):
            if suffix == letter:
                multiplier, text = factor, text[:-1]
                break

    text = text.strip()
    if not text:
        return None
    try:
        value = float(text) * multiplier
    except ValueError:
        return None
    # A non-finite result is not a measurement either.
    if value != value or abs(value) == float("inf"):
        return None
    return value


def classify_event(event: CalendarEvent) -> EventProfile | None:
    """Match a calendar title to a transmission profile, or None if unknown."""
    lowered = (event.title or "").lower()
    if not lowered:
        return None
    for needle, index in TITLE_RULES:
        if needle in lowered and index < len(PROFILES):
            return PROFILES[index]
    return None


def importance_from_impact(impact: str,
                           profile: EventProfile) -> EconomicImportance:
    """
    Combine the provider's own impact tag with the class default.

    A High-impact print is never demoted below MEDIUM, and a Low print is never
    promoted to the class ceiling, so a minor release cannot become a
    market-moving signal.
    """
    level = _IMPACT_RANK.get((impact or "").strip().lower(), 2)
    base = _IMPACT_RANK.get(profile.base_importance.value.lower(), 2)
    combined = level if level >= base else max(level, base - 1)
    return {
        3: EconomicImportance.VERY_HIGH,
        2: EconomicImportance.HIGH,
        1: EconomicImportance.MEDIUM,
        0: EconomicImportance.LOW,
    }[combined]


def _country_multiplier(event: CalendarEvent, asset: Asset) -> float:
    """
    How much a country's print should move ONE asset, as a 0..1 multiplier.

    A Japanese CPI print moves Japanese assets; it does not move gold the way
    a US print does. Without this the engine applies a US-calibrated relevance
    to every country, which over-weights the quiet ones.

    Parsed from config as "CCY=weight" pairs, plus optional "CCY:ASSET"
    overrides. Anything unlisted gets a conservative default rather than the
    US weight, so a newly added country cannot be over-weighted by omission.
    """
    if not settings.calendar_country_multipliers_enabled:
        return 1.0

    overrides: dict[str, float] = {}
    for pair in (settings.calendar_country_asset_overrides or "").split(","):
        key, _, value = pair.partition("=")
        key = key.strip().upper()
        if key and value.strip():
            try:
                overrides[key] = float(value)
            except ValueError:
                continue

    country = (event.country or "").upper()
    # The asset-specific override wins: China's industrial cycle matters far
    # more to silver and oil than to gold or bitcoin.
    for key in (f"{country}:{asset.value}", f"{country}:{asset.name}"):
        if key in overrides:
            return overrides[key]

    weights: dict[str, float] = {}
    for pair in (settings.calendar_country_multipliers or "").split(","):
        key, _, value = pair.partition("=")
        key = key.strip().upper()
        if key and value.strip():
            try:
                weights[key] = float(value)
            except ValueError:
                continue

    return weights.get(country,
                       float(settings.calendar_country_multiplier_default))


def _apply_polarity(bias: Direction, polarity: int) -> Direction:
    """
    Flip a hot-bias for an inverse indicator.

    A MIXED bias is left alone: it means "this class does not move the asset on
    its own", and inverting it would manufacture a direction that does not
    exist.
    """
    if polarity >= 0 or bias is Direction.MIXED:
        return bias
    return _opposite(bias)


def _opposite(direction: Direction) -> Direction:
    if direction is Direction.BULLISH:
        return Direction.BEARISH
    if direction is Direction.BEARISH:
        return Direction.BULLISH
    return Direction.MIXED


def _strength_for(impact: str, importance: EconomicImportance,
                  surprise: float | None, country: str | None = None) -> ImpactStrength:
    """
    Size the move by provider impact + importance + surprise magnitude.

    A small beat is not the same trade as a large one, so the magnitude of the
    surprise scales strength upward - but only for RELEASED prints, because an
    expected value has no realised magnitude to measure.

    Non-USD countries are capped at MEDIUM to prevent quiet calendars from
    producing outsized signals.
    """
    if country and country.upper() not in USD_CURRENCY:
        return ImpactStrength.MEDIUM
    tag = (impact or "").lower()
    if tag == "high":
        base = ImpactStrength.VERY_HIGH
    elif importance is EconomicImportance.VERY_HIGH:
        base = ImpactStrength.HIGH
    else:
        base = ImpactStrength.MEDIUM

    if surprise is None:
        return base
    magnitude = abs(surprise)
    if magnitude >= 1.0:
        return base
    if magnitude >= 0.3:
        return ImpactStrength.HIGH
    return ImpactStrength.MEDIUM


# --------------------------------------------------------------------------
# Direction resolution
# --------------------------------------------------------------------------
def evaluate(event: CalendarEvent, asset: Asset,
             profile: EventProfile | None = None) -> CalendarSignal | None:
    """
    Resolve one calendar event into a directional signal for one asset.

    Returns None when the event is unknown, irrelevant to the asset, or has no
    usable data - the engine would rather show nothing than invent a signal.
    """
    profile = profile or classify_event(event)
    if profile is None:
        return None

    relevance = profile.relevance.get(asset, 0.0)
    if relevance <= 0.0:
        return None

    # Only USD data transmits cleanly to all three assets. Other countries are
    # kept when they carry genuine industrial/China read-through (silver).
    if (event.country or "").upper() not in USD_CURRENCY and relevance < 0.45:
        return None

    hot_bias = profile.hot_bias.get(asset, Direction.MIXED)
    importance = importance_from_impact(event.impact, profile)

    # Phase 3: "hot" depends on the INDICATOR. An unemployment rate above
    # forecast means a weaker economy, so the print is COLD even though the
    # number is higher. Applying that sign here - at the single point where
    # direction is decided - keeps every branch below consistent.
    polarity = indicator_polarity(event)
    hot_bias = _apply_polarity(hot_bias, polarity)

    # A core measure (core CPI, core PCE) is the cleaner read of the same
    # release, so it carries slightly more weight than the food-and-energy
    # headline it sits beside.
    core_bonus = (float(settings.calendar_core_bonus)
                  if is_core_release(event) else 0.0)

    # Phase 5: scale relevance by the country. Applied to RELEVANCE rather than
    # to strength, because relevance is the "how much does this print mean for
    # this asset" factor - exactly what a quiet country's print changes.
    country_multiplier = _country_multiplier(event, asset)
    weighted_relevance = relevance * country_multiplier

    # ---- RELEASED: direction from the STANDARDISED surprise ----
    if event.actual is not None and event.forecast is not None:
        from app.analysis.calendar_risk import (
            standardised_surprise,
            surprise_band,
        )

        # Both operands must be REAL numbers. A provider placeholder ('-',
        # '<0.1%', '1.5|2.1') that reached here unparsed would raise a TypeError
        # on the subtraction below and could take the poll down, so a non-numeric
        # value is treated as "no usable data" and yields no signal - never a
        # guess and never an exception.
        actual_v = to_float(event.actual)
        forecast_v = to_float(event.forecast)
        if actual_v is None or forecast_v is None:
            log.info("Calendar: %s has a non-numeric actual/forecast "
                     "(actual=%r forecast=%r) - no surprise computed",
                     event.title[:70], event.actual, event.forecast)
            return None

        surprise = actual_v - forecast_v
        # Item 8: measure the miss in standard deviations of THIS indicator
        # rather than as a flat percentage of the forecast, so a 0.2% CPI miss
        # and a 120k payroll miss are comparable.
        z = standardised_surprise(actual_v, forecast_v, event.title)
        band = surprise_band(z)

        if band == "NEUTRAL" or hot_bias is Direction.MIXED:
            direction = Direction.NEUTRAL
            reason = (f"{event.title} came in at {actual_v:g} vs "
                      f"{forecast_v:g} forecast - no material surprise "
                      f"({band.lower()}).")
        else:
            is_hot = (z or 0.0) > 0
            direction = hot_bias if is_hot else _opposite(hot_bias)
            qualifier = "above" if is_hot else "below"
            reason = (f"{event.title} released {actual_v:g}, {qualifier} the "
                      f"{forecast_v:g} forecast ({surprise:+g}, "
                      f"{band.lower()} surprise, {z:+.2f} sigma).")

        return CalendarSignal(
            event=event, profile=profile, asset=asset, direction=direction,
            strength=_strength_for(event.impact, importance, z, event.country),
            importance=importance,
            relevance=min(1.0, weighted_relevance * (1.0 + core_bonus)),
            persistence=0.75, mode="RELEASED", reason=reason,
            transmission=profile.transmission, surprise=round(z or 0.0, 4),
        )

    # ---- EXPECTED: direction from published consensus only ----
    if event.forecast is not None and event.previous is not None:
        # Same rule as the released path: a placeholder is "no data", not 0.0.
        forecast_v = to_float(event.forecast)
        previous_v = to_float(event.previous)
        if forecast_v is None or previous_v is None:
            log.info("Calendar: %s has a non-numeric forecast/previous "
                     "(forecast=%r previous=%r) - no signal",
                     event.title[:70], event.forecast, event.previous)
            return None

        delta = forecast_v - previous_v
        if abs(delta) < 1e-9 or hot_bias is Direction.MIXED:
            direction = Direction.NEUTRAL
            reason = (f"{event.title} is scheduled; consensus is unchanged from "
                      f"the previous print.")
        else:
            is_hot = delta > 0
            direction = hot_bias if is_hot else _opposite(hot_bias)
            qualifier = "above" if is_hot else "below"
            reason = (f"{event.title} expected {qualifier} the previous print "
                      f"({delta:+g}). Consensus expectation, not an outcome.")

        # An un-released print is a catalyst/risk, not a realised move.
        return CalendarSignal(
            event=event, profile=profile, asset=asset, direction=direction,
            strength=_strength_for(event.impact, importance, None),
            importance=importance,
            relevance=min(1.0, weighted_relevance * (1.0 + core_bonus)),
            persistence=0.35, mode="EXPECTED", reason=reason,
            transmission=profile.transmission, surprise=None,
        )

    # No forecast and no previous: nothing to reason from.
    return None


# --------------------------------------------------------------------------
# Aggregation + presentation
# --------------------------------------------------------------------------
def signals_for_event(event: CalendarEvent) -> list[CalendarSignal]:
    """All asset signals for one event (skipped where not relevant)."""
    profile = classify_event(event)
    if profile is None:
        return []
    out: list[CalendarSignal] = []
    for asset in Asset:
        sig = evaluate(event, asset, profile)
        if sig is not None:
            out.append(sig)
    return out


def signals_for_events(events: Sequence[CalendarEvent],
                       horizon_hours: int = 48) -> list[CalendarSignal]:
    """
    Calendar signals for everything inside the forward-looking horizon.

    Events older than two hours are excluded: that print is history, and the
    news feed already covers it. Un-released events are excluded by default
    (``calendar_include_upcoming``): only a print whose ``actual`` has been
    published can carry the verdict, so an expectation never moves the score
    before the release exists.

    Delegates to :func:`resolve_calendar_signals`, which applies the Phase 3
    and Phase 6 stages after resolution.
    """
    return resolve_calendar_signals(events, horizon_hours=horizon_hours)


# --------------------------------------------------------------------------
# Phase 3: m/m vs y/y dedupe
# --------------------------------------------------------------------------
def dedupe_frequency_variants(
        signals: Sequence[CalendarSignal],
) -> list[CalendarSignal]:
    """
    Keep ONE signal per (asset, release, minute) when a release publishes both
    a monthly and a yearly figure.

    A single CPI print arrives as "CPI MoM" and "CPI YoY" at the same minute.
    Both are real and they usually agree, but counting both would enter the
    same macro moment twice and double the move it implies - which is the
    "one evidence, two names" problem the news side already guards against.

    The KEPT row is chosen on evidence, not on title order:

      1. the row that actually produced a usable surprise, then
      2. the core measure, because it excludes food and energy noise, then
      3. the larger absolute surprise, which carries more information.

    Returns a new list; the input is not mutated.
    """
    if not settings.calendar_dedupe_mom_yoy:
        return list(signals)

    # Group key: one asset, one release, one release minute. The release minute
    # keeps two genuinely separate prints (this month vs next week) apart.
    groups: dict[tuple[str, str, str], list[CalendarSignal]] = {}
    for sig in signals:
        key = (sig.asset.value,
               release_family(sig.event),
               sig.event.when_utc.strftime("%Y%m%dT%H%M"))
        groups.setdefault(key, []).append(sig)

    kept: list[CalendarSignal] = []
    for bucket in groups.values():
        variants = {_frequency_of(s.event.title) for s in bucket} - {""}
        if len(variants) < 2:
            # Not an m/m + y/y pair: every row is genuinely distinct.
            kept.extend(bucket)
            continue

        def rank(sig: CalendarSignal):
            return (
                sig.surprise is not None,
                is_core_release(sig.event),
                abs(sig.surprise or 0.0),
            )

        kept.append(max(bucket, key=rank))

    kept.sort(key=lambda s: (s.event.when_utc, s.asset.value))
    return kept


# --------------------------------------------------------------------------
# Phase 6: awaiting-release dampening + composite release cap
# --------------------------------------------------------------------------
def apply_awaiting_release(signals: Sequence[CalendarSignal],
                           now: datetime | None = None
                           ) -> list[CalendarSignal]:
    """
    Damp or drop an EXPECTED signal whose scheduled time has already passed
    with no actual yet.

    Four states, all honest about what is actually known:

      before the time      unchanged - it is still a live expectation
      within the grace     unchanged - a print can legitimately be late
      past grace..timeout  DAMPED    - the wait is itself information
      past the timeout     DROPPED   - a stale expectation is not evidence

    Only EXPECTED signals are touched: a RELEASED print is never damped,
    because its actual value is known regardless of the clock.
    """
    if not settings.calendar_awaiting_release_enabled:
        return list(signals)

    reference = now or datetime.now(timezone.utc)
    grace = timedelta(minutes=float(
        settings.calendar_awaiting_release_grace_minutes))
    timeout = timedelta(minutes=float(
        settings.calendar_awaiting_release_timeout_minutes))
    penalty = float(settings.calendar_awaiting_release_penalty)

    out: list[CalendarSignal] = []
    for sig in signals:
        if sig.mode != "EXPECTED" or sig.event.when_utc is None:
            out.append(sig)
            continue

        overdue = reference - sig.event.when_utc
        if overdue <= grace:
            out.append(sig)
            continue
        if overdue >= timeout:
            log.info("Calendar: dropping %r - still no actual %s after its "
                     "scheduled time", sig.event.title[:60], _ago(overdue))
            continue

        out.append(_damp(sig, penalty,
                         f"{sig.reason} Awaiting release, damped "
                         f"{_ago(overdue)} past its scheduled time."))
    return out


def _damp(sig: CalendarSignal, penalty: float, note: str) -> CalendarSignal:
    """Copy a signal with reduced weight, carrying the reason forward."""
    return CalendarSignal(
        event=sig.event, profile=sig.profile, asset=sig.asset,
        direction=sig.direction, strength=sig.strength,
        importance=sig.importance,
        relevance=sig.relevance * penalty,
        persistence=sig.persistence * penalty,
        mode=sig.mode, reason=note, transmission=sig.transmission,
        surprise=sig.surprise,
    )


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return "%d min" % minutes if minutes < 90 else "%dh%02dm" % divmod(
        minutes, 60)


def cap_composite_release(
        signals: Sequence[CalendarSignal],
        max_weight: float | None = None,
) -> list[CalendarSignal]:
    """
    Cap the weight a SINGLE macro moment can contribute.

    Several prints share one timestamp - NFP arrives with the unemployment
    rate and average hourly earnings. They are genuinely separate data, but
    they are one market moment, and summing all three would let a single
    release outweigh everything else in the calendar combined.

    THE CAP IS APPLIED PER ASSET. For each asset independently, that asset's
    signals are summed across the cluster and, if the total exceeds the cap,
    scaled down proportionally. One asset's scaling never touches another
    asset's weights, so a release that is overwhelmingly relevant to gold does
    not suppress the same release's read for silver or oil.

    This matters because a cluster always contains one signal per asset per
    print. Under a single shared factor, a 4-asset cluster split the cap four
    ways and every asset landed near 0.33 - far below the configured 1.30 for
    any one of them, which is not what "cap one macro moment" means.

    When a cluster exceeds the cap its signals are scaled PROPORTIONALLY, so
    relative weighting within the asset is preserved. Nothing is dropped and no
    direction is reversed: the cap reduces the total, it does not hide it.

    ``calendar_composite_cap_mode = "global"`` restores the previous shared-
    factor behaviour.
    """
    limit = (float(max_weight) if max_weight is not None
             else float(settings.calendar_composite_max_weight))
    if limit <= 0:
        return list(signals)

    per_asset = (settings.calendar_composite_cap_mode or "per_asset"
                 ).strip().lower() == "per_asset"

    window = timedelta(minutes=float(
        settings.calendar_composite_window_minutes))

    # Sort by time first so clustering walks the calendar in order. Events with
    # no timestamp sort first and each starts its own cluster.
    ordered = sorted(
        signals,
        key=lambda s: (s.event.when_utc is None,
                       s.event.when_utc or datetime(1970, 1, 1,
                                                    tzinfo=timezone.utc)))

    clusters: list[list[CalendarSignal]] = []
    for sig in ordered:
        when = sig.event.when_utc
        previous = clusters[-1][0].event.when_utc if clusters else None
        if (when is not None and previous is not None
                and when - previous <= window):
            clusters[-1].append(sig)
        else:
            clusters.append([sig])

    out: list[CalendarSignal] = []
    for cluster in clusters:
        if len(cluster) == 1:
            out.append(cluster[0])
            continue

        if per_asset:
            # Group this cluster BY ASSET and cap each group on its own. An
            # asset already under the limit is left completely untouched, so
            # scaling one asset can never change another asset's weight.
            by_asset: dict[Asset, list[CalendarSignal]] = {}
            for sig in cluster:
                by_asset.setdefault(sig.asset, []).append(sig)

            for asset, group in by_asset.items():
                total = sum(s.relevance for s in group)
                if total <= limit:
                    out.extend(group)
                    continue
                scale = limit / total
                log.info("Calendar: composite release at %s - %s has %d prints "
                         "totaling %.3f relevance, scaled by %.3f to reach the "
                         "%.2f cap", cluster[0].event.when_utc, asset.value,
                         len(group), total, scale, limit)
                for sig in group:
                    out.append(_damp(sig, scale,
                                     f"{sig.reason} Part of a {len(group)}-print"
                                     f" {asset.value} release; combined weight "
                                     f"capped."))
            continue

        # Global mode: one shared factor across the whole cluster. Retained so
        # the previous behaviour is recoverable from config alone.
        total = sum(s.relevance for s in cluster)
        if total <= limit:
            out.extend(cluster)
            continue
        scale = limit / total
        log.info("Calendar: composite release at %s - %d prints total %.3f "
                 "relevance, scaled by %.3f to reach the %.2f cap",
                 cluster[0].event.when_utc, len(cluster), total, scale, limit)
        for sig in cluster:
            out.append(_damp(sig, scale,
                             f"{sig.reason} Part of a {len(cluster)}-print "
                             f"release; combined weight capped."))
    return out


def resolve_calendar_signals(events: Sequence[CalendarEvent],
                             horizon_hours: int = 48,
                             now: datetime | None = None,
                             ) -> list[CalendarSignal]:
    """
    The full calendar pipeline, in the order the phases depend on each other:

      1. resolve each event into per-asset signals
      2. collapse the m/m and y/y rows of one release    (Phase 3)
      3. damp or drop overdue expectations               (Phase 6)
      4. cap the weight of one shared-timestamp cluster  (Phase 6)

    Order matters: the m/m+y/y collapse MUST run before the cluster cap. If it
    ran after, one print would arrive twice, pass the cap on its own merits,
    and then be halved again by the cluster it duplicated.

    ``signals_for_events`` is kept as the pre-Phase-3 entry point and delegates
    here, so existing callers pick the new behaviour up automatically.

    UN-RELEASED EVENTS ARE SKIPPED unless ``calendar_include_upcoming`` is set.
    The pipeline therefore counts a print only once its ``actual`` exists, which
    is also what makes the release appear in the cards: the provider's next
    poll re-reports the same release (same ``event_key``) with the number
    filled in, and it is resolved then through the RELEASED branch.
    """
    reference = now or datetime.now(timezone.utc)
    # horizon_hours may arrive as a non-int (a test double, a string, a
    # MagicMock auto-attribute). ``max(0, x)`` raises TypeError for anything
    # that is not orderable against an int, and that exception propagated out
    # of the pipeline and killed every subsequent tick (the calendar is
    # supposed to be optional enrichment, never fatal). Coerce defensively.
    try:
        horizon = int(horizon_hours)
    except (TypeError, ValueError):
        horizon = 48
    if horizon < 0:
        horizon = 0
    cutoff = reference + timedelta(hours=horizon)
    signals: list[CalendarSignal] = []
    for event in events:
        if event.when_utc is None:
            continue
        if event.when_utc < reference - timedelta(hours=2):
            continue
        if event.when_utc > cutoff:
            continue
        # An un-released print has no actual, so it is a consensus expectation
        # rather than a realised move: it must not enter the aggregation, the
        # cards, or the prep drivers before the number exists. With the flag
        # off it is skipped here and resolves as a RELEASED signal on the poll
        # that carries the actual.
        if event.actual is None and not settings.calendar_include_upcoming:
            continue
        signals.extend(signals_for_event(event))

    signals = dedupe_frequency_variants(signals)
    signals = apply_awaiting_release(signals, now=reference)
    return cap_composite_release(signals)


def headline(event: CalendarEvent, signal: CalendarSignal) -> str:
    """One-line label used as the signal title in the dashboard."""
    stamp = event.when_utc.strftime("%H:%M UTC")
    tag = "" if signal.mode == "RELEASED" else " (expected)"
    return f"[{event.country}] {event.title}{tag} - {stamp}"


def to_calendar_summary(signals: Sequence[CalendarSignal], asset: Asset,
                        limit: int = 8) -> list[dict]:
    """
    Dashboard-ready rows for one asset, released prints first.

    Kept separate from the news feed so a reader can always tell a scheduled
    macro release from a news headline.
    """
    rows: list[dict] = []
    for sig in signals:
        if sig.asset is not asset:
            continue
        ev = sig.event
        rows.append({
            "title": ev.title,
            "country": ev.country,
            "when_utc": ev.when_utc,
            "impact": ev.impact,
            "mode": sig.mode,
            "direction": sig.direction.value,
            "strength": sig.strength.value,
            "importance": sig.importance.value,
            "forecast": ev.forecast,
            "previous": ev.previous,
            "actual": ev.actual,
            "surprise": sig.surprise,
            "reason": sig.reason,
            "transmission": sig.transmission,
        })
    rows.sort(key=lambda r: (
        {"RELEASED": 0, "EXPECTED": 1}.get(r["mode"], 2),
        -r["when_utc"].timestamp(),
    ))
    return rows[:limit]
