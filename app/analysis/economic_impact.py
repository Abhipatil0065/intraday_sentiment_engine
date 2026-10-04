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

Every event carries a transmission chain explaining HOW it reaches the
asset (e.g. "hot CPI -> higher real yields -> bearish gold").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

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
        relevance={Asset.GOLD: gold, Asset.SILVER: silver, Asset.BITCOIN: btc},
        hot_bias={Asset.GOLD: hot[0], Asset.SILVER: hot[1], Asset.BITCOIN: hot[2]},
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
# Event + signal records
# --------------------------------------------------------------------------
@dataclass
class CalendarEvent:
    """One scheduled macro release, normalised from the provider feed."""

    title: str
    country: str
    when_utc: datetime
    impact: str = "Medium"          # High / Medium / Low / Holiday
    forecast: Optional[float] = None
    previous: Optional[float] = None
    actual: Optional[float] = None   # None until the print is released
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
    surprise: Optional[float] = None


# --------------------------------------------------------------------------
# Parsing + classification
# --------------------------------------------------------------------------
def to_float(raw) -> Optional[float]:
    """Parse '3.5%', '-0.2%', '1.2K', '250B' into a float, else None."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1]
    elif text[-1:].upper() == "K":
        multiplier, text = 1_000.0, text[:-1]
    elif text[-1:].upper() == "M":
        multiplier, text = 1_000_000.0, text[:-1]
    elif text[-1:].upper() == "B":
        multiplier, text = 1_000_000_000.0, text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def classify_event(event: CalendarEvent) -> Optional[EventProfile]:
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


def _opposite(direction: Direction) -> Direction:
    if direction is Direction.BULLISH:
        return Direction.BEARISH
    if direction is Direction.BEARISH:
        return Direction.BULLISH
    return Direction.MIXED


def _strength_for(impact: str, importance: EconomicImportance,
                  surprise: Optional[float]) -> ImpactStrength:
    """
    Size the move by provider impact + importance + surprise magnitude.

    A small beat is not the same trade as a large one, so the magnitude of the
    surprise scales strength upward - but only for RELEASED prints, because an
    expected value has no realised magnitude to measure.
    """
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
             profile: Optional[EventProfile] = None) -> Optional[CalendarSignal]:
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

    # ---- RELEASED: direction from the STANDARDISED surprise ----
    if event.actual is not None and event.forecast is not None:
        from app.analysis.calendar_risk import (
            standardised_surprise, surprise_band,
        )

        surprise = event.actual - event.forecast
        # Item 8: measure the miss in standard deviations of THIS indicator
        # rather than as a flat percentage of the forecast, so a 0.2% CPI miss
        # and a 120k payroll miss are comparable.
        z = standardised_surprise(event.actual, event.forecast, event.title)
        band = surprise_band(z)

        if band == "NEUTRAL" or hot_bias is Direction.MIXED:
            direction = Direction.NEUTRAL
            reason = (f"{event.title} came in at {event.actual:g} vs "
                      f"{event.forecast:g} forecast - no material surprise "
                      f"({band.lower()}).")
        else:
            is_hot = (z or 0.0) > 0
            direction = hot_bias if is_hot else _opposite(hot_bias)
            qualifier = "above" if is_hot else "below"
            reason = (f"{event.title} released {event.actual:g}, {qualifier} the "
                      f"{event.forecast:g} forecast ({surprise:+g}, "
                      f"{band.lower()} surprise, {z:+.2f} sigma).")

        return CalendarSignal(
            event=event, profile=profile, asset=asset, direction=direction,
            strength=_strength_for(event.impact, importance, z),
            importance=importance, relevance=relevance,
            persistence=0.75, mode="RELEASED", reason=reason,
            transmission=profile.transmission, surprise=round(z or 0.0, 4),
        )

    # ---- EXPECTED: direction from published consensus only ----
    if event.forecast is not None and event.previous is not None:
        delta = event.forecast - event.previous
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
            importance=importance, relevance=relevance,
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
    for asset in (Asset.GOLD, Asset.SILVER, Asset.BITCOIN):
        sig = evaluate(event, asset, profile)
        if sig is not None:
            out.append(sig)
    return out


def signals_for_events(events: Sequence[CalendarEvent],
                       horizon_hours: int = 48) -> list[CalendarSignal]:
    """
    Calendar signals for everything inside the forward-looking horizon.

    Events older than two hours are excluded: that print is history, and the
    news feed already covers it.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=max(0, horizon_hours))
    out: list[CalendarSignal] = []
    for event in events:
        if event.when_utc is None:
            continue
        if event.when_utc < now - timedelta(hours=2):
            continue
        if event.when_utc > cutoff:
            continue
        out.extend(signals_for_event(event))
    return out


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
