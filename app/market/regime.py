"""
MARKET REGIME DETECTION (section 15).

The regime changes how a given headline transmits to each asset: a hawkish
signal in a risk-off geopolitical regime does not necessarily mean "bearish
gold". Detected from today's news mix, with an explicit confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.logging_conf import get_logger
from app.models.enums import Asset, MarketRegime, NewsCategory

log = get_logger(__name__)

# Regime -> categories whose presence supports it.
REGIME_SIGNALS: dict[MarketRegime, list[NewsCategory]] = {
    MarketRegime.HAWKISH: [NewsCategory.CENTRAL_BANK, NewsCategory.INTEREST_RATES,
                            NewsCategory.TREASURY_YIELDS, NewsCategory.INFLATION],
    MarketRegime.DOVISH: [NewsCategory.CENTRAL_BANK, NewsCategory.INTEREST_RATES],
    MarketRegime.RISK_OFF: [NewsCategory.GEOPOLITICS, NewsCategory.BANKING_FINANCIAL_STRESS,
                            NewsCategory.RISK_SENTIMENT, NewsCategory.EQUITIES],
    MarketRegime.RISK_ON: [NewsCategory.EQUITIES, NewsCategory.RISK_SENTIMENT,
                           NewsCategory.ETF_FLOWS, NewsCategory.BITCOIN_ETF_FLOWS],
    MarketRegime.INFLATIONARY: [NewsCategory.INFLATION, NewsCategory.OIL_ENERGY],
    MarketRegime.DEFLATIONARY: [NewsCategory.EMPLOYMENT, NewsCategory.GDP],
    MarketRegime.LIQUIDITY_DRIVEN: [NewsCategory.CENTRAL_BANK, NewsCategory.TREASURY_YIELDS,
                                    NewsCategory.USD_DXY],
    MarketRegime.GROWTH_DRIVEN: [NewsCategory.GDP, NewsCategory.INDUSTRIAL_DEMAND,
                                 NewsCategory.CHINA, NewsCategory.EQUITIES],
    MarketRegime.GEOPOLITICAL: [NewsCategory.GEOPOLITICS],
    MarketRegime.MIXED: [],
}

# Regime -> how it modulates the usual transmission for each asset.
# Values nudge the assumed direction, they never overwrite per-item analysis.
REGIME_ASSET_BIAS: dict[MarketRegime, dict[Asset, float]] = {
    # positive = adds bullish pressure, negative = adds bearish pressure
    MarketRegime.HAWKISH: {Asset.GOLD: -0.35, Asset.SILVER: -0.35, Asset.BITCOIN: -0.45},
    MarketRegime.DOVISH: {Asset.GOLD: 0.35, Asset.SILVER: 0.35, Asset.BITCOIN: 0.45},
    MarketRegime.RISK_OFF: {Asset.GOLD: 0.25, Asset.SILVER: 0.05, Asset.BITCOIN: -0.45},
    MarketRegime.RISK_ON: {Asset.GOLD: -0.15, Asset.SILVER: 0.25, Asset.BITCOIN: 0.40},
    MarketRegime.INFLATIONARY: {Asset.GOLD: 0.30, Asset.SILVER: 0.20, Asset.BITCOIN: -0.20},
    MarketRegime.DEFLATIONARY: {Asset.GOLD: 0.25, Asset.SILVER: -0.10, Asset.BITCOIN: -0.30},
    MarketRegime.LIQUIDITY_DRIVEN: {Asset.GOLD: 0.10, Asset.SILVER: 0.10, Asset.BITCOIN: 0.35},
    MarketRegime.GROWTH_DRIVEN: {Asset.GOLD: -0.10, Asset.SILVER: 0.35, Asset.BITCOIN: 0.30},
    MarketRegime.GEOPOLITICAL: {Asset.GOLD: 0.40, Asset.SILVER: 0.10, Asset.BITCOIN: -0.20},
    MarketRegime.MIXED: {Asset.GOLD: 0.0, Asset.SILVER: 0.0, Asset.BITCOIN: 0.0},
}


@dataclass
class RegimeAssessment:
    regime: MarketRegime = MarketRegime.MIXED
    confidence: float = 0.0
    drivers: list[str] = field(default_factory=list)
    scores: dict[MarketRegime, float] = field(default_factory=dict)

    def bias_for(self, asset: Asset) -> float:
        return REGIME_ASSET_BIAS.get(self.regime, {}).get(asset, 0.0)


def detect_regime(category_counts: dict[NewsCategory, float]) -> RegimeAssessment:
    """
    Score each regime from weighted category presence in today's news.

    A regime only wins if it clearly outscores the runner-up, otherwise the
    engine reports MIXED - which is the honest answer when macro signals
    genuinely point both ways.
    """
    scores: dict[MarketRegime, float] = {}
    for regime, categories in REGIME_SIGNALS.items():
        if not categories:
            continue
        scores[regime] = sum(category_counts.get(c, 0.0) for c in categories)

    if not scores or max(scores.values()) <= 0:
        return RegimeAssessment(regime=MarketRegime.MIXED, confidence=0.0,
                                drivers=["No decisive macro cluster in today's news."],
                                scores=scores)

    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    top_name, top_score = ordered[0]
    runner_score = ordered[1][1] if len(ordered) > 1 else 0.0

    margin = top_score - runner_score
    total = sum(v for _, v in ordered[:3]) or 1.0
    confidence = round(max(0.05, min(0.9, (margin / total) * 1.5 + 0.1)), 3)

    if top_name is MarketRegime.HAWKISH:
        # Hawkish and dovish can both be "central bank" driven - only keep
        # HAWKISH when rates/yields/inflation actually dominate.
        dominance = (top_score - scores.get(MarketRegime.DOVISH, 0.0)) / (top_score or 1)
        if dominance < 0.15:
            top_name = MarketRegime.LIQUIDITY_DRIVEN

    drivers = [
        f"{regime.value}: {score:.1f} weighted signal"
        for regime, score in ordered[:3]
    ]

    return RegimeAssessment(regime=top_name, confidence=confidence,
                            drivers=drivers, scores=scores)