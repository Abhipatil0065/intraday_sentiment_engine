"""
SCHEDULED-DATA SURPRISE VS EXPECTATION for news articles (Task 3).

The economic-calendar engine (``app.analysis.economic_impact``) already
decides a released print's direction from the realised surprise versus the
published forecast, and ``app.analysis.calendar_risk`` already measures that
surprise in standard deviations. This module reuses BOTH for the news path:

  * an article that reports an actual print (``ReportKind.ACTUAL``) takes its
    per-asset direction from the surprise SIGN - not from words like
    "rises" or "falls" - read PER INDICATOR: higher than forecast is hot
    for inflation / payrolls / retail sales / GDP / ISM-PMI / yields, but
    COOL (inverted) for the unemployment rate and jobless / initial claims,
    and an unknown indicator keeps the caller's cue direction;
  * the equity card has its OWN direction column (``equity_direction`` /
    ``EQUITY_HIGHER``) instead of inheriting the metals and bitcoin one;
  * only a full release pattern parses (named indicator + actual figure +
    expectation figure or release verb), so speeches and projections are
    never marked ACTUAL;
  * its weight is scaled by the surprise SIZE, capped by one named constant;
  * when forecast and previous are BOTH missing there is nothing to compare
    against, so the caller's existing cue direction is kept;
  * text-only expectation cues ("as expected", "in line with", "widely
    anticipated") cut the weight via ``priced_in_multiplier``. No price data
    is read anywhere here.

Reuse, not duplication:
  ``economic_impact.to_float``            - provider-style value parsing
  ``calendar_risk.standardised_surprise`` - surprise size in sigma
  ``text_quality.priced_in_multiplier``   - expectation-cue damping
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.analysis.calendar_risk import standardised_surprise
from app.analysis.economic_impact import to_float
from app.analysis.text_quality import priced_in_multiplier
from app.models.enums import Asset, Direction

# THE one per-asset table: what a HOT (above-expectation) print does for the
# METALS AND BITCOIN. Hotter inflation / stronger jobs / higher yields ->
# metals and crypto down; cooler -> the mirror image. It is read per
# INDICATOR: a higher-than-forecast unemployment rate or jobless/initial
# claims print is cool (weaker economy), so its sign is inverted, and an
# unknown indicator is left on the caller's cue direction. The equity card
# does NOT inherit this column - it has its own, EQUITY_HIGHER below.
HOT_DIRECTION: dict[Asset, Direction] = {
    Asset.GOLD: Direction.BEARISH,
    Asset.SILVER: Direction.BEARISH,
    Asset.BITCOIN: Direction.BEARISH,
}

# The one named cap: no print, however extreme, weighs more than this multiple
# of the baseline weight, and no combination of surprise + text cues exceeds it.
MAX_SURPRISE_WEIGHT = 2.0

# A difference this small is an in-line print, not a direction.
_EPS = 1e-9

# A provider-style number: "0.3" "-0.2" "1,250" "0.3%" "235K" "1.2M" "-0.3%"
# (unit glued on, so "0.5 billion" cannot be misread as half a billion).
# K/k = thousands, M/m = millions, B/b = billions, % = percent.
_NUM = r"[+-]?\d[\d,]*(?:\.\d+)?\s*[kKmMbB%]?"

# Labelled fields, in the formats wire copy and calendar widgets actually use.
_LABELS: dict[str, tuple[str, ...]] = {
    "actual": ("actual",),
    "forecast": ("forecast", "expected", "consensus", "estimate"),
    "previous": ("previous", "prior"),
}

# "0.5% vs the 0.3% forecast" / "0.4 against 0.2 previous" / "225K vs 190K"
# - the FIRST number is the actual, the second is the expectation it is being
# compared with. The expectation keyword (forecast/expected/previous) may
# follow immediately or appear later in the sentence.
_VS_RE = re.compile(
    rf"({_NUM})\s*(?:vs\.?|versus|against)\s+(?:the\s+)?({_NUM})"
    r"(?:\s+(forecast|expected|consensus|estimate|previous|prior))?")

# Indicator families: what "higher than forecast" MEANS for each release.
# Earliest keyword in the text wins; ties break in this order (cool first,
# because the inversion is the safety-critical one):
#   cool      - unemployment rate / jobless + initial claims: higher = a
#               WEAKER economy, so the hot/cool sign is inverted;
#   inflation - CPI / PPI / PCE / wages: higher = hotter;
#   yields    - higher yields = hotter (opportunity cost up);
#   growth    - payrolls / NFP / retail sales / GDP / ISM / PMI: higher =
#               stronger economy = hotter.
_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cool", ("unemployment", "jobless", "claim", "claims")),
    ("inflation", ("cpi", "ppi", "pce", "inflation", "consumer price",
                   "producer price", "price index", "deflator", "wage",
                   "wages", "hourly earnings")),
    ("yields", ("yield", "yields", "bond yield", "treasury yield", "10-year")),
    ("growth", ("payroll", "payrolls", "nonfarm", "non-farm", "nfp",
                "retail sales", "gdp", "ism", "pmi", "industrial production")),
)

# An indicator outside every family is UNKNOWN: its sign is never guessed,
# the caller's existing cue direction simply stands.
UNKNOWN_FAMILY = "unknown"

# The one family whose higher-than-forecast print is COOL, not hot: the
# sign of the metals/bitcoin column is inverted for it.
_COOL_SET = frozenset({"cool"})

# The equity card's OWN direction column: what a HIGHER-than-forecast print
# means for equities, per family - hot inflation / higher yields are
# bearish, strong growth / jobs / PMI is bullish, and higher unemployment
# or claims (a weaker economy) is bearish. The metals and bitcoin column
# stays the HOT_DIRECTION table above.
EQUITY_HIGHER: dict[str, Direction] = {
    "inflation": Direction.BEARISH,
    "yields": Direction.BEARISH,
    "growth": Direction.BULLISH,
    "cool": Direction.BEARISH,
    UNKNOWN_FAMILY: Direction.NEUTRAL,
}

# Representative indicator name per family for std lookup (used when the
# full article title contains multiple indicators and would match the wrong one)
_FAMILY_STD_KEY: dict[str, str] = {
    "inflation": "cpi",
    "yields": "yield",
    "growth": "payroll",
    "cool": "unemployment rate",
    UNKNOWN_FAMILY: "",
}

# The named indicator half of a release pattern. Deliberately broader than
# _FAMILIES: an unclassifiable print still parses, its direction then just
# stays on the cue instead of being guessed at.
_RELEASE_NOUNS = re.compile(
    r"\bcpi\b|\bppi\b|\bpce\b|\binflation\b|consumer price|producer price|"
    r"price index|\bdeflator\b|\bwages?\b|hourly earnings|\bpayrolls?\b|"
    r"\bnonfarm\b|\bnon-farm\b|\bnfp\b|unemployment|jobless|\bclaims?\b|"
    r"retail sales|\bgdp\b|\bism\b|\bpmi\b|industrial production|\byields?\b|"
    r"bond yield|treasury yield|10-year|rate decision|interest rate|"
    r"consumer confidence|consumer sentiment|housing starts|building permits|"
    r"durable goods|factory orders|job openings|trade balance|trade deficit|"
    r"productivity|capacity utilization|new home sales|existing home sales|"
    r"personal income|consumer spending")

# A release verb: the wire's own way of supplying the ACTUAL figure.
_RELEASE_VERB = re.compile(
    r"\b(?:came|come|comes)\s+in\b|\b(?:rose|rises|rise|fell|falls|fall|"
    r"climbed|climbs|climb|dropped|drops|drop|eased|eases|ease|jumped|"
    r"jumps|jump|slipped|slips|slip|ticked|ticks|tick|inched|inches|inch|"
    r"advanced|advances|increased|increase|decreased|decrease)\s+to\b")

# A speech or a projection QUOTES a number; it does not release one. These
# never carry an actual figure of their own, so a number that only arrives
# through a comparison ("sees inflation at 3% versus 2.8% prior") stays a
# projection, not an ACTUAL print.
_PROJECTION = re.compile(
    r"\bsees\b|\bseen\b|\bexpects?\b|\bexpected to\b|\bforecast to\b|"
    r"\bprojects?\b|\bprojected\b|\bpredicts?\b|\banticipates?\b|"
    r"\bwill be\b|\bby year[- ]end\b|\boutlook\b|\bpreview\b")

# Revision language: "previous revised to 175K", "prior revised to 3.2%",
# "revised up to", "revised down to" - these update the previous value,
# they are NOT the actual release figure.
_REVISION = re.compile(
    r"\b(?:previous|prior)\s+(?:was\s+)?revised\s+(?:up|down)?\s*to\b|"
    r"\brevised\s+(?:up|down)?\s+to\b")


def _labelled(lowered: str, kind: str) -> Optional[float]:
    """First labelled number of one kind, or None (never a guess)."""
    for label in _LABELS[kind]:
        match = re.search(rf"\b{label}\b\s*[:=]?\s*({_NUM})", lowered)
        if match:
            value = to_float(match.group(1))
            if value is not None:
                return value
    return None


def _labelled_raw(lowered: str, kind: str) -> tuple[Optional[float], str]:
    """First labelled number of one kind, returning (value, raw_string) or (None, '')."""
    for label in _LABELS[kind]:
        match = re.search(rf"\b{label}\b\s*[:=]?\s*({_NUM})", lowered)
        if match:
            raw = match.group(1)
            value = to_float(raw)
            if value is not None:
                return value, raw
    return None, ""


@dataclass(frozen=True)
class ReleasePrint:
    """One actual scheduled-data print, as reported in article text."""

    actual: float
    forecast: Optional[float] = None
    previous: Optional[float] = None
    title: str = ""          # used for the per-indicator std lookup
    family: str = ""         # indicator family this print belongs to
    # Raw strings for mixed-unit detection
    actual_raw: str = ""
    forecast_raw: str = ""
    previous_raw: str = ""


def _extract_prints(text: str) -> list[ReleasePrint]:
    """
    Extract ALL actual prints from article text, matching each figure to
    its nearest indicator. Returns a list (possibly empty).

    This handles mixed headlines like "Payrolls beat forecast as unemployment
    rate rises to 4.3%" by finding each indicator and the number closest to it.
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return []

    # Find all indicator positions in the text
    indicator_hits: list[tuple[int, str, str]] = []  # (position, indicator_text, family)
    for family, words in _FAMILIES:
        for word in words:
            for match in re.finditer(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered):
                indicator_hits.append((match.start(), match.group(0), family))
    # Also check _RELEASE_NOUNS for broader indicator detection
    for match in _RELEASE_NOUNS.finditer(lowered):
        indicator_hits.append((match.start(), match.group(0), indicator_family(match.group(0))))

    # Deduplicate nearby hits (same indicator mentioned multiple times close together)
    indicator_hits.sort(key=lambda x: x[0])
    deduped: list[tuple[int, str, str]] = []
    for pos, ind, fam in indicator_hits:
        if not deduped or pos - deduped[-1][0] > 10:  # within 10 chars = same mention
            deduped.append((pos, ind, fam))
    indicator_hits = deduped

    if not indicator_hits:
        return []

    # Find all numbers in the text with their positions
    number_hits: list[tuple[int, float, str]] = []  # (position, value, raw_text)
    for match in re.finditer(rf"({_NUM})", lowered):
        raw = match.group(1)
        value = to_float(raw)
        if value is not None:
            number_hits.append((match.start(), value, raw))

    if not number_hits:
        return []

    # Identify revision numbers (these are NOT actual prints)
    # Also extract revised previous values
    revision_positions: set[int] = set()
    revised_previous: dict[int, float] = {}  # revision_match_pos -> revised_value
    for match in _REVISION.finditer(lowered):
        rev_start = match.start()
        # Find the number immediately after "to" in the revision
        after_to = lowered[match.end():match.end() + 30]
        num_match = re.match(rf"\s*({_NUM})", after_to)
        if num_match:
            rev_value = to_float(num_match.group(1))
            if rev_value is not None:
                revised_previous[rev_start] = rev_value
                # Mark ONLY this specific number as a revision number
                rev_num_pos = match.end() + num_match.start()
                revision_positions.add(rev_num_pos)

    # For each indicator, find the closest UNASSIGNED number that is not a revision
    # Process indicators RIGHT TO LEFT (later indicators get first pick of numbers)
    # because English typically places the number AFTER the indicator it modifies.
    assigned_numbers: set[int] = set()
    prints: list[ReleasePrint] = []
    for ind_pos, ind_text, family in reversed(indicator_hits):
        # Find the closest number to this indicator that hasn't been assigned
        # Prefer numbers that come AFTER the indicator (typical English word order)
        closest_num: Optional[tuple[int, float, str]] = None
        closest_dist = float('inf')
        for num_pos, value, raw in number_hits:
            if num_pos in revision_positions:
                continue  # skip revision numbers
            if num_pos in assigned_numbers:
                continue  # skip already assigned numbers
            # Prefer numbers after the indicator
            if num_pos >= ind_pos:
                dist = num_pos - ind_pos  # distance forward
            else:
                dist = (ind_pos - num_pos) + 100  # penalty for numbers before indicator
            if dist < closest_dist:
                closest_dist = dist
                closest_num = (num_pos, value, raw)
        
        if closest_num is None:
            continue

        num_pos, actual_value, raw_num = closest_num
        assigned_numbers.add(num_pos)

        # Initialize raw strings
        actual_raw = raw_num
        forecast = None
        previous = None
        forecast_raw = ""
        previous_raw = ""
        vs_used = False

        # Now find forecast/previous for this print
        # Look for labelled numbers near this indicator
        window_start = max(0, ind_pos - 200)
        window_end = min(len(lowered), ind_pos + 200)
        window_text = lowered[window_start:window_end]

        # Check for "X vs Y" pattern near this indicator FIRST (takes precedence)
        vs_match = _VS_RE.search(window_text)
        if vs_match:
            # Get positions of the two numbers in the vs_match relative to the window
            vs_first_start = vs_match.start(1)  # position of first number in window
            vs_second_start = vs_match.start(2)  # position of second number in window
            first, second = to_float(vs_match.group(1)), to_float(vs_match.group(2))
            
            # Only use this vs pattern if our assigned number is the FIRST number (the actual)
            # Check if num_pos aligns with the first number in the vs pattern
            if first is not None and abs(num_pos - (window_start + vs_first_start)) < 20:
                vs_used = True
                # The first number in "X vs Y" is likely the actual
                actual_value = first
                actual_raw = vs_match.group(1)
                # The second number is the expectation (forecast/previous)
                if second is not None:
                    # Group 3 is optional; may be None if not present
                    expectation_type = vs_match.group(3) if vs_match.lastindex >= 3 else "forecast"
                    if expectation_type in ("previous", "prior"):
                        previous = second
                        previous_raw = vs_match.group(2)
                    else:
                        forecast = second
                        forecast_raw = vs_match.group(2)

        # Then look for labelled numbers (only if vs pattern didn't set them)
        if not vs_used:
            forecast, forecast_raw = _labelled_raw(window_text, "forecast")
            previous, previous_raw = _labelled_raw(window_text, "previous")
        else:
            # Still check for labelled "previous" if vs pattern didn't set it
            if previous is None:
                prev_val, prev_raw = _labelled_raw(window_text, "previous")
                if prev_val is not None:
                    previous, previous_raw = prev_val, prev_raw
            if forecast is None:
                fcst_val, fcst_raw = _labelled_raw(window_text, "forecast")
                if fcst_val is not None:
                    forecast, forecast_raw = fcst_val, fcst_raw

        # Check for revised previous in this window
        for rev_pos, rev_value in revised_previous.items():
            if window_start <= rev_pos <= window_end:
                previous = rev_value
                # Find the raw string for the revised previous
                after_to = lowered[rev_pos:rev_pos + 30]
                num_match = re.match(rf"\s*({_NUM})", after_to)
                if num_match:
                    previous_raw = num_match.group(1)
                break

        # Check for release verb near this indicator
        verb_match = _RELEASE_VERB.search(window_text)
        has_verb = verb_match is not None

        # Check if this number is disqualified by projection language
        if _PROJECTION.search(window_text) is not None:
            # But explicit "actual:" label wins
            labelled_actual = _labelled(window_text, "actual")
            if labelled_actual is None:
                continue  # This is a projection, not a release

        # Need either an expectation (forecast/previous) or a release verb
        if forecast is None and previous is None and not has_verb:
            continue

        # Determine the title for this print (context around the indicator)
        title_start = max(0, ind_pos - 50)
        title_end = min(len(text), ind_pos + 100)
        title_context = text[title_start:title_end].strip()

        # Capture actual_raw from labelled "actual" if not already set
        labelled_actual, labelled_actual_raw = _labelled_raw(window_text, "actual")
        if labelled_actual is not None:
            actual_value = labelled_actual
            actual_raw = labelled_actual_raw

        prints.append(ReleasePrint(
            actual=actual_value,
            forecast=forecast,
            previous=previous,
            title=title_context,
            family=family,
            actual_raw=actual_raw,
            forecast_raw=forecast_raw,
            previous_raw=previous_raw,
        ))

    return prints


