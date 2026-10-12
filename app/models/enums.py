"""Domain enums and shared vocabulary for the sentiment engine."""

from __future__ import annotations

from enum import Enum


class Asset(str, Enum):
    """The supported instruments. Nothing else is ever analysed."""

    GOLD = "XAUUSD"
    SILVER = "XAGUSD"
    BITCOIN = "BTCUSD"
    # Item 2: S&P 500 as a first-class asset, not the legacy equity proxy.
    # "EQUITY" is the symbol in the DB/payload so the card keeps its own
    # identity separate from the three metal/crypto instruments.
    EQUITY = "EQUITY"

    @property
    def label(self) -> str:
        return {
            Asset.GOLD: "Gold",
            Asset.SILVER: "Silver",
            Asset.BITCOIN: "Bitcoin",
            Asset.EQUITY: "S&P 500",
        }[self]

    @classmethod
    def values(cls) -> list[str]:
        return [a.value for a in cls]


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"


class ImpactStrength(str, Enum):
    VERY_HIGH = "VERY_HIGH"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EconomicImportance(str, Enum):
    VERY_HIGH = "VERY_HIGH"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class NewsCategory(str, Enum):
    CENTRAL_BANK = "Central Bank"
    INTEREST_RATES = "Interest Rates"
    INFLATION = "Inflation"
    EMPLOYMENT = "Employment"
    GDP = "GDP"
    TREASURY_YIELDS = "Treasury Yields"
    USD_DXY = "USD/DXY"
    GEOPOLITICS = "Geopolitics"
    RISK_SENTIMENT = "Risk Sentiment"
    ETF_FLOWS = "ETF Flows"
    COMMODITY_SUPPLY_DEMAND = "Commodity Supply/Demand"
    INDUSTRIAL_DEMAND = "Industrial Demand"
    CHINA = "China"
    CRYPTO_REGULATION = "Crypto Regulation"
    BITCOIN_ETF_FLOWS = "Bitcoin ETF Flows"
    CRYPTO_MARKET_FLOWS = "Crypto Market Flows"
    OIL_ENERGY = "Oil/Energy"
    EQUITIES = "Equities"
    BANKING_FINANCIAL_STRESS = "Banking/Financial Stress"
    # CoinGlass derived categories (scraped from public CoinGlass charts)
    LIQUIDATION_HEATMAP = "Liquidation Heatmap"
    CVD = "CVD"
    FOOTPRINT = "Footprint"
    OTHER = "Other"


class ReportKind(str, Enum):
    """Section A depends on separating these two concepts."""

    PREP = "MARKET_PREP"     # expected / forward-looking setup
    WRAP = "MARKET_WRAP"     # what actually happened in the session
    GENERAL = "GENERAL"      # ordinary news, belongs in Section B only
    # Task 3: the article reports an ACTUAL scheduled-data print (its
    # direction comes from the surprise vs forecast/previous, not from
    # wording). Stored as a value only - the field name/shape is unchanged.
    ACTUAL = "ACTUAL_DATA"


class SessionName(str, Enum):
    ASIA = "ASIA"
    EUROPE = "EUROPE"
    US = "US"
    OFF_HOURS = "OFF_HOURS"


class MarketRegime(str, Enum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    INFLATIONARY = "INFLATIONARY"
    DEFLATIONARY = "DEFLATIONARY"
    HAWKISH = "HAWKISH"
    DOVISH = "DOVISH"
    LIQUIDITY_DRIVEN = "LIQUIDITY_DRIVEN"
    GROWTH_DRIVEN = "GROWTH_DRIVEN"
    GEOPOLITICAL = "GEOPOLITICAL"
    MIXED = "MIXED"


class Verification(str, Enum):
    """Section 18 - source quality / unverified labelling."""

    CONFIRMED = "CONFIRMED"
    UNVERIFIED = "UNVERIFIED"


class PriceConfirmation(str, Enum):
    """Section 12 - news is not price."""

    CONFIRMED = "CONFIRMED"
    DIVERGENT = "DIVERGENT"
    UNAVAILABLE = "UNAVAILABLE"