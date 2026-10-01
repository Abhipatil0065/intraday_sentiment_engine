"""Heuristic knowledge base - categories, keywords and cue tables."""

from __future__ import annotations

from typing import Optional

from app.models.enums import (
    EconomicImportance,
    NewsCategory,
    ReportKind,
    Verification,
)

CATEGORY_KEYWORDS: dict[NewsCategory, list[str]] = {
    NewsCategory.CENTRAL_BANK: ["federal reserve", "fed", "fomc", "ecb", "boe", "boj",
                                "central bank", "rate decision", "powell", "lagarde"],
    NewsCategory.INTEREST_RATES: ["interest rate", "rate cut", "rate hike", "policy rate",
                                  "monetary policy", "basis points", "bps", "funds rate"],
    NewsCategory.INFLATION: ["cpi", "pce", "inflation", "consumer prices", "price index",
                             "core inflation", "deflator", "producer prices", "ppi"],
    NewsCategory.EMPLOYMENT: ["nonfarm", "nfp", "payrolls", "jobs report", "unemployment",
                              "jobless claims", "employment", "labor market", "hiring"],
    NewsCategory.GDP: ["gdp", "gross domestic product", "economic growth", "recession",
                       "pmi", "ism", "industrial production", "retail sales"],
    NewsCategory.TREASURY_YIELDS: ["treasury yield", "10-year", "2-year", "bond yield",
                                   "yields", "real yields", "treasur", "note auction"],
    NewsCategory.USD_DXY: ["dollar index", "dxy", "greenback", "usd", "currency", "forex",
                          "euro", "yen", "sterling", "dollar"],
    NewsCategory.GEOPOLITICS: ["war", "conflict", "sanction", "missile", "invasion",
                               "tension", "strike", "ceasefire", "tariff", "trade war",
                               "election", "coup", "border"],
    NewsCategory.RISK_SENTIMENT: ["risk-off", "risk-on", "risk sentiment", "safe haven",
                                 "flight to quality", "investor sentiment", "panic",
                                 "risk aversion", "volatility", "vix"],
    NewsCategory.ETF_FLOWS: ["etf", "gold fund", "gld", "flows", "holdings", "inflow",
                             "outflow", "reserves"],
    NewsCategory.COMMODITY_SUPPLY_DEMAND: ["gold supply", "mine", "mining", "bullion",
                                           "output", "production", "refinery", "opec"],
    NewsCategory.INDUSTRIAL_DEMAND: ["copper", "manufacturing", "factory", "industrial",
                                     "solar", "electronics", "pmi", "factory gate"],
    NewsCategory.CHINA: ["china", "chinese", "beijing", "shanghai", "yuan", "renminbi",
                         "pboc", "caixin", "shenzhen"],
    NewsCategory.CRYPTO_REGULATION: ["sec", "cftc", "regulation", "regulatory", "ban",
                                    "lawsuit", "custody", "crypto regulation",
                                    "stablecoin bill", "congress"],
    NewsCategory.BITCOIN_ETF_FLOWS: ["bitcoin etf", "spot etf", "ibit", "fbtc", "btco",
                                     "bitcoin fund", "etf inflow", "etf outflow"],
    NewsCategory.CRYPTO_MARKET_FLOWS: ["liquidat", "bitcoin price", "btc", "ethereum",
                                       "crypto", "coinbase", "hashrate", "whale",
                                       "exchange inflow", "on-chain"],
    NewsCategory.OIL_ENERGY: ["oil", "crude", "wti", "brent", "opec", "energy prices",
                              "natural gas"],
    NewsCategory.EQUITIES: ["stocks", "s&p", "nasdaq", "dow", "equities", "wall street",
                            "shares", "index", "earnings"],
    NewsCategory.BANKING_FINANCIAL_STRESS: ["bank", "credit", "default", "insolvency",
                                            "bailout", "contagion", "liquidity crisis",
                                            "leverage", "margin debt"],
    NewsCategory.OTHER: [],
}

VERY_HIGH_TRIGGERS = [
    "fomc", "federal reserve announces", "emergency meeting", "interest rate decision",
    "cpi", "consumer price index", "pce", "nonfarm payroll", "jobs report",
    "recession", "default", "war", "invasion", "ceasefire", "rate cut", "rate hike",
    "bitcoin etf approval", "sec approves", "intervention", "crisis",
    # Explicit policy-path repricing is a genuine intraday market mover.
    "higher for longer", "fewer rate cuts", "more rate cuts", "cut rates",
    "raise rates", "hawkish", "dovish",
]
HIGH_TRIGGERS = ["fed", "rate", "yield", "dollar", "gdp", "pmi", "inflation",
                 "payroll", "etf", "sanction", "tariff", "china", "treasury",
                 "policy"]

# Low-impact tells: filler/minor items that must not be promoted just because
# they mention a macro word. NOTE: "speech"/"comment" are deliberately absent -
# section 5 explicitly ranks Fed speeches as HIGH impact.
LOW_TRIGGERS = ["single store", "holiday", "weather", "celebration",
                "anniversary", "recipe", "horoscope", "giveaway",
                "sponsored", "best deals", "shopping guide"]

PREP_CUES = ["market prep", "session prep", "preview", "what to watch", "ahead of",
             "set up for", "before the open", "pre-market", "is set to open",
             "look ahead", "setup:", "primer"]
WRAP_CUES = ["market wrap", "session wrap", "recap", "what moved", "closing bell",
             "session recap", "market close", "end of session", "roundup",
             "close:", "wrap:", "review of the session", "what changed"]
RUMOR_CUES = ["report:", "rumor", "rumour", "sources say", "people familiar",
              "unconfirmed", "allegedly", "leak", "not yet confirmed"]