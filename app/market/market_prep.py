"""
MARKET PREP ENGINE (Section A).

Market PREP describes the EXPECTED / forward-looking setup: what the session
is positioned for before it develops. It is built strictly from articles
classified as MARKET_PREP plus forward-looking catalysts - it never mixes in
MARKET_WRAP content (the two concepts are kept separate by design).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.analysis.sentiment_engine import (
    SentimentResult,
    WeightedSignal,
)
from app.analysis.sentiment_engine import (
    compute as compute_sentiment,
)
from app.logging_conf import get_logger
from app.models.enums import Asset, NewsCategory, ReportKind

log = get_logger(__name__)

# Catalysts the session is likely to respond to, per asset.
ASSET_CATALYSTS: dict[Asset, list[str]] = {
    Asset.GOLD: [
        "Fed speakers and policy communication",
        "US inflation and employment data",
        "Treasury yields and real-rate moves",
        "USD index direction",
        "Geopolitical escalation or de-escalation",
    ],
    Asset.SILVER: [
        "Gold-equivalent macro drivers",
        "China industrial and manufacturing data",
        "Copper and industrial-metal sentiment",
        "Gold/silver ratio behaviour",
        "Physical and solar/electronics demand",
    ],
    Asset.BITCOIN: [
        "Spot Bitcoin ETF flows",
        "US macro prints and Fed path",
        "Nasdaq and equity risk appetite",
        "Stablecoin and exchange liquidity",
        "Crypto regulatory developments",
    ],
}

MACRO_DRIVER_LABELS: dict[NewsCategory, str] = {
    NewsCategory.CENTRAL_BANK: "Central-bank policy expectations",
    NewsCategory.INTEREST_RATES: "Interest-rate path repricing",
    NewsCategory.INFLATION: "Inflation trajectory",
    NewsCategory.EMPLOYMENT: "Labour-market strength",
    NewsCategory.GDP: "Growth outlook",
    NewsCategory.TREASURY_YIELDS: "Treasury / real yields",
    NewsCategory.USD_DXY: "US dollar strength",
    NewsCategory.GEOPOLITICS: "Geopolitical risk",
    NewsCategory.RISK_SENTIMENT: "Global risk appetite",
    NewsCategory.ETF_FLOWS: "ETF flow positioning",
    NewsCategory.BITCOIN_ETF_FLOWS: "Spot Bitcoin ETF flows",
    NewsCategory.CRYPTO_MARKET_FLOWS: "Crypto market flows",
    NewsCategory.INDUSTRIAL_DEMAND: "Industrial demand",
    NewsCategory.CHINA: "China growth signals",
    NewsCategory.CRYPTO_REGULATION: "Crypto regulation",
    NewsCategory.EQUITIES: "Equity risk tone",
    NewsCategory.OIL_ENERGY: "Energy cost pressure",
    NewsCategory.BANKING_FINANCIAL_STRESS: "Financial-system stress",
    NewsCategory.COMMODITY_SUPPLY_DEMAND: "Physical supply/demand",
    NewsCategory.OTHER: "Unclassified flow",
}


@dataclass
class PrepResult:
    asset: Asset
    sentiment: SentimentResult
    macro_drivers: list[str] = field(default_factory=list)
    upcoming_catalysts: list[str] = field(default_factory=list)
    source_headlines: list[str] = field(default_factory=list)
    note: str = ""


def build(
    signals: Sequence[WeightedSignal],
    category_weights: dict[NewsCategory, float],
    asset: Asset,
) -> PrepResult:
    """
    Build the prep view for one asset.

    Falls back to the overall news flow when no explicit MARKET_PREP article
    exists today, but states that clearly so the reader knows the setup is
    inferred rather than sourced from a prep report.
    """
    asset_signals = [s for s in signals if s.asset is asset]

    prep_signals = [s for s in asset_signals if s.report_kind is ReportKind.PREP]
    explicit = bool(prep_signals)

    # Prep sentiment blends explicit prep articles with the forward-looking
    # weight of the general flow (general news informs expectations too).
    if explicit:
        blended = list(prep_signals)
        blended.extend(s for s in asset_signals if s.report_kind is not ReportKind.PREP)
        sentiment = compute_sentiment(blended, asset)
        note = "Built from today's MARKET_PREP reports plus supporting macro flow."
    else:
        sentiment = compute_sentiment(asset_signals, asset)
        note = (
            "No MARKET_PREP article found today - setup inferred from "
            "general intraday news flow."
        )

    macro = [
        MACRO_DRIVER_LABELS[c]
        for c, w in sorted(category_weights.items(), key=lambda kv: -kv[1])
        if w > 0 and c is not NewsCategory.OTHER
    ][:4]

    result = PrepResult(
        asset=asset,
        sentiment=sentiment,
        macro_drivers=macro or ["Data unavailable - no relevant macro news today."],
        upcoming_catalysts=ASSET_CATALYSTS[asset],
        source_headlines=[s.title for s in (prep_signals or asset_signals)[:3]],
        note=note,
    )
    return result