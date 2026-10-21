"""
CALENDAR CONFIG + REGION WHITELIST.

The economic calendar carries every scheduled macro release the provider
publishes - for EVERY country. That is correct for a calendar view and wrong
for a bias engine: a Swiss CPI print is not a dollar story, and a Brazilian
retail-sales print is not either, but both arrive with the same shape as an
FOMC rate decision.

This module is the filter that decides which of them may reach the sentiment
engine, and it is DATA, not code:

    app/config/calendar_bias.json

holds the region list, the per-region event keyword whitelist, the region
weights, the asset-sensitivity matrix, the impact weights, the time-decay
window and the score labels. Adding a region, or a release to a region, is a
JSON edit - no Python changes, no redeploy of logic.

THE MATCH IS TWO-STAGE AND STRICT
---------------------------------
1. COUNTRY/CURRENCY FIRST. The event's ``country`` field (an ISO currency code
   such as ``USD``/``EUR``/``GBP``/``JPY``/``CNY``) must map to a region that
   is listed in the config. An event from a region that is not listed is
   dropped - there is no "default region" that catches everything.
2. THEN THE TITLE. The event title must match one of that region's
   ``include_keywords``. A region may set ``require_keyword: false`` to
   accept every one of its events instead.

Both stages must pass. Anything else is dropped, and the drop is always
explainable: :func:`filter_events` returns the reason for every rejection, so
a suddenly empty calendar can always be traced to a config edit.

FAIL-SAFE
---------
A missing or malformed config file raises :class:`CalendarConfigError`. The
CALLER decides what that means: the input filter falls back to the previous,
unfiltered behaviour (so a typo can never silently change a published
verdict), and the regional bias engine reports the error on the dashboard
instead of showing scores it cannot justify.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from app.logging_conf import get_logger

log = get_logger(__name__)

#: The one config file for the region whitelist AND the bias engine.
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "calendar_bias.json"


class CalendarConfigError(RuntimeError):
    """The calendar config file is missing, unreadable or malformed."""


# --------------------------------------------------------------------------
# Region
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Region:
    """One whitelisted region: its countries, its events, its weight."""

    code: str
    label: str
    currencies: tuple[str, ...]
    country_aliases: tuple[str, ...]
    weight: float
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    require_keyword: bool

    def matches_title(self, title: str) -> bool:
        """True when the title matches this region's include list."""
        if not self.require_keyword:
            return True
        return title_matches(self.include_keywords, title)

    def excludes_title(self, title: str) -> bool:
        """True when the title matches this region's exclude list."""
        return bool(self.exclude_keywords) and title_matches(
            self.exclude_keywords, title)


# --------------------------------------------------------------------------
# Whole config
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ImpactOverride:
    """A rule that forces an event's impact, matched on its title."""

    impact: str
    keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...] = ()

    def matches(self, title: str) -> tuple[str, ...]:
        """The keywords this rule matched, or an empty tuple."""
        if self.exclude_keywords and title_matches(self.exclude_keywords, title):
            return ()
        hits = tuple(kw for kw in self.keywords if title_matches((kw,), title))
        return hits