def parse(text: str) -> Optional[ReleasePrint]:
    """
    Extract the primary actual print from article text (backward compatible).

    Returns the first/most significant print, or None if no release pattern.
    For multiple prints, use parse_all() instead.
    """
    prints = _extract_prints(text)
    if not prints:
        return None
    # Return the print with the highest importance indicator (cool > inflation > yields > growth)
    # or the first one if unclear
    priority = {"cool": 0, "inflation": 1, "yields": 2, "growth": 3, UNKNOWN_FAMILY: 4}
    prints.sort(key=lambda p: priority.get(p.family, 5))
    return prints[0]


def parse_all(text: str) -> list[ReleasePrint]:
    """
    Extract ALL actual prints from article text, matching each figure to
    its nearest indicator.

    Returns a list of ReleasePrint objects (empty if no release pattern).
    Each print has its own indicator family and associated numbers.
    """
    return _extract_prints(text)


def surprise_delta(p: ReleasePrint) -> Optional[float]:
    """
    actual minus forecast, falling back to actual minus previous.

    None when there is no expectation at all - the engine does not invent
    the consensus it was not given.
    """
    base = p.forecast if p.forecast is not None else p.previous
    if base is None:
        return None
    return float(p.actual) - float(base)


def _opposite(direction: Direction) -> Direction:
    if direction is Direction.BULLISH:
        return Direction.BEARISH
    if direction is Direction.BEARISH:
        return Direction.BULLISH
    return direction


