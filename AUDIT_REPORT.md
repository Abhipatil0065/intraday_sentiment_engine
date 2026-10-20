# Intraday Market Sentiment Analysis - Full Audit Report

Do NOT change any code. Only read and report.

## 1. PROJECT OVERVIEW

### Purpose
Classifies financial news as bullish / bearish / neutral for specific assets: Gold (XAUUSD), Silver (XAGUSD), Bitcoin (BTCUSD), and Global Equities (EQUITY/S&P 500) for intraday day trading. News-based sentiment engine (not price prediction, not financial advice per README).

### Tech Stack
- Runtime: Python 3.10+ (current 3.12.10)
- Web/API: FastAPI 0.115.6, Uvicorn 0.34.0, websockets 14.1
- Dashboard: Streamlit 1.41.1, Plotly 5.24.1; web dashboard in app/dashboard/web
- DB/ORM: SQLAlchemy 2.0.36, SQLite (default), pydantic 2.10.4, pydantic-settings 2.7.0
- Ingestion: httpx 0.28.1, feedparser 6.0.11, beautifulsoup4 4.12.3, lxml 5.3.0, trafilatura 1.12.0, playwright 1.49.1 (optional)
- Scheduling: APScheduler 3.11.0
- Testing: pytest 8.3.4, pytest-asyncio 0.25.0

### Entry Points
- run.py (--api, --streamlit, --demo, --check-llm)
- start_server.bat / start_server.sh
- uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

## 2. DATA PIPELINE

