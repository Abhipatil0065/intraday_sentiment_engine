"""Asset relevance matrix (sections 6-9).

How much a given news *category* can plausibly move each asset. This is the
"Asset Relevance" factor of the final weight, and the routing rule for which
assets an article is analysed against at all.
"""

from __future__ import annotations

from app.models.enums import Asset, NewsCategory

# Base relevance 0.0-1.0 per (category, asset).
RELEVANCE: dict[NewsCategory, dict[Asset, float]] = {
    NewsCategory.CENTRAL_BANK:            {Asset.GOLD: 0.90, Asset.SILVER: 0.88, Asset.BITCOIN: 0.86},
    NewsCategory.INTEREST_RATES:          {Asset.GOLD: 0.90, Asset.SILVER: 0.85, Asset.BITCOIN: 0.84},
    NewsCategory.INFLATION:               {Asset.GOLD: 0.82, Asset.SILVER: 0.80, Asset.BITCOIN: 0.76},
    NewsCategory.EMPLOYMENT:              {Asset.GOLD: 0.78, Asset.SILVER: 0.72, Asset.BITCOIN: 0.76},
    NewsCategory.GDP:                     {Asset.GOLD: 0.62, Asset.SILVER: 0.70, Asset.BITCOIN: 0.70},
    NewsCategory.TREASURY_YIELDS:         {Asset.GOLD: 0.92, Asset.SILVER: 0.84, Asset.BITCOIN: 0.82},
    NewsCategory.USD_DXY:                 {Asset.GOLD: 0.88, Asset.SILVER: 0.84, Asset.BITCOIN: 0.72},
    NewsCategory.GEOPOLITICS:             {Asset.GOLD: 0.82, Asset.SILVER: 0.70, Asset.BITCOIN: 0.66},
    NewsCategory.RISK_SENTIMENT:          {Asset.GOLD: 0.72, Asset.SILVER: 0.72, Asset.BITCOIN: 0.80},
    NewsCategory.ETF_FLOWS:               {Asset.GOLD: 0.86, Asset.SILVER: 0.80, Asset.BITCOIN: 0.55},
    NewsCategory.COMMODITY_SUPPLY_DEMAND: {Asset.GOLD: 0.80, Asset.SILVER: 0.78, Asset.BITCOIN: 0.10},
    NewsCategory.INDUSTRIAL_DEMAND:       {Asset.GOLD: 0.28, Asset.SILVER: 0.72, Asset.BITCOIN: 0.20},
    NewsCategory.CHINA:                   {Asset.GOLD: 0.55, Asset.SILVER: 0.80, Asset.BITCOIN: 0.40},
    NewsCategory.CRYPTO_REGULATION:       {Asset.GOLD: 0.05, Asset.SILVER: 0.05, Asset.BITCOIN: 0.88},
    NewsCategory.BITCOIN_ETF_FLOWS:       {Asset.GOLD: 0.05, Asset.SILVER: 0.05, Asset.BITCOIN: 0.95},
    NewsCategory.CRYPTO_MARKET_FLOWS:     {Asset.GOLD: 0.02, Asset.SILVER: 0.02, Asset.BITCOIN: 0.92},
    NewsCategory.OIL_ENERGY:              {Asset.GOLD: 0.44, Asset.SILVER: 0.40, Asset.BITCOIN: 0.22},
    NewsCategory.EQUITIES:                {Asset.GOLD: 0.52, Asset.SILVER: 0.58, Asset.BITCOIN: 0.72},
    NewsCategory.BANKING_FINANCIAL_STRESS: {Asset.GOLD: 0.78, Asset.SILVER: 0.66, Asset.BITCOIN: 0.74},
    NewsCategory.OTHER:                   {Asset.GOLD: 0.30, Asset.SILVER: 0.30, Asset.BITCOIN: 0.30},
}

# Direct asset mentions override the category default.
DIRECT_MENTION: dict[Asset, tuple[str, ...]] = {
    Asset.GOLD: ("gold", "xau", "bullion", "comex gold", "gold price"),
    Asset.SILVER: ("silver", "xag", "silver price"),
    Asset.BITCOIN: ("bitcoin", "btc", "xbt", "bitcoin price"),
}

# Below this relevance the asset is not analysed for that article at all.
MIN_RELEVANCE = 0.20


def base_relevance(category: NewsCategory, asset: Asset) -> float:
    return RELEVANCE.get(category, {}).get(asset, 0.25)


def relevance_for(category: NewsCategory, asset: Asset, text: str) -> float:
    """
    Blend the category default with explicit mention in the headline/body.

    An explicit asset mention is strong evidence the item is about that asset
    and raises relevance; it never fully overrides the category default
    because a headline may mention an asset while driving a different
    mechanism (e.g. an oil headline that mentions gold).
    """
    lowered = text.lower()
    base = base_relevance(category, asset)

    mentions = sum(1 for token in DIRECT_MENTION[asset] if token in lowered)
    if mentions:
        boosted = min(1.0, base + 0.25 * mentions)
        return max(base, boosted)
    return base


def relevant_assets(category: NewsCategory, text: str) -> list[Asset]:
    """Which assets this article should be analysed against."""
    out: list[Asset] = []
    for asset in (Asset.GOLD, Asset.SILVER, Asset.BITCOIN):
        if relevance_for(category, asset, text) >= MIN_RELEVANCE:
            out.append(asset)
    return out or [Asset.GOLD]  # never silently drop everything