def indicator_family(text: str) -> str:
    """
    Which indicator family `text` names, or UNKNOWN_FAMILY.

    Earliest keyword wins (ties prefer the cool family, whose inverted sign
    is the safety-critical one). Never guesses: an indicator outside every
    family is reported as UNKNOWN_FAMILY.
    """
    lowered = (text or "").lower()
    best: Optional[tuple[int, str]] = None
    for family, words in _FAMILIES:
        for word in words:
            hit = re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered)
            if hit and (best is None or hit.start() < best[0]):
                best = (hit.start(), family)
    return best[1] if best else UNKNOWN_FAMILY


def direction_for(asset: Asset, p: Optional[ReleasePrint],
                  cue_direction: Direction) -> Direction:
    """
    Direction for one asset: the surprise SIGN, INDICATOR-AWARE.

    The metals/bitcoin column is the HOT_DIRECTION table: for inflation,
    growth/jobs/PMI and yields a higher-than-forecast print is HOT (bearish
    for the metals and bitcoin); for the unemployment rate and jobless /
    initial claims a higher print means a WEAKER economy, so the sign is
    inverted. An unknown indicator is never guessed at.

    Falls back to the caller's existing cue direction when there is no print,
    no expectation to compare against, an in-line result (|delta| ~ 0), or an
    indicator this table does not know.
    """
    if p is None:
        return cue_direction
    delta = surprise_delta(p)
    if delta is None or abs(delta) < _EPS:
        return cue_direction
    # Use the family stored in the print (set during parsing), fall back to
    # detecting from title for backward compatibility with manually created prints
    family = p.family if p.family else indicator_family(p.title)
    if family is UNKNOWN_FAMILY:
        return cue_direction
    hot = HOT_DIRECTION.get(asset)
    if hot is None:
        return cue_direction
    higher_is_cool = family in _COOL_SET
    if delta > 0:
        return _opposite(hot) if higher_is_cool else hot
    return hot if higher_is_cool else _opposite(hot)


