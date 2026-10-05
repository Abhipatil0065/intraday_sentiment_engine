"""Asset relevance matrix (sections 6-9).

How much a given news *category* can plausibly move each asset. This is the
"Asset Relevance" factor of the final weight, and the routing rule for which
assets an article is analysed against at all.
"""

from __future__ import annotations

from app.analysis.text_match import contains_phrase
from app.models.enums import Asset, NewsCategory

# Base relevance 0.0-1.0 per (category, asset).
RELEVANCE: dict[NewsCategory, dict[Asset, float]] = {
    NewsCategory.CENTRAL_BANK:            {Asset.GOLD: 0.90, Asset.SILVER: 0.88, Asset.BITCOIN: 0.86, Asset.OIL: 0.72},
    NewsCategory.INTEREST_RATES:          {Asset.GOLD: 0.90, Asset.SILVER: 0.85, Asset.BITCOIN: 0.84, Asset.OIL: 0.70},
    NewsCategory.INFLATION:               {Asset.GOLD: 0.82, Asset.SILVER: 0.80, Asset.BITCOIN: 0.76, Asset.OIL: 0.66},
    NewsCategory.EMPLOYMENT:              {Asset.GOLD: 0.78, Asset.SILVER: 0.72, Asset.BITCOIN: 0.76, Asset.OIL: 0.62},
    NewsCategory.GDP:                     {Asset.GOLD: 0.62, Asset.SILVER: 0.70, Asset.BITCOIN: 0.70, Asset.OIL: 0.78},
    NewsCategory.TREASURY_YIELDS:         {Asset.GOLD: 0.92, Asset.SILVER: 0.84, Asset.BITCOIN: 0.82, Asset.OIL: 0.60},
    NewsCategory.USD_DXY:                 {Asset.GOLD: 0.88, Asset.SILVER: 0.84, Asset.BITCOIN: 0.72, Asset.OIL: 0.80},
    NewsCategory.GEOPOLITICS:             {Asset.GOLD: 0.82, Asset.SILVER: 0.70, Asset.BITCOIN: 0.66, Asset.OIL: 0.88},
    NewsCategory.RISK_SENTIMENT:          {Asset.GOLD: 0.72, Asset.SILVER: 0.72, Asset.BITCOIN: 0.80, Asset.OIL: 0.68},
    NewsCategory.ETF_FLOWS:               {Asset.GOLD: 0.86, Asset.SILVER: 0.80, Asset.BITCOIN: 0.55, Asset.OIL: 0.05},
    NewsCategory.COMMODITY_SUPPLY_DEMAND: {Asset.GOLD: 0.80, Asset.SILVER: 0.78, Asset.BITCOIN: 0.10, Asset.OIL: 0.95},
    NewsCategory.INDUSTRIAL_DEMAND:       {Asset.GOLD: 0.28, Asset.SILVER: 0.72, Asset.BITCOIN: 0.20, Asset.OIL: 0.82},
    NewsCategory.CHINA:                   {Asset.GOLD: 0.55, Asset.SILVER: 0.80, Asset.BITCOIN: 0.40, Asset.OIL: 0.74},
    NewsCategory.CRYPTO_REGULATION:       {Asset.GOLD: 0.05, Asset.SILVER: 0.05, Asset.BITCOIN: 0.88, Asset.OIL: 0.05},
    NewsCategory.BITCOIN_ETF_FLOWS:       {Asset.GOLD: 0.05, Asset.SILVER: 0.05, Asset.BITCOIN: 0.95, Asset.OIL: 0.05},
    NewsCategory.CRYPTO_MARKET_FLOWS:     {Asset.GOLD: 0.02, Asset.SILVER: 0.02, Asset.BITCOIN: 0.92, Asset.OIL: 0.02},
    NewsCategory.OIL_ENERGY:              {Asset.GOLD: 0.44, Asset.SILVER: 0.40, Asset.BITCOIN: 0.22, Asset.OIL: 1.00},
    NewsCategory.EQUITIES:                {Asset.GOLD: 0.52, Asset.SILVER: 0.58, Asset.BITCOIN: 0.72, Asset.OIL: 0.58},
    NewsCategory.BANKING_FINANCIAL_STRESS: {Asset.GOLD: 0.78, Asset.SILVER: 0.66, Asset.BITCOIN: 0.74, Asset.OIL: 0.76},
    # OTHER deliberately sits BELOW MIN_RELEVANCE. An item we could not
    # classify has no demonstrated transmission path, so it must not reach the
    # assets on category alone. It still routes once the text explicitly names
    # an asset, because the +0.25 mention boost lifts it back over the line.
    NewsCategory.OTHER:                   {Asset.GOLD: 0.10, Asset.SILVER: 0.10, Asset.BITCOIN: 0.10, Asset.OIL: 0.10},
}

