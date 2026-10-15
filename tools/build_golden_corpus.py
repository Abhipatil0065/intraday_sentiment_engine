"""
Build tests/golden/headlines.jsonl - the Step 0 baseline harness corpus.

This script is a BUILD TOOL, not part of the test suite. It
  1. pulls real headlines from the project's own data (logs/decisions.jsonl,
     data/reports/morning_brief_*.json) plus a curated manual sample,
  2. runs the REAL engine path (detect_event_types -> resolve) on each,
  3. freezes that output as `baseline_*`,
  4. emits one JSONL row carrying BOTH the frozen baseline and the human
     ground truth (`expected_*`).

`known_defect` is derived, never typed: it is True exactly when the frozen
baseline disagrees with the human label. That is what makes the suite a
"before" snapshot - known-defect rows assert the CURRENT behaviour so the
suite is green now and goes red the moment a fix lands.

Run:  python tools/build_golden_corpus.py
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.analysis.cue_rules import detect_category
from app.analysis.transmission_map import detect_event_types, resolve
from app.models.enums import Asset

OUT = BASE / "tests" / "golden" / "headlines.jsonl"

# ---------------------------------------------------------------------------
# Selection. Each entry is
#   (group, expected_asset, expected_direction, expected_event_types,
#    title, human note)
#
# `expected_*` is the HUMAN ground truth. It is written by reading the
# headline, not by looking at what the engine does. Where the two disagree,
# build() flags the row `known_defect` automatically.
#
# REAL rows are matched against the project's own stored headlines (reports
# and the decision log) so `source` and `published_at` are genuine.
# CURATED rows are hand-written edge cases the real feed does not cover.
# ---------------------------------------------------------------------------
REAL = [
    # ---- REAL - XAUUSD (gold)
    ('geopolitics_risk', 'XAUUSD', 'BULLISH',
     ["GEOPOLITICAL_RISK"],
     'UKMTO: Tanker hit by drone, local authorities help evacuate crew',
     'Real FJ wire headline. Direct conflict event -> safe-haven read for gold.'),
    ('geopolitics_deescalation_misfire', 'XAUUSD', 'BULLISH',
     ["GEOPOLITICAL_RISK"],
     'Dollar Index hits an 18-month high as the energy ceasefire fails',
     "Ceasefire FAILED = escalation. The 'ceasefire' keyword fires DEESCALATION."),
    ('geopolitics_deescalation_misfire', 'XAUUSD', 'NEUTRAL',
     [],
     'Anduril Unveils New Missiles to Counter Drones, Hypersonic Threats - Benzinga',
     "'Missile' matches a product unveiling. No conflict occurred."),
    ('policy_dovish_misfire', 'XAUUSD', 'NEUTRAL',
     [],
     'Bernstein SocGen cuts Boeing stock price target on delivery delays',
     "'cuts' fires DOVISH_POLICY. This is an analyst target cut, not policy."),
    ('policy_dovish_misfire', 'XAUUSD', 'NEUTRAL',
     [],
     'China to cut VAT export rebate rate for NEV to 9%',
     "'cut' inside a fiscal headline fires DOVISH_POLICY."),
    ('policy_dovish_misfire', 'XAUUSD', 'BEARISH',
     ["RISING_YIELDS"],
     'Barclays sees U.K. bond crisis risk easing as global yields rise',
     "'easing' fires DOVISH and cancels the RISING_YIELDS read."),
    ('usd', 'XAUUSD', 'BULLISH',
     ["WEAK_USD"],
     'Australian Dollar edges higher to near 0.7000 on weak US Sentiment',
     "'weak US Sentiment' is not a weak-dollar print; should not fire STRONG_USD."),
    ('metal_flows', 'XAUUSD', 'BULLISH',
     ["GOLD_ETF_FLOW"],
     'Gold drifts higher above $4,200 on surging ETF inflows, markets await US inflation data this week',
     'Direction is right, but THREE overlapping rules fire on one gold-ETF headline.'),
    ('metal_flows', 'XAUUSD', 'BEARISH',
     ["GOLD_ETF_FLOW"],
     'Bitcoin and Gold Outlook: BTC slides as ETF outflows weigh, XAU gives back recent gains',
     'XAU is giving back gains; the GOLD_ETF_FLOW sign override reads it bullish.'),
    ('oil', 'XAUUSD', 'BULLISH',
     ["GEOPOLITICAL_RISK", "OIL_SUPPLY_TIGHTNESS"],
     '2 More Oil Tankers Attacked Near Strait of Hormuz - Investing.com South Africa',
     'Tanker attacks are an escalation, not only a supply read.'),
    ('oil', 'XAUUSD', 'MIXED',
     ["OIL_SUPPLY_TIGHTNESS"],
     'ARAMCO CEO Amin Nasser: Until Hormuz fully reopens and confidence returns, the pressure across the energy chain will only intensify',
     'Supply commentary with no new event. MIXED is the honest read.'),
    ('china_demand', 'XAUUSD', 'BULLISH',
     ["CHINA_DEMAND"],
     "CNBC's The China Connection newsletter: How a gold-buying spree helps the yuan go global",
     'Central-bank and retail gold buying in China -> gold up.'),
    ('commodity_supply_misfire', 'XAUUSD', 'NEUTRAL',
     [],
     'BlackRock World Mining Trust Plc - Net Asset Value(s) - Company Announcement - Financial Times',
     "'Mining' in a trust name fires COMMODITY_SUPPLY for gold."),
    ('non_macro', 'XAUUSD', 'NEUTRAL',
     [],
     'Not buying Chipotle: Starbucks squashes blockbuster acquisition speculation',
     'Corporate M&A rumour with no macro transmission.'),

    # ---- REAL - XAGUSD (silver)
    ('yields', 'XAGUSD', 'BULLISH',
     ["FALLING_YIELDS"],
     'Eurozone yields ease as energy pullback temporarily soothes duration fears',
     'Yields easing -> metals up. Control case for the gold yields row.'),
    ('yields', 'XAGUSD', 'BULLISH',
     ["FALLING_YIELDS"],
     'British Pound gains positive traction as USD remains depressed on softer bond yields',
     "'softer bond yields' = FALLING. The 'bond yield' substring hits RISING_YIELDS."),
    ('china_demand', 'XAGUSD', 'MIXED',
     ["CHINA_DEMAND"],
     'China PBOC likely to set yuan midpoint at 6.6977 per dollar: estimate',
     'An FX fix estimate, not a demand signal.'),
    ('china_demand', 'XAGUSD', 'BULLISH',
     ["CHINA_DEMAND"],
     "CNBC's The China Connection newsletter: How a gold-buying spree helps the yuan go global",
     'Yuan-internationalisation read-through to the metals complex.'),
    ('industrial_demand', 'XAGUSD', 'BULLISH',
     ["COMMODITY_SUPPLY"],
     "Chile's El Teniente mine partially suspends operations after fatal landslide - Reuters",
     'Copper mine suspension; the silver read-through is a defensible model choice.'),
    ('industrial_demand', 'XAGUSD', 'BULLISH',
     ["SILVER_INDUSTRIAL_DEMAND"],
     'AIB Data Centers Hits Key Milestone, Inks 12-Year Deal To Supply 50 MW To AI Cloud Company Nebius - Benzinga',
     'Genuine grid and electrical infrastructure build-out. Correctly read as silver demand.'),
    ('industrial_demand_misfire', 'XAGUSD', 'NEUTRAL',
     [],
     'Broadcom CEO says biggest hurdle in AI-fuelled tech growth is capital, not memory chips',
     "'memory chips' fires SILVER_INDUSTRIAL_DEMAND."),
    ('industrial_demand', 'XAGUSD', 'MIXED',
     [],
     'COMMENTARY: US has built a strategic copper reserve. Now can it keep it? - Reuters',
     'Commentary column about a copper reserve. No new demand event.'),
    ('silver_china_misfire', 'XAGUSD', 'NEUTRAL',
     [],
     'BMO downgrades Alexandria Real Estate stock rating on earnings outlook',
     "An equity analyst downgrade. 'outlook' is in the SILVER_CHINA_DEMAND set."),
    ('silver_china_misfire', 'XAGUSD', 'NEUTRAL',
     [],
     'EfTEN Real Estate Fund AS NAV per share increased by 0.9% in September to 20.41 - Company Announcement',
     'A NAV print. Nothing about silver or China.'),
    ('silver_china_misfire', 'XAGUSD', 'BULLISH',
     ["SILVER_INDUSTRIAL_DEMAND"],
     'Demand for Tech Services in Europe Continues to Surge, Led by AI-Driven Infrastructure Spending - Financial Times',
     'Infrastructure demand read; wrong event family (China rather than industrial).'),
    ('geopolitics_risk', 'XAGUSD', 'MIXED',
     ["GENERAL_MACRO"],
     "Silver Price Forecast: XAG/USD remains below $61.00 following attacks on Riyadh's airport",
     "Armed attacks on an airport do not fire GEOPOLITICAL_RISK. 'attacks' is not a keyword."),
    ('geopolitics_deescalation_misfire', 'XAGUSD', 'BULLISH',
     ["GEOPOLITICAL_DEESCALATION"],
     'Silver Price Forecast: XAG/USD holds gains near $61 amid Russia-Ukraine ceasefire',
     'Direction agrees, but STRONG_USD also fires with no dollar content in the headline.'),
    ('silver_policy', 'XAGUSD', 'BEARISH',
     ["HAWKISH_POLICY"],
     'Gold, silver soften as markets price in December Fed hike risk - Kitco PM Report',
     'Direction right; HAWKISH and RATE_REPRICING double-fire on one headline.'),

    # ---- REAL - BTCUSD (bitcoin)
    ('btc_flows', 'BTCUSD', 'BEARISH',
     ["BTC_ETF_FLOW"],
     'Bitcoin Price Forecast: BTC struggles as ETF outflows, geopolitical uncertainty weigh',
     'ETF outflows -> BTC down. Single clean event type.'),
    ('btc_usd', 'BTCUSD', 'BULLISH',
     ["WEAK_USD"],
     'British Pound gains positive traction as USD remains depressed on softer bond yields',
     'USD softness -> risk assets up.'),
    ('crypto_regulation_gap', 'BTCUSD', 'BEARISH',
     ["REGULATION_CLAMP"],
     'Australia proposes leverage caps for crypto-settled derivatives',
     'Genuine crypto regulation proposal. The map misses it entirely.'),
    ('btc_price_action', 'BTCUSD', 'BEARISH',
     [],
     'Bitcoin drops 2.2% as Ukraine, Iran strikes send oil toward one-month high',
     "Real air strikes. 'strikes' is not a GEOPOLITICAL_RISK keyword."),

    # ---- REAL - EQUITY (S&P 500 proxy)
    ('geopolitics_risk', 'EQUITY', 'BULLISH',
     ["GEOPOLITICAL_RISK"],
     '2 More Oil Tankers Attacked Near Strait of Hormuz - Investing.com South Africa',
     'Escalation is risk-off for equities.'),
    ('geopolitics_deescalation_misfire', 'EQUITY', 'BEARISH',
     ["GEOPOLITICAL_RISK"],
     'Dollar Index hits an 18-month high as the energy ceasefire fails',
     'A failed ceasefire is escalation, not de-escalation.'),
    ('policy_hawkish', 'EQUITY', 'BEARISH',
     ["HAWKISH_POLICY"],
     'Bernstein SocGen cuts Boeing stock price target on delivery delays',
     'Target cut, not policy. DOVISH_POLICY fires instead.'),
    ('policy_hawkish', 'EQUITY', 'BEARISH',
     ["HAWKISH_POLICY"],
     'China to cut VAT export rebate rate for NEV to 9%',
     'Fiscal policy, not monetary policy.'),
    ('yields', 'EQUITY', 'BULLISH',
     ["RISING_YIELDS"],
     'Equities: Stable oil and yields support stock gains - Danske Bank',
     'Supportive yields -> equities up; the map reads RISING_YIELDS as bearish.'),
    ('policy_dovish_misfire', 'EQUITY', 'NEUTRAL',
     [],
     'Barclays sees U.K. bond crisis risk easing as global yields rise',
     'Credit-easing commentary; the DOVISH keyword swallows the yield leg.'),
    ('banking_stress', 'EQUITY', 'BEARISH',
     ["BANKING_STRESS"],
     'Beijing police detain Yiren Digital chairman after massive product defaults',
     'Credit default -> banking stress -> equities down.'),
    ('non_macro', 'EQUITY', 'NEUTRAL',
     [],
     '$100 Invested In Visa 10 Years Ago Would Be Worth This Much Today - Benzinga',
     'Retrospective retail-finance filler.'),
]

CURATED = [
    # ---- CURATED - negation / hedging
    ('negation', 'XAUUSD', 'BEARISH',
     ["HAWKISH_POLICY"],
     'Fed rules out a rate cut in June, signals higher for longer',
     'Negation correctly suppresses the rate-cut phrase.'),
    ('negation', 'XAUUSD', 'MIXED',
     ["GENERAL_MACRO"],
     'No rate cut from the Federal Reserve this year',
     'Negation correctly suppresses DOVISH; nothing else fires.'),
    ('negation', 'XAUUSD', 'BEARISH',
     ["HOT_INFLATION"],
     'Officials say the Fed is unlikely to cut rates amid sticky inflation',
     'Double negation; HOT_INFLATION carries the read.'),
    ('negation', 'XAUUSD', 'BEARISH',
     ["HOT_INFLATION"],
     'Rate cut hopes fade as services inflation stays sticky',
     "'fade' comes AFTER the phrase, so the negator scope misses it."),
    ('negation', 'XAUUSD', 'BEARISH',
     ["HAWKISH_POLICY"],
     'Fed abandons rate cut plans, shifts to higher for longer',
     "'abandons' comes after 'rate cut'; DOVISH still fires."),

    # ---- CURATED - hedging
    ('hedged', 'XAUUSD', 'BULLISH',
     ["DOVISH_POLICY"],
     'Fed reportedly plans a rate cut in December',
     'Hedged language still yields a signal - an accepted model choice.'),

    # ---- CURATED - dovish policy
    ('policy_dovish', 'XAUUSD', 'BULLISH',
     ["DOVISH_POLICY"],
     'Fed announces a 50 basis point rate cut',
     'Unambiguous dovish.'),

    # ---- CURATED - hawkish policy
    ('policy_hawkish', 'XAUUSD', 'BEARISH',
     ["HAWKISH_POLICY"],
     'Fed announces surprise 75 basis point rate hike',
     'Direction right; HAWKISH and RATE_REPRICING double-fire.'),

    # ---- CURATED - direct asset price action
    ('price_action', 'XAUUSD', 'BEARISH',
     [],
     'Gold plunges 2% in early trade',
     'Direction right, but it arrives via STOCK_PLUNGE, not a gold price rule.'),
    ('price_action', 'BTCUSD', 'BULLISH',
     [],
     'Bitcoin surges to 64000 as retail demand returns',
     'Direct-asset override handles this correctly.'),
    ('price_action', 'XAGUSD', 'BULLISH',
     [],
     'Silver jumps 3% as traders cover shorts',
     'Direct-asset override handles this correctly.'),

    # ---- CURATED - in-line / priced-in prints
    ('priced_in', 'XAUUSD', 'MIXED',
     ["GENERAL_MACRO"],
     'CPI comes in as expected at 3.2 percent',
     'In-line print -> no side.'),
    ('priced_in', 'XAUUSD', 'BEARISH',
     ["HOT_INFLATION"],
     'CPI comes in surprisingly hot, hotter than expected',
     'Surprise hot print.'),
    ('priced_in', 'XAUUSD', 'BEARISH',
     ["STRONG_JOBS"],
     'Payrolls blow past forecasts in sharpest beat since 2022',
     "'blow past' and 'sharpest beat' are absent from STRONG_JOBS keywords."),

    # ---- CURATED - treasury yields
    ('yields', 'XAUUSD', 'BEARISH',
     ["RISING_YIELDS"],
     'Treasury yields climb to four-week high on inflation fears',
     'Rising yields -> gold down.'),

    # ---- CURATED - US dollar
    ('usd', 'XAUUSD', 'BEARISH',
     ["STRONG_USD"],
     'Dollar index surges as traders price a longer higher-for-longer path',
     'Strong dollar plus hawkish pricing.'),
    ('usd', 'XAUUSD', 'BULLISH',
     ["WEAK_USD"],
     'Dollar weakens sharply after soft jobs report',
     'Weak dollar plus soft jobs.'),

    # ---- CURATED - bitcoin regulation and ETFs
    ('btc_regulation', 'BTCUSD', 'BULLISH',
     ["BTC_ETF_FLOW", "REGULATION_CLARITY"],
     'SEC approves spot bitcoin ETF trading',
     'Direction right; CRYPTO_REGULATION is spurious on top.'),

    # ---- CURATED - bitcoin ETF flows
    ('btc_flows', 'BTCUSD', 'BULLISH',
     ["BTC_ETF_FLOW"],
     'Spot Bitcoin ETFs record largest daily inflows since launch',
     'Clean single event type.'),
    ('btc_flows', 'BTCUSD', 'BEARISH',
     ["BTC_ETF_FLOW", "RISK_OFF"],
     'Spot bitcoin ETFs see heavy outflows as risk appetite fades',
     'Both mechanisms legitimately present.'),

    # ---- CURATED - bitcoin liquidations
    ('btc_liquidation', 'BTCUSD', 'BEARISH',
     ["CRYPTO_LIQUIDATION"],
     'Crypto liquidations top 500 million in cascade',
     'Forced selling -> BTC down.'),

    # ---- CURATED - bitcoin regulation and ETFs
    ('btc_regulation', 'BTCUSD', 'BULLISH',
     ["REGULATION_CLARITY"],
     'Congress passes stablecoin legislation after years of debate',
     'Clarity -> crypto up.'),

    # ---- CURATED - bitcoin price action
    ('btc_price_action', 'BTCUSD', 'BEARISH',
     [],
     'Bitcoin Drops Below $83,000, Ethereum Down 1%, XRP, Dogecoin Plunge 3% - Benzinga',
     'Direction right, but it arrives via STOCK_PLUNGE, not a crypto rule.'),

    # ---- CURATED - crypto regulation misfires
    ('crypto_regulation_misfire', 'BTCUSD', 'NEUTRAL',
     [],
     'Letter: The SEC revised its quorum rules for a reason - Financial Times',
     "'SEC' is a US equities-regulator word, not a crypto word."),
    ('crypto_regulation_misfire', 'BTCUSD', 'NEUTRAL',
     [],
     'ASPEN GROUP LTD STPLD SEC by Aspen Group, Ltd. Financials - Benzinga',
     "An SEC filing header. 'SEC' fires CRYPTO_REGULATION at magnitude 1.00."),

    # ---- CURATED - crypto hacks
    ('crypto_hack', 'BTCUSD', 'BEARISH',
     ["CRYPTO_HACK"],
     'Changpeng Zhao Warns Crypto Holders After $86M Ledger-Linked Theft, $112M Coldcard Exploit Put Wallet Security in Focus - Benzinga',
     'A real wallet exploit. Direction right; CRYPTO_REGULATION is spurious.'),

    # ---- CURATED - crypto hack misfires
    ('crypto_hack_misfire', 'BTCUSD', 'NEUTRAL',
     [],
     'Sim Swap Fraudster Who Blew Stolen Crypto on Gold Grills and Luxury Holidays Jailed for 2.5 Years',
     "'Gold' here is a restaurant chain; 'crypto' is the proceeds of fraud."),

    # ---- CURATED - regulation-clamp misfires
    ('regulation_clamp_misfire', 'BTCUSD', 'NEUTRAL',
     [],
     'Aprocitentan efficacy data reinforce endothelin pathway blockade as a distinct therapeutic approach - Financial Times',
     "Pharma paper. 'blockade' plus 'efficacy' fire OIL_SUPPLY_TIGHTNESS / REGULATION_CLAMP."),
    ('regulation_clamp_misfire', 'BTCUSD', 'NEUTRAL',
     [],
     'Chinese wealth manager CreditEase placed under criminal investigation - Reuters',
     'Genuinely negative for China credit, but not a crypto-regulation event.'),

    # ---- CURATED - bitcoin microstructure
    ('btc_whale', 'BTCUSD', 'NEUTRAL',
     ["BTC_WHALE_ALERT"],
     'Bitcoin whale that sat through the March 2020 crash moves $378 million',
     'BTC_WHALE_ALERT is deliberately directionless.'),

    # ---- CURATED - inflation prints
    ('inflation', 'BTCUSD', 'BEARISH',
     ["HOT_INFLATION"],
     'Denmark Inflation (HICP) (YoY) rose from previous 2% to 2.1% in September',
     'Hot print -> tighter policy -> crypto down.'),

    # ---- CURATED - China demand
    ('china_demand', 'XAGUSD', 'BULLISH',
     ["CHINA_DEMAND"],
     'China factory output beats forecasts on strong export demand',
     'Direction right; CHINA / SILVER_CHINA / INDUSTRIAL triple-fire.'),

    # ---- CURATED - silver industrial demand
    ('silver_demand', 'XAGUSD', 'BULLISH',
     ["SILVER_INDUSTRIAL_DEMAND"],
     'Solar panel demand surges, boosting industrial silver consumption',
     'Direction right; INDUSTRIAL and SILVER_INDUSTRIAL overlap.'),

    # ---- CURATED - China demand
    ('china_demand', 'XAGUSD', 'BEARISH',
     ["CHINA_DEMAND"],
     'China demand weakens sharply as property sector slumps',
     'The demand verb correctly flips the sign.'),

    # ---- CURATED - equity earnings
    ('equities_earnings', 'EQUITY', 'MIXED',
     ["EARNINGS_BEAT", "EARNINGS_MISS"],
     'Anand Rathi Wealth Q2 FY27 slides: profit beats despite revenue miss',
     'Beat and miss in one line -> MIXED is the honest read.'),
    ('equities_earnings', 'EQUITY', 'BULLISH',
     ["EARNINGS_BEAT"],
     'Enovix earnings beat, revenue topped estimates',
     'Clean earnings beat.'),

    # ---- CURATED - index-flow misfires
    ('equities_index_flow_misfire', 'EQUITY', 'NEUTRAL',
     [],
     'Chainlink Price Forecast: LINK regains strength on ETF inflows, whale demand',
     "An altcoin forecast. 'etf inflow' with no qualifier makes it an equity signal."),

    # ---- CURATED - equity selloffs
    ('equities_plunge', 'EQUITY', 'BEARISH',
     ["STOCK_PLUNGE"],
     'ActiveOps crashes 43% as major customer loss clouds FY28 outlook',
     'Real single-name crash.'),

    # ---- CURATED - equity selloff misfires
    ('equities_plunge_misfire', 'EQUITY', 'NEUTRAL',
     [],
     'AST SpaceMobile upgraded to buy at Deutsche Bank as selloff called misread',
     "'selloff' here is explicitly called misread - the headline is bullish."),

    # ---- CURATED - equity rallies
    ('equities_rally', 'EQUITY', 'BULLISH',
     ["STOCK_RALLY"],
     '9 Of 11 Sectors Rise Monday As Cyclicals Lead - Benzinga',
     'Broad advance. Real Benzinga headline.'),

    # ---- CURATED - equity rally misfires
    ('equities_rally_misfire', 'EQUITY', 'NEUTRAL',
     [],
     "Ethereum Price Forecast: Lee predicts 'largest ever crypto bull market' as BitMine nears 5% supply",
     "'bull market' plus 'rally' make an ETH forecast an equity rally."),
    ('equities_rally_misfire', 'EQUITY', 'BEARISH',
     ["STOCK_PLUNGE"],
     'Will surging equity supply derail the U.S. bull market? Goldman Sachs weighs in',
     "'derail' is a down-verb; the map still reads it BULLISH."),

    # ---- CURATED - Dow / index misfires
    ('equities_dow_misfire', 'EQUITY', 'BULLISH',
     ["STOCK_RALLY"],
     'Dow Jones Industrial Average edges higher with no memory chips to sell',
     'A Dow tape headline routed to SILVER_INDUSTRIAL_DEMAND for silver.'),

    # ---- CURATED - equity flows and growth
    ('equities_growth', 'EQUITY', 'MIXED',
     [],
     "India's monthly fund investments hit record highs despite market slide - Reuters",
     'Fund flows up AND market slide - genuinely mixed.'),

    # ---- CURATED - genuinely mixed headlines
    ('mixed', 'EQUITY', 'BEARISH',
     ["HAWKISH_POLICY"],
     'Fed turns hawkish on inflation but cuts growth forecast sharply',
     'Hawkish leg dominates the map; the growth cut is not modelled.'),

    # ---- CURATED - risk-on / risk-off
    ('risk_sentiment', 'EQUITY', 'BULLISH',
     ["RISK_ON"],
     'Dow Jumps Over 400 Points, Records Weekly Gain: Investor Sentiment Improves, Fear & Greed Index',
     'Risk-on tape -> equities up.'),
    ('risk_sentiment', 'XAUUSD', 'BULLISH',
     ["RISK_OFF"],
     'FX Today: US Dollar at YTD high as safe-haven demand surges - FXStreet',
     'Safe-haven demand -> gold up, and the dollar leg is real.'),

    # ---- CURATED - risk-sentiment misfires
    ('risk_sentiment_misfire', 'BTCUSD', 'NEUTRAL',
     [],
     'UBS sees rising risks for a mild Nasdaq pullback in near term',
     'A pullback warning. RISK_ON fires instead of STOCK_PLUNGE.'),

    # ---- CURATED - risk-on / risk-off
    ('risk_sentiment', 'BTCUSD', 'BEARISH',
     ["RISK_OFF"],
     'New Zealand Dollar retreats toward 17-month low on Oil-driven risk aversion',
     'Risk aversion -> crypto and equities down, gold up.'),

    # ---- CURATED - banking and credit stress
    ('banking_stress', 'XAUUSD', 'BULLISH',
     ["BANKING_STRESS"],
     'Major bank fails as credit crunch spreads through markets',
     'Bank failure -> safe haven.'),
    ('banking_stress', 'XAUUSD', 'NEUTRAL',
     [],
     'Liquidity crisis eases as central backstop funds announced',
     "The crisis is EASING. 'ease' is not a negator for BANKING_STRESS keywords."),

    # ---- CURATED - geopolitical escalation
    ('geopolitics_risk', 'XAUUSD', 'BULLISH',
     ["GEOPOLITICAL_RISK"],
     'Missile strike widens as sanctions bite, ceasefire collapses',
     "'ceasefire' fires DEESCALATION alongside RISK - contradictory."),

    # ---- CURATED - geopolitical de-escalation
    ('geopolitics_deescalation', 'XAUUSD', 'BEARISH',
     ["GEOPOLITICAL_DEESCALATION"],
     'Full ceasefire declared, tensions ease across the border',
     'Clean de-escalation.'),

    # ---- CURATED - geopolitical escalation
    ('geopolitics_risk', 'XAUUSD', 'BULLISH',
     ["GEOPOLITICAL_RISK", "STRONG_USD"],
     'Geopolitical tensions push gold higher as dollar strengthens',
     "'tensions' is NOT a GEOPOLITICAL_RISK keyword - only STRONG_USD fires."),

    # ---- CURATED - duplicate wire rewrites
    ('duplicate', 'XAUUSD', 'BULLISH',
     ["DOVISH_POLICY"],
     'Fed holds interest rates steady, signals one cut in 2027',
     'First of three near-identical wire rewrites.'),
    ('duplicate', 'XAUUSD', 'BULLISH',
     ["DOVISH_POLICY"],
     'Fed holds rates steady and signals a single cut in 2027',
     "Second rewrite: 'one cut' matches, 'a single cut' does not."),
    ('duplicate', 'XAUUSD', 'BULLISH',
     ["DOVISH_POLICY"],
     'Federal Reserve keeps rates on hold, one cut pencilled in for 2027',
     'Third rewrite: same event, different verdict.'),

    # ---- CURATED - fallback and non-macro
    ('general_macro', 'XAUUSD', 'NEUTRAL',
     [],
     'Manchester United beats Chelsea three nil in derby thriller',
     'Football. classify_category reads it as Equities -> GENERAL_MACRO.'),

    # ---- CURATED - equity general
    ('equities_general', 'EQUITY', 'MIXED',
     ["CORP_EARNINGS_SEASON"],
     'Asia stocks mixed as oil retreats, earnings season gathers pace',
     'Directionless by design (CORP_EARNINGS_SEASON is [0, 0.25]).'),
]
def load_real_pool() -> dict:
    """title -> (source, published_at, source_quality)."""
    pool: dict[str, tuple[str, str, str]] = {}
    for name in ('morning_brief_2026-10-12.json', 'morning_brief_2026-10-13.json', 'morning_brief_2026-10-14.json'):
        p = BASE / 'data' / 'reports' / name
        if not p.exists():
            continue
        j = json.loads(p.read_text(encoding='utf-8'))
        for it in j.get('items', []):
            t = (it.get('title') or '').strip()
            if t:
                pool[t] = (it.get('source', ''), it.get('published_at_utc', ''), it.get('source_quality', 'TIER_4_OTHER'))
    p = BASE / 'logs' / 'decisions.jsonl'
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            for d in o.get('drivers') or []:
                t = (d.get('title') or '').strip()
                if t and t not in pool:
                    pool[t] = ('decisions.jsonl', '', 'TIER_2_MAJOR')
    return pool

def lookup(title: str, pool: dict) -> tuple | None:
    """Exact match first, then a close-match fallback.

    Report titles carry typographic quotes and en-dashes that are awkward to
    retype by hand, so a near-identical pool title counts as a hit. The
    RESOLVED title is what the engine actually sees, so the baseline can never
    drift from the stored article.

    Returns ``(resolved_title, source, published_at, source_quality)`` or None.
    """
    if title in pool:
        return (title,) + pool[title]
    near = difflib.get_close_matches(title, list(pool), n=1, cutoff=0.55)
    if near:
        return (near[0],) + pool[near[0]]
    return None

def engine_view(title: str):
    cat = detect_category(title)
    ets = [e.value for e in detect_event_types(title, cat)]
    res = resolve(title, cat)
    return (cat, ets, res)

def build() -> list[dict]:
    from app.config import settings

    pool = load_real_pool()
    rows: list[dict] = []
    missing: list[str] = []

    for kind, entries in (("real", REAL), ("curated", CURATED)):
        for group, asset, exp_dir, exp_ets, title, note in entries:
            # Only REAL rows are looked up in the project's stored headlines.
            # CURATED rows are hand-written edge cases and must be used
            # verbatim - fuzzy-matching them against the pool silently
            # substitutes a different headline.
            hit = lookup(title, pool) if kind == "real" else None
            if kind == "real" and hit is None:
                missing.append(title)
                continue
            if hit is None:
                resolved = title
                src, ts, quality = "curated manual sample", "", "TIER_2_MAJOR"
            else:
                resolved, src, ts, quality = hit

            cat, ets, res = engine_view(resolved)
            try:
                baseline_dir = res[Asset(asset)].direction.value
            except Exception:
                baseline_dir = "UNROUTED"

            known_defect = baseline_dir != exp_dir or ets != exp_ets
            rows.append({
                "id": "R%03d" % (len(rows) + 1),
                "group": group,
                "title": resolved,
                "body": "",
                "published_at": ts,
                "source": src,
                "source_quality": quality,
                "expected_asset": asset,
                "expected_direction": exp_dir,
                "expected_event_types": exp_ets,
                "baseline_direction": baseline_dir,
                "baseline_event_types": ets,
                "baseline_category": cat.value,
                "gate": bool(settings.only_high_impact_counts),
                "known_defect": known_defect,
                "notes": note,
            })

    if missing:
        print("WARNING: %d real titles not found in the project data:" % len(missing))
        for m in missing:
            print("   ", m[:100])
    return rows

def main() -> int:
    rows = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join((json.dumps(r, ensure_ascii=False) for r in rows)) + '\n', encoding='utf-8')
    clean = sum((1 for r in rows if not r['known_defect']))
    dirty = len(rows) - clean
    print('wrote %d rows -> %s' % (len(rows), OUT))
    print('  clean (baseline == label) : %d' % clean)
    print('  known_defect              : %d' % dirty)
    assets: dict[str, int] = {}
    for r in rows:
        assets[r['expected_asset']] = assets.get(r['expected_asset'], 0) + 1
    print('  per-asset                 : %s' % assets)
    print('  groups                    : %d' % len({r['group'] for r in rows}))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())