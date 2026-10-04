"""
NEWS CLASSIFIER (section 26 - NLP / LLM ANALYSIS).

Classifies each article into: category, economic importance, report kind,
verification status, and a per-asset direction / strength / relevance /
persistence with an explicit transmission chain.

Two engines:
  * LLM engine - used when LLM_API_KEY is configured. Works with any
    OpenAI-compatible endpoint (NVIDIA NIM, DeepSeek, OpenRouter, Groq...).
  * Heuristic engine - deterministic rules, always available.

The active mode is recorded on every result so the dashboard can never
present heuristic output as if it were model-derived.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.analysis import llm_client
from app.analysis.asset_relevance import relevant_assets, relevance_for
from app.analysis.cue_rules import (
    BEARISH_CUES,
    BULLISH_CUES,
    TRANSMISSION,
    detect_category,
    detect_direction,
    detect_importance,
    detect_metal_character,
    detect_report_kind,
    detect_verification,
    score_cues,
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

log = get_logger(__name__)

MAX_BODY_CHARS = 2000


@dataclass
class AssetAnalysis:
    asset: Asset
    direction: Direction = Direction.NEUTRAL
    strength: ImpactStrength = ImpactStrength.LOW
    relevance: float = 0.0
    persistence: float = 0.5
    reason: str = ""
    transmission: str = ""
    metal_character: Optional[str] = None


@dataclass
class ArticleAnalysis:
    category: NewsCategory = NewsCategory.OTHER
    importance: EconomicImportance = EconomicImportance.LOW
    report_kind: ReportKind = ReportKind.GENERAL
    verification: Verification = Verification.CONFIRMED
    event_title: str = ""
    event_summary: str = ""
    relevance_score: float = 0.0
    mode: str = "heuristic"
    note: str = ""
    assets: list[AssetAnalysis] = field(default_factory=list)


# ==========================================================================
# Heuristic engine (always available)
# ==========================================================================
def _strength_from(cue_magnitude: float, relevance: float,
                   importance: EconomicImportance) -> ImpactStrength:
    """
    Map the ONE cue magnitude + relevance + importance onto a strength band.

    Item 6: ``cue_magnitude`` is the single measured cue score. Persistence is
    no longer derived from the same number (see ``_persistence_from``), so the
    same evidence is not counted three times under three names.
    """
    if cue_magnitude >= 0.80 or importance is EconomicImportance.VERY_HIGH:
        base = ImpactStrength.VERY_HIGH
    elif cue_magnitude >= 0.55:
        base = ImpactStrength.HIGH
    elif cue_magnitude >= 0.33:
        base = ImpactStrength.MEDIUM
    else:
        base = ImpactStrength.LOW
    # Weak relevance caps the strength regardless of headline drama.
    if relevance < 0.22:
        return ImpactStrength.LOW
    if relevance < 0.30 and base is ImpactStrength.VERY_HIGH:
        return ImpactStrength.HIGH
    return base


def _persistence_from(category: NewsCategory) -> float:
    """
    How long this kind of event should keep influencing today's session.

    Derived from the EVENT TYPE (the category), not from the cue total. This is
    the item 6 fix: persistence used to be a function of the same cue magnitude
    as strength and importance, so one measurement drove three multipliers.
    """
    from app.analysis.transmission_map import CATEGORY_PERSISTENCE

    return CATEGORY_PERSISTENCE.get(category, 0.5)


def _build_reason(asset: Asset, category: NewsCategory, direction: Direction,
                  text: str, dominant: Optional[str] = None,
                  priced_in: bool = False) -> str:
    cat_label = category.value if category else "news"
    snippet = " ".join(text.split())[:110]
    parts = [
        f"{cat_label} headline reading net {direction.value.lower()} for "
        f"{asset.label} ({asset.value})."
    ]
    if dominant:
        parts.append(f"Transmission path: {dominant}.")
    if priced_in:
        parts.append("Price-action report only - directional weight suppressed.")
    parts.append(f'Snippet: "{snippet}"')
    return " ".join(parts)


def _transmission_for(asset: Asset, direction: Direction) -> str:
    if direction in (Direction.BULLISH, Direction.BEARISH):
        return TRANSMISSION[direction.value].get(asset, "Transmission unclear.")
    return ("No dominant directional transmission path; treated as a neutral "
            "intraday input for this asset.")


def heuristic_analyze(title: str, body: str = "") -> ArticleAnalysis:
    """
    Deterministic classification - the always-available fallback engine.

    Direction is now resolved PER ASSET through the transmission map rather than
    computed once and stamped onto every routed asset, so a headline that is
    bullish gold and bearish bitcoin produces exactly that.
    """
    from app.analysis import text_quality as tq
    from app.analysis.transmission_map import measure_cue_magnitude, resolve

    text = f"{title} {body}".strip()
    result = ArticleAnalysis(mode="heuristic")

    result.category = detect_category(text)
    result.importance = detect_importance(text, result.category)
    result.report_kind = detect_report_kind(text)
    result.verification = detect_verification(text)
    result.event_title = title.strip()
    result.event_summary = (body or title)[:300].strip()

    lowered = text.lower()
    # THE single cue magnitude for this article (item 6).
    cue_magnitude = measure_cue_magnitude(text)
    resolved = resolve(text, result.category, cue_magnitude)
    persistence = _persistence_from(result.category)
    quality = tq.analyse(text)

    char = detect_metal_character(lowered)
    for asset in relevant_assets(result.category, text):
        rel = relevance_for(result.category, asset, text)
        impact = resolved[asset]
        result.assets.append(
            AssetAnalysis(
                asset=asset,
                direction=impact.direction,
                strength=_strength_from(cue_magnitude, rel, result.importance),
                relevance=rel,
                persistence=persistence,
                reason=_build_reason(asset, result.category, impact.direction,
                                     text,
                                     dominant=impact.dominant.value
                                     if impact.dominant else None,
                                     priced_in=quality["price_action"]),
                transmission=_transmission_for(asset, impact.direction),
                metal_character=char if asset is Asset.SILVER else None,
            )
        )

    result.relevance_score = max((a.relevance for a in result.assets), default=0.0)
    if not result.assets:
        # Routed to no asset: the item has no demonstrable transmission path to
        # XAUUSD/XAGUSD/BTCUSD. This is a valid, expected outcome - it must not
        # be padded with a fallback asset just to produce a row.
        result.note = (
            "Heuristic: no material intraday impact on "
            "XAUUSD/XAGUSD/BTCUSD; recorded but excluded from asset scoring."
        )
    else:
        result.note = "Deterministic heuristic classification (no LLM key configured)."
    return result
# ==========================================================================
# LLM engine (any OpenAI-compatible provider)
# ==========================================================================
SYSTEM_PROMPT = """You are an intraday market microstructure analyst specialising in \
XAUUSD (gold), XAGUSD (silver) and BTCUSD (bitcoin).