### News Sources
From app/config.py and .env (NEWS_SOURCES):
- CNBC Economy (rss, https://www.cnbc.com/id/100003114/device/rss/rss.html, weight 1.0, TIER_2_MAJOR)
- Investing.com (rss, https://www.investing.com/rss/news.rss, 0.8, TIER_4_OTHER)
- CoinDesk (rss, https://www.coindesk.com/arc/outboundfeeds/rss/, 0.9, TIER_3_CRYPTO)
- Financial Juice (fj, https://www.financialjuice.com/, 0.9, TIER_2_MAJOR)
- CNBC Finance (rss, https://www.cnbc.com/id/10000664/device/rss/rss.html, 0.9, TIER_2_MAJOR)
- Reuters (Google RSS, 1.0, TIER_2_MAJOR)
- Bloomberg Markets (rss, feeds.bloomberg.com/markets/news.rss, 1.0, TIER_2_MAJOR)
- Kitco (kitco, https://www.kitco.com/news/, 1.0, TIER_2_MAJOR)
- MarketWatch (rss, feeds.content.dowjones.io/public/rss/mw_bulletins, 0.9, TIER_2_MAJOR)
- Fed Press Releases (rss, https://www.federalreserve.gov/feeds/press_all.xml, 1.2, TIER_1_OFFICIAL)
- FXStreet (rss, 1.0, TIER_2_MAJOR)
- Benzinga (Google RSS, 0.9, TIER_2_MAJOR)
- FT Markets (Google RSS, 1.0, TIER_2_MAJOR)
- Kitco Metals (Google RSS, 1.0, TIER_2_MAJOR)
- FXStreet Gold (Google RSS, 0.9, TIER_2_MAJOR)

Update frequency: Configured via POLL_INTERVAL_SECONDS=180 (3 minutes) by scheduler (APScheduler). See config.py: poll_interval_seconds = 180.
Rate limits/cost: HTTP_TIMEOUT_SECONDS=20. LLM rate-limited by LLM_REQUESTS_PER_MINUTE (40 default). LLM_API_KEY present (NVIDIA NIM) but disabled behavior noted; engine runs heuristic when LLM unavailable. No explicit per-source rate limit shown beyond httpx timeouts; some use Google RSS.

### Fetch/Dedup/Clean/Store
- Fetch: app.ingestion.news_fetcher.fetch_all_news() polls sources concurrently (asyncio.gather), isolated (one dead feed doesn't block). Builds sources via build_sources() parsing NEWS_SOURCES.
- Cleaning: strip_html in base.py removes tags/entities. Body limited by article_fetch_max_chars (4000). Summary/body extraction per source (rss_reader extracts content/summary/description).
- Deduplication: app.analysis.event_deduplication.EventDeduplicator and event clustering (corroboration). GUID computed from url or title (SHA256). Clustered by event_key/cluster_id; 'one cluster = one vote' with capped corroboration boost per independent source (config: corroboration_per_source=0.10, corroboration_max_boost=0.30). Time-window based similarity (DEDUP_SIMILARITY_THRESHOLD=0.72, DEDUP_TIME_WINDOW_HOURS=12).
- Storage: SQLAlchemy models (app.models.db_models) - NewsArticle, AssetImpact, SentimentSnapshot, NewsEvent, ArticleEvent, PipelineRun, Alert, BiasOutcome, DroppedArticle etc. Database URL from config (default sqlite:///./data/intraday_sentiment.db). Retention: NEWS_RETENTION_DAYS=2 (keep today+prev day, purged on pipeline pass).

### Asset Mapping Logic
Assets: Asset enum has GOLD=XAUUSD, SILVER=XAGUSD, BITCOIN=BTCUSD, EQUITY=EQUITY (S&P 500 label). Relevant assets determined by app.analysis.asset_relevance:
- relevance_for(category, asset, text): blends category default + direct mention boost (+0.25 per mention, capped). For EQUITY in OTHER category with mega-cap earnings + earnings event types, can force relevance to 1.0.
- relevant_assets(): returns assets where relevance >= MIN_RELEVANCE (0.20). If none relevant, may return empty list (valid outcome - 'no demonstrable transmission path').
- Direct mentions: GOLD('gold','xau','bullion','comex gold','gold price'), SILVER('silver','xag','silver price'), BITCOIN('bitcoin','btc','xbt','bitcoin price'), EQUITY includes 's&p 500','spx','sp500','nasdaq','ndx','dow jones','dow','wall street','us stocks','stock market','equities','equity market','russell 2000','russell','vix'.

### Timestamp/Timezone Handling
- market_timezone default 'America/New_York', display_timezone 'Asia/Kolkata' (config.py).
- 'TODAY' means market-local day (uses MARKET_TIMEZONE) for today-only filtering. See README and config comments.
- Timestamp parsing: app.utils.timestamp_parse handles multiple formats; timestamps made aware to UTC in DB paths. Pipeline uses rolling window (NEWS_WINDOW_HOURS=18.0, MAX_ARTICLE_AGE_HOURS=26) rather than strict calendar-day gate in recent logic.

## 3. CLASSIFICATION LOGIC

### Method
Hybrid: deterministic heuristic engine (always available) + optional LLM engine (OpenAI-compatible) when llm_enabled and API key configured. Mode recorded on each result (e.g. 'heuristic' vs model-derived). Fallback to heuristics on LLM failures/timeouts.

### LLM Details
- Provider-neutral via app.analysis.llm_client (DeepSeek, NVIDIA NIM, OpenRouter, Groq, Together, Ollama, vLLM supported). Legacy DEEPSEEK_* aliases accepted.
- Config: llm_api_key, llm_base_url, llm_model, llm_timeout_seconds (60 per call default), llm_max_concurrent (2), llm_requests_per_minute (40, rolling window), llm_max_tokens (1024 default), llm_json_mode (True), llm_disable_thinking (True), llm_enabled (True).
- Current .env: LLM_API_KEY set (NVIDIA NIM), model 'meta/llama-3.2-11b-vision-instruct', base_url https://integrate.api.nvidia.com/v1.
- Schema enforced (llm_schema.py), strict JSON, validation + extraction fallback; per-article timeout 60s; circuit breaker after failures; batch cap LLM_BATCH_LIMIT=15 per tick.
- Exact prompt: SYSTEM_PROMPT in app.analysis.news_classifier (see file for full text). Returns structured JSON with category, importance, report_kind, verification, assets with direction/strength/relevance/persistence/reason/transmission etc.

### Heuristic Engine (core)
Key modules: app.analysis.cue_rules (category/importance/report_kind/verification), app.analysis.text_match (word-boundary matching with inflections/stems/plurals), app.analysis.transmission_map (per-asset event-type -> sign+magnitude, resolves per asset - key feature), app.analysis.asset_relevance (routing/relevance), app.analysis.news_classifier.heuristic_analyze() builds ArticleAnalysis with per-asset AssetAnalysis.

Category detection (examples from heuristics/cue_rules): CENTRAL_BANK, INTEREST_RATES, INFLATION, EMPLOYMENT, GDP, TREASURY_YIELDS, USD_DXY, GEOPOLITICS, RISK_SENTIMENT, ETF_FLOWS, COMMODITY_SUPPLY_DEMAND, INDUSTRIAL_DEMAND, CHINA, CRYPTO_REGULATION, BITCOIN_ETF_FLOWS, CRYPTO_MARKET_FLOWS, OIL_ENERGY, EQUITIES, BANKING_FINANCIAL_STRESS, OTHER.

Transmission map (transmission_map.py + transmission_map.json): Event types include HAWKISH_POLICY, DOVISH_POLICY, RECESSION_CUT, RISING_YIELDS, FALLING_YIELDS, STRONG_USD/WEAK_USD, HOT_INFLATION/COOL_INFLATION, STRONG_JOBS/WEAK_JOBS, GEOPOLITICAL_RISK/DEESCALATION, BANKING_STRESS, RISK_ON/RISK_OFF, ETF flows, CRYPTO_REGULATION variants (REGULATION_CLAMP/CLARITY), LIQUIDITY_EASING/TIGHTENING, SILVER_INDUSTRIAL_DEMAND/SILVER_CHINA_DEMAND, GOLD_SILVER_RATIO_HIGH/LOW, plus BTC/GOLD microstructure (funding rates, OI, liquidations, flows), and equity-specific types (EARNINGS_BEAT/MISS, GUIDANCE_RAISED/CUT, INDEX_FLOW, SECTOR_ROTATION, RATE_REPRICING, STOCK_RALLY/PLUNGE, VIX_SPIKE/COMPRESSION, etc.). Each gives per-asset (XAUUSD,XAGUSD,BTCUSD,EQUITY) [sign, magnitude].

Per-asset logic differences (observed):
- Gold (XAUUSD): highly sensitive to Fed/CB policy, real yields (e.g. REAL_YIELDS_FALLING -> very high bullish), safe-haven (geopolitics often bullish gold), central bank buying, ETF flows. Hawkish/Fed-driven often bearish gold (opportunity cost/real yields).
- Silver (XAGUSD): dual nature (precious + industrial). Transmission includes SILVER_INDUSTRIAL_DEMAND, SILVER_CHINA_DEMAND. Industrial/China demand boosts silver; geopolitical/safe-haven also affects but sometimes weaker or different sign relative to gold in specific scenarios (e.g. certain risk-off cases give different magnitudes).
- Bitcoin (BTCUSD): treated distinctly; reacts to monetary policy/liquidity, ETF flows (BTC_ETF_FLOW), regulation variants, liquidations/funding/OI, risk sentiment; often mixed vs metals in some cases. Crypto-specific event types present.
- Equities (EQUITY/S&P 500): has its own event types (earnings/guidance/index flows/sector rotation/rate repricing/VIX). Equity transmission paths distinct; in rate-hike/Fed hawkish often bearish equity (as seen in tests/examples).

Output format: ArticleAnalysis with category, importance (EconomicImportance: VERY_HIGH/HIGH/MEDIUM/LOW), report_kind (GENERAL/PREP/WRAP/ACTUAL_DATA), verification (CONFIRMED/UNVERIFIED), event_title/summary, relevance_score, mode, note, assets (list of AssetAnalysis each with asset, direction (BULLISH/BEARISH/NEUTRAL/MIXED), strength (VERY_HIGH/HIGH/MEDIUM/LOW), relevance, persistence, reason, transmission, metal_character, speaker_tier).

Aggregation into one signal per asset: Per article -> per-asset analyses become WeightedSignal(s) (signal_factory.build_signal). Aggregation via sentiment_engine.compute(signals, asset, now) which computes net score/bias/ confidence over signals (with decay by age, relevance, persistence, source quality, session multiplier, corroboration from clustering, burst bonuses, tie-break rules). Binary bias contract: positive score -> BULLISH, negative -> BEARISH (never returns NO_BIAS as final verdict; NO_BIAS only for compatibility in some cases). Tie-break when |score| < EPS uses heaviest/newest directional signal.

Conflicting news handling: Direction can be MIXED for individual asset in a headline context; aggregation tracks both sides. Contradiction detection (step 8) flags when both sides heavily weighted. Corroboration caps multi-source repetition ('one cluster = one vote'). Preview suppression for scheduled releases (ACTUAL_DATA). Impact gate (config only_high_impact_counts) can drop MEDIUM/LOW impact signals at weight choke point.

### Scheduled Releases (ACTUAL_DATA)
- Economic calendar ingestion: app.ingestion.economic_calendar.fetch_calendar() (or related). app.analysis.release_surprise.parse_all() extracts prints (actual/forecast/previous) and determines direction from surprise vs expectation (not wording). When release is ACTUAL, direction comes from surprise; combines multiple prints per asset (release_surprise.py). Report kind becomes ACTUAL_DATA.
- Calendar signals integrated into pipeline (economic_impact module) - per-asset directional signals from scheduled releases with transmission chains.

## 4. MARKET CONTEXT & TRADING USE

### Price Data / Calendar / Macro
- Economic calendar: app.ingestion.economic_calendar, app.analysis.economic_impact. Supports parsing calendar events, deriving calendar signals, risk assessment (calendar_risk).
- Price data: app.market.price_data present. Market prep/wrap and session detection (app.market.session_detector) handle sessions (ASIA/EUROPE/US/OFF_HOURS), next/prev session, equity session/title.
- Macro references in transmission map/config: USD/DXY, yields/treasury, inflation (CPI/PCE), employment (NFP), FOMC/CB, VIX, etc. Present.
- DXY/yields explicitly modeled (USD_DXY category, TREASURY_YIELDS, real yields event types).

### Sentiment -> Price Link
- Has decision logging (app.analysis.decision_log) and hit-rate analysis tool (tools/hitrate_report.py) to measure whether calls predicted price moves after N minutes. Tools exist to compute hit rates by confidence buckets/assets.
- Does not claim prediction as core - README states 'This is news-based sentiment, not a price prediction'.

### Delivery
- API + web dashboard (FastAPI on port 8000 serves JSON API, WebSocket, and HTML dashboard at app.dashboard.web). Streamlit UI also available.
- Alerts: app.services.alerts - fires on VERY_HIGH economic importance or VERY_HIGH impact strength; records estimated sentiment delta.
- Dashboard shows Market Prep/Market Wrap, news feed, per-asset sentiment (score/bias/confidence), drivers, burst, conflict indicators.

### Risk Filters / Weighting / Time Decay
- Source quality tiers (TIER_1_OFFICIAL..TIER_4_OTHER) with multipliers.
- Importance weighting (EconomicImportance). Strength bands (ImpactStrength).
- Recency/time decay: decay by age_hours / half-life per tier/asset (recency_engine, decay tables in config). Half-lives scaled by ASSET_DECAY_MULTIPLIER. Persistence derived from event type (not cue magnitude).
- Corroboration (capped), session multipliers (per asset/session), impact gate (only_high_impact_counts), speaker tiering, text quality filters (negation/certainty/priced-in/price-action).

## 5. TESTING & VALIDATION

### Tests
- Extensive test suite under tests/ (1469 passed, 1 deselected per pytest run shown earlier).
- Cover transmission map, text quality (negation/inversion/price-action), event clustering/corroboration, sentiment engine (binary bias), release surprise, equity card, pipeline counters, overnight, rate limiting, etc.
- Tests enforce hermetic DB (temp DB in conftest).

### Backtests/Validation
- Hit-rate tooling: tools/hitrate_report.py - measures whether sentiment call predicted price moves after N minutes, broken down by confidence buckets and by asset. Can load price CSV and compute hit rates.
- Decision logging present (app.analysis.decision_log). No prepackaged full backtest dataset visible in obvious files, but infrastructure for validation exists.

### Logging/Monitoring/Error Handling/Retries
- Logging configured via app.logging_conf (setup_logging, get_logger). LOG_LEVEL configurable, LOG_FILE path.
- Error handling: sources isolated (one failure doesn't block), LLM failures fall back to heuristics, circuit breaker on consecutive LLM failures, batch cap. Pipeline catches exceptions in scheduled ticks.
- Retries: tenacity (v9.0.0 in requirements) present; also retry logic in LLM client (backoff mentioned in code). HTTP timeouts, per-article LLM timeout (60s).
- Monitoring: /api/health endpoint (main.py), pipeline stats tracked (IngestStats), health metrics cached, hung-tick detection (MAX_TICK_DURATION_SECONDS=600).

## 6. KNOWN PROBLEMS

### Bugs/TODO/FIXME
- Grep for TODO/FIXME/XXX/HACK: No obvious TODO/FIXME strings found in core app code in quick scan (common in production codebase). Comments present explaining design decisions.

### Hardcoded Values / API Keys
- API keys: LLM_API_KEY present in .env (real-looking key for NVIDIA NIM). .env is not gitignored in obvious way? Check .gitignore - .env present in .gitignore? Let us look: .gitignore contains '.env' (line shows in typical form). But .env file exists locally with key. Key is in local .env only - not in source code.
- Hardcoded defaults throughout config (multipliers, thresholds) - by design/config-driven.

### Performance/Fragile Parsing
- Feedparser used (can be slow on huge feeds) but runs in to_thread. HTTP calls concurrent. DB writes batched via pipeline. SQLite with WAL pragmas configured.
- HTML scraping optional (playwright). Fragile parsing noted as design tradeoff for multiple sources.

### Known Weak Spots (from design)
- Sarcasm/negation: Handled by text_quality (negation rules, certainty, priced-in, price-action) - attempts to catch negation like 'no rate cut', 'rules out' etc. Not perfect for complex sarcasm.
- Phrases like 'beats expectations but guidance weak': Handled by transmission map + cue rules + text quality; direction resolved per asset based on detected event types. But mixed signals can lead to MIXED or conflict detection.
- Asset ambiguity (e.g. 'gold' in non-commodity contexts): text_match uses word-boundary matching; asset_relevance requires direct mentions and relevance >= 0.20 - prevents weak/unrelated matches. Also mega-cap earnings handling for EQUITY in OTHER.
- 'credit' vs 'credits', 'ban' vs 'bank' etc: text_match prevents substring false positives (strict whole-word matching, plural exemptions limited, stems explicit).

## 7. SAMPLE I/O

Ran examples through heuristic_analyze() to show actual behavior:

1. **Headline:** 'Fed signals fewer rate cuts in 2025'
   - Category: Central Bank (via detect_category)
   - Importance: VERY_HIGH (from cue rules)
   - Detected event types: HAWKISH_POLICY (dominant), DOVISH_POLICY also detected in example shown
   - XAUUSD: direction BEARISH, strength VERY_HIGH, relevance 0.90
   - XAGUSD: BEARISH, VERY_HIGH, 0.88
   - BTCUSD: BEARISH, VERY_HIGH, 0.86
   - EQUITY: BEARISH, VERY_HIGH, 0.90
   - Reasoning: Transmission via transmission_map (hawkish policy -> bearish metals/crypto/equities). Magnitude scaled per asset.

2. **Headline:** 'Geopolitical tensions escalate, safe haven demand rises'
   - XAUUSD: BULLISH, HIGH, 0.82 (safe-haven bid)
   - XAGUSD: BEARISH, HIGH, 0.70
   - BTCUSD: BEARISH, HIGH, 0.66
   - EQUITY: BEARISH, HIGH, 0.60
   - Shows per-asset divergence (gold up on safe-haven, others down on risk-off/liquidity concerns).

3. **Headline:** 'Strong NFP beats expectations'
   - XAUUSD: BEARISH, HIGH, 0.78
   - XAGUSD: BEARISH, HIGH, 0.72
   - BTCUSD: BEARISH, HIGH, 0.76
   - EQUITY: BULLISH, HIGH, 0.85
   - Divergent: strong labor -> hawkish path bearish metals/crypto, but equity reaction context gives bullish equity in this mapping (transmission differs by asset).

4. **Headline:** 'Bitcoin ETF sees massive inflows'
   - XAUUSD: MIXED, HIGH, 0.86
   - XAGUSD: MIXED, HIGH, 0.80
   - BTCUSD: BULLISH, HIGH, 0.80
   - EQUITY: MIXED, HIGH, 0.70
   - BTC-specific flow drives BTC bullish; others less directly affected (MIXED).

5. **Headline:** 'Real yields fall sharply on dovish Fed comments'
   - XAUUSD: BULLISH, VERY_HIGH, 0.92
   - XAGUSD: BULLISH, VERY_HIGH, 0.84
   - BTCUSD: BULLISH, VERY_HIGH, 0.82
   - EQUITY: BULLISH, VERY_HIGH, 0.85
   - Real yields falling is strongly bullish for precious metals (opportunity cost down). Transmission uses REAL_YIELDS_FALLING event type.

Additional example (silver-specific): 'China industrial output surges boosting silver demand' -> XAGUSD BULLISH (MEDIUM), others mixed/lower relevance. Core classification logic: detect_category -> relevant_assets -> resolve per-asset via transmission_map (event types + magnitudes) -> build AssetAnalysis with strength derived from cue magnitude/relevance/importance -> aggregate via sentiment_engine with weighting/decay/corroboration.

## 8. SELF-ASSESSMENT

### Top 10 Weaknesses Ranked by Impact on Trading Accuracy
1. **Complex sarcasm/ironic tone** (High) - text_quality handles explicit negation/priced-in cues but subtle sarcasm can misread direction.
2. **Mixed headlines with multiple clauses** (High) - 'beats but guidance weak' can be hard; transmission map picks dominant event types but edge cases exist.
3. **Asset ambiguity** (Med-High) - 'gold' in company names/context rare but possible; word-boundary + relevance>=0.20 mitigate but not eliminate.
4. **News vs price action conflation** (Med-High) - price-action detection tries to suppress 'gold plunges 2%' as merely reporting move; can be tricked by phrasing.
5. **Source quality variance** (Med) - Google RSS and mixed sources; tiering helps but speed vs quality tradeoff.
6. **LLM availability/cost** (Med) - falls back to heuristics when unavailable; heuristics solid but LLM could catch nuance.
7. **Timeliness of breaking news** (Med) - 3-min poll interval; some ultra-fast moves may lag.
8. **Cross-asset bleed complexity** (Med) - explicit bleed signals added; tuning can be delicate.
9. **Earnings nuance** (Med) - guidance vs beat distinction handled via event types but can misclassify in complex reports.
10. **Non-English/abbreviations** (Lower) - primarily English-focused sources.

### Missing vs Professional News-Sentiment Systems
- **Named Entity Recognition (NER)**: More explicit entity-asset linking beyond keyword matching could improve precision.
- **Event extraction**: Could extract structured events (who/what/when) beyond categories.
- **Real-time price context integration**: Could compare vs price action to filter stale/contradictory reports.
- **Historical calibration**: More extensive backtesting with labeled datasets; hit-rate tooling exists but labeled corpora not obvious.
- **Multilingual support**: English-only.
- **Social/sentiment from alternative sources** (e.g. Twitter/X, Reddit) - not present; focus is on news wires.
- **More granular confidence calibration**: Confidence formula exists; continuous improvement possible.
- **Order book/liquidity signals**: Not integrated (news-focused).
- **Better handling of revisions/corrections**: Dedup/clustering helps but explicit correction detection limited.
- **News impact curves**: Time-decay exists; more sophisticated impact decay models could help.

---

**Core classification files referenced:** app/analysis/news_classifier.py, app/analysis/transmission_map.py (+ .json), app/analysis/asset_relevance.py, app/analysis/text_match.py, app/analysis/cue_rules.py, app/analysis/text_quality.py, app/analysis/sentiment_engine.py, app/analysis/signal_factory.py, app/analysis/release_surprise.py, app/models/enums.py, app/config.py