def equity_direction(p: Optional[ReleasePrint],
                     cue_direction: Direction) -> Direction:
    """
    The EQUITY card's own column (Task 3b): the surprise SIGN read for
    equities rather than for the metals and bitcoin.

    Higher than forecast -> EQUITY_HIGHER[family]: hot inflation / yields
    bearish, strong growth / jobs / PMI bullish, higher unemployment or
    claims (weaker economy) bearish. Unknown indicator, no print, no
    expectation or an in-line result -> the caller's cue direction.
    """
    if p is None:
        return cue_direction
    delta = surprise_delta(p)
    if delta is None or abs(delta) < _EPS:
        return cue_direction
    family = p.family if p.family else indicator_family(p.title)
    higher = EQUITY_HIGHER.get(family, Direction.NEUTRAL)
    if higher is Direction.NEUTRAL:
        return cue_direction
    return higher if delta > 0 else _opposite(higher)


def _std_scale_for_unit(unit: str) -> float:
    """Return the scale factor to convert std to match parsed number units."""
    unit = (unit or "").lower()
    if unit == "k":
        return 1000.0
    if unit == "m":
        return 1000000.0
    if unit == "b":
        return 1000000000.0
    if unit == "%":
        return 1.0  # percentage points are already in the same scale
    return 1.0


