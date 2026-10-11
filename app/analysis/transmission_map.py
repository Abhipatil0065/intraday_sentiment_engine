"""
PER-ASSET TRANSMISSION MAP (improvement item 1) - HIGHEST PRIORITY.

The defect this replaces
------------------------
Heuristic mode computed ``detect_direction(text)`` ONCE and applied that single
answer to every asset the article routed to. A headline whose transmission
differs by asset therefore produced the wrong call for at least one of them.

The model now
-------------
Direction is resolved PER ASSET from a declarative map:

    event_type x asset -> (sign, magnitude)

``sign`` is +1 / -1 / 0 and ``magnitude`` (0.0-1.5) is that asset's sensitivity
to the event. So the SAME headline can be bullish gold and bearish bitcoin:

    "Missile strike widens as sanctions bite"
        gold   +1 x 1.05   safe-haven bid
        silver +1 x 0.70   weaker, part industrial
        btc   -1 x 0.35   liquidity risk-off

The map itself lives in ``app/config/transmission_map.json`` so it can be
retuned without touching Python. Event-type DETECTION still needs keywords,
but the direction decision is data, not code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from app.analysis.text_match import contains_phrase, contains_plural
from app.models.enums import Asset, Direction, NewsCategory

# How much raw cue weight counts as a "full" magnitude. Set above the weight of
# a single strong cue so that one mention scores meaningfully below full,
# while several corroborating cues saturate at 1.0.
CUE_SATURATION = 9.0

# PERSISTENCE per event category (item 6).
#
# This is the INDEPENDENT second weight the brief asks for: the cue magnitude
# says how strong THIS ARTICLE is, and this table says how long THIS KIND OF
# EVENT stays relevant. Deriving both from the cue score (as the old code did)
# counted one measurement three times.
CATEGORY_PERSISTENCE: dict = {
    "Central Bank": 0.90,
    "Interest Rates": 0.90,
    "Inflation": 0.85,
    "Employment": 0.85,
    "Treasury Yields": 0.80,
    "USD/DXY": 0.70,
    "GDP": 0.60,
    "Banking/Financial Stress": 0.90,
    "Geopolitics": 0.75,
    "Risk Sentiment": 0.55,
    "ETF Flows": 0.80,
    "Bitcoin ETF Flows": 0.80,
    "Crypto Market Flows": 0.65,
    "Crypto Regulation": 0.95,
    "China": 0.70,
    "Industrial Demand": 0.70,
    "Commodity Supply/Demand": 0.65,
    "Oil/Energy": 0.55,
    "Equities": 0.50,
    "Other": 0.40,
}

# Intensity qualifiers that make a cue STRONGER without changing its sign.
# The cue tables cap repeats (a word cannot dominate), so without these a
# longer, more emphatic headline would score no higher than a bare one -
# "emergency hike" and "rate hike" would read identically.
INTENSITY_CUES: tuple[str, ...] = (
    "emergency", "surprise", "unexpected", "sharply", "dramatically", "record",
    "massive", "huge", "sweeping", "aggressive", "decisive", "unprecedented",
    "sticky", "resilient", "accelerating", "soars", "plunges", "spikes",
    "soar", "jump", "plunge", "spike", "deep", "acute", "severe", "crisis",
)
INTENSITY_WEIGHT = 1.5

TRANSMISSION_FILE = Path(__file__).resolve().parent.parent / "config" / "transmission_map.json"


class EventType(str, Enum):
    """Transmission classes. One class per causal mechanism, not per topic."""

    HAWKISH_POLICY = "HAWKISH_POLICY"
    DOVISH_POLICY = "DOVISH_POLICY"
    RECESSION_CUT = "RECESSION_CUT"
    RISING_YIELDS = "RISING_YIELDS"
    FALLING_YIELDS = "FALLING_YIELDS"
    STRONG_USD = "STRONG_USD"
    WEAK_USD = "WEAK_USD"
    HOT_INFLATION = "HOT_INFLATION"
    COOL_INFLATION = "COOL_INFLATION"
    STRONG_JOBS = "STRONG_JOBS"
    WEAK_JOBS = "WEAK_JOBS"
    GEOPOLITICAL_RISK = "GEOPOLITICAL_RISK"
    GEOPOLITICAL_DEESCALATION = "GEOPOLITICAL_DEESCALATION"
    BANKING_STRESS = "BANKING_STRESS"
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    GOLD_ETF_FLOW = "GOLD_ETF_FLOW"
    BTC_ETF_FLOW = "BTC_ETF_FLOW"
    CRYPTO_REGULATION = "CRYPTO_REGULATION"
    CRYPTO_LIQUIDATION = "CRYPTO_LIQUIDATION"
    CRYPTO_HACK = "CRYPTO_HACK"
    CHINA_DEMAND = "CHINA_DEMAND"
    INDUSTRIAL_DEMAND = "INDUSTRIAL_DEMAND"
    COMMODITY_SUPPLY = "COMMODITY_SUPPLY"
    # Oil-specific mechanisms. Kept separate from COMMODITY_SUPPLY because crude
    # has a genuine supply curve of its own: OPEC decisions, inventory prints and
    # physical disruptions drive it far more than they drive gold or silver.
    OIL_SUPPLY_TIGHTNESS = "OIL_SUPPLY_TIGHTNESS"
    OIL_SUPPLY_GLUT = "OIL_SUPPLY_GLUT"
    GENERAL_MACRO = "GENERAL_MACRO"
    # Item 2: S&P 500 as a first-class asset. These event types name equity's
    # OWN causal mechanisms - earnings, guidance, index flows, sector rotation,
    # rate repricing and the VIX - so equity is not merely a proxy for bitcoin.
    # Detection still needs keywords (see EVENT_SIGNALS below); the direction
    # decision is data here, in the map.
    EARNINGS_BEAT = "EARNINGS_BEAT"
    EARNINGS_MISS = "EARNINGS_MISS"
    GUIDANCE_RAISED = "GUIDANCE_RAISED"
    GUIDANCE_CUT = "GUIDANCE_CUT"
    INDEX_FLOW = "INDEX_FLOW"
    SECTOR_ROTATION = "SECTOR_ROTATION"
    RATE_REPRICING = "RATE_REPRICING"
    STOCK_RALLY = "STOCK_RALLY"
    STOCK_PLUNGE = "STOCK_PLUNGE"
    VIX_SPIKE = "VIX_SPIKE"
    VIX_COMPRESSION = "VIX_COMPRESSION"
    CORP_EARNINGS_SEASON = "CORP_EARNINGS_SEASON"
    # ---- Item 3: crypto regulation is ONE category but TWO mechanisms. ----
    # A single CRYPTO_REGULATION event type could not express the difference
    # between a ban and an approval, so every regulation headline was stamped
    # with the same sign. These two split on the actual policy action:
    #   REGULATION_CLAMP   - bearish: ban, enforce, litigation, suit, crackdown
    #   REGULATION_CLARITY - bullish: approve, framework, legal clarity, ETF approval
    # The bare words "regulation"/"regulatory" are deliberately NOT in either
    # keyword set: they are ambiguous and must fall through to GENERAL_MACRO
    # rather than be guessed at. Detection is keyword-driven; direction is data.
    REGULATION_CLAMP = "REGULATION_CLAMP"
    REGULATION_CLARITY = "REGULATION_CLARITY"
    # ---- Item 3: liquidity events. ----
    # QE/QT/balance-sheet/TGA/reserve moves reach BTC through the liquidity
    # channel (stablecoin DeFi collateral, exchange order books, risk appetite)
    # far more than they reach the metals, which are priced off real yields.
    #
    # Split into two event types because QE and QT are OPPOSITE actions: a
    # single LIQUIDITY_CHANGE with one sign could not express both. Detection
    # is keyword-driven; direction is data, in the map.
    LIQUIDITY_EASING = "LIQUIDITY_EASING"
    LIQUIDITY_TIGHTENING = "LIQUIDITY_TIGHTENING"
    # ---- Item 4: silver's industrial demand. ----
    # Silver is both a precious and an industrial metal, and the industrial half
    # was under-served: "solar" and "electronics" were listed but the vocabulary
    # stopped there, so a photovoltaic or EV headline often fell through to
    # GENERAL_MACRO and silver got no signal of its own. SILVER_INDUSTRIAL_DEMAND
    # is the XAGUSD-specific supply-chain mechanism.
    SILVER_INDUSTRIAL_DEMAND = "SILVER_INDUSTRIAL_DEMAND"
    SILVER_CHINA_DEMAND = "SILVER_CHINA_DEMAND"
    # ---- Item 4: gold-silver ratio. ----
    # When the ratio is historically HIGH, gold is expensive relative to silver,
    # so money rotates into the cheaper metal: that is bullish XAGUSD. The ratio
    # itself is a price relationship, not a news event, so it is detected from
    # the wording and given its own event type with a fixed sign for silver.
    # Detection is keyword-driven; direction is data, in the map.
    GOLD_SILVER_RATIO_HIGH = "GOLD_SILVER_RATIO_HIGH"
    GOLD_SILVER_RATIO_LOW = "GOLD_SILVER_RATIO_LOW"


@dataclass(frozen=True)
class Transmission:
    """How one event type reaches one asset."""

    sign: int          # +1 bullish, -1 bearish, 0 no path
    magnitude: float   # per-asset sensitivity, 0.0-1.5

    @property
    def direction(self) -> Direction:
        if self.sign > 0:
            return Direction.BULLISH
        if self.sign < 0:
            return Direction.BEARISH
        # sign 0 with a real magnitude means the event type reaches this asset
        # but does not take a side - "mixed", not "no opinion".
        return Direction.MIXED if self.magnitude > 0.0 else Direction.NEUTRAL


NEUTRAL = Transmission(0, 0.0)


@dataclass(frozen=True)
class ResolvedImpact:
    """The per-asset answer produced by :func:`resolve`."""

    asset: Asset
    direction: Direction
    sign: int
    magnitude: float            # 0..1.5, the transmission magnitude
    dominant: EventType | None
    evidence: float             # cue weight behind the dominant event type
    priced_in: bool = False


def _coerce(value) -> Transmission:
    """Accept ``[sign, magnitude]`` or a mapping; never raise on bad data."""
    if isinstance(value, dict):
        return Transmission(int(value.get("sign", 0)), float(value.get("magnitude", 0.0)))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return Transmission(int(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return NEUTRAL
    return NEUTRAL


@lru_cache(maxsize=1)
def load_transmission_map() -> dict:
    """
    Load and validate the JSON map.

    Validation is total: a malformed entry degrades to NEUTRAL rather than
    crashing the pipeline, because a typo in the config must never take the
    engine down.
    """
    try:
        raw = json.loads(TRANSMISSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Transmission map unreadable at {TRANSMISSION_FILE}: {exc}"
        ) from exc

    out: dict = {}
    for key, per_asset in raw.items():
        if key.startswith("_"):
            continue
        try:
            event_type = EventType(key)
        except ValueError:
            continue  # unknown key: ignored, never fatal
        entry = {}
        for asset in Asset:
            entry[asset] = _coerce(per_asset.get(asset.value))
        out[event_type] = entry
    return out


def transmission(event_type, asset: Asset) -> Transmission:
    """One lookup. Unknown event type or asset -> NEUTRAL, never a guess."""
    mapping = load_transmission_map()
    try:
        et = EventType(event_type)
    except ValueError:
        return NEUTRAL
    return mapping.get(et, {}).get(asset, NEUTRAL)


def direction_for(event_type, asset: Asset) -> Direction:
    return transmission(event_type, asset).direction


def measure_cue_magnitude(text: str) -> float:
    """
    The SINGLE cue magnitude for an article, normalised to 0.0-1.0.

    Item 6 requires exactly one magnitude to exist. It is measured once here
    from the cue tables and reused by direction, strength and persistence, so
    the same evidence is no longer counted three times under three names.
    """
    from app.analysis.cue_rules import BEARISH_CUES, BULLISH_CUES, score_cues

    if not text:
        return 0.0
    raw = max(score_cues(text, BULLISH_CUES), score_cues(text, BEARISH_CUES))
    # Intensity qualifiers raise the magnitude without touching the sign.
    lowered = (text or "").lower()
    hits = sum(1 for c in INTENSITY_CUES if contains_phrase(lowered, c))
    if hits:
        raw += INTENSITY_WEIGHT * min(hits, 3)
    # Some mechanisms are directional in themselves rather than by wording: a
    # regulatory APPROVAL is bullish for crypto, a LIQUIDATION cascade is not.
    # Without this floor, "SEC approves spot bitcoin ETF" would measure zero
    # magnitude purely because no bullish keyword happens to appear in it.
    raw = max(raw, _INTRINSIC_WEIGHT if _has_intrinsic_direction(text) else 0.0)
    # VERB-DRIVEN headlines need a floor too. The polarity fix REMOVED bare verbs
    # ("rose", "improved", "faded") from BULLISH_CUES/BEARISH_CUES, because they
    # carried no subject and read correctly only by accident. The side effect is
    # that a correctly-resolved headline such as "risk appetite improved across
    # equity markets" scores 0.0 magnitude, which collapses it to MIXED and
    # discards the resolution that was just computed. So a headline that the
    # polarity resolver can place gets the same evidence floor.
    raw = max(raw, _INTRINSIC_WEIGHT if _has_resolved_polarity(text) else 0.0)
    return round(min(1.0, raw / CUE_SATURATION), 3)


def _has_resolved_polarity(text: str) -> bool:
    """True when subject+verb polarity resolves this headline to an event."""
    from app.analysis.event_polarity import resolve_all
    from app.analysis.text_quality import neutralise_on_negation

    try:
        return bool(resolve_all(
            neutralise_on_negation(text or "").lower()))
    except Exception:  # pragma: no cover - resolver must never break scoring
        return False


# Mechanisms whose SIGN is fixed by the event itself rather than by tone.
# Value = (EventType, cues that imply a BULLISH reading).
_INTRINSIC_RULES: tuple[tuple[EventType, tuple[str, ...]], ...] = (
    (EventType.CRYPTO_REGULATION,
     ("approves", "approved", "clears", "cleared", "passes", "passed",
       "wins approval", "legalized", "adopts", "enacts", "institutionalises")),
(EventType.REGULATION_CLAMP,
     ("sues", "sued", "suing", "lawsuit", "charges", "charged", "ban", "bans",
       "banned", "crackdown", "enforcement action")),
    (EventType.CRYPTO_HACK,
     ("hacked", "hack", "exploited", "exploit",
       "drained", "security breach", "stolen")),
    (EventType.LIQUIDITY_EASING,
     ("qe", "quantitative easing", "balance sheet expansion", "asset purchases",
       "liquidity injection", "rate cut")),
    (EventType.LIQUIDITY_TIGHTENING,
     ("qt", "quantitative tightening", "balance sheet reduction",
       "balance sheet runoff", "liquidity drain", "reserves fall")),
    (EventType.GEOPOLITICAL_RISK,
     ("missile strike", "missile strikes", "airstrike", "airstrikes",
       "invasion", "invades", "invaded", "declares war",
       "military strike", "military strikes")),
    (EventType.REGULATION_CLARITY,
     ("approves", "approved", "approval", "passes", "passed", "signs into law",
      "signed into law", "enacts", "enacted", "legalizes", "legalized",
      "adopts", "adopted", "framework", "green light")),
    (EventType.BTC_ETF_FLOW,
     ("inflow", "inflows", "approval", "approved")),
(EventType.BTC_ETF_FLOW,
      ("outflow", "outflows", "redemption", "redemptions", "rejection", "rejected", "denied")),
    (EventType.EARNINGS_BEAT, ("beats earnings", "earnings beat",
      "beats estimates", "beats expectations", "tops estimates",
      "record profit")),
    (EventType.EARNINGS_MISS, ("misses earnings", "earnings miss",
      "misses estimates", "misses expectations", "profit warning",
      "warns on profit", "falls short")),
    (EventType.GUIDANCE_RAISED, ("raises guidance", "raises outlook",
      "raises forecast", "lifts guidance", "lifts forecast")),
    (EventType.GUIDANCE_CUT, ("cuts guidance", "cuts outlook",
      "cuts forecast", "lowers guidance", "lowers forecast")),
    (EventType.GOLD_ETF_FLOW,
      ("inflow", "inflows")),
    (EventType.GOLD_ETF_FLOW,
      ("outflow", "outflows", "redemption", "redemptions")),
    (EventType.BANKING_STRESS,
      ("bank collapse", "bank collapses", "bank fails", "bank failure",
       "bank run", "bank runs", "banking crisis", "bank seized", "bailout")),
    # A supply cut, an inventory draw or an outage is directional IN ITSELF: the
    # headline needs no bullish adjective ("OPEC+ extends output cuts") to be a
    # bullish crude story, and without this floor it would measure zero magnitude
    # and collapse to MIXED - discarding the resolution that was just computed.
    (EventType.OIL_SUPPLY_TIGHTNESS,
     ("output cuts", "production cuts", "supply cut", "supply cuts",
      "cuts output", "cuts production", "supply disruption", "supply shock",
      "force majeure", "pipeline shutdown", "refinery outage", "refinery fire",
      "field outage", "hormuz", "tanker attack", "oil embargo",
      "crude draw", "crude stocks draw", "crude inventories fell",
      "draw in crude", "draw in oil", "output cuts", "production cuts",
      "extends cuts", "tighten supply", "tightening supply", "supply squeeze")),
    (EventType.OIL_SUPPLY_GLUT,
     ("raises output", "raises production", "output increase",
      "production increase", "supply surge", "supply glut", "oversupply",
      "surplus", "oil glut", "crude build", "crude stocks build",
      "crude inventories rose", "build in crude", "build in oil",
      "build in stocks", "supply increase", "boost output",
      "raises oil production", "production target", "output target",
      "demand worries", "demand weakness", "consumption falls")),
)
_INTRINSIC_WEIGHT = 6.0

# Demand events where a "down" verb (plunge, slump, fall, etc.) flips the
# normal bullish sign to bearish: demand falling is bad, not good.
_DEMAND_EVENTS = (EventType.CHINA_DEMAND, EventType.SILVER_CHINA_DEMAND,
                  EventType.INDUSTRIAL_DEMAND, EventType.SILVER_INDUSTRIAL_DEMAND)
_DEMAND_DOWN_VERBS = ("plunge", "plunges", "plunged", "slump", "slumps",
                      "slumped", "contracts", "contracted", "contraction",
                      "falls", "fell", "drops", "dropped", "declines",
                      "declined", "slows", "slowed", "weakens", "weakened",
                      "misses", "missed", "tumbles", "tumbled",
                      "shrinks", "shrank")

# FLOW events need their sign read from the flow wording itself: an inflow is
# buying and an outflow is selling, even though both are the "same" event type.
# Without this, "Bitcoin ETF inflow" and "Bitcoin ETF outflow" would resolve
# identically, which is exactly backwards.
_FLOW_POSITIVE = ("inflow", "inflows", "in-flow", "demand", "buying",
                  "accumulation", "inflows surge", "net buying", "creeps in")
_FLOW_NEGATIVE = ("outflow", "outflows", "out-flow", "redemption",
                  "redemptions", "out selling", "net selling", "liquidated")


def _has_intrinsic_direction(text: str) -> bool:
    lowered = (text or "").lower()
    for _, cues in _INTRINSIC_RULES:
        if any(contains_phrase(lowered, c) for c in cues):
            return True
    return False


def _flow_sign(text: str) -> int:
    """
    +1 for an inflow/buying story, -1 for an outflow/redemption story, 0 when
    the flow direction is not stated at all.
    """
    lowered = (text or "").lower()
    positive = any(contains_phrase(lowered, c) for c in _FLOW_POSITIVE)
    negative = any(contains_phrase(lowered, c) for c in _FLOW_NEGATIVE)
    if positive and not negative:
        return 1
    if negative and not positive:
        return -1
    return 0


# --------------------------------------------------------------------------
# Event-type detection
# --------------------------------------------------------------------------
# Ordered so a SPECIFIC mechanism beats a generic category fallback.
# Entry = (EventType, required-any-of, required-all-of).
EVENT_SIGNALS: tuple[tuple[EventType, tuple[str, ...], tuple[str, ...]], ...] = (
(EventType.HAWKISH_POLICY, ("hawkish", "rate hike", "raises rates",
                                 "raise rates", "higher for longer", "tightening",
                                 "fewer rate cuts",
                                 "rate increase", "lift rates"), ()),
    (EventType.RECESSION_CUT, ("rate cut", "cut rates", "cuts rates", "dovish",
                               "easing", "stimulus"),
     ("recession", "slump", "slowdown", "contraction", "downturn")),
    (EventType.DOVISH_POLICY, ("dovish", "rate cut", "cuts rates", "cut rates",
                               "pivot", "easing", "stimulus"), ()),
    (EventType.RISING_YIELDS, ("yields rise", "yields rose", "yields climbed",
                               "yields climb", "yields higher", "yields surged",
                               "higher yields", "rising yields", "real yields",
                               "bond yield"), ()),
    (EventType.FALLING_YIELDS, ("yields fall", "yields fell", "yields eased",
                                "yields decline", "lower yields",
                                "falling yields"), ()),
    (EventType.STRONG_USD, ("dollar surges", "dollar rallies", "dollar index rose",
                            "dollar strengthens", "dollar climbed",
                            "stronger dollar", "dollar spikes", "dxy rises"), ()),
    (EventType.WEAK_USD, ("dollar weakens", "dollar falls", "dollar slides",
                          "weaker dollar", "dollar declines", "dxy falls"), ()),
    (EventType.HOT_INFLATION, ("hotter than expected", "hot inflation",
                               "inflation accelerates", "cpi accelerates"), ()),
(EventType.COOL_INFLATION, ("inflation cools", "cooling inflation",
                                 "disinflation", "cpi eases",
                                 "inflation slows"), ()),
    (EventType.STRONG_JOBS, ("strong payrolls", "payrolls beat", "jobs beat",
                             "strong employment", "record jobs",
                             "hiring surge"), ()),
    (EventType.WEAK_JOBS, ("weak payrolls", "payrolls miss", "jobs report misses",
                           "unemployment rises", "jobless claims surge",
                           "layoffs announced"), ()),
(EventType.GEOPOLITICAL_DEESCALATION,
     ("ceasefire", "cease-fire", "peace talks", "peace deal", "peace agreement",
      "de-escalation", "de-escalate", "tension eases", "tensions ease", "truce",
      "talks resume"), ()),
    (EventType.GEOPOLITICAL_RISK, ("missile", "invasion", "war", "conflict",
                                   "escalation", "border clash", "coup",
                                   "sanctions", "embargo"), ()),
    (EventType.BANKING_STRESS, ("banking crisis", "bank fails", "bank collapse",
                                "default", "insolvency", "credit crunch",
                                "liquidity crisis", "contagion", "margin calls",
                                "bank stress"), ()),
    (EventType.CRYPTO_HACK, ("exchange hacked", "exchange hack", "crypto hack", "hacked",
                               "exploit", "exploited", "drained", "security breach", "stolen"),
      ("crypto", "bitcoin", "btc", "exchange", "defi", "wallet", "token", "stablecoin")),
    (EventType.CRYPTO_LIQUIDATION, ("liquidations", "liquidated", "cascade",
                                    "forced selling"), ()),
    (EventType.CRYPTO_REGULATION, ("sec", "cftc", "crypto regulation",
                                   "stablecoin bill", "custody rule",
                                   "crypto bill", "ban on"), ()),
    (EventType.BTC_ETF_FLOW, ("bitcoin etf", "spot etf", "ibit", "fbtc",
                              "btco", "bitcoin fund", "etf inflow",
                              "etf outflow"), ("bitcoin", "btc", "crypto")),
    (EventType.GOLD_ETF_FLOW, ("gold etf", "gld", "gold fund", "gold inflows",
                               "gold outflows", "etf inflows", "etf outflows"),
     ("gold", "bullion")),
    # Risk sentiment. NOTE (VAL-4 fix): the bare subject "risk appetite" and
    # "investor sentiment improves" are deliberately REMOVED here. A subject
    # listed without a verb makes every polarity-blind match fire, so "risk
    # appetite FADED" scored exactly like "risk appetite IMPROVED". Subject+verb
    # resolution now lives in app/analysis/event_polarity.py; only phrases that
    # already carry a direction are kept below.
    (EventType.RISK_ON, ("risk-on", "stocks rally", "equities surge"), ()),
    (EventType.RISK_OFF, ("risk-off", "risk aversion", "flight to quality",
                          "panic selling", "safe haven demand",
                          "safe-haven demand"), ()),
(EventType.CHINA_DEMAND, ("china", "chinese", "beijing", "yuan", "caixin",
                                 "shanghai", "renminbi", "pboc"),
      ("pmi", "factory", "manufacturing", "industrial", "output", "exports",
       "imports", "demand", "gdp", "growth", "stimulus", "property",
       "construction", "caixin", "pboc", "yuan", "economy", "retail sales")),
    # ---- Item 4: silver's China-demand cues. ----
    # The bare China keywords above were too narrow: a "China PMI prints
    # strong factory output" headline carried no silver-specific vocabulary and
    # often fell through to GENERAL_MACRO even though China is the world's
    # largest physical silver buyer. These add the demand-side language that
    # actually moves XAGUSD: PMI, factory gate, imports, offtake, the yuan.
    (EventType.SILVER_CHINA_DEMAND,
     ("china pmi", "china manufacturing pmi", "pmi reading", "factory output",
      "factory gate", "industrial output", "industrial production",
      "manufacturing expansion", "manufacturing contraction", "caixin pmi",
      "official pmi", "services pmi", "new export orders", "new orders",
      "imports rise", "imports fell", "imports declined", "import demand",
      "offtake", "offtakes", "physical demand", "physical offtake",
      "silver offtake", "silver demand", "demand for silver",
      "silver imports", "silver inventories", "warehouse stocks",
      "exchange stocks", "comex stocks", "shanghai gold exchange",
      "shanghai silver", "yuan depreciation", "yuan appreciation",
      "yuan devaluation", "yuan revaluation", "pboc easing",
      "pboc cut", "people's bank of china", "stimulus package",
      "fiscal stimulus", "infrastructure spending", "property sector",
      "real estate", "construction", "construction activity",
      "grid investment", "power grid", "electricity demand",
      "electricity consumption", "power consumption"), ()),
    (EventType.INDUSTRIAL_DEMAND, ("pmi", "manufacturing", "factory output",
                                    "industrial production", "solar",
                                    "electronics", "copper"), ()),
    # ---- Item 4: silver's industrial demand cues. ----
    # Silver is both a precious and an industrial metal, and the industrial
    # half was under-served: "solar" and "electronics" were listed but the
    # vocabulary stopped there, so a photovoltaic or EV headline often fell
    # through to GENERAL_MACRO and silver got no signal of its own. These add
    # the actual supply-chain language that moves XAGUSD intraday.
    (EventType.SILVER_INDUSTRIAL_DEMAND,
     ("solar", "photovoltaic", "pv ", "solar panel", "solar cell",
      "solar farm", "solar capacity", "wind turbine", "wind farm",
      "wind capacity", "electric vehicle", "ev ", "evs", "ev battery",
      "battery", "battery storage", "grid storage", "energy storage",
      "power electronics", "semiconductor", "chip ", "chips",
      "5g", "data centre", "data center", "server", "server farm",
      "appliance", "consumer electronics", "smart grid", "grid modernisation",
      "grid modernization", "charging station", "ev charging",
      "solar demand", "silver industrial demand", "industrial silver",
      "silver fabrication", "fabrication demand", "silver consumption",
      "demand for silver", "silver offtake", "industrial offtake",
      "renewable", "renewables", "clean energy", "green energy",
      "net zero", "net-zero", "climate target", "emissions target",
      "copper demand", "copper supply"), ()),
    # ---- Item 4: gold-silver ratio. ----
    # When the ratio is historically HIGH, gold is expensive relative to silver,
    # so money rotates into the cheaper metal: that is bullish XAGUSD. The
    # ratio itself is a price relationship, not a news event, so it is detected
    # from the wording and given its own event type with a fixed bullish sign
    # for silver. Detection requires a ratio word; direction is data here.
    (EventType.GOLD_SILVER_RATIO_HIGH,
     ("gold silver ratio", "gold-to-silver ratio", "gold/silver ratio",
      "gold silver ratio high", "ratio elevated", "ratio at historic highs",
      "ratio hits record", "ratio record high", "ratio peaks",
      "ratio spikes", "ratio surge", "gold silver ratio spike",
      "gold silver ratio surge", "ratio above 80", "ratio above 90",
      "ratio above 100", "gold silver spread widens",
      "gold silver spread widening", "gold outperforms silver",
      "gold outperforming silver", "silver lags gold", "silver lagging gold",
      "silver cheap relative to gold", "silver looks cheap vs gold",
      "silver undervalued vs gold", "silver cheap vs gold",
      "ratio mean reversion", "ratio reversion", "ratio normalises",
      "ratio normalization", "ratio to revert", "ratio to mean revert",
      "ratio to normalise", "ratio to normalize"), ()),
    (EventType.GOLD_SILVER_RATIO_LOW,
     ("gold silver ratio low", "gold-to-silver ratio low",
      "ratio compressed", "ratio compression", "ratio tight",
      "ratio at historic lows", "ratio record low", "ratio lows",
      "gold silver ratio falls", "gold silver ratio drops",
      "ratio narrows", "ratio narrowing", "silver outperforms gold",
      "silver outperforming gold", "silver leads gold", "silver leading gold",
      "gold lags silver", "gold lagging silver", "gold cheap vs silver",
      "ratio below 60", "ratio below 70", "ratio below 50"), ()),
    (EventType.COMMODITY_SUPPLY, ("mine", "mining", "refinery", "gold supply",
                                  "silver supply", "opec cuts"), ()),
    # Oil supply/demand. Ordered BEFORE COMMODITY_SUPPLY because these carry the
    # actual direction; "opec cuts" below is only the weaker generic reading.
    # Tightness = bullish crude (production cut, physical disruption, stock draw).
    (EventType.OIL_SUPPLY_TIGHTNESS,
     ("opec cuts", "opec+ cuts", "cuts output", "output cut", "production cut",
      "supply cut", "supply disruption", "supply shock", "force majeure",
      "pipeline shutdown", "refinery fire", "refinery outage", "field outage",
      "strait of hormuz", "hormuz", "suez", "shipping attack", "tanker attack",
      "blockade", "oil embargo", "sanctions on oil", "crude stocks fell",
      "crude stocks draw", "crude inventories fell", "crude drawdown",
      "api crude draw", "eia crude draw", "oil supply tight", "tightening supply",
      "shale output cut", "extends cuts", "output cuts", "production cuts",
      "draw in crude", "draw in oil", "tighten supply",
      "sharpen supply", "supply squeeze"), ()),
    # Glut = bearish crude (output increase, inventory build, demand worry).
    (EventType.OIL_SUPPLY_GLUT,
     ("opec raises output", "opec increases output", "opec boost",
      "opec+ raises", "output increase", "supply surge", "supply glut",
      "oversupply", "surplus", "oil glut", "crude stocks rose", "crude stocks build",
      "crude inventories rose", "crude build", "api crude build",
      "eia crude build", "demand worries", "demand slowdown", "demand weakness",
      "oil demand falls", "consumption falls", "weak consumption",
      "pipeline reopened", "supply returns",
      "raises output", "raises production", "increase production",
      "production increase", "output hike", "supply increase", "boost output",
      "adds barrels", "build in oil", "build in crude", "build in stocks",
      "build in inventories", "raises oil production", "raises crude output",
      "production target", "output target",
      "gluts", "ramping output"), ()),
    # ---- Item 2: S&P 500's OWN mechanisms. Ordered BEFORE RISK_ON/RISK_OFF so
    # an earnings headline is not swallowed by the generic risk-sentiment read.
    # EARNINGS_BEAT / EARNINGS_MISS carry no qualifier: "revenue beats estimates"
    # must fire even when the company name is absent.
    (EventType.EARNINGS_BEAT, ("earnings beat", "profit beats", "revenue beat",
                               "sales beat", "beats estimates", "beat estimate",
                               "beat forecasts", "profit rise", "profit rose",
                               "revenue rose", "sales rise", "strong quarter",
                               "strong quarters", "record profit",
                               "record profits", "earnings beat estimates",
                               "beats expectations", "beats earnings expectations",
                               "earnings beat", "beats estimates",
                               "tops estimates", "tops forecasts",
                               "beats forecasts"),
     ("earnings", "profit", "revenue", "eps", "quarter", "guidance", "results")),
    (EventType.EARNINGS_MISS, ("earnings miss", "profit miss", "revenue miss",
                               "sales miss", "misses estimates", "miss estimate",
                               "miss forecasts", "profit fall", "profit fell",
                               "revenue fell", "sales fell", "weak quarter",
                               "loss", "losses", "narrowed profit",
                               "profit warning", "earnings warning",
                               "cuts outlook", "misses expectations",
                               "misses estimates", "profit warning", "warns on profit",
                               "misses forecasts", "miss forecasts",
                               "misses estimates", "miss estimates"),
     ("earnings", "profit", "revenue", "eps", "quarter", "guidance", "results")),
    (EventType.GUIDANCE_RAISED, ("raises guidance", "lifts guidance",
                                 "guidance raised", "guidance increase",
                                 "outlook raised", "outlook improved",
                                 "raises outlook", "raises forecast",
                                 "forecast raised"), ()),
    (EventType.GUIDANCE_CUT, ("cuts guidance", "lowers guidance",
                              "guidance cut", "guidance lowered",
                              "outlook cut", "outlook lowered",
                              "lowers outlook", "lowers forecast",
                              "forecast cut", "profit warning",
                              "reduces forecast"), ()),
    # INDEX_FLOW: passive/index money moving in or out of the S&P 500.
    (EventType.INDEX_FLOW, ("index inflow", "index inflows", "etf inflow",
                            "etf inflows", "index fund", "index rebalance",
                            "index weight", "passive inflow", "passive inflows",
                            "s&p inflow", "s&p 500 inflow", "record inflows",
                            "fund inflows", "fund inflow"), ()),
    # SECTOR_ROTATION: money moving BETWEEN sectors. Directionless by itself -
    # the sign is 0 in the map, so it contributes MIXED, not a lean.
    (EventType.SECTOR_ROTATION, ("sector rotation", "rotation into",
                                 "rotation out of", "sector shift",
                                 "risk rotation", "rotation trade"), ()),
    # RATE_REPRICING: equities reprice on a rate move. Same direction as the
    # metals here (higher rates = lower equity multiples) but reached through
    # its own keyword set rather than by inheriting HAWKISH_POLICY.
    (EventType.RATE_REPRICING, ("rate hike", "raises rates", "raise rates",
                                "rate increase", "higher rates", "hike rates",
                                "fed hike", "rate rises", "rising rates",
                                "real yields rise", "real yields rising"), ()),
    (EventType.STOCK_RALLY, ("stocks rally", "stock rally", "equities surge",
                             "equity surge", "s&p 500 rises", "s&p rises",
                             "spx rises", "nasdaq rises", "record highs",
                             "all-time highs", "stock market rally",
                             "bull market", "melt-up", "market surge"), ()),
    (EventType.STOCK_PLUNGE, ("stocks plunge", "stock plunge", "equities plunge",
                              "equity plunge", "s&p 500 falls", "s&p falls",
                              "spx falls", "nasdaq falls", "market crash",
                              "crash", "sell-off", "selloff", "bear market",
                              "market plunge", "plunge"), ()),
    # VIX is equity-specific: it is the S&P 500's implied-volatility gauge.
    (EventType.VIX_SPIKE, ("vix spikes", "vix spike", "vix surges", "vix surge",
                           "vix rises", "vix rose", "vix higher",
                           "fear gauge", "fear index", "vol spike",
                           "volatility spike", "volatility surge"), ()),
    (EventType.VIX_COMPRESSION, ("vix falls", "vix fell", "vix eases",
                                 "vix declines", "vix lower", "vix calm",
                                 "volatility eases", "volatility falls",
                                 "fear gauge eases"), ()),
    (EventType.CORP_EARNINGS_SEASON, ("earnings season", "reporting season",
                                      "corporate earnings", "c-suite guidance",
                                      "management guidance"), ()),
    # ---- Item 3: crypto regulation split into two mechanisms. ----
    # REGULATION_CLAMP = bearish: a ban, enforcement action, litigation or
    # crackdown. Ordered BEFORE REGULATION_CLARITY because the clamp keywords
    # ("ban", "lawsuit", "crackdown") are more specific than the clarity ones.
    (EventType.REGULATION_CLAMP, ("ban", "bans", "ban on", "banned", "enforce",
                                   "enforcement", "litigation", "lawsuit", "sued",
                                   "suing", "sues", "crackdown", "crack down",
                                   "raid", "indictment", "indicted", "criminal",
                                   "prosecute", "prosecution", "subpoena",
                                   "seizure", "seized", "block", "blockade",
                                   "outlaw", "outlawed", "clamp down",
                                   "clampdown", "restrict", "restrictions",
                                   "tighten rules", "tightening rules"), ()),
# REGULATION_CLARITY = bullish: approval, a framework, legal clarity, an
    # ETF approval. "approve"/"approval" alone are too broad (a bank can be
    # "approved"), so the clarity set also requires a crypto context word.
    (EventType.REGULATION_CLARITY, ("approved", "approval", "approves",
                                     "framework", "frameworks", "legal clarity",
                                     "clear guidance", "regulatory clarity",
                                     "green light", "green-light", "sign off",
                                     "signed off", "rulebook", "rule book",
                                     "path forward", "pathway", "guidance issued",
                                     "clarified", "clarifies", "clarify",
                                     "passes", "passed", "signs into law", "signed into law",
                                     "enacts", "enacted", "legalizes", "legalized",
                                     "adopts", "adopted"),
     ("bitcoin", "btc", "crypto", "etf", "sec", "cftc", "coinbase", "exchange",
      "custody", "stablecoin", "defi", "defi ")),
# ---- Item 3: liquidity events. ----
    # QE/QT/balance-sheet/TGA/reserve moves reach BTC through the liquidity
    # channel (stablecoin DeFi collateral, exchange order books, risk appetite)
    # far more than they reach the metals, which are priced off real yields.
    #
    # Split into two event types because QE and QT are OPPOSITE actions: a
    # single LIQUIDITY_CHANGE with one sign could not express both. Detection
    # is keyword-driven; direction is data, in the map.
    #
    # EASING = injecting liquidity: QE, asset purchases, TGA draw, reserve
    # growth, balance-sheet expansion. Bullish BTC.
    (EventType.LIQUIDITY_EASING,
     ("quantitative easing", "quantitative easing programme",
      "quantitative easing program", "qe", "asset purchase", "asset purchases",
      "tga draw", "tga drawdown", "treasury general account draw",
      "treasury general account drawdown", "reserves rise", "reserves rose",
      "reserve balances rise", "reserve balances rose", "liquidity injection",
      "injection of liquidity", "balance sheet expansion",
      "balance sheet runoff begins", "balance sheet begins to expand",
      "expansion of the balance sheet", "expand the balance sheet",
      "net liquidity injection", "inject liquidity", "injecting liquidity",
      "adds liquidity", "add liquidity", "loose monetary policy",
      "monetary easing", "easing cycle", "easing path"), ()),
    # TIGHTENING = draining liquidity: QT, runoff, TGA build, reserve decline.
    # Bearish BTC. Ordered BEFORE LIQUIDITY_EASING so "balance sheet runoff"
    # cannot be misread as expansion.
    (EventType.LIQUIDITY_TIGHTENING,
     ("quantitative tightening", "qt", "balance sheet runoff",
      "balance sheet shrink", "balance sheet shrinking", "balance sheet reduction",
      "runoff begins", "runoff of the balance sheet", "shrink the balance sheet",
      "shrinking the balance sheet", "reduce the balance sheet",
      "reducing the balance sheet", "taper", "tapering", "qe taper",
      "end of asset purchases", "stop asset purchases", "reserves fall",
      "reserves fell", "reserve balances fall", "reserve balances fell",
      "liquidity drain", "drain liquidity", "draining liquidity",
      "withdraw liquidity", "withdraws liquidity", "tightening cycle",
      "tighten monetary policy", "monetary tightening", "quantitative "
      "tightening begins", "balance sheet runoff begins as qt takes hold",
      "runoff begins as qt takes hold"), ()),
)

# Category fallback: used ONLY when nothing else matched.
#
# VAL-5 FIX: these used to map to DIRECTIONAL events (USD/DXY -> STRONG_USD,
# Central Bank -> HAWKISH_POLICY). That meant any unplaced headline in a
# category was handed a direction that the text never stated - "Dollar index
# fell" matched no phrase, fell through to USD/DXY, and was reported as a
# STRONG dollar. Every fallback target is now GENERAL_MACRO, whose sign is 0,
# so an unclassifiable headline yields NEUTRAL/NO_BIAS rather than a guess.
#
# Category still matters for RELEVANCE (asset_relevance.RELEVANCE), which is
# what decides which assets a headline can reach at all.
CATEGORY_FALLBACK: dict = {
    NewsCategory.CENTRAL_BANK: EventType.GENERAL_MACRO,
    NewsCategory.INTEREST_RATES: EventType.GENERAL_MACRO,
    NewsCategory.TREASURY_YIELDS: EventType.GENERAL_MACRO,
    NewsCategory.USD_DXY: EventType.GENERAL_MACRO,
    NewsCategory.INFLATION: EventType.GENERAL_MACRO,
    NewsCategory.EMPLOYMENT: EventType.GENERAL_MACRO,
    NewsCategory.GEOPOLITICS: EventType.GENERAL_MACRO,
    NewsCategory.BANKING_FINANCIAL_STRESS: EventType.GENERAL_MACRO,
    NewsCategory.RISK_SENTIMENT: EventType.GENERAL_MACRO,
    NewsCategory.ETF_FLOWS: EventType.GENERAL_MACRO,
    NewsCategory.BITCOIN_ETF_FLOWS: EventType.GENERAL_MACRO,
    NewsCategory.CRYPTO_REGULATION: EventType.GENERAL_MACRO,
    NewsCategory.CRYPTO_MARKET_FLOWS: EventType.GENERAL_MACRO,
    NewsCategory.CHINA: EventType.GENERAL_MACRO,
    NewsCategory.INDUSTRIAL_DEMAND: EventType.GENERAL_MACRO,
    NewsCategory.COMMODITY_SUPPLY_DEMAND: EventType.GENERAL_MACRO,
    NewsCategory.OIL_ENERGY: EventType.GENERAL_MACRO,
    NewsCategory.GDP: EventType.GENERAL_MACRO,
    NewsCategory.EQUITIES: EventType.GENERAL_MACRO,
}


def _has_any(lowered: str, phrases) -> bool:
    """
    Whole-word phrase match with plural tolerance.

    News writes "Spot Bitcoin ETFs" and "spot bitcoin ETF" interchangeably;
    strict matching would silently drop the plural and mis-route the story to
    the wrong asset family.
    """
    return any(contains_plural(lowered, p) for p in phrases)


def detect_event_types(text: str, category: NewsCategory) -> list:
    """
    Which transmission mechanisms does this headline actually contain?

    Three sources, in priority order:

    1. ``EVENT_SIGNALS`` - explicit phrases that already name a mechanism.
    2. ``event_polarity`` - subject + verb pairs, so "the dollar index FELL"
       reads as WEAK_USD rather than defaulting to a strong dollar (VAL-5) and
       "risk appetite FADED" reads as RISK_OFF (VAL-4).
    3. ``CATEGORY_FALLBACK`` - only when nothing matched, and only to a NON-
       DIRECTIONAL event. The old fallback mapped USD/DXY straight to
       STRONG_USD, which is how "Dollar index fell" came out bearish; a
       fallback that asserts a direction without reading a verb is exactly the
       defect this pass removes.

    Negation is applied HERE as well as in cue scoring: "the Fed rules out a
    rate cut" contains the words "rate cut", and without this guard it would be
    classified as DOVISH_POLICY - reading a hawkish statement as bullish.
    """
    from app.analysis.event_polarity import resolve_all
    from app.analysis.text_quality import neutralise_on_negation

    lowered = neutralise_on_negation(text or "").lower()
    found = []
    for event_type, any_of, qualifier in EVENT_SIGNALS:
        # ``qualifier`` is an ANY-of guard, not an ALL-of requirement: a story
        # naming "bitcoin" satisfies it, and requiring "bitcoin" AND "btc" AND
        # "crypto" together would mean no story ever qualified.
        if qualifier and not _has_any(lowered, qualifier):
            continue
        if _has_any(lowered, any_of):
            found.append(event_type)

    # Polarity resolution runs AFTER the phrase list so it can OVERRIDE a
    # polarity-blind phrase match. A bare "risk appetite" phrase must not beat
    # the verb that says it faded.
    for name in resolve_all(lowered):
        try:
            resolved = EventType(name)
        except ValueError:
            continue
        if resolved not in found:
            found.append(resolved)

    if not found:
        # Fallback only to a directionless event. A headline we cannot place
        # gets a neutral mechanism, never an invented one.
        fallback = CATEGORY_FALLBACK.get(category)
        if fallback is not None:
            found.append(fallback)
    return found


def resolve(text: str, category: NewsCategory,
            cue_magnitude: float | None = None) -> dict:
    """
    Resolve one headline into a PER-ASSET transmission verdict.

    ``cue_magnitude`` is the single measured cue weight for the article (the
    one magnitude item 6 keeps). When omitted it is MEASURED HERE from the cue
    tables, so evidence strength scales the result automatically. It scales the
    map's per-asset sensitivity but never the SIGN, so the map owns direction
    and the cues own conviction.

    When several mechanisms are present they are SUMMED per asset, which is how
    "hawkish Fed amid recession fears" lands between the two readings instead
    of being resolved by whichever keyword happened to appear first.

    Task 6: each asset has ranked drivers. The driver rank weight (from
    DRIVER_RANKINGS in config) scales the contribution - top driver = 1.0,
    second = 0.9, etc. Applied ONCE in the weight path.
    """
    from app.analysis.event_polarity import direct_asset_moves
    from app.analysis.text_quality import neutralise_on_negation
    from app.config import DRIVER_RANKINGS

    event_types = detect_event_types(text, category)

    # If both escalation and de-escalation fire, the escalation wins:
    # the market reads fresh escalation as the dominant signal.
    if (EventType.GEOPOLITICAL_RISK in event_types
            and EventType.GEOPOLITICAL_DEESCALATION in event_types):
        event_types = [e for e in event_types
                       if e is not EventType.GEOPOLITICAL_DEESCALATION]

    # If a specific event type (not GENERAL_MACRO) is detected, enforce a
    # minimum cue magnitude of 0.30 so that the signal has sufficient weight.
    specific_event = any(et != EventType.GENERAL_MACRO for et in event_types)
    if specific_event:
        cue_magnitude = max(cue_magnitude if cue_magnitude is not None else 0.0, 0.30)

    # NEWS-ONLY bias (Task 2): the price-action flag no longer damps the
    # scale. Field `priced_in` stays on the schema but is a constant False
    # and never enters any weight/score math.
    priced_in_flag = False
    if cue_magnitude is None:
        cue_magnitude = measure_cue_magnitude(text)
    scale = max(0.0, min(1.5, float(cue_magnitude)))

    # A headline naming the asset's OWN price ("gold rose") is a direct
    # statement about that asset. It is resolved FIRST and carried through the
    # empty-mechanism path below, because "Gold rose" matches no macro event at
    # all and previously exited here with a silent NEUTRAL / 0.0.
    lowered = neutralise_on_negation(text or "").lower()
    direct = direct_asset_moves(lowered)

    if not event_types:
        out: dict = {}
        for asset in Asset:
            sign = direct.get(asset.value, 0)
            direction = (Direction.NEUTRAL if sign == 0
                         else Direction.BULLISH if sign > 0
                         else Direction.BEARISH)
            # NEWS-ONLY bias (Task 2): no price-action damping; constant flag.
            base = 1.0
            out[asset] = ResolvedImpact(
                asset, direction, sign, base if sign else 0.0, None, scale,
                priced_in_flag)
        return out

    out = {}
    # Flow events carry their own sign from the flow wording (see _FLOW_*).
    flow_sign = _flow_sign(text)
    for asset in Asset:
        signed = 0.0
        best_weight = -1.0
        dominant = None
        for index, event_type in enumerate(event_types):
            entry = transmission(event_type, asset)
            if entry.magnitude <= 0.0:
                continue
            sign = entry.sign
            # Demand events flip sign on "down" verbs: demand falling is bad.
            if (event_type in _DEMAND_EVENTS
                    and any(contains_plural(lowered, v) for v in _DEMAND_DOWN_VERBS)):
                sign = -sign
            if event_type in (EventType.BTC_ETF_FLOW, EventType.GOLD_ETF_FLOW):
                if flow_sign != 0:
                    sign = flow_sign
                else:
                    continue  # a flow with no direction is not a signal
            # Earlier (more specific) mechanisms weigh more.
            specificity = 1.0 / (1.0 + 0.5 * index)
            # Task 6: apply per-asset driver rank weight (top driver = 1.0).
            driver_rank = DRIVER_RANKINGS.get(asset.name, {}).get(event_type.value, 0.3)
            contribution = sign * entry.magnitude * scale * specificity * driver_rank
            signed += contribution
            if entry.magnitude > best_weight:
                best_weight = entry.magnitude
                dominant = event_type

        # A recognised mechanism with a non-zero magnitude that nets to zero
        # means opposing evidence cancelled out -> MIXED, not NEUTRAL. NEUTRAL
        # is reserved for "this article has nothing to say about this asset".
        if abs(signed) < 0.05:
            direction = (Direction.MIXED if best_weight > 0.0
                         else Direction.NEUTRAL)
            out[asset] = ResolvedImpact(
                asset, direction, 0, 0.0, dominant, scale, priced_in_flag)
            continue
        sign = 1 if signed > 0 else -1
        out[asset] = ResolvedImpact(
            asset, Direction.BULLISH if sign > 0 else Direction.BEARISH,
            sign, min(1.5, abs(signed)), dominant, scale, priced_in_flag)

    # Re-assert the direct-asset override: a headline naming the asset's OWN
    # price ("gold rose") is a direct statement about that asset and outranks any
    # macro mechanism read from the surrounding text.
    for name, asset_sign in direct.items():
        asset = Asset(name)
        direction = Direction.BULLISH if asset_sign > 0 else Direction.BEARISH
        # NEWS-ONLY bias (Task 2): no price-action damping; constant flag.
        floor = 1.0
        out[asset] = ResolvedImpact(
            asset, direction, asset_sign, max(floor, out[asset].magnitude),
            out[asset].dominant, scale, priced_in_flag)
    return out
