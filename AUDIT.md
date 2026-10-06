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
