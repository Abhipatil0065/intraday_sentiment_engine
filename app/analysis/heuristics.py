"""Heuristic knowledge base - categories, keywords and cue tables."""

from __future__ import annotations

from app.models.enums import (
    NewsCategory,
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
    # "risk appetite" / "risk mood" / "risk tone" belong here: the polarity
    # resolver already reads them as RISK_ON/RISK_OFF, but without the category
    # keyword a headline like "risk appetite faded across equity markets" was
    # classified Other (relevance 0.1, below the 0.20 gate) and discarded before
    # its correctly-resolved transmission could be used. Category keywords route;
    # the resolver decides direction. Both are needed.
    NewsCategory.RISK_SENTIMENT: ["risk-off", "risk-on", "risk sentiment",
                                 "risk appetite", "risk mood", "risk tone",
                                 "safe haven",
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
                            "shares", "index", "earnings", "guidance", "guidance raise",
                            "guidance cut", "guidance raised", "guidance lowered",
                            "profit", "revenue", "eps", "beat", "miss", "outlook",
                            "quarterly", "results", "forecast", "pre-market", "after-hours",
                            "after hours", "premarket", "ah"],
    NewsCategory.BANKING_FINANCIAL_STRESS: ["bank", "credit", "default", "insolvency",
                                            "bailout", "contagion", "liquidity crisis",
                                            "leverage", "margin debt"],
    NewsCategory.OTHER: [],
}

# Keywords intentionally authored as STEMS rather than whole words. They are
# matched with a start-anchored prefix test instead of a whole-word test, so
# "liquidat" still catches "liquidations" without letting an unrelated word
# containing the fragment trigger the category.
#
# Everything NOT listed here is matched as a whole word or its regular plural.
# That is the point of the registry: prefix matching becomes an explicit,
# reviewable decision rather than the accidental default that made "credits"
# match the keyword "credit".
STEM_KEYWORDS: frozenset[str] = frozenset({
    "liquidat",      # CRYPTO_MARKET_FLOWS: liquidations / liquidated
    "escalat",       # GEOPOLITICS: escalation / escalates / escalating
    "treasur",       # TREASURY_YIELDS: treasury / treasuries
})

# Keywords that must NEVER accept a plural suffix. Whole-word matching alone is
# not sufficient overall, because news keywords are usually plural
# ("strikes", "sanctions", "yields", "ETFs") - but for these particular words
# the "+s" form is a different word, not a plural:
#
#   "credit" + s -> "credits"  (the verb "acknowledges", not bank credit)
#   "war"    + s -> "wars"      (and "toward"/"warrant"/"warm"/"warn" are
#                               rejected by the whole-word guard either way)
#   "ban"    + s -> "banks"     (must not let the crypto keyword match "bank")
#   "sec"    + s -> "secs"      (must not match "second")
#   "usd"    + s -> "usds"      (must not match "USDT")
#   "fed"    + s -> "feds"      (must not match "federated")
#
# Everything not listed here matches as a whole word OR its regular plural.
PLURAL_EXEMPT_KEYWORDS: frozenset[str] = frozenset({
    "credit", "ban", "war", "fed", "sec", "usd", "pmi",
})

VERY_HIGH_TRIGGERS = [
    "fomc", "federal reserve announces", "emergency meeting", "interest rate decision",
    "cpi", "consumer price index", "pce", "nonfarm payroll", "jobs report",
    "recession", "default", "war", "invasion", "rate cut", "rate hike",
    "bitcoin etf approval", "sec approves", "intervention", "crisis",
    # Explicit policy-path repricing is a genuine intraday market mover.
    "higher for longer", "fewer rate cuts", "more rate cuts", "cut rates",
    "raise rates", "hawkish", "dovish",
    "missile strike", "missile strikes", "airstrike", "airstrikes", "declares war",
    "us payrolls", "u.s. payrolls", "payrolls report", "payrolls data",
    "non-farm payrolls", "employment report", "jobs data",
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
              "unconfirmed", "allegedly", "leak", "not yet confirmed", "may"]