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
def _strength_from(mag: float, relevance: float,
                   importance: EconomicImportance) -> ImpactStrength:
    """Map cue magnitude + relevance + importance onto a strength band."""
    if mag >= 8 or importance is EconomicImportance.VERY_HIGH:
        base = ImpactStrength.VERY_HIGH
    elif mag >= 5:
        base = ImpactStrength.HIGH
    elif mag >= 3:
        base = ImpactStrength.MEDIUM
    else:
        base = ImpactStrength.LOW
    # Weak relevance caps the strength regardless of headline drama.
    if relevance < 0.22:
        return ImpactStrength.LOW
    if relevance < 0.30 and base is ImpactStrength.VERY_HIGH:
        return ImpactStrength.HIGH
    return base


def _persistence_from(importance: EconomicImportance, direction: Direction,
                      category: NewsCategory) -> float:
    """Expected persistence through TODAY's session (section 13)."""
    base = {
        EconomicImportance.VERY_HIGH: 0.90,
        EconomicImportance.HIGH: 0.70,
        EconomicImportance.MEDIUM: 0.50,
        EconomicImportance.LOW: 0.30,
    }[importance]
    if category in (NewsCategory.BANKING_FINANCIAL_STRESS, NewsCategory.GEOPOLITICS,
                    NewsCategory.CENTRAL_BANK):
        base = min(1.0, base + 0.10)
    if direction is Direction.MIXED:
        base *= 0.8
    return base


def _build_reason(asset: Asset, category: NewsCategory, direction: Direction,
                  text: str) -> str:
    cat_label = category.value if category else "news"
    snippet = " ".join(text.split())[:110]
    return (
        f"{cat_label} headline reading net {direction.value.lower()} for "
        f"{asset.label} ({asset.value}). Relevance from category weight plus "
        f"explicit asset mention. Snippet: \"{snippet}\""
    )


def _transmission_for(asset: Asset, direction: Direction) -> str:
    if direction in (Direction.BULLISH, Direction.BEARISH):
        return TRANSMISSION[direction.value].get(asset, "Transmission unclear.")
    return ("No dominant directional transmission path; treated as neutral "
            "intraday input.")


def heuristic_analyze(title: str, body: str = "") -> ArticleAnalysis:
    """Deterministic classification - the always-available fallback engine."""
    text = f"{title} {body}".strip()
    result = ArticleAnalysis(mode="heuristic")

    result.category = detect_category(text)
    result.importance = detect_importance(text, result.category)
    result.report_kind = detect_report_kind(text)
    result.verification = detect_verification(text)
    result.event_title = title.strip()
    result.event_summary = (body or title)[:300].strip()

    lowered = text.lower()
    bull = score_cues(lowered, BULLISH_CUES)
    bear = score_cues(lowered, BEARISH_CUES)
    direction = detect_direction(lowered)
    mag = max(bull, bear)

    char = detect_metal_character(lowered)
    for asset in relevant_assets(result.category, text):
        rel = relevance_for(result.category, asset, text)
        result.assets.append(
            AssetAnalysis(
                asset=asset,
                direction=direction,
                strength=_strength_from(mag, rel, result.importance),
                relevance=rel,
                persistence=_persistence_from(result.importance, direction,
                                              result.category),
                reason=_build_reason(asset, result.category, direction, text),
                transmission=_transmission_for(asset, direction),
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
  "report_kind": "MARKET_PREP|MARKET_WRAP|GENERAL",
  "verification": "CONFIRMED|UNVERIFIED",
  "event_title": "canonical EVENT name (not a restatement of the headline)",
  "event_summary": "one sentence describing what happened",
  "relevance_score": 0.0-1.0,
  "assets": [
    {
      "asset": "XAUUSD|XAGUSD|BTCUSD",
      "direction": "BULLISH|BEARISH|NEUTRAL|MIXED",
      "strength": "VERY_HIGH|HIGH|MEDIUM|LOW",
      "relevance": 0.0-1.0,
      "persistence": 0.0-1.0,
      "reason": "WHY this affects the asset - 1-2 specific sentences",
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
- Never force an entry just to fill the array.
- At most 3 entries, ordered by relevance descending.
- Keep "reason" under 60 words and "transmission" under 40 words so the
  whole object stays well inside the token limit."""


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
async def llm_analyze(title: str, body: str = "") -> ArticleAnalysis:
    """Ask the configured LLM to classify. Raises on failure so the caller can fall back."""
    body = (body or "")[:MAX_BODY_CHARS]
    user_prompt = (
        f"HEADLINE:\n{title}\n\n"
        f"ARTICLE BODY (may be truncated or empty):\n{body or '[not available]'}"
    )
    data = await llm_client.chat_json(SYSTEM_PROMPT, user_prompt)

    from app.config import settings

    result = ArticleAnalysis(mode="llm")
    result.category = _coerce_enum(NewsCategory, data.get("category"), NewsCategory.OTHER)
    result.importance = _coerce_enum(
        EconomicImportance, data.get("importance"), EconomicImportance.LOW)
    result.report_kind = _coerce_enum(ReportKind, data.get("report_kind"), ReportKind.GENERAL)
    result.verification = _coerce_enum(
        Verification, data.get("verification"), Verification.CONFIRMED)
    result.event_title = str(data.get("event_title") or title)[:300]
    result.event_summary = str(data.get("event_summary") or "")[:500]
    result.relevance_score = _clamp(data.get("relevance_score"))

    raw_assets = data.get("assets")
    entries: list = []
    if isinstance(raw_assets, list):
        entries = raw_assets[:3]
    elif isinstance(raw_assets, dict):
        # Some models key the map by asset name instead of using a list.
        entries = [
            {"asset": key, **(value if isinstance(value, dict) else {})}
            for key, value in list(raw_assets.items())[:3]
        ]
    elif raw_assets is None:
        # Genuinely malformed response - the key is missing entirely.
        raise ValueError(
            "LLM response has no 'assets' key. Keys=" f"{sorted(data.keys())}"
        )

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        asset = _coerce_enum(Asset, entry.get("asset"), None)
        if asset is None:
            continue
        char = entry.get("metal_character") if asset is Asset.SILVER else None
        result.assets.append(
            AssetAnalysis(
                asset=asset,
                direction=_coerce_enum(Direction, entry.get("direction"), Direction.NEUTRAL),
                strength=_coerce_enum(ImpactStrength, entry.get("strength"), ImpactStrength.LOW),
                relevance=_clamp(entry.get("relevance")),
                persistence=_clamp(entry.get("persistence"), 0.05, 1.0),
                reason=str(entry.get("reason") or "")[:600],
                transmission=str(entry.get("transmission") or "")[:600],
                metal_character=char if char in ("PRECIOUS", "INDUSTRIAL", "BOTH") else None,
            )
        )

    if not result.assets:
        # An EMPTY assets array is a VALID answer, not a failure: the model is
        # correctly reporting that this item does not materially affect
        # XAUUSD/XAGUSD/BTCUSD. It must not be discarded or silently
        # upgraded to a directional call.
        result.note = (
            f"{settings.llm_model}: no material intraday impact on "
            f"{'/'.join(Asset.values())}."
        )
        return result

    result.note = f"Classified by {settings.llm_model}."
    return result


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