def surprise_weight(p: Optional[ReleasePrint], text: str = "") -> float:
    """
    Weight multiplier: small miss = low weight, large miss = high weight,
    capped by MAX_SURPRISE_WEIGHT.

    Size is measured in standard deviations of the indicator itself (the
    calendar's own rule), so a 0.2pp CPI miss and a 120k payroll miss are
    comparable. The result is then multiplied by the TEXT-ONLY expectation
    cues ("as expected", "in line with", "widely anticipated"), so an
    in-line print written up as in-line lands well below the baseline. No
    price data is read. No print -> 1.0 (baseline, unchanged behaviour).
    """
    if p is None:
        return 1.0
    base = p.forecast if p.forecast is not None else p.previous
    weight = 1.0
    if base is not None:
        # Use family-specific std key if available, otherwise fall back to title
        std_title = _FAMILY_STD_KEY.get(p.family, "") or p.title
        z = standardised_surprise(p.actual, base, std_title)
        if z is not None:
            # Adjust z for unit mismatch: std table uses indicator-specific units
            # (e.g., payrolls std is in thousands, but parsed numbers are absolute)
            unit = _unit_suffix(p.actual_raw)
            scale = _std_scale_for_unit(unit)
            # If the std table uses different units than parsed numbers, adjust z
            # e.g., payrolls: std=120 (thousands), parsed=225000 -> scale z by 1000/1000 = 1
            # Actually, std is already in the indicator's native units, parsed numbers are absolute
            # So if std=120 (meaning 120K), and actual=225000, we need to divide actual by 1000
            # But standardised_surprise already computed z = (actual - forecast) / std
            # where actual is 225000 and std is 120. So z = 35000/120 = 291 instead of 35/120 = 0.29
            # Fix: if the unit suffix indicates thousands, the std is also in thousands,
            # so we need to divide the parsed numbers by 1000 before computing z.
            # Since we can't change standardised_surprise easily, we adjust z post-hoc:
            if scale > 1.0:
                z = z / scale
            magnitude = abs(float(z))
            weight = (1.0 + (MAX_SURPRISE_WEIGHT - 1.0)
                      * (magnitude / (1.0 + magnitude)))
    combined = weight * priced_in_multiplier(text)
    return round(max(0.0, min(MAX_SURPRISE_WEIGHT, combined)), 4)