# Direct asset mentions override the category default.
#
# OIL deliberately has NO bare "oil" token. contains_phrase() is whole-word, and
# "oil" IS a whole word inside "olive oil", "palm oil" and "cooking oil", so a
# bare token would route grocery and cooking stories to a crude-oil card. Only
# unambiguous multi-word phrases and proper nouns are listed here instead.
DIRECT_MENTION: dict[Asset, tuple[str, ...]] = {
    Asset.GOLD: ("gold", "xau", "bullion", "comex gold", "gold price"),
    Asset.SILVER: ("silver", "xag", "silver price"),
    Asset.BITCOIN: ("bitcoin", "btc", "xbt", "bitcoin price"),
    Asset.OIL: (
        "crude oil", "crude", "wti", "brent", "opec", "opec+", "petroleum",
        "barrel", "refinery", "oil price", "oil prices", "oil futures",
        "oil supply", "oil demand", "oil output", "oil production",
        "oil inventories", "oil stocks", "oil market", "oil majors",
        "oilfield", "oil tanker", "oil embargo", "oil sanctions",
        "crude stocks", "crude inventories", "crude output", "crude pipeline",
        "eia crude", "api crude", "saudi aramco", "aramco", "hormuz",
        "strait of hormuz", "shale oil", "shale producer", "energy prices",
        "oil rig", "oil glut", "oil shortage",
    ),
}

# Text that carries the word "oil" but is about a CONSUMER or INDUSTRIAL
# product, never crude. These must never reach the OIL card, even though the
# shared OIL_ENERGY category keywords legitimately contain the bare word "oil"
# (they predate this asset and are left untouched for gold/silver/bitcoin).
OIL_FALSE_FRIENDS: tuple[str, ...] = (
    "olive oil", "palm oil", "cooking oil", "coconut oil", "sesame oil",
    "sunflower oil", "rapeseed oil", "canola oil", "soybean oil", "mustard oil",
    "fish oil", "cod liver oil", "olive", "snake oil", "essential oil",
    "hair oil", "machine oil", "lubricating oil", "engine oil", "motor oil",
    "cooking", "recipe", "cuisine", "restaurant menu",
)

# Below this relevance the asset is not analysed for that article at all.
MIN_RELEVANCE = 0.20


def base_relevance(category: NewsCategory, asset: Asset) -> float:
    return RELEVANCE.get(category, {}).get(asset, 0.25)


def _is_oil_false_friend(text: str) -> bool:
    """
    True when the text is about consumer/industrial oil, not crude.

    Scoped to OIL on purpose. The shared OIL_ENERGY category keywords contain the
    bare word "oil" and always did; tightening that shared list would change what
    reaches gold, silver and bitcoin, which this change must not do.
    """
    lowered = (text or "").lower()
    return any(contains_phrase(lowered, phrase) for phrase in OIL_FALSE_FRIENDS)


def relevance_for(category: NewsCategory, asset: Asset, text: str) -> float:
    """
    Blend the category default with explicit mention in the headline/body.

    An explicit asset mention is strong evidence the item is about that asset
    and raises relevance; it never fully overrides the category default
    because a headline may mention an asset while driving a different
    mechanism (e.g. an oil headline that mentions gold).
    """
    lowered = text.lower()
    # A cooking/olive-oil story must never reach the crude card, whatever the
    # category says. Dropped below MIN_RELEVANCE so routing skips it.
    if asset is Asset.OIL and _is_oil_false_friend(lowered):
        return 0.0
    base = base_relevance(category, asset)

    mentions = sum(1 for token in DIRECT_MENTION[asset] if contains_phrase(lowered, token))
    if mentions:
        boosted = min(1.0, base + 0.25 * mentions)
        return max(base, boosted)
    return base


def relevant_assets(category: NewsCategory, text: str) -> list[Asset]:
    """
    Which assets this article should be analysed against.

    An article that routes to NO asset is a legitimate outcome and must be
    allowed to stay empty: the item simply has no demonstrable transmission
    path to XAUUSD / XAGUSD / BTCUSD / USOIL. Forcing a fallback asset here is
    what let a sports-betting marketing story surface as a gold driver.

    Iterating the enum means a newly added asset is picked up here with no
    further edit; declaration order fixes card order (gold, silver, bitcoin, oil).
    """
    out: list[Asset] = []
    for asset in Asset:
        if relevance_for(category, asset, text) >= MIN_RELEVANCE:
            out.append(asset)
    return out