You receive a financial news headline and body published TODAY.

Classify the item. Critical rules:
1. Do NOT simply classify the tone of the article. Determine HOW the news \
actually transmits to the underlying asset price.
2. Do NOT assume every item affects these assets. If relevance is weak, say so.
3. Relationships are contextual, not absolute. "Hawkish Fed" is usually \
bearish gold, but not when driven by a recession shock that triggers safe-haven \
buying - judge the actual context in the article.
4. GOLD reacts to: Fed, rate expectations, REAL yields, Treasury yields, \
USD/DXY, inflation, employment, recession risk, geopolitics, safe-haven demand, \
central-bank purchases, gold ETF flows, China/India demand, risk sentiment.
5. SILVER has BOTH precious-metal and industrial characteristics. Decide \
whether the item drives PRECIOUS demand, INDUSTRIAL demand, BOTH, or NEITHER.
6. BITCOIN: judge the actual regime. Do not automatically call it risk-on or \
risk-off. Consider monetary policy, real yields, USD, liquidity, equities/Nasdaq, \
spot bitcoin ETF flows, regulation, stablecoin liquidity, liquidations, on-chain.
7. Only intraday / day-trading relevance for TODAY. Ignore long-term, monthly \
or yearly implications.
8. If the item is an unconfirmed rumour, mark verification UNVERIFIED.
9. Never invent numbers, prices or data not present in the text.

Return ONLY a JSON object with this exact schema:
{
  "category": EXACTLY one of these strings, nothing else:
    "Central Bank","Interest Rates","Inflation","Employment","GDP",
    "Treasury Yields","USD/DXY","Geopolitics","Risk Sentiment","ETF Flows",
    "Commodity Supply/Demand","Industrial Demand","China","Crypto Regulation",
    "Bitcoin ETF Flows","Crypto Market Flows","Oil/Energy","Equities",
    "Banking/Financial Stress","Other",
  "importance": "VERY_HIGH|HIGH|MEDIUM|LOW",
  "verification": "CONFIRMED|UNVERIFIED",
  "event_title": "canonical EVENT name (not a restatement of the headline)",
  "event_summary": "one sentence describing what happened",
  "relevance_score": 0.0-1.0,
  "assets": [
    {
      "asset": "XAUUSD|XAGUSD|BTCUSD",
      "direction": "BULLISH|BEARISH|NEUTRAL|MIXED",
      "magnitude": 0.0-1.0,
      "horizon": "intraday|session|multi_day",
      "priced_in": true|false,
      "reason": "WHY this affects the asset - ONE line",
      "transmission": "the causal CHAIN using -> arrows",
      "metal_character": "PRECIOUS|INDUSTRIAL|BOTH|null"
    }
  ]
}

"assets" rules - read carefully:
- Include an entry ONLY for assets this item genuinely affects intraday.
- If the item has NO material effect on gold, silver or bitcoin (e.g. a
  routine corporate filing, a sports result, an isolated local story),
  return an EMPTY array: "assets": []. That is a correct and expected answer.
- Never force an entry just to fill the array. Never list an asset twice.
- At most 3 entries, ordered by relevance descending.
- "magnitude" is YOUR conviction for that asset (0.0-1.0), not a probability.
- Set "priced_in": true when the market already expected this ("as expected",
  "in line with forecasts"); it reduces the weight the engine gives the item.
