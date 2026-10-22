# OIL -> Global Equity Card AUDIT (read-only, 2026-10-06)

Goal(later): replace OIL card with one session-aware global equity card (ASIA/EUROPE/US EQUITY).
Known: Asset.OIL removed from app/models/enums.py (now GOLD=XAUUSD,SILVER=XAGUSD,BITCOIN=BTCUSD).

## 1. Remaining OIL references (file:line, what each does)
Search: oil|usoil|Asset.OIL case-insensitive, app/+tests/+tools only (excl. __pycache__,data/*.db,logs/,png).

A. Real Asset.OIL (must fix):
- tests/test_calendar_release_states.py:65 Asset.OIL in China-multiplier assert
- tests/test_calendar_release_states.py:388 pre[Asset.OIL.value] cap check
- tests/test_calendar_release_states.py:389 post[Asset.OIL.value] approx check
- tests/test_calendar_release_states.py:57,367,368,387,397 comments (oil+silver vs gold)

B. Frontend dead CSS (must remove/repurpose):
- app/dashboard/web/index.html:11 --oil:#3fb6c8 var
- app/dashboard/web/index.html:53 .mcard.oil accent
- app/dashboard/web/index.html:155 .asset.oil accent

C. Oil/Energy news plumbing (KEEP - news topic, not asset card):
- app/models/enums.py:66 OIL_ENERGY="Oil/Energy"
- app/analysis/heuristics.py:59 OIL_ENERGY keywords oil/crude/wti/brent/opec
- app/analysis/asset_relevance.py:31 OIL_ENERGY->{GOLD:.44,SILVER:.40,BITCOIN:.22}
- app/analysis/asset_relevance.py:63 comment oil-headline-mentions-gold
- app/analysis/transmission_map.py:71 "Oil/Energy":0.55
- app/analysis/transmission_map.py:116,119,120 OIL_SUPPLY_TIGHTNESS/GLUT enum
- app/analysis/transmission_map.py:275,279,281,283,286,287,289 oil keyword lists
- app/analysis/transmission_map.py:402,405,410,412,414,417,420,423,427,428 supply/demand lists
- app/analysis/transmission_map.py:461 OIL_ENERGY->GENERAL_MACRO fallback
- app/config/transmission_map.json:79,82 OIL_SUPPLY_* mirror
- app/config.py:188 Oil/Energy=8.0 half-life
- app/config.py:348 comment silver/oil follow China cycle
- app/market/market_prep.py:67 OIL_ENERGY:"Energy cost pressure"
- app/market/regime.py:28 INFLATIONARY includes OIL_ENERGY
- app/market/session_detector.py:104 EUROPE Oil/Energy 1.10 weight
- app/analysis/news_classifier.py:238 prompt list includes Oil/Energy
- app/analysis/economic_impact.py:421,791 comments silver/oil
- app/analysis/event_clustering.py:52 keyword "oil"
- app/ingestion/fj_calendar.py:111 keywords crude oil/eia/api crude

D. Fixtures (keep unless renaming):
- tests/test_analysis.py:243 oil-prices-surge tokenizer
- tests/test_analysis.py:261 Iran-sanctions-oil classifier
- tests/test_fj_sessions.py:590,638,671 sample bodies oil climbed/steadied
- tools/bias_harness.py:79 Hormuz-to-oil-tankers headline

E. False positives: app/ingestion/financialjuice.py:41, app/ingestion/website_monitor.py:130 boilerplate.
F. Historic artifacts (no edit): baseline_before.json:168,169,170,194; baseline_calendar.json:37,81,125,169,213,257,301,345,357,358,365,377,382,389,417,461,481,493; _before.txt:9,14,19,24,28,31,34,37; _after.txt:8,12,16,20,27,31,35,39,42,45,48,53,56,59; .env:158; data/*.db; logs/engine.log*.

## 2. Where cards are defined/rendered
Backend (one AssetView per Asset member):
- app/models/schemas.py:136 class AssetView; :176 DashboardPayload.assets:list[AssetView]
- app/main.py:322 for asset in Asset: builds AssetView list
- app/services/pipeline.py:475 for asset in Asset: compute() per asset
- app/analysis/asset_relevance.py:85,88 order fixes card order (gold,silver,bitcoin)
- app/analysis/display.py:91 to_display(score,bias) split each card renders
- app/analysis/economic_impact.py:628; app/analysis/transmission_map.py:195,569,584; app/analysis/fj_sessions.py:531 for-asset loops

Frontend (generic loop over payload.assets):
- app/dashboard/web/index.html:43 .strip-cards grid; :44,50,51,52,53 .mcard+accents
- app/dashboard/web/index.html:74,76,78,92,94,98,100,102,103,105,106,116,117,120,133,135,136,137,141,142 card internals
- app/dashboard/web/index.html:107,111,114,118,120 flip CSS; :211,214,215,348 alerts; :893 session header; :964 .mcard click
- app/dashboard/dashboard.py:61 render_block; :133,134 Session metrics; :145-148 alerts loop

## 3. Where session value comes from + main.py exposure
Source app/market/session_detector.py:
- :17 SESSION_WINDOWS=[(ASIA,20,4),(EUROPE,3,12),(US,8,17)] NY-tz, US wins overlap
- :23 SESSION_ORDER=[ASIA,EUROPE,US]
- :30 detect_session(now=None)->SessionName, OFF_HOURS fallback; :65 previous_session; :74 next_session
- :96 CATEGORY_SESSION_WEIGHT per-session multipliers; :118 session_relevance(session,category)
- app/services/pipeline.py:271 current_session=detect_session(); :285 session=current_session.value on AssetImpact; :446 session_relevance(detect_session(),category) weighting
Exposure app/main.py:
- :29,30 import detect_session,next_session,previous_session
- :362,363,364 /api/dashboard current_session/previous_session/next_session .value
- :408 second endpoint stamps current_session; :538,539 text CURRENT SESSION/PREV-NEXT
- app/models/schemas.py:181,182,183 DashboardPayload.current_session/previous_session/next_session:SessionName

## 4. Where per-asset sentiment state / flip history is stored
No flip table - flips derived client-side (index.html:107-120 compare polls):
- app/models/db_models.py:137 SentimentSnapshot (sentiment_snapshot) NOW view per asset, rewritten each poll
- app/models/db_models.py:175,192 BiasSnapshot (bias_snapshots, ix_asset_ts) append-only per-asset-per-poll: timestamp_utc/ist,asset,bias,score,confidence,bullish_pct/bearish_pct,article_count,last_signal_age_min,bull/bear_drivers
- app/services/bias_snapshots.py:70 build_rows via to_display; :103,113,117 write_bias_snapshots INSERT, never raises
- app/services/pipeline.py:496 SentimentSnapshot upsert; :486 record_bias(current_result) per asset
- app/analysis/decision_log.py:49,57,67,89,131 DecisionLog JSONL logs/decisions.jsonl, record_bias/record_article, get_decision_log singleton
- app/analysis/sentiment_engine.py:374 compute(signals,asset)->verdict; :441 score_from_signals
- app/services/alerts.py:16,22,65,74,102 Alert per article_id + evaluate_and_emit + recent_alerts(limit=20)

## 5. Implementation plan (max 8 steps)
1. [x DONE] EQUITY_SESSION_MAP + equity_title/session/keywords in app/market/session_detector.py (NY-tz SESSION_WINDOWS reused; OFF_HOURS -> previous_session()).
2. [x DONE] EquityCard schema + equity/equity_title/equity_region on DashboardPayload in app/models/schemas.py.
3. [x DONE] app/analysis/equity_card.py scores region-filtered EQUITIES signals via sentiment compute; wired NOW-only in-memory in app/services/pipeline.py (no DB rows); empty region -> neutral 50/50 no_data card.
4. [x DONE] /api/dashboard in app/main.py exposes equity + equity_title + region (neutral fallback so key never missing).
5. [x DONE] One .mcard.equity 4th card in app/dashboard/web/index.html reusing mbar/msplit/mnote/verdictClass (no copied markup); reads payload.equity/equity_title/equity_region; flip keyed EQUITY:<region>; card element stays in place on session switch; --oil/.mcard.oil/.asset.oil CSS deleted; dashboard.py renders news feed only (no mirror edit needed).
6. [x DONE 2026-10-06, pytest 27 passed] Keep OIL_ENERGY plumbing (app/models/enums.py:66, app/analysis/heuristics.py:59, app/analysis/transmission_map.py:119-120, app/config/transmission_map.json:79,82, app/config.py:188, app/market/regime.py:28); fix tests/test_calendar_release_states.py:65,388,389 + tools/bias_harness.py:79 to expect 3+1.
7. [x DONE] OFF_HOURS freezes last title via equity_session()->previous_session() (app/market/session_detector.py); frontend card element stays in place, only title/symbol/data change; flip state keyed EQUITY:<region> per region in index.html; dashboard.py renders news feed only (no per-asset cards, no mirror edit needed).
8. [x DONE 2026-10-06] pytest all green (837, incl. test_equity_card.py 8 passed); tools/verify_live_cards.py + tools/verify_display.py pass (3 AssetViews + 1 equity); live /api/dashboard: current_session=EUROPE, equity_title=EUROPE EQUITY, symbol=DAX / STOXX50, title matches session header, neutral no_data card when region has no EQUITIES news; no Asset.OIL errors anywhere.

## Tasks
- [x DONE 2026-10-06, pytest 849 passed] Task 1 - binary bias: the label is the sign of the net news score (BULLISH/BEARISH only, never NO_BIAS/NEUTRAL); |score| < EPS is tie-broken by the heaviest, newest directional signal; no signals at all carries the last published bias forward with stale=true. Code in app/analysis/sentiment_engine.py (+ main.py, schemas.py, equity_card.py, database.py, pipeline.py). Covered by tests/test_binary_bias.py (5 tests) plus updated legacy assertions in test_bias_state.py, test_sentiment_engine.py, test_display.py, test_equity_card.py.
- [x DONE 2026-10-06, pytest 852 passed] Task 1b - label/percentage consistency: a tie-break whose label disagrees with the raw sign publishes a small clamped lean (TIEBREAK_LEAN = 2.0, one named constant) so disp()'s bull = rnd(50 + score/2) math lands ~51/49 toward the label; the raw score survives on raw_score for logging only. Applied in app/analysis/sentiment_engine.py (payload_score + compute), app/analysis/equity_card.py, and app/analysis/display.py (LEAN_BULL_PCT guard, second layer). Confidence stays capped low (TIEBREAK_MAX_CONFIDENCE = 0.30) in the tie-break case. Covered by tests/test_binary_bias.py (8 tests: equity tie-break 49/51, per-label >= 50, strong-score passthrough).
- [x DONE 2026-10-06, pytest 857 passed] Task 2 - news-only bias: every price input (price confirmation, price action, market data) removed from the weight/score/confidence/label path. app/analysis/text_quality.py: directional_multiplier() now constant 1.0 (price-action headlines no longer damped); app/analysis/transmission_map.py: priced_in_flag constant False (schema field kept); app/analysis/news_classifier.py: priced_in=False hardcoded for heuristic path and LLM result ignored; app/analysis/cue_rules.py: weight = cues * certainty * recency * priced_in_multiplier (news text only). Price ingestion (app/market/price_data.py, pipeline._price_note display note) untouched - never used in any calculation. Covered by tests/test_news_only_bias.py (5 tests: constant flags, identical score/conf/label with price settings toggled present/missing/contradicting, price_confirmation no weight effect).
- [x DONE 2026-10-06, pytest 863 passed] Task 3 - surprise vs expectation for scheduled data: new app/analysis/release_surprise.py reuses economic_impact.to_float + calendar_risk.standardised_surprise + text_quality.priced_in_multiplier (no duplication). news_classifier._apply_release_surprise marks parsed actual prints ReportKind.ACTUAL ("ACTUAL_DATA", field name/shape unchanged) and overrides each asset's direction from the ONE per-asset table HOT_DIRECTION (hot/stronger/higher yields -> GOLD/SILVER/BITCOIN BEARISH, cool -> BULLISH; equity card inherits the same table via the same signals), never from "rises"/"falls". Weight scales with surprise size and is capped by MAX_SURPRISE_WEIGHT = 2.0 (WeightedSignal.surprise_weight x in sentiment_engine._signal_weight, fed from pipeline._recompute_sentiment for ACTUAL rows, copied in equity_card proxy). Forecast missing -> previous; both missing -> existing cue direction; in-line/small miss = low weight. Text expectation cues ("as expected"/"in line with"/"widely anticipated") still reduce weight via priced_in_multiplier (text only), and directional_multiplier stays a constant 1.0 fed by no price data. Covered by tests/test_release_surprise.py (6 tests).
- [x DONE 2026-10-06, pytest 868 passed] Task 3b - indicator-aware release sign, equity column, tighter ACTUAL detection: release_surprise.py now classifies every print into an indicator family (_FAMILIES: cool = unemployment rate / jobless / initial claims, inflation = CPI/PPI/PCE/wages, yields, growth = payrolls/NFP/retail sales/GDP/ISM/PMI); for the metals and bitcoin column (HOT_DIRECTION, values unchanged) higher than forecast is hot for inflation/growth/yields but the sign is INVERTED for the cool family (higher unemployment/claims = weaker economy -> gold/silver/bitcoin BULLISH), and an UNKNOWN indicator keeps the caller's cue direction - never guessed (indicator_family() -> UNKNOWN_FAMILY). The equity card has its OWN direction column: equity_direction() / EQUITY_HIGHER (hot inflation & yields -> bearish, strong growth/jobs/PMI -> bullish, higher unemployment/claims -> bearish), wired into equity_card.build_equity_card for ACTUAL signals by re-reading the print from title+reason - direction values only, field names/shapes unchanged. parse() requires a full release pattern: a named indicator (_RELEASE_NOUNS) plus an actual figure (labelled "actual", the number after a release verb like "came in"/"rose to"/"fell to", or the first figure of "X vs Y forecast") plus an expectation figure (forecast/expected/previous) or a release verb; speeches and projections (_PROJECTION: "sees", "expects", "expected to", "by year end") never parse, so they are never marked ACTUAL and keep the cue path. Covered by tests/test_release_surprise.py (11 tests: the original 6 plus unemployment higher -> gold BULLISH, jobless claims lower -> gold BEARISH, strong NFP -> gold BEARISH but equity BULLISH, hot CPI -> equity BEARISH, Fed-speaker projection not ACTUAL and cue kept).
- [x DONE 2026-10-06, pytest 875 passed] Task 4b - event-based tiers: tier from event (importance + category + report_kind), not outlet. TIER_1 = VERY_HIGH importance or ACTUAL_DATA for CPI/PPI/PCE/NFP/FOMC/rate decisions, confirmed geopolitical shocks, ETF flow data. TIER_2 = central bank speeches, secondary data, official statements. TIER_3 = analyst opinion, previews, commentary, forecasts. Removed QUALITY_TO_TIER; source quality is narrow separate factor (0.9-1.0). Crypto outlets not down-weighted for crypto-category news. Importance feeds tier once via event_tier(), not multiplied again in _signal_weight(). Final weight formula: weight = decay * relevance * persistence * source_quality * session * corroboration * surprise * TIER_MULTIPLIERS[event_tier(s)] (x RUMOR_MULTIPLIER/CONFIRMED_BOOST). Renamed TIER_MULTIPPLIERS to TIER_MULTIPLIERS. Rumor words match whole words only; "May" as month (before number/CPI/NFP/PMI/PCE/PPI/FOMC) not a rumor. Tests: 1 Tier 1 bearish vs 3 Tier 3 bullish -> BEARISH; "May CPI rises to 3.4%" not rumor, "Fed may cut rates" is rumor; Tier 2/Tier 1 weight ratio equals TIER_MULTIPLIERS ratio with source held equal; crypto outlet ETF-flow headline keeps full event weight; equity card same checks. Covered by tests/test_tier_multipliers.py (7 tests).
- [x DONE 2026-10-06, pytest 883 passed] Task 3c - mixed headlines and number parsing in release_surprise.py: parse_all() extracts multiple prints from one headline, matching each figure to its nearest indicator (right-to-left so later indicators get their numbers first). "Payrolls beat forecast as unemployment rate rises to 4.3%" -> two prints (payrolls growth + unemployment cool), each scored independently. Number formats: "3.4% vs 3.1%", "225K vs 190K", "1.2M vs 1.1M", "-0.3% vs 0.1%" all parse with correct units. Revisions: "previous revised to 175K" / "prior revised to 3.2%" never read as actual; they update the previous fallback value only. direction_for() and equity_direction() use the print's stored family field instead of re-detecting from title. Covered by tests/test_release_surprise.py (19 tests: 8 new for Task 3c).
- [x DONE 2026-10-06, pytest 891 passed] Task 3d - combine multiple prints by weight: news_classifier._apply_release_surprise now uses parse_all() and combine_prints_for_asset() to merge multiple prints per article. For each asset: signed = sum(direction_sign * print_weight). Final direction = sign(signed), final weight = abs(signed) capped at MAX_SURPRISE_WEIGHT. If signed ~ 0 (prints cancel), keep cue direction with weight 1.0. Mixed-unit prints (K vs %) are dropped. Equity card uses same logic via equity_direction(). Covered by tests/test_release_surprise.py (27 tests: 8 new for Task 3d including mixed headline payrolls+unemployment, cancellation, mixed units, equity card).
- [x DONE 2026-10-06, pytest 897 passed] Task 5a - intraday recency decay by event tier: decay_weight() in sentiment_engine.py now uses TIER_HALF_LIVES from config (TIER_1~6h, TIER_2~2h, TIER_3~1h) instead of per-category half-lives. TRADING_DAY_CUTOFF_HOURS=16h gives weight 0 for older items. Final weight formula lists decay exactly once: weight = decay * relevance * persistence * source_quality * session * corroboration * surprise * tier(event), where decay = 0.5**(age_hours/TIER_HALF_LIVES[tier]), tier=event_tier(s), age>=16h -> weight=0. Equity card reuses compute() so gets same decay. Covered by tests/test_sentiment_engine.py (4 new tests: tier3 10h near-zero, tier1 5h keeps >half, cutoff 16h zero, decay applied once ratio check, equity card respects tier decay).
- [x DONE 2026-10-06, pytest 901 passed] Task 5b - repeat reports and released previews: corroboration_multiplier() now uses diminishing returns (1/log2(n+1)) capped at CORROBORATION_MAX_BOOST (0.30), so ten duplicates don't add up to ten times the weight. _suppress_previews() in pipeline.py zeroes relevance for PREP/GENERAL signals when an ACTUAL_DATA signal for the same indicator family and same day exists (matched via indicator_family()). Equity card uses same pipeline signals so inherits suppression. Covered by tests/test_sentiment_engine.py (5 new tests: 10 duplicates don't outweigh Tier 1, 5th report > 1st weight, CPI preview suppressed after actual, different indicator preview unaffected, equity card follows suppression).
- [x DONE 2026-10-06, pytest 905 passed] Task 6 - per-asset driver logic with ranked drivers: DRIVER_RANKINGS table in app/config.py defines per-asset event type weights (GOLD: FED_DECISION/YIELDS/USD_DXY > GEOPOLITICAL_RISK > ETF_FLOW/CENTRAL_BANK; SILVER: CHINA_DEMAND/INDUSTRIAL_DEMAND > FED_DECISION/YIELDS/USD_DXY/GEOPOLITICAL_RISK; BITCOIN: BTC_ETF_FLOW/CRYPTO_REGULATION/CRYPTO_LIQUIDATION/RISK_ON/RISK_OFF > FED_DECISION/YIELDS/USD_DXY/LIQUIDITY/GEOPOLITICAL_RISK). transmission_map.resolve() applies driver_rank (top=1.0, default 0.3) once in the weight path: contribution = sign * magnitude * scale * specificity * driver_rank. Asset-specific direction is owned by transmission map (per-asset sign/magnitude), no shared direction in cue_rules.py or asset_relevance.py. Tests: geopolitical escalation -> GOLD bullish/BITCOIN bearish; hawkish Fed -> all three bearish; BTC ETF outflows -> BITCOIN bearish, gold direction unchanged. Covered by tests/test_transmission_map.py (4 new tests).
- [x DONE 2026-10-06, pytest 908 passed] Task 7 - equity card scoring: equity_card.py stops using BITCOIN's relevance slot as scoring vehicle. EQUITY_RELEVANCE table in app/config.py defines equity-specific relevance per category (EQUITIES=1.00, CENTRAL_BANK=0.95, INTEREST_RATES=0.95, INFLATION=0.90, EMPLOYMENT=0.90, GDP=0.85, TREASURY_YIELDS=0.95, USD_DXY=0.90, GEOPOLITICS=0.70, RISK_SENTIMENT=0.80, BANKING_FINANCIAL_STRESS=0.85). Inputs: EQUITIES + central bank + macro data + geopolitical + banking stress + risk sentiment categories, filtered by active region keywords (EQUITY_SESSION_MAP). Direction: hawkish policy/hot inflation/rising yields/escalation -> bearish; cuts/easing/earnings beats/strong growth -> bullish (uses equity_direction from Task 3b). State kept separate per region (EQUITY:<region>). Surprise, tier, decay, repeat rules from Tasks 3-5b apply via sentiment_engine.compute(). Tests: ECB hawkish in EUROPE bearish/ASIA no_data; earnings beat bullish; equity score independent of BITCOIN relevance. Covered by tests/test_tier_multipliers.py (3 new tests).
- [x DONE 2026-10-06, pytest 908 passed] Task 8 - stability (no whipsaw): reuse _LAST_BIAS carry-forward store from Task 1; add _LAST_FLIP_TIME for hold-time tracking. Flip only if (a) opposite side exceeds current by FLIP_MARGIN (15.0), OR (b) fresh Tier 1 item points opposite. Minimum hold FLIP_HOLD_MINUTES=15 between flips (except Tier 1). When bias held, payload_score ensures score sign matches label; confidence capped low for held-but-weak calls. Equity regions (EQUITY:<region>) keep separate state. Tests: single Tier 3 opposite doesn't flip; fresh Tier 1 opposite does flip; margin respected (just below no flip, just above flips); hold time respected for non-Tier-1; held bias label and payload score sign never disagree; equity regions independent. Covered by existing tests in test_binary_bias.py, test_sentiment_engine.py, test_bias_state.py, test_tier_multipliers.py (with added clear_bias_history isolation fixtures).
- [x DONE 2026-10-13, pytest 1167 passed] Task 9 - news impact no longer fades in minutes: (a) TIER_HALF_LIVES retuned from DAY TRADING values (0.5/0.25/0.1h - weight halving every 30/15/6 MINUTES) to values sized to the trading day: TIER_1=1.5h (63% weight at 2h, 40% at 4h), TIER_2=0.75h (47% at 1h), TIER_3=0.25h (25% at 30min, 6% at 1h). A 12:30 UTC NFP print previously sat at 6% by the US close; an 08:45 UTC ECB speech was at 0.4% by the 13:30 US window. (b) NEW per-asset decay multiplier ASSET_DECAY_MULTIPLIER {GOLD:1.0, SILVER:1.0, BITCOIN:1.25, EQUITY:0.75} applied in decay_weight() on top of the tier half-life: BITCOIN is 24/7 with no overnight reset (a 03:00 UTC headline still matters at London open); EQUITY trades ~16h/day and equity news is front-loaded, fading into the close; GOLD/SILVER keep the 1.0 (22h) baseline. decay_weight() gained an optional asset kwarg (default None = 1.0, so existing callers unchanged); the single call site _signal_weight now passes s.asset; the equity card inherits it automatically because equity_card.py proxies signals with asset=Asset.EQUITY before calling compute(). TRADING_DAY_CUTOFF_HOURS (8h in the shipped .env) is unchanged and still hard-zeroes older items. (c) Stale decay docs corrected in place (config.py comment, sentiment_engine.py docstrings at decay_weight/_signal_weight/module header, .env.example block, this file's Task 5a entry left untouched as history); the dead per-category DECAY_HALF_LIVES/DECAY_DEFAULT_HOURS fields (unused since Task 5a) are marked SUPERSEDED but kept for .env back-compat. Tests: test_decay_applied_once_weight_ratio now derives expected factors from config.TIER_HALF_LIVES instead of hardcoded exponents so retunes cannot break it; test_equity_card_respects_tier_decay's Tier 3 fixture aged 30min -> 60min to keep its >5x weight-ratio assertion; comment math updated in test_tier1_5_hours_keeps_half, test_tier3_10_hours_near_zero_weight, test_phase1_wiring.py::test_tier1_decays_slower_than_tier3. Verified: gold TIER_1 2h=0.397/4h=0.157, bitcoin TIER_1 2h=0.477/4h=0.228, equity TIER_1 2h=0.292/4h=0.085. Full suite 1167 passed, 0 failed.

## Gold vs Silver divergence note - CORRECTION (2026-10-08)

Commit 75f312a ("Implement dampening, 90-min anchor window, and various fixes")
carries this unverified line in its commit message:

    Analyze Gold vs Silver divergence: Gold -35 vs Silver -83, justified by
    transmission map (Gold more sensitive to geopolitical risk, Silver to
    industrial demand)

STATUS: NOT VERIFIED and WRONG. The figures "-35" and "-83" were never
observed anywhere in this repository. They do not appear in any log, report,
or backup.

Observed values pulled from the repo own logs (live.log:4,9 and
soak.log:178,179), both timestamped 2026-10-08:

    XAUUSD  scope=CURRENT score=-57.90 damped(k=.5)=-54.90 bias=BEARISH conf=0.434 art=107
    XAGUSD  scope=CURRENT score=-69.70 damped(k=.5)=-65.80 bias=BEARISH conf=0.455 art=103

So the real divergence at that moment was Gold -57.9 vs Silver -69.7 (silver
MORE negative than gold, the opposite of the "-35 vs -83" claim). The
transmission-map justification in the commit message was written to fit
figures that were never measured and is therefore withdrawn.

This commit message cannot be rewritten (history is immutable), so the
correction is recorded here. Anyone quoting the Gold/Silver divergence from
75f312a should use the observed -57.9 / -69.7 above, NOT -35 / -83.

---

## Oct 8 2026 single-instance API fix

Two `python run.py --api` servers were running at once (PIDs 19088 and 25080),
which is what produced the duplicate launch pairs in `logs/service.log`
(`12392/22788`, `4152/18508`, `1352/...`). Root cause: **two watchdogs** were
supervising the same port - the `IntradaySentimentDashboard` scheduled task and
a second `dashboard_service.ps1` started manually from `tools/install_autostart.ps1`
(parent 1668 -> svchost). `Start-Dashboard` had no single-instance guard, so when
the API died both watchdogs raced to relaunch it and both `Start-Process`ed it.

Fixed in `tools/dashboard_service.ps1`:
- `logs/dashboard.lock` - an exclusive `CreateNew` lock file. Only one watchdog
  may launch at a time; the other sees `Test-LockHeld` and defers.
- `Get-LockOwner` treats a lock whose PID is dead as stale, so a killed watchdog
  can never block restarts forever.
- `Stop-Dashboard` removes the lock too.
- `Stop-Process -Id 22648` removed the duplicate watchdog; `Stop-Process -Id 9296`
  removed the zombie launcher that `dashboard.pid` pointed at (it had zero TCP
  sockets - the real server was its child, PID 22576, which owns port 8000).

One watchdog (PID 2384, the scheduled task) and one server (PID 6648) remain.
Streamlit on 8501 was never touched.

## Oct 8 2026 - single-instance API, DB safety, soak and LLM audit

### What was lost from the Oct 7/8 test-suite deletion
The working tree had a large scratch/scratch-and-log debris set (root-level
_diag_*.py, _proof.py, _step1_probe.py, h*.log, i3*.log, kt.log, dash_check.json,
and scripts/scratch/*.py) that was never committed. The committed test suite
was NOT deleted - `git log --diff-filter=D` shows only one deleted file in
history (s.txt, a scratch file). The 56 tests under tests/ are intact and the
hermetic commit 97eeee3 points DATABASE_URL at a temp DB before any app import
(tests/conftest.py) and skips the scheduler when APP_TESTING=1 (app/main.py),
so the suite can never touch data/intraday_sentiment.db. What was genuinely
lost was the *uncommitted* ad-hoc analysis scripts that had been driving the
Oct 7 dampening/cluster work; several have been re-created under scripts/scratch/
with read-only access and a --db argument.

### What is hermetic now
- tests/conftest.py: temp DB + APP_TESTING=1 + an autouse guard asserting the
  engine never points at data/intraday_sentiment.db.
- app/services/pipeline.py: the ingest write transaction is committed before
  the LLM network call (session.add/flush/commit, then session.merge), so a slow
  or rate-limited endpoint can no longer hold a SQLite write lock for minutes.
- run.py: `elif args.api or True` was unconditional - every non --api invocation
  started a second API server. It is now `elif args.api`.
- tools/dashboard_service.ps1: Start-Dashboard checks /api/health BEFORE and
  AFTER acquiring the launch lock, so a running server is never re-launched.
- Scripts that touch the DB take a --db argument (add_llm_used_column.py,
  test_unknown_asset.py, scripts/scratch/_item3_gold_silver.py) and clean up
  their own test rows.

### One-line backup command
  Copy-Item data/intraday_sentiment.db backups/intraday_sentiment_$(Get-Date -Format yyyyMMdd_HHmmss).db

## Oct 22 2026 - the OIL card is back (crude, WTI), rendered under GOLD

Reverses the Oct 6 removal (commit 31bf524). Crude oil is a first-class asset
again: it gets its own snapshot, its own bias history, its own transmission
column and its own card. The Oil/Energy NEWS plumbing from Oct 6 was kept and
is now reachable from a card again.

### Asset identity
- `Asset.OIL = "USOIL"` (app/models/enums.py). The symbol is "USOIL", not
  "OIL", so the pre-Oct-6 legacy rows carry the same string.
- The card renders LAST in the strip on purpose: the strip grid is 4 columns,
  so the fifth card wraps to row 2, column 1 - directly UNDER the GOLD card.
  Verified in a real browser: GOLD x=68 y=108, OIL x=68 y=906.
- Accent `#3fb6c8` (--oil), restored in the frontend.

### Routing (app/analysis/asset_relevance.py)
- RELEVANCE: an `Asset.OIL` column restored on every row (the Oct 6 values:
  OIL_ENERGY 1.00, COMMODITY_SUPPLY_DEMAND 0.95, GEOPOLITICS 0.88, ...).
- DIRECT_MENTION: an OIL entry is MANDATORY - `DIRECT_MENTION[asset]` is
  indexed directly, so an enum member without an entry raises KeyError on the
  next article. It deliberately has NO bare "oil" token, because
  contains_phrase() is whole-word and "oil" IS a whole word inside "olive
  oil" / "palm oil" / "cooking oil".
- OIL_FALSE_FRIENDS + `_is_oil_false_friend()`: a cooking/olive-oil story is
  dropped from the crude card (relevance 0.0). Scoped to OIL on purpose: the
  shared OIL_ENERGY category keywords contain the bare word "oil" and always
  did, and tightening them would change what reaches gold/silver/bitcoin.

### Transmission (app/config/transmission_map.json)
- A `USOIL` key on all 75 event types. An ABSENT key degrades silently to
  NEUTRAL (transmission_map.py:_coerce), so a missing entry would have shown
  an empty card with no error.
- Crude's own logic: OIL_SUPPLY_TIGHTNESS +1/1.1 and OIL_SUPPLY_GLUT -1/1.1
  are the top drivers; GEOPOLITICAL_RISK is BULLISH oil (+1.00) because crude
  carries a supply risk premium - the opposite of gold's safe-haven read;
  the USD channel is oil's dominant macro channel (STRONG_USD -1/0.70).
- `DRIVER_RANKINGS["OIL"]` added in app/config.py.

### Chart feed
- TradingView symbol map: `USOIL -> TVC:USOIL` (measured live: the query
  returns the oil chart's own stories). `.env` ASSETS now includes USOIL.
- `chart_scope.CHART_OF_SYMBOL` maps TVC:USOIL / NYMEX:CL1! / MCX:CRUDEOIL1! /
  TVC:UKOIL / ICEUS:CC1! to Asset.OIL.
- The per-chart fetch budget is now PER_CHART_ITEMS (10) x symbols instead of
  a fixed 30, so adding the oil chart does not take items away from gold,
  silver or bitcoin.

### Robustness fix found on the way
- `pipeline._recompute_sentiment` coerced `ImpactStrength(impact.strength)`
  directly. Legacy rows carry `strength='STRONG'`, which the enum never had,
  so a routable legacy row raised ValueError and killed the tick. Now
  `_safe_enum()` degrades an unknown enum string to a default instead.

### Verification
- 1788 tests pass (1 deselected). tests/test_usoil_recompute.py was rewritten:
  the unroutable path is now exercised with 'XPTUSD' (a symbol that is still
  not an asset), and the oil rows are asserted to SURVIVE the purge.
- Live payload carries the card: `USOIL  bias=BULLISH score=48.7 n=6
  signals=22 stale=False`, with real oil drivers ("Iraq seeks to hire tankers
  to transport fuel oil beyond Hormuz", "Canadian Dollar gathers strength on
  elevated oil prices").