@dataclass(frozen=True)
class CalendarBiasConfig:
    """Everything the region filter and the bias engine need, validated."""

    regions: tuple[Region, ...]

    # impact
    impact_min: str = "Medium"
    impact_weights: dict = field(default_factory=dict)
    impact_overrides: tuple[ImpactOverride, ...] = ()

    # surprise
    use_standardised: bool = True
    percent_divisor: float = 10.0
    surprise_min_magnitude: float = 0.0
    surprise_max_sigma: float = 3.0

    # time decay
    full_weight_hours: float = 4.0
    decay_to_zero_hours: float = 48.0

    # event taxonomy
    inverted_keywords: tuple[str, ...] = ()
    decision_keywords: tuple[str, ...] = ()
    speech_keywords: tuple[str, ...] = ()
    speech_tags: tuple[tuple[str, tuple[str, ...]], ...] = ()
    speech_magnitude: float = 1.0

    # scenario resolution
    scenario_map: dict = field(default_factory=dict)
    stance_map: dict = field(default_factory=dict)
    asset_sensitivity: dict = field(default_factory=dict)

    # score
    dedupe_frequency_variants: bool = True
    normal_scale: float = 10.0
    normal_method: str = "linear_clip"
    normal_min: float = -100.0
    normal_max: float = 100.0
    label_strong_bearish: float = -60.0
    label_bearish: float = -20.0
    label_bullish: float = 20.0
    label_strong_bullish: float = 60.0
    label_names: dict = field(default_factory=dict)

    # event risk / upcoming
    risk_before_minutes: int = 30
    risk_after_minutes: int = 15
    risk_min_impact: str = "High"
    risk_neutral_multiplier: float = 2.0
    upcoming_horizon_hours: int = 24
    upcoming_min_impact: str = "High"
    upcoming_max_items: int = 20

    # presentation
    top_drivers: int = 3
    stale_minutes: int = 30
    max_events_per_asset: int = 0

    def region_by_code(self, code: str) -> Region | None:
        for region in self.regions:
            if region.code == code:
                return region
        return None

    def region_for_country(self, country: str) -> Region | None:
        """
        Map a country/currency field to a region.

        The field is matched against each region's currencies first and then
        against its aliases, so ``USD`` and ``US`` both resolve to USA while
        ``EUROZONE`` resolves to EUR.
        """
        key = (country or "").strip().upper()
        if not key:
            return None
        for region in self.regions:
            if key in region.currencies:
                return region
        for region in self.regions:
            if key in region.country_aliases:
                return region
        return None

    def impact_weight(self, impact: str) -> float:
        """Weight of one release, by its impact tag. Unknown -> 0."""
        key = (impact or "").strip().capitalize()
        try:
            return float(self.impact_weights.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    def impact_rank(self, impact: str) -> int:
        """Ordinal rank of an impact tag, for threshold comparisons."""
        key = (impact or "").strip().capitalize()
        order = {"Holiday": 0, "Low": 1, "Medium": 2, "High": 3}
        return order.get(key, 0)

    def sensitivity(self, scenario: str, asset: str) -> float:
        """Signed multiplier of one scenario for one asset. Unknown -> 0."""
        row = self.asset_sensitivity.get(scenario) or {}
        try:
            return float(row.get(asset, 0.0))
        except (TypeError, ValueError):
            return 0.0


# --------------------------------------------------------------------------
# Keyword matching
# --------------------------------------------------------------------------
_WORD_BOUNDARY = r"(?<![a-z0-9]){needle}(?![a-z0-9])"
#: An optional plural "s" on the last word of a needle, so ONE keyword covers
#: both spellings: "jobless claim" matches "Jobless Claims", "yield" matches
#: "yields". Without it a plural title silently fell out of the whitelist.
_OPTIONAL_PLURAL = r"(?:s)?"


@lru_cache(maxsize=256)
def _matcher(keywords: tuple[str, ...]) -> re.Pattern | None:
    """
    One compiled alternation for a keyword list.

    Lookarounds rather than ``\\b``: they behave the same for plain words and
    also handle needles that end in a digit or a hyphen ("10-year", "non-farm")
    without escaping surprises.
    """
    needles = [k.strip().lower() for k in (keywords or ()) if k and k.strip()]
    if not needles:
        return None
    parts = [
        _WORD_BOUNDARY.format(
            needle=re.escape(n) + _OPTIONAL_PLURAL)
        for n in needles
    ]
    return re.compile("|".join(parts))


def normalise_title(title: str) -> str:
    """
    Lowercase a title and fold the possessive away.

    "Fed's Williams Speaks" -> "fed williams speaks", so the needle ``fed``
    still matches a title written with an apostrophe.
    """
    lowered = (title or "").lower().replace("\u2019", "'")
    lowered = lowered.replace("'s ", " ").replace("' ", " ")
    return lowered.replace("'", " ")


def title_matches(keywords, title: str) -> bool:
    """True when any keyword in the list appears in the title."""
    matcher = _matcher(tuple(keywords or ()))
    if matcher is None:
        return False
    return matcher.search(normalise_title(title)) is not None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _as_tuple(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value if str(v).strip())


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_regions(raw: dict) -> tuple[Region, ...]:
    regions: list[Region] = []
    for entry in raw.get("regions") or []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip().upper()
        if not code:
            continue
        regions.append(Region(
            code=code,
            label=str(entry.get("label") or code).strip(),
            currencies=tuple(c.strip().upper()
                             for c in _as_tuple(entry.get("currencies"))),
            country_aliases=tuple(a.strip().upper()
                                  for a in _as_tuple(entry.get("country_aliases"))),
            weight=_as_float(entry.get("weight"), 1.0),
            include_keywords=_as_tuple(entry.get("include_keywords")),
            exclude_keywords=_as_tuple(entry.get("exclude_keywords")),
            require_keyword=bool(entry.get("require_keyword", True)),
        ))
    if not regions:
        raise CalendarConfigError(
            f"{CONFIG_FILE} declares no regions - the whitelist would be empty")
    return tuple(regions)


def _load_speech_tags(raw: dict) -> tuple[tuple[str, tuple[str, ...]], ...]:
    tags: list[tuple[str, tuple[str, ...]]] = []
    for entry in (raw.get("central_bank") or {}).get("speech_tags") or []:
        if not isinstance(entry, dict):
            continue
        stance = str(entry.get("stance") or "").strip().upper()
        keywords = _as_tuple(entry.get("keywords"))
        if stance and keywords:
            tags.append((stance, keywords))
    return tuple(tags)


def _load_mapping(raw: dict, key: str) -> dict:
    """A two-level mapping, with every ``_``-prefixed key skipped."""
    out: dict = {}
    source = raw.get(key) or {}
    if not isinstance(source, dict):
        return out
    for name, entry in source.items():
        if str(name).startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        out[str(name).upper()] = {
            str(k).upper(): str(v)
            for k, v in entry.items()
            if not str(k).startswith("_")
        }
    return out


def _load_asset_sensitivity(raw: dict) -> dict:
    out: dict = {}
    source = raw.get("asset_sensitivity") or {}
    if not isinstance(source, dict):
        return out
    for scenario, row in source.items():
        if str(scenario).startswith("_") or not isinstance(row, dict):
            continue
        clean: dict = {}
        for asset, value in row.items():
            if str(asset).startswith("_"):
                continue
            clean[str(asset).upper()] = _as_float(value, 0.0)
        out[str(scenario).upper()] = clean
    return out


def _load_impact_overrides(raw: dict) -> tuple[ImpactOverride, ...]:
    rules: list[ImpactOverride] = []
    for entry in raw.get("impact_overrides") or []:
        if not isinstance(entry, dict):
            continue
        impact = str(entry.get("impact") or "").strip().capitalize()
        keywords = _as_tuple(entry.get("keywords"))
        if not impact or not keywords:
            continue
        rules.append(ImpactOverride(
            impact=impact,
            keywords=keywords,
            exclude_keywords=_as_tuple(entry.get("exclude_keywords")),
        ))
    return tuple(rules)


@lru_cache(maxsize=4)
def _load_cached(path: str) -> CalendarBiasConfig:
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalendarConfigError(
            f"calendar config unreadable at {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CalendarConfigError(
            f"calendar config at {target} is not a JSON object")

    impact = raw.get("impact") or {}
    surprise = raw.get("surprise") or {}
    decay = raw.get("time_decay") or {}
    central = raw.get("central_bank") or {}
    normalization = raw.get("normalization") or {}
    labels = raw.get("labels") or {}
    risk = raw.get("event_risk") or {}
    upcoming = raw.get("upcoming") or {}
    dashboard = raw.get("dashboard") or {}
    inverted = raw.get("inverted_events") or {}
    if isinstance(inverted, list):
        inverted_keywords = _as_tuple(inverted)
    else:
        inverted_keywords = _as_tuple(inverted.get("keywords"))

    regions = _load_regions(raw)
    log.info("Calendar config loaded from %s: %d region(s) - %s", target,
             len(regions), ", ".join(r.code for r in regions))

    return CalendarBiasConfig(
        regions=regions,
        impact_min=str(impact.get("min_impact") or "Medium"),
        impact_weights={
            str(k).capitalize(): _as_float(v, 0.0)
            for k, v in (impact.get("weights") or {}).items()
            if not str(k).startswith("_")
        },
        impact_overrides=_load_impact_overrides(raw),
        use_standardised=bool(surprise.get("use_standardised", True)),
        percent_divisor=_as_float(surprise.get("percent_divisor"), 10.0),
        surprise_min_magnitude=_as_float(surprise.get("min_magnitude"), 0.0),
        surprise_max_sigma=_as_float(surprise.get("max_sigma"), 3.0),
        full_weight_hours=_as_float(decay.get("full_weight_hours"), 4.0),
        decay_to_zero_hours=_as_float(decay.get("decay_to_zero_hours"), 48.0),
        inverted_keywords=inverted_keywords,
        decision_keywords=_as_tuple(central.get("decision_keywords")),
        speech_keywords=_as_tuple(central.get("speech_keywords")),
        speech_tags=_load_speech_tags(raw),
        speech_magnitude=_as_float(central.get("speech_magnitude"), 1.0),
        scenario_map=_load_mapping(raw, "scenario_map"),
        stance_map=_load_mapping(raw, "stance_map"),
        asset_sensitivity=_load_asset_sensitivity(raw),
        dedupe_frequency_variants=bool(
            (raw.get("dedupe") or {}).get("frequency_variants", True)),
        normal_scale=_as_float(normalization.get("scale"), 10.0),
        normal_method=str(normalization.get("method") or "linear_clip"),
        normal_min=_as_float(normalization.get("min"), -100.0),
        normal_max=_as_float(normalization.get("max"), 100.0),
        label_strong_bearish=_as_float(labels.get("strong_bearish"), -60.0),
        label_bearish=_as_float(labels.get("bearish"), -20.0),
        label_bullish=_as_float(labels.get("bullish"), 20.0),
        label_strong_bullish=_as_float(labels.get("strong_bullish"), 60.0),
        label_names={
            str(k): str(v)
            for k, v in (labels.get("names") or {}).items()
            if not str(k).startswith("_")
        },
        risk_before_minutes=int(_as_float(risk.get("window_before_minutes"), 30)),
        risk_after_minutes=int(_as_float(risk.get("window_after_minutes"), 15)),
        risk_min_impact=str(risk.get("min_impact") or "High"),
        risk_neutral_multiplier=_as_float(
            risk.get("neutral_band_multiplier"), 2.0),
        upcoming_horizon_hours=int(_as_float(
            upcoming.get("horizon_hours"), 24)),
        upcoming_min_impact=str(upcoming.get("min_impact") or "High"),
        upcoming_max_items=int(_as_float(upcoming.get("max_items"), 20)),
        top_drivers=int(_as_float(dashboard.get("top_drivers"), 3)),
        stale_minutes=int(_as_float(dashboard.get("stale_minutes"), 30)),
        max_events_per_asset=int(_as_float(dashboard.get("max_events_per_asset"), 0)),
    )


def load_calendar_config(path: Path | str | None = None) -> CalendarBiasConfig:
    """
    Load and validate the calendar config.

    Raises :class:`CalendarConfigError` when the file is missing or malformed.
    The result is cached: the file is read once per process.
    """
    target = str(Path(path) if path else CONFIG_FILE)
    return _load_cached(target)


def clear_config_cache() -> None:
    """Drop the cached config so a test can edit the file and reload it."""
    _load_cached.cache_clear()


# --------------------------------------------------------------------------
# The filter
# --------------------------------------------------------------------------
def classify_region(event, config: CalendarBiasConfig | None = None
                    ) -> tuple[Region | None, str]:
    """
    Which region does this event belong to, and why not?

    Returns ``(region, reason)``. ``reason`` is empty when the event passed.
    The two rejection reasons are distinct on purpose: "country not in the
    whitelist" is a policy decision, "title keyword not in the whitelist" is a
    data decision, and they need different fixes.
    """
    cfg = config or load_calendar_config()
    country = str(getattr(event, "country", "") or "").strip().upper()
    region = cfg.region_for_country(country)
    if region is None:
        return None, f"country '{country or '?'}' is not in the whitelist"
    title = str(getattr(event, "title", "") or "")
    if region.excludes_title(title):
        return None, "title matches an exclude keyword"
    if not region.matches_title(title):
        return None, "title does not match the region's event list"
    return region, ""


def filter_events(events, config: CalendarBiasConfig | None = None
                  ) -> tuple[list, list[tuple]]:
    """
    Split events into (kept, dropped).

    ``dropped`` carries ``(event, reason)`` for every rejection, so a filter
    change is always explainable rather than a silent shrink of the calendar.
    """
    cfg = config or load_calendar_config()
    kept: list = []
    dropped: list[tuple] = []
    for event in (events or []):
        region, reason = classify_region(event, cfg)
        if region is None:
            dropped.append((event, reason))
        else:
            kept.append(event)
    return kept, dropped


def override_impact(event, config: CalendarBiasConfig | None = None
                    ) -> tuple[str, tuple[str, ...]]:
    """
    The forced impact of one event, and the keywords that forced it.

    Returns ``(impact, matched_keywords)``. With no rule matching, the event's
    own impact is returned unchanged and no keywords.
    """
    cfg = config or load_calendar_config()
    title = str(getattr(event, "title", "") or "")
    current = str(getattr(event, "impact", "") or "")
    for rule in cfg.impact_overrides:
        hits = rule.matches(title)
        if not hits:
            continue
        if rule.impact and rule.impact != current:
            return rule.impact, hits
        # The rule matched but the event already carries that impact.
        return current, hits
    return current, ()


def apply_impact_overrides(events, config: CalendarBiasConfig | None = None
                           ) -> tuple[list, list[tuple]]:
    """
    Force the impact of the events the config says are more important.

    Returns ``(events, changes)`` where ``changes`` is a list of
    ``(event, from_impact, to_impact, matched_keywords)``. The override runs
    BEFORE any impact weighting, so the weight, the upcoming-release list and
    the event-risk window all see the forced value.
    """
    cfg = config or load_calendar_config()
    out: list = []
    changes: list[tuple] = []
    for event in (events or []):
        forced, hits = override_impact(event, cfg)
        original = str(getattr(event, "impact", "") or "")
        if hits and forced and forced != original:
            event = replace(event, impact=forced)
            changes.append((event, original, forced, hits))
        out.append(event)
    return out, changes