- If the headline only REPORTS a price move ("gold plunges 2%"), the move has
  already happened: keep magnitude low and say so in the reason.
- Keep "reason" to ONE line and "transmission" under 40 words."""


def _coerce_enum(enum_cls, raw, fallback):
    try:
        return enum_cls(raw)
    except (ValueError, TypeError):
        return fallback


def _clamp(value, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low
def _magnitude_to_strength(magnitude: float) -> ImpactStrength:
    """Map the model's own 0-1 conviction onto the strength bands."""
    if magnitude >= 0.75:
        return ImpactStrength.VERY_HIGH
    if magnitude >= 0.50:
        return ImpactStrength.HIGH
    if magnitude >= 0.25:
        return ImpactStrength.MEDIUM
    return ImpactStrength.LOW


def _horizon_to_persistence(horizon: str) -> float:
    """How long the model says the effect lasts -> a persistence weight."""
    return {"intraday": 0.45, "session": 0.75, "multi_day": 0.95}.get(
        horizon, 0.6)


async def _llm_call(system_prompt: str, user_prompt: str):
    """Indirection point so tests can stub the provider call."""
    return await llm_client.chat_json(system_prompt, user_prompt)


async def llm_analyze(title: str, body: str = "") -> ArticleAnalysis:
    """
    Ask the LLM to classify, VALIDATE the response, retry once, then give up.

    Raises on persistent failure so :func:`analyze_article` can fall back to the
    deterministic heuristic engine. A malformed response is never partially
    trusted: it is either fully valid or it is discarded.
    """
    from app.analysis.llm_schema import validate_llm_payload
    from app.config import settings

    body = (body or "")[:MAX_BODY_CHARS]
    user_prompt = (
        f"HEADLINE:\n{title}\n\n"
        f"ARTICLE BODY (may be truncated or empty):\n{body or '[not available]'}"
    )

    last_error: Exception | None = None
    for attempt in range(2):   # item 9: exactly one retry
        try:
            raw = await _llm_call(SYSTEM_PROMPT, user_prompt)
            # Models occasionally key the map by asset instead of using a list.
            if isinstance(raw, dict) and isinstance(raw.get("assets"), dict):
                raw = {**raw, "assets": [
                    {"asset": k, **(v if isinstance(v, dict) else {})}
                    for k, v in raw["assets"].items()
                ]}
            data = validate_llm_payload(raw)
        except Exception as exc:   # noqa: BLE001 - any failure is retryable
            last_error = exc
            log.warning("LLM classification attempt %d/2 failed: %s",
                        attempt + 1, exc)
            continue

        result = ArticleAnalysis(mode="llm")
        result.category = data.category
        result.importance = _coerce_enum(
            EconomicImportance, data.importance, EconomicImportance.LOW)
        result.report_kind = detect_report_kind(f"{title} {body}")
        result.verification = data.verification
        result.event_title = (data.event_title or title)[:300]
        result.event_summary = (data.event_summary or "")[:500]
        result.relevance_score = data.relevance_score

        for entry in data.assets:
            # Relevance for the aggregate still comes from the category matrix
            # plus an explicit mention - the model does not get to invent how
            # much an asset should matter.
            rel = relevance_for(result.category, entry.asset, title)
            magnitude = entry.magnitude
            if entry.priced_in:
                magnitude *= float(settings.priced_in_multiplier)
            result.assets.append(
                AssetAnalysis(
                    asset=entry.asset,
                    direction=Direction(entry.direction),
                    strength=_magnitude_to_strength(entry.magnitude),
                    relevance=rel,
                    persistence=_horizon_to_persistence(entry.horizon),
                    reason=entry.reason or f"{settings.llm_model}: {entry.asset}",
                    transmission=entry.transmission or _transmission_for(
                        entry.asset, Direction(entry.direction)),
                    metal_character=(entry.metal_character
                                     if entry.asset is Asset.SILVER else None),
                )
            )

        if not result.assets:
            # An EMPTY assets array is a VALID answer: the model is correctly
            # reporting no material intraday impact. It must not be discarded or
            # upgraded into a directional call.
            result.note = (f"{settings.llm_model}: no material intraday impact "
                           f"on {'/'.join(Asset.values())}.")
            return result

        result.note = f"Classified by {settings.llm_model} (validated)."
        return result

    raise RuntimeError(f"LLM classification failed after 2 attempts: {last_error}")


async def analyze_article(title: str, body: str = "") -> tuple[ArticleAnalysis, bool]:
    """Public entry point. Returns (analysis, llm_ok). Never raises."""
    from app.config import settings

    if settings.has_llm:
        try:
            return await llm_analyze(title, body), True
        except Exception as exc:  # noqa: BLE001 - fallback is mandatory
            log.warning("LLM analysis failed, using heuristic: %s", exc)
            analysis = heuristic_analyze(title, body)
            analysis.note = (
                f"LLM unavailable ({type(exc).__name__}); heuristic fallback used."
            )
            return analysis, False

    return heuristic_analyze(title, body), False