def surprise_weight_all(prints: list[ReleasePrint], text: str = "") -> float:
    """
    Combined weight multiplier for MULTIPLE prints in one headline.

    Each print contributes its own surprise weight. Prints with opposite
    directions for the same asset partially cancel out (weighted by their
    individual surprise magnitudes). The text expectation cues apply once
    to the combined result.

    Returns 1.0 if no prints provided.
    """
    if not prints:
        return 1.0

    # Calculate weight for each print
    weights: list[float] = []
    for p in prints:
        base = p.forecast if p.forecast is not None else p.previous
        weight = 1.0
        if base is not None:
            std_title = _FAMILY_STD_KEY.get(p.family, "") or p.title
            z = standardised_surprise(p.actual, base, std_title)
            if z is not None:
                magnitude = abs(float(z))
                weight = (1.0 + (MAX_SURPRISE_WEIGHT - 1.0)
                          * (magnitude / (1.0 + magnitude)))
        weights.append(weight)

    # For now, take the maximum weight (most surprising print dominates)
    # Future: could do weighted average by surprise magnitude
    max_weight = max(weights) if weights else 1.0
    combined = max_weight * priced_in_multiplier(text)
    return round(max(0.0, min(MAX_SURPRISE_WEIGHT, combined)), 4)


def _unit_suffix(raw: str) -> str:
    """Return the unit suffix of a raw number string: '', 'k', 'm', 'b', '%'."""
    raw = (raw or "").strip().lower()
    if raw.endswith("%"):
        return "%"
    if raw.endswith("k"):
        return "k"
    if raw.endswith("m"):
        return "m"
    if raw.endswith("b"):
        return "b"
    return ""


def _has_mixed_units(actual_raw: str, base_raw: str) -> bool:
    """True if actual and base have different unit suffixes (e.g., K vs %)."""
    if not actual_raw or not base_raw:
        return False
    return _unit_suffix(actual_raw) != _unit_suffix(base_raw)


def combine_prints_for_asset(asset: Asset, prints: list[ReleasePrint],
                              cue_direction: Direction,
                              use_equity_column: bool = False,
                              text: str = "") -> tuple[Direction, float]:
    """
    Combine multiple prints for ONE asset into a single direction and weight.

    For each valid print:
      - direction = direction_for(asset, p, cue_direction) [or equity_direction]
      - weight = surprise_weight(p, text)
    Signed contribution = sign(direction) * weight
    Final signed = sum of signed contributions
    Final direction = sign(final_signed) (BULLISH if >0, BEARISH if <0, cue if ~0)
    Final weight = abs(final_signed) capped at MAX_SURPRISE_WEIGHT

    Prints with mixed units (K vs %, M vs K, etc.) are dropped.
    If no valid print remains, return (cue_direction, 1.0).
    """
    if not prints:
        return cue_direction, 1.0

    # Small epsilon for cancellation detection
    EPS = 1e-6

    total_signed = 0.0
    has_valid = False

    for p in prints:
        # Check for mixed units: compare actual_raw with forecast_raw or previous_raw
        base_raw = p.forecast_raw if p.forecast is not None else p.previous_raw
        if _has_mixed_units(p.actual_raw, base_raw):
            # Mixed units (e.g., K vs %) - drop this print
            continue

        # Get direction for this print
        if use_equity_column:
            direction = equity_direction(p, cue_direction)
        else:
            direction = direction_for(asset, p, cue_direction)

        # Get weight for this print
        weight = surprise_weight(p, text)

        # Convert direction to sign
        sign = 0
        if direction is Direction.BULLISH:
            sign = 1.0
        elif direction is Direction.BEARISH:
            sign = -1.0
        else:
            sign = 0.0

        if sign != 0:
            total_signed += sign * weight
            has_valid = True

    if not has_valid or abs(total_signed) < EPS:
        return cue_direction, 1.0

    final_direction = Direction.BULLISH if total_signed > 0 else Direction.BEARISH
    final_weight = min(abs(total_signed), MAX_SURPRISE_WEIGHT)
    return final_direction, final_weight
