# Current Sentiment Engine — As-Built Specification

**Scope:** what the code in `C:\Users\HP\.vscode\intraday_sentiment_engine` does today.
**Audit date:** 2026-10-22. **Method:** static read of source only. No file was modified.
**Citation convention:** every claim cites `file:line`. Snippets marked `[verbatim from file:line]` are pasted as they appear.

> **Note on `docs/SCORING.md`:** that document states "Only **three** assets are scored: XAUUSD, XAGUSD, BTCUSD" (`docs/SCORING.md:30`). That is **stale**. `Asset.OIL = "USOIL"` is a live member of the enum (`app/models/enums.py:21`) and IS scored and persisted (`app/services/pipeline.py:2030`, `:2103-2156`). Where this document and `SCORING.md` disagree, this document wins; both are cited.

---

## Section 1: System Overview

### 1.1 Entry point that produces a sentiment score

There are three layers. The **producer** is the pipeline tick; the **calculator** is one function.

**Producer — `app/services/pipeline.py::run_once()`** (`app/services/pipeline.py:2536`)

One full pass: fetch → classify → persist → recompute sentiment. It is invoked from four places:

| Trigger | Code | Frequency |
|---|---|---|
| APScheduler job `intraday_pipeline` | `app/main.py:247-254` → `_scheduled_tick` `app/main.py:71` → `asyncio.to_thread(_run_pipeline_sync)` `app/main.py:86` | every `poll_interval_seconds` (180s live) |
| Boot sequence | `app/main.py:149-185` `_boot_sequence()` → `_scheduled_tick()` then `_boot_morning_brief()` `app/main.py:175-177` | once per process start |
| `POST /api/run` | `app/main.py:1050-1062` | on request |
| CLI `python run.py --once` | `run.py:258-269` | on request |

**Calculator — `app/analysis/sentiment_engine.py::compute()`** (`app/analysis/sentiment_engine.py:1470`)

```python
def compute(signals: Sequence[WeightedSignal], asset: Asset,
            now: datetime | None = None,
            event_risk: bool = False,
            event_risk_note: str = "",
            history_key: str | None = None,
            baseline: float | None = None) -> SentimentResult:
```
`[verbatim from app/analysis/sentiment_engine.py:1470-1475]`

Called from two places:
- `app/services/pipeline.py:2083-2087` — once per asset, per tick.
- `app/analysis/equity_card.py:475-477` — once per tick for the session-aware equity card.

**Wrapper — `app/analysis/sentiment_engine.py::score_from_signals()`** (`:1876`) exists but is **never called** (see Section 7).

### 1.2 Output format

**In-process result object** — `SentimentResult` dataclass (`app/analysis/sentiment_engine.py:673-749`):

| Field | Type | Range / domain |
|---|---|---|
| `score` | float | −100.0 .. +100.0 (label-consistent; see §3.8) |
| `raw_score` | float | −100.0 .. +100.0 (unflipped net lean, audit only) |
| `baseline_score` | float \| None | the asset's trailing baseline used |
| `bull_weight` / `bear_weight` | float | ≥ 0.0, aggregate weight totals |
| `damped_score` | float | −100 .. +100, display-only damping |
| `confidence` | float | 0.0 .. 0.95 (clamped at `:1334`) |
| `bias` | `Bias` | `BULLISH` \| `BEARISH` (binary; `NO_BIAS` exists only for legacy/display — `:116-127`) |
| `mode` | str | `"llm"` \| `"heuristic"` \| `"mixed"` |
| `drivers` / `bull_drivers` / `bear_drivers` | list[Driver] | max 3 each |
| `conflict` | Driver \| None | strongest opposing headline |
| `delta_60m` | float | score movement over the last 60 min |
| `event_risk` / `event_risk_note` | bool / str | imminent high-impact release |
| `contributing_articles` | int | signals that survived the gate |
| `contributing_clusters` | int | distinct cluster ids |
| `signal_count` | int | signals that reached the asset |
| `stale` | bool | verdict carried forward |
| `cluster_conflict` / `evidence_split` | bool | the two split flags |
| `burst_bull` / `burst_bear` | int | same-side headlines in the last 15 min |
| `note` | str | human-readable reason for the verdict |
| `confidence_label` | str | `LOW` \| `MEDIUM` \| `HIGH` |

**Persisted** — `SentimentSnapshot` (`app/models/db_models.py:155-247`), one row per asset per tick, **scope = `"CURRENT"` only** (`app/services/pipeline.py:2103-2105`).

**Served** — `app/main.py::_block()` (`app/main.py:377-518`) returns the wire dict. Exact keys:

```python
{
  "asset": str, "scope": str,
  "score": float, "raw_score": float, "baseline_score": float|None,
  "bull": float|None, "bear": float|None, "damped_score": float,
  "confidence": float, "bias": str,
  "display": {"label": str, "pct": int|None, "bull_pct": int, "bear_pct": int},
  "bias_since_utc": str|None, "mode": str, "delta_60m": float,
  "event_risk": bool, "event_risk_note": str,
  "drivers": [...], "bull_drivers": [...], "bear_drivers": [...], "conflict": {...}|None,
  "stale": bool, "confidence_label": str, "note": str,
  "contributing_articles": int, "contributing_clusters": int, "signal_count": int,
  "contradiction_detected": bool, "cluster_conflict": bool, "evidence_split": bool,
  "burst_bull": int, "burst_bear": int,
  "price_note": str, "last_updated": str,
}
```
`[structure from app/main.py:461-518]`

**API endpoints** (`app/main.py`):

| Endpoint | Line | Returns |
|---|---|---|
| `GET /api/health` | `:782-854` | liveness + staleness; 503 when stale |
| `GET /api/dashboard` | `:857-861` | full payload via `build_payload` (`:625-776`) |
| `GET /api/sentiment/{asset}` | `:924-936` | `market_prep` / `market_wrap` / `current` blocks |
| `GET /api/news` | `:905-921` | paged news feed |
| `GET /api/alerts` | `:939-951` | alerts raised today |
| `GET /api/financialjuice/sessions` | `:864-902` | **legacy** FJ five-box scorer (see §7) |
| `GET/POST /api/calibration/labels` | `:975-1047` | manual outcome labels |
| `POST /api/run` | `:1050-1062` | trigger one tick |
| `POST /api/engine/pause` / `resume` | `:1065-1112` | pause/resume the scheduler |
| `GET /api/console` | `:1115+` | plain-text rendering |

**Important:** `/api/sentiment/{asset}` serves `MARKET_PREP` and `MARKET_WRAP` blocks (`app/main.py:933-934`), but **no row is ever written with those scopes** (the write loop is `for scope, result in (("CURRENT", current_result),)` — `app/services/pipeline.py:2103-2105`). Those two blocks therefore always return the "no sentiment computed yet" empty state from `app/main.py:390-414`.

### 1.3 Frequency

- **Polling:** APScheduler `IntervalTrigger(seconds=settings.poll_interval_seconds)` (`app/main.py:249`), job id `intraday_pipeline`, `max_instances=1`, `coalesce=True`, `misfire_grace_time=120` (`app/main.py:250-253`).
- **Live value:** `POLL_INTERVAL_SECONDS=180` in `.env`; declared default `180` at `app/config.py:109`.
- **Event-driven:** none for sentiment. The scheduler is paused/resumed around the boot sequence and by the operator pause (`app/main.py:125-146`).
- **Streaming:** the API exposes a WebSocket (`app/main.py:18`, `_clients` at `:63`), but sentiment is computed on the poll, not pushed per headline.
- **Overnight:** the "morning brief" runs once at boot (`app/main.py:103-122`, `app/services/overnight.py`), LLM-judging everything published while the process was down.

### 1.4 Assets and internal mapping

Enum `Asset` (`app/models/enums.py:8-35`):

| Enum | Value | Label | Scored into `SentimentSnapshot`? | Dashboard card |
|---|---|---|---|---|
| `GOLD` | `XAUUSD` | Gold | **Yes** | yes |
| `SILVER` | `XAGUSD` | Silver | **Yes** | yes |
| `BITCOIN` | `BTCUSD` | Bitcoin | **Yes** | yes |
| `EQUITY` | `EQUITY` | S&P 500 | **No** — handled by the session-aware card (`app/services/pipeline.py:2030-2039`) | top-level `"equity"` key |
| `OIL` | `USOIL` | Oil | **Yes** | yes |

Internal → external symbol mapping (TradingView chart feed), `app/ingestion/tradingview_api.py:67-76`:

```python
DEFAULT_SYMBOL_MAP: dict[str, str] = {
    "XAUUSD": "OANDA:XAUUSD",
    "XAGUSD": "OANDA:XAGUSD",
    "BTCUSD": "BITSTAMP:BTCUSD",
    "USOIL": "TVC:USOIL",
}
```
`[verbatim from app/ingestion/tradingview_api.py:67-76]`

Equity session symbol mapping (`app/ingestion/tradingview_api.py:81-85`):

```python
EQUITY_SESSION_SYMBOLS: dict[str, list[str]] = {
    "asia": ["NASDAQ:TSLA", "NASDAQ:NVDA"],
    "europe": ["NASDAQ:AAPL", "NYSE:JPM"],
    "us": ["NASDAQ:MSFT", "AMEX:SPY"],
}
```
`[verbatim from app/ingestion/tradingview_api.py:81-85]`

Live `.env` `ASSETS=XAUUSD,XAGUSD,BTCUSD,USOIL` — note this **omits `EQUITY`** from the TradingView metals feed (equity has its own source, `TradingView Equity News`). Declared default is `XAUUSD,XAGUSD,BTCUSD,EQUITY,USOIL` (`app/config.py:408`).

---

## Section 2: Data Inputs

### 2.1 Sources actually wired in

All news sources come from `NEWS_SOURCES` in `.env` (parsed by `app/ingestion/base.py:91-117` into `SourceSpec`), and are instantiated by `app/ingestion/news_fetcher.py::build_sources()` (`:41-74`). The `kind` field selects the backend class.

Live feed list, exactly as configured in `.env` (`NEWS_SOURCES=`, one line):

| # | Name | kind | Backend | Quality tier | Weight |
|---|---|---|---|---|---|
| 1 | CNBC Economy | rss | `RssSource` | TIER_2_MAJOR | 1.0 |
| 2 | Investing.com | rss | `RssSource` | TIER_4_OTHER | 0.8 |
| 3 | CoinDesk | rss | `RssSource` | TIER_3_CRYPTO | 0.9 |
| 4 | Financial Juice | fj | `FinancialJuiceSource` | TIER_2_MAJOR | 0.9 |
| 5 | CNBC Finance | rss | `RssSource` | TIER_2_MAJOR | 0.9 |
| 6 | Reuters | rss (Google News proxy) | `RssSource` | TIER_2_MAJOR | 1.0 |
| 7 | Bloomberg Markets | rss | `RssSource` | TIER_2_MAJOR | 1.0 |
| 8 | Kitco | kitco | `KitcoSource` | TIER_2_MAJOR | 1.0 |
| 9 | MarketWatch | rss | `RssSource` | TIER_2_MAJOR | 0.9 |
| 10 | Fed Press Releases | rss | `RssSource` | TIER_1_OFFICIAL | 1.2 |
| 11 | FXStreet | rss | `RssSource` | TIER_2_MAJOR | 1.0 |
| 12 | Benzinga | rss (Google News proxy) | `RssSource` | TIER_2_MAJOR | 0.9 |
| 13 | FT Markets | rss (Google News proxy) | `RssSource` | TIER_2_MAJOR | 1.0 |
| 14 | Kitco Metals | rss (Google News proxy) | `RssSource` | TIER_2_MAJOR | 1.0 |
| 15 | FXStreet Gold | rss (Google News proxy) | `RssSource` | TIER_2_MAJOR | 0.9 |
| 16 | TradingView API News | tradingview_api | `TradingViewAPISource` | TIER_2_MAJOR | 1.0 |
| 17 | TradingView Equity News | tradingview_api | `TradingViewAPISource` | TIER_2_MAJOR | 1.0 |

Other source kinds implemented but **not** in the live `.env` list: `atom`, `html`/`scrape` (`WebsiteSource`, `app/ingestion/website_monitor.py`), `mock` (`app/ingestion/mock_source.py`, enabled by `USE_MOCK_SOURCE`, currently `false`).

**Economic calendar:** one provider is implemented — FinancialJuice, via a headless Chromium render (`app/ingestion/economic_calendar.py:102-115` → `app/ingestion/fj_calendar.py`). `.env` sets `CALENDAR_PROVIDERS=financialjuice,faireconomy`, but **there is no `faireconomy` code anywhere in `app/`** — `fetch_calendar()` calls only `_fetch_fj()` (`app/ingestion/economic_calendar.py:125-127`). Declared default is `"financialjuice"` (`app/config.py:577`).

**Price feed:** Coinbase spot, **BITCOIN only** (`app/market/price_data.py:33`):

```python
_COINBASE_PRODUCTS = {Asset.BITCOIN: "BTC-USD"}
```
`[verbatim from app/market/price_data.py:33]`

**NOT wired in:** the `.env` has a large `CoinGlass` block (`COINGLASS_ENABLED`, `COINGLASS_POLL_SECONDS`, `COINGLASS_BTC_URL`, `COINGLASS_ASSETS=…`). There is **no CoinGlass module in `app/`** — only a browser profile directory at `data/coinglass_profile/`. `COINGLASS_ENABLED` and `COINGLASS_POLL_SECONDS` are not fields on `Settings` (`app/config.py` has no such names) and `Settings.model_config` sets `extra="ignore"` (`app/config.py:37`), so they are silently ignored.

### 2.2 Ingestion frequency

| Feed | Cadence | Evidence |
|---|---|---|
| All 17 news sources | every tick (180s), all polled **concurrently** via `asyncio.gather` | `app/ingestion/news_fetcher.py:97-99` |
| TradingView per-symbol calls | ≥500 ms apart, in-process 5-minute cache | `app/ingestion/tradingview_api.py:52`, `:189-205` |
| Economic calendar | own cadence `FJ_CALENDAR_POLL_SECONDS=600` | `app/services/pipeline.py:2512-2533`, `app/config.py:603` |
| Economic calendar (burst) | `FJ_CALENDAR_BURST_SECONDS=60` inside a HIGH-impact release window (±15 / ±30 min) | `app/services/pipeline.py:2485-2509`, `app/config.py:606-608` |
| Price points (BTC) | once per tick, outside the write transaction | `app/services/pipeline.py:2859`, `:2450-2482` |
| LLM classification | concurrent, bounded by count (15) and wall-clock (90s) per tick | `app/services/pipeline.py:2686-2688`, `app/config.py:452-454` |

### 2.3 Filters applied before scoring

**A. Time / recency filters (ingest, `app/services/pipeline.py:1149-1206`)**

| Filter | Rule | Location | Live value |
|---|---|---|---|
| Missing timestamp | reject | `pipeline.py:1176-1180` | — |
| Rolling window | age ≤ `news_window_hours` | `app/utils/timestamp_parse.py:151-154` | 24h (`.env`), default 18.0 (`config.py:201`) |
| Max age | age ≤ `max_article_age_hours` | `app/utils/timestamp_parse.py:166-170` | 26h (`config.py:207`) |
| Clock skew | reject if >2h in the future | `app/utils/timestamp_parse.py:148-149`, `:168` | — |
| FJ session IST rule | extra IST-date admission test | `pipeline.py:1192-1206` | — |

**B. Market-day filter (read side)** — `pipeline.news_day_predicate()` (`app/services/pipeline.py:2245-2305`): `published_at_utc >= market_day_start_utc` **OR** source is FinancialJuice **OR** the row falls inside the overnight admission window, all ANDed with `is_today IS TRUE`.

**C. One-shot low-impact deletion** — `app/analysis/news_classifier.py::is_section_b_verdict()` (`:136-174`). Keep if `importance ∈ {HIGH, VERY_HIGH}` OR any per-asset `strength ∈ {HIGH, VERY_HIGH}`; otherwise delete the row, its events and impacts, and suppress its guid for the trading day (`pipeline.py:1271-1281`, `_drop_low_impact_article` `pipeline.py:675`). Exempt: FinancialJuice session reports; chart-scoped stories; and any row the model did not judge when `only_llm_verdict_may_drop=True` (`config.py:173`).

**D. Impact gate** — `sentiment_engine._passes_impact_gate()` (`:172-190`). With `only_high_impact_counts=True`, only `VERY_HIGH` and `HIGH` strength reach a card (`STRENGTH_GATE`, `:169`). `MEDIUM` survives only for chart-scoped stories when `chart_scope_medium_counts=True`.

**E. Relevance floor** — `asset_relevance.MIN_RELEVANCE = 0.20` (`app/analysis/asset_relevance.py:89`). An asset below it is not analysed at all (`relevant_assets`, `:152-168`).

**F. Chart scope isolation** — `pipeline._chart_scope_for()` (`:1375-1397`), applied by `news_classifier.apply_chart_scope()` (`:435-475`). A story listed on the gold chart can only reach the gold card.

**G. Oil false friends** — `asset_relevance.OIL_FALSE_FRIENDS` (`app/analysis/asset_relevance.py:80-86`), e.g. `olive oil`, `palm oil`, `cooking`, `recipe`, `restaurant menu`. A match forces OIL relevance to `0.0` (`asset_relevance.py:135-136`).

**H. Calendar region whitelist** — `calendar_region_filter_enabled=True` (`config.py:574`). Regions, currencies, `include_keywords` and `exclude_keywords` live in `app/config/calendar_bias.json` (USA / EUR / CNY / … ), applied at `app/ingestion/economic_calendar.py:163`.

**I. Unreleased prints do not score** — `calendar_upcoming_contributes=False` (`config.py:701`); enforced at `pipeline.py:1878-1881`. Only `mode == "RELEASED"` calendar signals enter the aggregate.

**J. FJ session-report asset restriction** — `fj_sessions.session_report_asset_allowed()` (`app/analysis/fj_sessions.py:293`), applied at `pipeline.py:1781-1788`.

**K. Language filter** — only the TradingView API applies one: `DEFAULT_FILTERS = ["lang:en"]` (`app/ingestion/tradingview_api.py:34`). No other source or stage filters language.

**L. Preview suppression** — when an `ACTUAL_DATA` print exists for an indicator family + date, matching `PREP`/`GENERAL` signals get `relevance = 0` (`pipeline.py:1480-1515`).

**M. Calendar-duplicate down-weight** — news matching a released calendar print (same asset, direction, indicator family, within 60 min) has `relevance *= CALENDAR_NEWS_DUP_FACTOR` (0.35, `pipeline.py:288`, applied `:1518-1588`).

### 2.4 Deduplication — exact algorithm

**Layer 1 — ingest-level event dedup** (`app/analysis/event_deduplication.py`)

- Tokenisation (`event_deduplication.py:39-42`): lowercase → strip numbers (`_NUM_RE`) → strip punctuation → keep words of length > 2 that are not in `_STOPWORDS` (`:24-31`).
- Similarity (`:45-59`): `score = 0.5 * jaccard + 0.5 * containment`, where containment = `|A∩B| / min(|A|,|B|)`.
- Threshold: `settings.dedup_similarity_threshold = 0.72` (`config.py:140`).
- Window: `settings.dedup_time_window_hours = 12` (`config.py:141`), pruned against **the tick's clock**, not the article's own timestamp (`event_deduplication.py:88-111`).
- New event key: `f"{published_at.timestamp():.0f}-{abs(hash(frozenset(tokens))):x}"` (`:134`).
- The index is in-memory (`_events` list) and is reseeded from the DB once per process (`pipeline.seed_dedup_index_from_db`, `pipeline.py:591`).

**Layer 2 — scoring-time clustering** (`app/analysis/event_clustering.py::cluster_signals`, `:473-634`)

- **Identity first**: signals sharing an `event_key`/`cluster_id` collapse with **no time window** (`:536-545`). The group key is the event alone (not event+direction), so opposing readings of one event collapse to the dominant direction and raise `cluster_conflict` (`:530-535`, `_collapse_members` `:253`).
- **Fallback** for identity-less signals: title token Jaccard + `0.15 × shared macro entities`, threshold `0.72` (`:584-618`); window 30 min by default, 90 min for `*_minutes` and `release:*` anchors (`:504-506`, `:573-580`).
- **One vote per cluster**: the highest-weight member survives (`:544-545`, `:626-627`).
- **Capped corroboration** (`corroboration_multiplier`, `:149-165`): `boost = min(cap, Σ_{i=2..n} per / log2(i+1))`, `per = 0.10`, `cap = 0.30` (`config.py:146-147`), applied only for **independent** sources.
- **Same-source repeat collapse** (`_collapse_same_source_repeats`, `:415`).

**Layer 3 — equity card cross-asset collapse** (`app/analysis/equity_card.py::_collapse_cross_asset`, `:169-264`): by `event_key` → `cluster_id` → normalised title within 30 min, and by direction.

**Layer 4 — GUID dedup**: `news_article.guid` is `UNIQUE` (`app/models/db_models.py:35`). The guid is `sha256(url or title).lower()[:32]` (`app/ingestion/base.py:41-43`); TradingView uses the API's `news_id` (`app/ingestion/tradingview_api.py:448`).

**Layer 5 — one-shot suppression**: `dropped_article` table holds deleted guids for the trading day (`pipeline.py:634`, `:654`).

---

## Section 3: Scoring Logic (Step by Step)

### 3.0 Stage 0 — classification (the only "model" stage)

There is **no FinBERT, no VADER, no local checkpoint**. The classifier has two engines, selected per article:

**Engine A — LLM (primary).** `app/analysis/news_classifier.py::llm_analyze()` (`:588-672`).
- Endpoint: OpenAI-compatible. Live: `https://integrate.api.nvidia.com/v1`, model `meta/llama-3.2-11b-vision-instruct` (`.env` `LLM_BASE_URL` / `LLM_MODEL`).
- Prompt: `SYSTEM_PROMPT` at `app/analysis/news_classifier.py:482-551`.
- Response schema: `app/analysis/llm_schema.py:74-93` (`LLMAnalysisSchema`), validated by `validate_llm_payload` (`llm_schema.py:138-150`). Unknown asset names are dropped, not coerced (`:97-135`). `assets: []` is valid.
- Raw output range: per-asset `magnitude` **0.0–1.0** (`llm_schema.py:46`), `relevance_score` 0.0–1.0, `importance` ∈ {VERY_HIGH, HIGH, MEDIUM, LOW}.
- Mapping to internal bands: `magnitude → strength` at `news_classifier.py:566-574` (≥0.75 VERY_HIGH, ≥0.50 HIGH, ≥0.25 MEDIUM, else LOW); `horizon → persistence` at `:577-580` (intraday 0.45, session 0.75, multi_day 0.95).
- **The model does not set relevance.** Relevance always comes from the category matrix + explicit mention (`news_classifier.py:635-640` → `asset_relevance.relevance_for`).
- One retry, then fallback: `for attempt in range(2)` (`news_classifier.py:608`), then `raise RuntimeError` (`:672`) caught by `analyze_article` (`:692-714`).

**Engine B — deterministic heuristics (always available).** `news_classifier.heuristic_analyze()` (`:298-366`).
- Cue tables: `BULLISH_CUES` (`app/analysis/cue_rules.py:35-64`), `BEARISH_CUES` (`:65-95`).
- Category keywords: `heuristics.CATEGORY_KEYWORDS` (`app/analysis/heuristics.py:9-84`).
- Importance triggers: `VERY_HIGH_TRIGGERS` (`heuristics.py:119-130`), `HIGH_TRIGGERS` (`:138-140`), `LOW_TRIGGERS` (`:145-147`).
- Direction resolution: `transmission_map.resolve()` (`app/analysis/transmission_map.py:1099-1237`).

**Selection logic** — `analyze_article()` (`news_classifier.py:675-716`):
1. If no LLM key → heuristic (`has_llm`, `app/config.py:901-904`).
2. If the circuit breaker is open → heuristic, reason `circuit_breaker`.
3. Else call the LLM with a per-article 90s wall-clock (`LLM_PER_ARTICLE_TIMEOUT = 90`, `news_classifier.py:60`).
4. Timeout / error → heuristic fallback.
5. Per tick, at most `llm_batch_limit` (15) articles are sent, and only for `llm_tick_budget_seconds` (90s) of wall clock; the rest get heuristics (`pipeline.py:2649-2729`).

### 3.1 Preprocessing

There is **no tokenizer, no stopword list, no stemmer** in the scoring path beyond the regex phrase matching in `app/analysis/text_match.py`. The four text-quality passes are:

**(a) Negation** — `app/analysis/text_quality.py::neutralise_on_negation()` (`:102-120`), driven by `NEGATION_RULES` (`:31-59`):

```python
NEGATION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no", ("rate cut", "rate hike", "recession", "default", "sanction", "stimulus")),
    ("rules out", ("rate cut", "rate hike", "recession")),
    ("ruled out", ("rate cut", "rate hike", "recession")),
    ("unlikely to", ("cut", "hike", "raise", "lift", "tighten")),
    ("unlikely", ("recession", "to rise", "to fall")),
    ("dismissed", ("rate cut", "rate hike")),
    ("denies", ("rate cut", "rate hike", "default")),
    ("denied", ("rate cut", "rate hike", "default")),
    ("not expected", ("cut", "hike", "raise", "lift", "tighten")),
    ("no longer", ("cut", "hike", "expected")),
    ("hopes fade", ("rate cut", "cut")),
    ("won't", ("cut", "hike", "raise", "lift", "tighten")),
    ("will not", ("cut", "hike", "raise", "lift", "tighten")),
    ("without", ("a rate cut", "a rate hike", "approval")),
    ("abandoned", ("rate cut", "rate hike")),
    ("scrapped", ("rate cut", "rate hike")),
    ("on hold", ("rate cut", "rate hike")),
    ("rate cut hopes fade", ()),
    ("cut hopes fade", ()),
    ("hopes of a cut fade", ()),
    ("did not", ("raise", "lift", "tighten", "hike", "increase")),
    ("didn't", ("raise", "lift", "tighten", "hike", "increase")),
    ("not raise", ("rates",)),
    ("not hike", ("rates",)),
)
```
`[verbatim from app/analysis/text_quality.py:31-59]`
Reach = `negation_window_tokens` = 4 (`config.py:287`), bounded by sentence/clause boundaries (`text_quality.py:96`).

**(b) Certainty / hedge** — `certainty_multiplier()` (`text_quality.py:138-156`). `HEDGE_CUES` (`:126-131`): reportedly, sources say, could, might, would, rumor, rumour, allegedly, unconfirmed, expected to, is said to, are said to, in talks, considering, weighing, mulling, speculation. Multiplier = `hedge_multiplier ** min(hits,3)` floored at `hedge_floor` → 0.70, floor 0.35 (`config.py:292-293`). A single hedge rescued by a confirmation cue (`announces`, `confirms`, `approves`, `official`, …) stays at 1.0.

**(c) Priced-in / surprise** — `priced_in_multiplier()` (`text_quality.py:174-190`). `PRICED_IN_CUES` (`:162-166`): as expected, in line with, as forecast, as anticipated, widely anticipated, as predicted, as planned, as consensus, meets expectations, priced in, fully priced, no surprise. `SURPRISE_CUES` (`:167-171`): unexpectedly, surprise, more than forecast, above forecast, below forecast, much larger, far larger, bigger than expected, overshoot, blows past, sharply. Multiplier `priced_in_multiplier` (0.45) floored at `priced_in_floor` (0.25); surprise multiplies by `surprise_multiplier` (1.35) (`config.py:289-291`).

**(d) Price-action detection** — `is_price_action()` (`text_quality.py:211-225`). Requires a price verb + a price subject + a figure. `directional_multiplier()` (`:228-236`) is a **constant 1.0** — the price-action path was removed from the weight math and is dead in the scoring path.

**Cue scoring** — `cue_rules.score_cues()` (`:289-303`): `total += weight * min(hits, _MAX_REPEAT_HITS)` with `_MAX_REPEAT_HITS = 2` (`:197`).

**Subject/verb inversion** — `detect_inversion_subjects()` (`:249-286`) uses a **token-distance** window (`inversion_max_tokens = 8`, `config.py:285`), not characters. `YIELD_SUBJECTS` (`:210-213`), `METAL_SUBJECTS` (`:219-221`), `EQUITY_SUBJECTS` (`:225-229`), `UP_VERBS` (`:214-218`), `DOWN_VERBS` (`:235-240`), `_INVERSION_WEIGHT = 2.0` (`:200`).

### 3.2 Signal construction

`app/analysis/signal_factory.py::build_signal()` (`:49-123`) builds one `WeightedSignal` per (article × asset impact row). It sets `category` and `corroboration` explicitly — the two fields the docstring at `:1-19` says used to be silently defaulted.

**Cross-asset bleed** — `build_bleed_signal()` (`signal_factory.py:166-227`): only `GOLD↔SILVER` (`BLEED_PAIR`, `:155-158`), only for categories in `BLEED_CATEGORIES` (`:146-153`: Central Bank, Interest Rates, Inflation, USD/DXY, Treasury Yields, Banking/Financial Stress), only `relevance ≥ 0.70` and strength ∈ {HIGH, VERY_HIGH}. The copy's relevance **and** persistence are multiplied by `settings.gold_silver_bleed` = 0.30 (`config.py:435`; read at `signal_factory.py:163`).

### 3.3 Model 1 / additional signals

**Model 1 = the LLM classifier** (§3.0). There is no second statistical model.

**Additional signal sources that feed the same aggregate:**

1. **Economic calendar** — `pipeline.py:1878-1892`. `CalendarSignal`s are converted to `WeightedSignal`s by `_calendar_signal_to_weighted()` (`pipeline.py:1443-1477`) with `source_quality="TIER_1_OFFICIAL"` and `session_multiplier=1.0`. Only `RELEASED` prints enter (`:1878-1881`). They also bleed GOLD↔SILVER (`:1887-1889`).
2. **Transmission map** — `app/analysis/transmission_map.py`, data in `app/config/transmission_map.json`. Per-asset `[sign, magnitude]` per event type (`:287-294` lookup). Magnitudes range 0.0–1.5 (`:216`).
3. **Event-type detection** — `detect_event_types()` (`transmission_map.py:995-1026`, memoised at `:1029-1063`). Priority: `EVENT_SIGNALS` phrase table (`:517`) → `event_polarity.resolve_all()` subject/verb resolver → `CATEGORY_FALLBACK` (`:961-981`, always `GENERAL_MACRO`, sign 0).
4. **Driver rankings** — `config.DRIVER_RANKINGS` (`app/config.py:1108-1300`), applied at `transmission_map.py:1197` as `driver_rank` (default 0.3 for unlisted event types).
5. **Policy scope** — `app/analysis/policy_scope.py`. A foreign central bank's stance is scaled by `FOREIGN_CB_POLICY_SCALE = 0.30` (`policy_scope.py:125`), applied at `transmission_map.py:1204` and `sentiment_engine.py:1046`.
6. **Release surprise** — `app/analysis/release_surprise.py`. An `ACTUAL_DATA` article's direction comes from the surprise vs forecast/previous, not from wording (`news_classifier.py:242-295`); `surprise_weight` scales the signal, capped at `MAX_SURPRISE_WEIGHT = 2.0` (`release_surprise.py:58`).
7. **Regional calendar bias** — `app/analysis/regional_bias.py`, served in the payload (`pipeline.py:1617-1629`).

### 3.4 The signal weight — `_signal_weight`

`app/analysis/sentiment_engine.py:897-1059`. Signature:

```python
def _signal_weight(s: WeightedSignal, now: datetime | None) -> float:
```
`[verbatim from app/analysis/sentiment_engine.py:897]`

The full formula, as it appears in code:

```python
    return decay * rel * per * qual * sess * corr * surprise * tier * metal_mult * earnings_mult * asset_sensitivity * s.speaker_tier
```
`[verbatim from app/analysis/sentiment_engine.py:1059]`

with each factor:

| Factor | Expression | Location |
|---|---|---|
| impact gate | `if not _passes_impact_gate(s): return 0.0` | `:917-918` |
| `decay` | `decay_weight(published_ref, tier_key, now, asset=s.asset)` | `:943` |
| `rel` | `max(0.0, min(1.0, s.relevance))` | `:944` |
| `per` | `max(0.05, min(1.0, s.persistence))` | `:945` |
| `qual` | `QUALITY_MULT.get((s.source_quality or "").upper(), 0.90)`; forced to `1.0` when category is `BITCOIN_ETF_FLOWS` | `:946-948` |
| `sess` | `max(0.5, min(1.5, s.session_multiplier))` + BTC clock multiplier | `:949-964` |
| `corr` | `max(1.0, float(s.corroboration or 1.0))` | `:965` |
| `surprise` | `max(0.0, min(MAX_SURPRISE_WEIGHT, float(s.surprise_weight or 0.0)))` | `:966` |
| `tier` | `TIER_MULTIPLIERS.get(event_tier(s), 0.45)` × rumor/confirmed multiplier | `:978-985` |
| `metal_mult` | `1.10` for SILVER with `metal_character ∈ {PRECIOUS, INDUSTRIAL}` | `:972-974` |
| `earnings_mult` | `2.0` mega-cap / `0.3` unnamed | `:989-998` |
| `asset_sensitivity` | `min(1.0, max(0.3, max_m))`, or `0.5` when no usable event type | `:1021-1057` |
| `speaker_tier` | `s.speaker_tier` | `:1059` |

**`QUALITY_MULT`** (`app/analysis/sentiment_engine.py:193-198`):

```python
QUALITY_MULT = {
    "TIER_1_OFFICIAL": 1.00,
    "TIER_2_MAJOR": 0.97,
    "TIER_3_CRYPTO": 0.94,
    "TIER_4_OTHER": 0.90,
}
```
`[verbatim from app/analysis/sentiment_engine.py:193-198]`

**`STRENGTH_MULT`** (`app/analysis/sentiment_engine.py:129-134`):

```python
STRENGTH_MULT = {
    ImpactStrength.VERY_HIGH: 1.00,
    ImpactStrength.HIGH: 0.72,
    ImpactStrength.MEDIUM: 0.45,
    ImpactStrength.LOW: 0.20,
}
```
`[verbatim from app/analysis/sentiment_engine.py:129-134]`

**Tier assignment** — `event_tier()` (`app/analysis/sentiment_engine.py:846-894`): `VERY_HIGH` importance → TIER_1 (capped to TIER_2 if the headline names only a foreign central bank, `:863-866`); `ReportKind.ACTUAL` → TIER_1; `BITCOIN_ETF_FLOWS`/`ETF_FLOWS` → TIER_1; `GEOPOLITICS` + HIGH importance → TIER_1 else TIER_2; opinion words (`analyst`, `opinion`, `preview`, `commentary`, `forecast`, `comment`, `outlook`) or `ReportKind.PREP` → TIER_3; HIGH/MEDIUM importance → TIER_2; else TIER_3.

**Tier multipliers** (`app/config.py:959-963`): `TIER_1 = 1.00`, `TIER_2 = 0.45`, `TIER_3 = 0.15`. Verification multiplier: `RUMOR_MULTIPLIER = 0.5` (`config.py:1096`), `CONFIRMED_BOOST = 1.05` (`config.py:1099`), applied at `sentiment_engine.py:982-985`.

### 3.5 Decay — half-lives and the time window

`app/analysis/sentiment_engine.py::decay_weight()` (`:752-816`):

```python
    if published_at is None:
        return DECAY_FLOOR
    hours = _age_hours(published_at, now)
    if hours < 0:
        return 1.0  # within clock skew: treat as brand new
    cutoff = getattr(settings, 'trading_day_cutoff_hours', 2.0)
    if hours >= cutoff:
        return DECAY_FLOOR  # Step 7: floor instead of hard zero
    return max(DECAY_FLOOR, min(1.0, 0.5 ** (hours / half_life)))
```
`[verbatim from app/analysis/sentiment_engine.py:807-816]`

Half-life resolution (`:795-805`):
1. `EVENT_HALF_LIFE_OVERRIDE.get(event_type)` — per-event override.
2. else `TIER_HALF_LIVES.get(tier, 2.0)`.
3. then `*= ASSET_DECAY_MULTIPLIER.get(asset.name, 1.0)`.
4. then `max(0.1, half_life)`.

**`TIER_HALF_LIVES`** (`app/config.py:986-990`):

```python
TIER_HALF_LIVES = {
    "TIER_1": 1.5,
    "TIER_2": 1.25,
    "TIER_3": 1.0,
}
```
`[verbatim from app/config.py:986-990]`

**`EVENT_HALF_LIFE_OVERRIDE`** (`app/config.py:995-1004`): `FOMC_DECISION 4.0`, `NFP_PRINT 3.0`, `CPI_PRINT 3.0`, `PPI_PRINT 3.0`, `FOMC_MINUTES 2.0`, `ECB_DECISION 4.0`, `BOE_DECISION 4.0`, `GEOPOLITICAL_RISK 6.0`.

**`ASSET_DECAY_MULTIPLIER`** (`app/config.py:1017-1022`):

```python
ASSET_DECAY_MULTIPLIER = {
    "GOLD": 1.0,
    "SILVER": 1.0,
    "BITCOIN": 1.25,
    "OIL": 1.0,
}
```
`[verbatim from app/config.py:1017-1022]` — **no EQUITY entry** (documented at `config.py:1015-1016`).

**`DECAY_FLOOR = 0.05`** (`app/config.py:1033`). **`TRADING_DAY_CUTOFF_HOURS`** (`app/config.py:1028`, from `settings.trading_day_cutoff_hours`): declared default **2.0**, live `.env` **8**.

**λ / half-life summary:** exponential decay, `weight = 0.5 ** (age_hours / half_life)`. Effective half-life = tier half-life × asset multiplier, floored at 0.1h. TIER_1 1.5h (1.875h for BTC), TIER_2 1.25h (1.5625h BTC), TIER_3 1.0h (1.25h BTC).

**Overnight admission** shifts the ageing reference: `s.age_reference_utc or s.published_at` (`sentiment_engine.py:942`), set for rows inside the covered off-window (`pipeline.py:1717-1723`).

### 3.6 Aggregation — `_aggregate`

`app/analysis/sentiment_engine.py:1087-1249`.

```python
        magnitude = weight * STRENGTH_MULT.get(s.strength, 0.45)
```
`[verbatim from app/analysis/sentiment_engine.py:1112]`

```python
        if sign > 0:
            bull += magnitude
            ...
        elif sign < 0:
            bear += magnitude
            ...
        else:
            directionless += magnitude * (1.4 if s.direction is Direction.MIXED
                                          else 1.0)
```
`[verbatim from app/analysis/sentiment_engine.py:1127-1145]`

**Stale mass cap** (`sentiment_engine.py:1150-1218`): each side's stale mass (age ≥ `trading_day_cutoff_hours`) is capped at `settings.stale_mass_cap_ratio` (0.5) × the card's **total fresh mass** (both sides). The cap rescales `magnitude`, `final_weight` and `contribution` in place and rebuilds both totals (`:1196-1218`).

**Burst bonus** (`sentiment_engine.py:1220-1243`):

```python
    if not conflicting_burst:
        if burst_bull >= 5 and burst_bull > burst_bear:
            bull *= 1.05
        elif burst_bull >= 3 and burst_bull > burst_bear:
            bull *= 1.02
        elif burst_bear >= 5 and burst_bear > burst_bear:
            bear *= 1.05
        elif burst_bear >= 3 and burst_bear > burst_bear:
            bear *= 1.02
```
`[verbatim from app/analysis/sentiment_engine.py:1235-1243]`

The burst window is **900 seconds** (15 min) of publication time (`:1225-1228`). Conflicting burst (both sides ≥ 2) cancels the bonus entirely (`:1233`).

### 3.7 Combination rule — how signals become a score

**It is a weighted sum per side, then a ratio.** Not a product, not a max, not an average.

Step 1 — `bull = Σ magnitude` over BULLISH signals, `bear = Σ magnitude` over BEARISH signals (`:1127-1140`).

Step 2 — `_score_from()` (`app/analysis/sentiment_engine.py:1252-1279`):

```python
    total = bull + bear
    if total <= 0:
        return 0.0
    score = round(100.0 * (bull - bear) / total, 1)
```
`[verbatim from app/analysis/sentiment_engine.py:1258-1261]`

```python
    if CONTRADICTION_DAMPENER_ENABLED:
        smaller = min(bull, bear)
        larger = max(bull, bear)
        if larger > 0 and smaller / larger > CONTRADICTION_RATIO:
            score = round(
                score * CONTRADICTION_DAMPENER_FACTOR, 1)
```
`[verbatim from app/analysis/sentiment_engine.py:1273-1278]`

with `CONTRADICTION_DAMPENER_ENABLED = True` (`config.py:1049`), `CONTRADICTION_RATIO = 0.6` (`config.py:1051`), `CONTRADICTION_DAMPENER_FACTOR = 0.5` (`config.py:1053`).

**The complete score formula, reconstructed:**

```
score = round(100 × (bull − bear) / (bull + bear), 1)
if min(bull, bear) / max(bull, bear) > 0.6:
    score = round(score × 0.5, 1)
```
`[reconstructed verbatim from app/analysis/sentiment_engine.py:1252-1279]`

### 3.8 Confidence, damping, and label selection

**Confidence** — `_confidence_from()` (`app/analysis/sentiment_engine.py:1303-1334`):

```python
    net = abs(bull - bear) / directional
    conviction = min(1.0, net / 0.6)
    breadth = min(1.0, used / 8.0)
    agreement = 1.0 - (2.0 * min(bull, bear) / directional)
    total_flow = directional + directionless
    certainty = directional / total_flow if total_flow > 0 else 0.0

    confidence = (0.40 * conviction + 0.28 * breadth + 0.22 * agreement) * certainty
```
`[verbatim from app/analysis/sentiment_engine.py:1316-1323]`

then `calibrate_confidence(confidence)` (`:1328-1332`) — **an identity today**, because `CONFIDENCE_CALIBRATION_ENABLED = False` (`config.py:1083`) and the breakpoint table is `((0.0, 0.0), (1.0, 1.0))` (`config.py:1084-1087`). Finally clamped: `round(max(0.0, min(0.95, confidence)), 3)` (`:1334`).

**Confidence never scales the score.** It gates flips and caps the label strength.

**Damped score** (display/cue only) — `_damped_score()` (`:1282-1300`):

```python
    k = getattr(settings, "score_damping_k", 0.5)
    if total_weight <= 0:
        return 0.0
    if k <= 0:
        return raw_score
    return round(raw_score * total_weight / (total_weight + k), 1)
```
`[verbatim from app/analysis/sentiment_engine.py:1295-1300]` — `k = settings.score_damping_k`, default **1.0** (`config.py:242`).

**Label selection** — `compute()` (`app/analysis/sentiment_engine.py:1470-1873`):

1. No signals for the asset → carry the last published bias forward, `stale=True` (`:1512-1527`, `_carry_forward_bias` `:1384-1410`).
2. `total_weight < MIN_TOTAL_WEIGHT (0.15)` → weak-evidence branch (`:1601-1659`). Bias = heavier side; the score is published as `raw × min(1, total/0.15)` (`:1636-1639`); confidence capped at `TIEBREAK_MAX_CONFIDENCE`.
3. Otherwise (`:1694-1723`):
   - `|raw| >= EPS (25.0)` → `bias = sign(raw)`.
   - `|raw| < EPS` → `_tie_break_bias()` (`:1346-1381`): the side of the **highest-magnitude, newest** directional signal; else carried history; else `DEFAULT_BIAS` (BULLISH).
4. Baseline applied for context only — see §3.10.

**Flip / stability gates** — `_should_flip()` (`app/analysis/sentiment_engine.py:484-581`), in order:

| # | Gate | Code |
|---|---|---|
| 1 | No current bias → take the computed bias | `:521-523` |
| 2 | `bull + bear <= 0` → hold | `:526-528` |
| 3 | Fresh (<1h) TIER_1 signal pointing the other way → **flip immediately**, bypassing every other gate | `:531-537`, `_has_fresh_tier1_opposite` `:438-458` |
| 4 | Inside `COOLING_MINUTES` → effective margin = `COOLING_MARGIN` | `:539-556` |
| 5 | `hold_elapsed < FLIP_HOLD_MINUTES` → hold | `:559-562` |
| 6 | `confidence < FLIP_MIN_CONFIDENCE` → hold | `:569-570` |
| 7 | Score has not crossed the effective margin → hold | `:572-581` |

Effective margin = `FLIP_MARGIN` (5.0 live), widened by `+10.0` when `cluster_conflict` (`effective_flip_margin`, `:349-353`; `CONTRADICTION_FLIP_MARGIN_ADJUSTMENT = 10.0`, `config.py:338`).

**Label/score consistency** — `payload_score()` (`app/analysis/sentiment_engine.py:265-298`):

```python
    name = getattr(bias, "value", bias)
    if name == "BULLISH" and raw_score < 0:
        return TIEBREAK_LEAN
    if name == "BEARISH" and raw_score > 0:
        return -TIEBREAK_LEAN
    return float(raw_score)
```
`[verbatim from app/analysis/sentiment_engine.py:293-298]` — `TIEBREAK_LEAN = 2.0` (`:224`).

**Split flags**:
- `cluster_conflict` — `compute()` `:1549-1577`: conflicted weight share ≥ `CLUSTER_CONFLICT_WEIGHT_SHARE` (0.05) **and** total weight ≥ `CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT` (0.5) (`config.py:1064-1066`). Widens the flip margin only.
- `evidence_split` — `evidence_split_from_totals()` (`:341-346`): `min(bull,bear)/max(bull,bear) > EVIDENCE_SPLIT_RATIO (0.7)` (`:337`). Caps confidence at `TIEBREAK_MAX_CONFIDENCE` (0.30) via `cap_confidence_for_evidence_split` (`:356-358`) and forces `confidence_label = "LOW"` (`:1593-1594`, `:1793-1794`).

**Driver selection** — drivers follow the **published label**, not the computed one (`:1811-1817`).

### 3.9 Macro / cross-asset adjustment

**There is no macro data input into the score.** No VIX level, no DXY level, no yield level, no regime filter is read.

- VIX, DXY and yields enter **only as news event types** (`VIX_SPIKE`, `VIX_COMPRESSION`, `STRONG_USD`, `WEAK_USD`, `RISING_YIELDS`, `FALLING_YIELDS`, `REAL_YIELDS_RISING`, `REAL_YIELDS_FALLING`) whose sign and magnitude come from `app/config/transmission_map.json`.
- **Market regime is computed but does not modulate any score.** `regime_mod.detect_regime(category_weights)` is called at `app/services/pipeline.py:1863` and stored in `_last_regime` (`:1915-1916`), then reported in the dashboard payload (`app/main.py:686`, `:723-725`). `REGIME_ASSET_BIAS` (`app/market/regime.py:39-51`) and `RegimeAssessment.bias_for()` (`:61-62`) are **never called** (see §7).
- **Cross-asset channels that ARE applied:**
  - GOLD↔SILVER bleed, factor `gold_silver_bleed = 0.30` (§3.2).
  - Per-country calendar multipliers (`config.py:625-627`: `USD=1.0, EUR=0.30, GBP=0.20, JPY=0.15, CNY=0.25, CHF=0.05, CAD=0.05, AUD=0.05, NZD=0.05`; default 0.05 at `:629`) and the asset override `CNY:XAGUSD=0.8` (`:633`).
  - Calendar composite cluster cap `calendar_composite_max_weight = 1.30`, mode `"per_asset"` (`config.py:666`, `:675`).
  - Foreign-central-bank policy scale 0.30 (`policy_scope.py:125`).
  - The equity card's own relevance table and region keywords (§4).

### 3.10 Final normalization and the baseline

**Baseline = context, not a transform (as built).** `_apply_baseline()` (`sentiment_engine.py:461-481`) calls `bias_baseline.adjusted_score()` (`app/analysis/bias_baseline.py:89-103`):

```python
    if baseline is None:
        return float(raw_score), None
    if abs(raw_score) < settings.baseline_min_score:
        return float(raw_score), baseline
    return round(float(raw_score) - baseline, 4), baseline
```
`[verbatim from app/analysis/bias_baseline.py:100-103]`

But the **centred value is not used for the verdict**: `compute()` reads `raw = score` (the unflipped `_score_from` output) at `:1694` and derives `computed_bias` from `abs(raw) >= EPS` at `:1698-1699`. `centred` is used only for the note (`:1695-1697`, `:1831-1843`). The published `raw_score` is the **uncentred** net lean (`:1848`).

Baseline source: `bias_baseline.baseline_score()` (`bias_baseline.py:52-86`) — trailing **median** of the asset's last `baseline_window_ticks` (20) persisted `bias_snapshots.raw_score` rows, requiring at least `baseline_min_ticks` (5); returns `None` (warm-up) below that. Read **before** this tick's rows are written (`pipeline.py:1989-2000`), so no look-ahead.

**Final normalization chain, in order:**

1. `raw_score` = `round(100 × (bull − bear) / (bull + bear), 1)`, then contradiction dampener ×0.5 (`:1252-1279`).
2. `score` (published) = `payload_score(raw_score, final_bias)` (`:265-298`) — the raw score unless it contradicts the label, in which case ±2.0.
3. `damped_score` = `round(raw × total_weight / (total_weight + k), 1)`, k = 1.0 (`:1282-1300`).
4. `display` (in `app/analysis/display.py::to_display`, `:147-343`):
   - **Ratio path** (taken whenever `bull`/`bear` are usable): `bull_pct = int(round(100.0 * b / (b + r)))`, `bear_pct = 100 − bull_pct` (`display.py:116-144`).
   - **Fallback path**: `bull_pct = _clamp_pct(int(round(50 + numeric / 2.0)))` (`display.py:272`), then confidence shrink `bull_pct = int(round(50 + (bull_pct - 50) * min(1.0, c/0.30)))` (`display.py:279-293`).
   - Tie-break + even split → `{"label": "NEUTRAL", "pct": 50}` (`:302-308`).
   - A named side is never contradicted: BULLISH with `bull_pct < 50` → 51; BEARISH with `bear_pct < 50` → 49 (`:315-320`, `LEAN_BULL_PCT = 51` at `:68`).

### 3.11 Aggregation window

| Window | Value | Location |
|---|---|---|
| Ingest admission (rolling) | `news_window_hours` = 24 live / 18.0 default | `config.py:201`, `timestamp_parse.py:151-154` |
| Hard max age | `max_article_age_hours` = 26 | `config.py:207` |
| Scoring window (decay cutoff) | `trading_day_cutoff_hours` = 8 live / 2.0 default | `config.py:203`, `config.py:1028` |
| Delta-60m window | `delta_window_minutes` = 60 | `config.py:205`, `sentiment_engine.py:1454` |
| Burst window | 900 s | `sentiment_engine.py:1228` |
| Retention | `news_retention_days` = 2 | `config.py:124`, `pipeline.py:2312` |

---

## Section 4: Asset-Specific Behavior

### 4.1 Gold (XAUUSD)

- **Keyword list** — `asset_relevance.DIRECT_MENTION[Asset.GOLD]` (`app/analysis/asset_relevance.py:47`):
  `("gold", "xau", "bullion", "comex gold", "gold price")`
- **Category relevance row** — `asset_relevance.py:19-42`; e.g. Treasury Yields 0.92, Central Bank 0.90, Interest Rates 0.90, USD/DXY 0.88, Geopolitics 0.82, Inflation 0.82, Employment 0.78, Banking Stress 0.78.
- **Decay multiplier** — `ASSET_DECAY_MULTIPLIER["GOLD"] = 1.0` (`config.py:1018`).
- **Session profile** — `"metal"` (`session_detector.py:337`): the `CATEGORY_SESSION_WEIGHT` curve (`session_detector.py:109-130`) plus a **weekend discount of ×0.70** (`WEEKEND_MULTIPLIER["metal"]`, `session_detector.py:343-347`, applied `:409-412`).
- **Gold-specific session boost** — `GOLD_SESSION_BOOST` (`cue_rules.py:148-172`), applied only for `XAUUSD` (`session_detector.py:401-404`). e.g. US session: Central Bank 1.25, Interest Rates 1.25, Inflation 1.25, Employment 1.20, Treasury Yields 1.25, USD/DXY 1.20, ETF Flows 1.25, Banking Stress 1.15; EUROPE: Central Bank/Interest Rates/Inflation/Treasury Yields 1.15, ETF Flows 1.20; ASIA: China 1.20, Central Bank 1.05, Industrial Demand 1.15.
- **Gold cue lists** — `BULLISH_CUES` gold block (`cue_rules.py:50-57`): `real yields fall` (4), `tips yields fall` (3), `breakevens rise` (2), `gold funding rate reset` (3), `gold short squeeze` (5), `gold exchange outflow` (3), `gold etf inflow` (4), `central bank buying` (5), `reserve accumulation` (4)… `BEARISH_CUES` gold block (`:81-88`): `real yields rise` (4), `gold funding rate spike` (4), `gold long liquidation` (6), `gold exchange inflow` (3), `gold etf outflow` (4), `central bank selling` (5)…
- **Gold event types** — `GOLD_FUNDING_RATE_SPIKE/RESET`, `GOLD_OPEN_INTEREST_SURGE/DROP`, `GOLD_LONG_LIQUIDATION`, `GOLD_SHORT_SQUEEZE`, `GOLD_EXCHANGE_INFLOW/OUTFLOW`, `GOLD_ETF_FLOW_IN/OUT`, `CENTRAL_BANK_GOLD_BUYING/SELLING`, `REAL_YIELDS_RISING/FALLING`, `TIPS_AUCTION`, `BREAKEVENS_RISING/FALLING` (`transmission_map.py:189-207`).
- **Driver rankings** — `DRIVER_RANKINGS["GOLD"]` (`config.py:1111-1139`).
- **Bleed partner** — Silver (`signal_factory.py:155-158`).
- **No sign flip.** Gold's safe-haven polarity is *data*: e.g. `GEOPOLITICAL_RISK` → XAUUSD `[+1, 1.2]` while BTCUSD is `[-1, 0.35]` (`app/config/transmission_map.json`).

### 4.2 Silver (XAGUSD)

- **Keyword list** — `DIRECT_MENTION[Asset.SILVER]` (`asset_relevance.py:48`): `("silver", "xag", "silver price")`.
- **Dual-nature character** — `detect_metal_character()` (`cue_rules.py:491-502`) returns `PRECIOUS` / `INDUSTRIAL` / `BOTH` / `None` from `PRECIOUS_CUES` (`:97-98`) and `INDUSTRIAL_CUES` (`:99-100`). When the character is `PRECIOUS` or `INDUSTRIAL`, the weight is multiplied by **1.10** (`sentiment_engine.py:972-974`); `BOTH` and `None` get 1.0.
- **Silver-specific event types** — `SILVER_INDUSTRIAL_DEMAND`, `SILVER_CHINA_DEMAND`, `GOLD_SILVER_RATIO_HIGH`, `GOLD_SILVER_RATIO_LOW` (`transmission_map.py:160-175`).
- **Driver rankings** — `DRIVER_RANKINGS["SILVER"]` (`config.py:1141-1184`): `CHINA_DEMAND 1.00`, `INDUSTRIAL_DEMAND 0.95`, `SILVER_INDUSTRIAL_DEMAND 1.00`, `SILVER_CHINA_DEMAND 1.00`, `GOLD_SILVER_RATIO_HIGH 1.00`, `GOLD_SILVER_RATIO_LOW 1.00`, `COMMODITY_SUPPLY 0.75`, `GEOPOLITICAL_RISK 0.70`, `BANKING_STRESS 0.60`, `GOLD_ETF_FLOW 0.20`, `BTC_ETF_FLOW 0.10`, crypto types 0.05, `GENERAL_MACRO 0.30`.
- **Decay multiplier** — `1.0` (`config.py:1019`). **Session profile** — `"metal"` (`session_detector.py:338`). **No gold session boost** (gold-only, `session_detector.py:401`).
- **Bleed partner** — Gold, factor 0.30.
- **Calendar country override** — `CNY:XAGUSD = 0.8` (`config.py:633`).
- **Price feed** — none (`price_data.py:33`).

### 4.3 Bitcoin (BTCUSD)

- **Keyword list** — `DIRECT_MENTION[Asset.BITCOIN]` (`asset_relevance.py:49`): `("bitcoin", "btc", "xbt", "bitcoin price")`.
- **BTC clock session multiplier** (`sentiment_engine.py:950-964`) — applied on top of the session multiplier:

```python
    if s.asset is Asset.BITCOIN and s.published_at is not None:
        hour_utc = _ensure_aware(s.published_at).hour
        if 0 <= hour_utc < 8:
            sess *= 0.85
        elif 8 <= hour_utc < 12:
            sess *= 1.15
        elif 13 <= hour_utc < 21:
            sess *= 1.25
        else:
            sess *= 0.95
```
`[verbatim from app/analysis/sentiment_engine.py:951-964]`

- **Decay multiplier** — `1.25` (`config.py:1020`) — the only asset with a non-1.0 value.
- **Session profile** — `"flat"` (`session_detector.py:339`): `asset_session_multiplier` returns **1.0** in every session and on every weekday (`session_detector.py:391-392`).
- **Source quality override** — category `BITCOIN_ETF_FLOWS` forces `qual = 1.0` (`sentiment_engine.py:947-948`).
- **Bitcoin cue lists** — `BULLISH_CUES` (`:45-48`): `funding rate reset` (4), `positive funding` (3), `short squeeze` (5), `exchange outflow` (4), `whale withdrawal` (3), `stablecoin mint` (4), `tether mint` (4), `usdt mint` (4), `open interest surge` (3), `oi surge` (3). `BEARISH_CUES` (`:76-79`): `funding rate spike` (5), `negative funding` (4), `long liquidation` (6), `exchange inflow` (4), `whale deposit` (3), `stablecoin redemption` (4), `open interest drop` (3).
- **Bitcoin event types** — `BTC_FUNDING_RATE_SPIKE/RESET`, `BTC_OPEN_INTEREST_SURGE/DROP`, `BTC_LONG_LIQUIDATION`, `BTC_SHORT_SQUEEZE`, `BTC_WHALE_ALERT`, `BTC_EXCHANGE_INFLOW/OUTFLOW`, `BTC_STABLECOIN_MINT/REDEEM`, `REGULATION_CLAMP`, `REGULATION_CLARITY`, `LIQUIDITY_EASING`, `LIQUIDITY_TIGHTENING` (`transmission_map.py:176-187`, `:139-159`).
- **Driver rankings** — `DRIVER_RANKINGS["BITCOIN"]` (`config.py:1186-1238`): `RISK_ON 1.00`, `RISK_OFF 1.00`, `BTC_ETF_FLOW 1.00`, `CRYPTO_REGULATION 1.00`, `CRYPTO_LIQUIDATION 1.00`, `CRYPTO_HACK 1.00`, `REGULATION_CLAMP/CLARITY 1.00`, `LIQUIDITY_EASING/TIGHTENING 1.00`, `BTC_LONG_LIQUIDATION 1.00`, `BTC_SHORT_SQUEEZE 1.00`, `BANKING_STRESS 0.90`, `GOLD_ETF_FLOW 0.05`.
- **Price feed** — **yes**, Coinbase BTC-USD (`price_data.py:33`). Only asset with a price feed.
- **No bleed** — not in `BLEED_PAIR`.

### 4.4 Crude Oil (USOIL)

- **Keyword list** — `DIRECT_MENTION[Asset.OIL]` (`asset_relevance.py:62-72`):

```python
    Asset.OIL: (
        "crude oil", "crude", "wti", "brent", "opec", "opec+", "petroleum",
        "barrel", "refinery", "oil price", "oil prices", "oil futures",
        "oil supply", "oil demand", "oil output", "oil production",
        "oil inventories", "oil stocks", "oil market", "oil majors",
        "oilfield", "oil tanker", "oil embargo", "oil sanctions",
        "crude stocks", "crude inventories", "crude output", "crude pipeline",
        "eia crude", "api crude", "saudi aramco", "aramco", "hormuz",
        "strait of hormuz", "shale oil", "shale producer", "energy prices",
        "oil rig", "oil glut", "oil shortage",
    ),
```
`[verbatim from app/analysis/asset_relevance.py:62-72]`

  Note the deliberate absence of a bare `"oil"` token (rationale at `asset_relevance.py:57-61`).

- **False-friend denylist** — `OIL_FALSE_FRIENDS` (`asset_relevance.py:80-86`): `olive oil`, `palm oil`, `cooking oil`, `coconut oil`, `sesame oil`, `sunflower oil`, `rapeseed oil`, `canola oil`, `soybean oil`, `mustard oil`, `fish oil`, `cod liver oil`, `olive`, `snake oil`, `essential oil`, `hair oil`, `machine oil`, `lubricating oil`, `engine oil`, `motor oil`, `cooking`, `recipe`, `cuisine`, `restaurant menu`. A match forces OIL relevance to `0.0` (`asset_relevance.py:135-136`).
- **Category relevance** — `OIL_ENERGY` → OIL **1.00**; `COMMODITY_SUPPLY_DEMAND` → OIL 0.95; `INDUSTRIAL_DEMAND` → 0.82; `GDP` → 0.78; `USD_DXY` → 0.80; `GEOPOLITICS` → 0.88; `CHINA` → 0.74 (`asset_relevance.py:19-42`).
- **Event types** — `OIL_SUPPLY_TIGHTNESS`, `OIL_SUPPLY_GLUT` (`transmission_map.py:119-120`), with an intrinsic bullish floor for supply-tightness headlines (`transmission_map.py:391-398`).
- **Driver rankings** — `DRIVER_RANKINGS["OIL"]` (`config.py:1246-1299`): `OIL_SUPPLY_TIGHTNESS 1.00`, `OIL_SUPPLY_GLUT 1.00`, `COMMODITY_SUPPLY 0.95`, `GEOPOLITICAL_RISK 1.00` (escalation = bullish oil), `GEOPOLITICAL_DEESCALATION 1.00`, `INDUSTRIAL_DEMAND 0.85`, `CHINA_DEMAND 0.80`, `SILVER_CHINA_DEMAND 0.70`, `SILVER_INDUSTRIAL_DEMAND 0.55`, `STRONG_USD 0.75`, `WEAK_USD 0.75`, crypto types 0.05, `GENERAL_MACRO 0.30`.
- **Decay multiplier** — `1.0` (`config.py:1021`).
- **Session profile** — **no `USOIL` entry** in `ASSET_SESSION_PROFILES` (`session_detector.py:336-341`), so `_profile_for` falls back to `"metal"` (`session_detector.py:366`) → session curve + ×0.70 weekend discount.
- **Price feed** — none. **No bleed.**

### 4.5 Global Equity Indices (EQUITY)

**Not scored through the asset loop.** `Asset.EQUITY` is skipped in `pipeline._recompute_sentiment` (`pipeline.py:2030-2039`); the verdict comes from the session-aware card.

- **Card builder** — `app/analysis/equity_card.py::build_equity_card()` (`:405-556`), which calls the **same** `compute()` (`:475-477`) with `history_key = "EQUITY:<region>"`.
- **Region map** — `EQUITY_SESSION_MAP` (`app/market/session_detector.py:135-166`):

| Session (NY) | Title | Symbol | Keywords |
|---|---|---|---|
| ASIA 20:00–04:00 | ASIA EQUITY | NIKKEI / HSI | nikkei, topix, japan, boj, bank of japan, hang seng, hsi, shanghai, sse, csi 300, china, kospi, korea, asian shares, asia-pacific, nifty, sensex, india, asx, australia, rba |
| EUROPE 03:00–12:00 | EUROPE EQUITY | DAX / STOXX50 | dax, stoxx, ftse, cac 40, cac40, euro stoxx, european shares, europe, ecb, european central bank, bank of england, boe, bundesbank |
| US 08:00–17:00 | US EQUITY | SPX / NDX | s&p, s&p 500, spx, nasdaq, ndx, dow jones, dow, nyse, wall street, us stocks, fed, federal reserve, fomc, treasury, russell 2000, earnings, apple, microsoft, amazon, nvidia, google, meta, tesla, profit, revenue, eps, guidance |

- **Global macro keywords** (apply to every region at reduced weight) — `GLOBAL_MACRO_KEYWORDS` (`equity_card.py:120-132`), weight `GLOBAL_MACRO_WEIGHT = 0.65` (`equity_card.py:136`).
- **Own relevance table** — `config.EQUITY_RELEVANCE` (`config.py:1310-1331`): Equities 1.00, Treasury Yields 1.00, Central Bank 0.95, Interest Rates 0.95, Inflation 0.95, Employment 0.95, GDP 0.85, USD/DXY 0.95, Banking Stress 0.70, ETF Flows 0.70, Geopolitics 0.50, Risk Sentiment 0.60, China 0.40, Oil/Energy 0.35, Commodity Supply/Demand 0.30, Industrial Demand 0.25, Crypto Regulation 0.10, Bitcoin ETF Flows 0.05, Crypto Market Flows 0.05, Other 0.10.
- **Own direction column** — the equity card re-derives direction with `detect_direction(text, equity=True)` (`equity_card.py:448`), and uses `combine_prints_for_asset(..., use_equity_column=True)` for `ACTUAL` prints (`:431-437`). This is the closest thing to a "sign flip": *"stocks rise"* is bullish for equities even though *"rise"* is a metal-bearish cue (`cue_rules.py:347-367`).
- **Categories listened to** — `EQUITY_CATEGORIES` (`equity_card.py:268-280`).
- **US sub-session multipliers** — `EQUITY_SUB_SESSION_WEIGHT` (`session_detector.py:225-286`): PRE_MARKET 04:00–09:30, REGULAR 09:30–16:00, AFTER_HOURS 16:00–20:00, CLOSED 20:00–04:00. Applied via `asset_session_multiplier` when `asset == "EQUITY"` (`session_detector.py:395-396`).
- **Persistence** — the card is **in-memory only**; no `SentimentSnapshot` / `BiasSnapshot` row is written for equity. Its verdict **is** written to `bias_snapshots` and the decision log (`pipeline.py:2050-2072`).
- **No regional news** → `neutral_card()` (`equity_card.py:368-402`) carries the region's last bias forward with `stale=True`.

---

## Section 5: State, Caching, and Persistence

### 5.1 In-memory state

| State | Location | Lifetime |
|---|---|---|
| `_LAST_BIAS` — last published bias per key (asset symbol, or `EQUITY:<region>`) | `sentiment_engine.py:363` | process; reset on market-day change (`_maybe_reset_bias_history`, `:101-113`) |
| `_LAST_FLIP_TIME` — last flip timestamp per key | `sentiment_engine.py:366` | process; same reset |
| `_LAST_BIAS_DAY_KEY` | `sentiment_engine.py:370` | process |
| Dedup index `_events` | `app/analysis/event_deduplication.py:86` | process; pruned by 12h window; reseeded from DB once per process (`pipeline.py:591`) |
| `_last_successful_tick`, `_news_since_update`, `_running`, `_tick_generation`, `_tick_started_at` | `pipeline.py:261-269`, `:306` | process |
| `_last_regime` | `pipeline.py:274` | process |
| `_calendar_signals`, `_calendar_events`, `_calendar_last_fetch_utc` | `pipeline.py:276-285` | process |
| `_last_equity_card`, `_last_equity_signals` | `pipeline.py:290-295` | process |
| `_health_metrics` | `pipeline.py:299-303` | process |
| `_DECISION_LOG` | `pipeline.py:1433` | process (lazy singleton) |
| `_LLM_SEMAPHORE` / `_CLIENT` (rebound per event loop) | `news_classifier.py:66`, `llm_client.py:33-37` | per event loop |
| LRU caches: `_detect_event_types_cached` (8192), `_detect_verification_cached` (8192), `load_transmission_map` (1) | `transmission_map.py:1029`, `cue_rules.py:466`, `transmission_map.py:256` | process |
| TradingView response cache, 300 s TTL | `app/ingestion/tradingview_api.py:52`, `:189-205` | process |
| `_cache` (calendar provider payload) | `app/ingestion/calendar_cache.py` | on disk, 7 days |

**Boot warm-up:** `app/main.py:203-225` loads the last HIGH/MEDIUM-confidence, non-stale `SentimentSnapshot.bias` per asset and calls `warm_bias_history()` (`sentiment_engine.py:400-406`).

### 5.2 On-disk / database state

**Database** — SQLite at `data/intraday_sentiment.db` (`.env` `DATABASE_URL=sqlite:///./data/intraday_sentiment.db`; resolved relative to `BASE_DIR`, `config.py:910-915`).

Tables (`app/models/db_models.py`):

| Table | Class | Purpose |
|---|---|---|
| `news_article` | `NewsArticle` `:29` | every ingested headline, its classification, `chart_scope`, `tv_symbols` |
| `news_event` | `NewsEvent` `:78` | the dedup event cluster |
| `article_event` | `ArticleEvent` `:103` | article↔event link + similarity |
| `asset_impact` | `AssetImpact` `:116` | per-asset direction/strength/relevance/persistence + `final_weight` |
| `sentiment_snapshot` | `SentimentSnapshot` `:155` | the published verdict, append-only, scope `CURRENT` |
| `bias_snapshots` | `BiasSnapshot` `:256` | the audit trail + the baseline source |
| `bias_outcome` | `BiasOutcome` `:359` | PENDING / WORKED / FAILED / Mixed labels |
| `price_point` | — | ground-truth prices for the outcome loop |
| `alert` | `Alert` `:327` | high-impact alerts |
| `pipeline_run` | `PipelineRun` `:343` | per-tick bookkeeping |
| `settings` | `Settings` | key/value (e.g. `engine_paused`) |
| `dropped_article` | — | one-shot deletion suppression log |

**Files:**

| Path | Written by |
|---|---|
| `data/last_successful_tick.txt` | `pipeline.py:161`, `_save_last_successful_tick` `:178` |
| `logs/decisions.jsonl` | `app/analysis/decision_log.py` (`settings.decision_log_path`, `config.py:496`) |
| `logs/engine.log` | `app/logging_conf.py` (`settings.log_file`, `config.py:45`) |
| `data/reports/morning_brief_YYYY-MM-DD.json` | `app/services/overnight.py` |
| `data/state/overnight_boundary.json`, `data/state/morning_brief_state.json` | `app/services/overnight.py` |
| `data/coinglass_profile/` | browser profile only — no code uses it |

### 5.3 Invalidation / refresh

| What | How it is invalidated |
|---|---|
| Sentiment snapshots | **Rebuilt from scratch every tick** (`pipeline.py:1650-1656`); snapshots are append-only |
| Bias history | reset when the market-timezone date changes (`sentiment_engine.py:101-113`) |
| Dedup index | pruned to a 12h window measured against the tick clock (`event_deduplication.py:88-90`); reseeded from the DB once per process (`pipeline.py:591`) |
| TradingView cache | 300 s TTL (`tradingview_api.py:194`) |
| LLM circuit breaker | reset at the start of every tick (`pipeline.py:2560`) |
| LLM cooldown | 150 s when the provider returned zero verdicts (`pipeline.py:92`, `:1140-1145`) |
| Baseline | recomputed per tick from persisted `bias_snapshots` **before** this tick's rows are written (`pipeline.py:1974-2000`) |
| News | purged after `news_retention_days` (2) market days (`pipeline.py:2312-2406`) |
| Unroutable impact rows | purged every tick (`pipeline.py:2794`, `_purge_unroutable_impacts` `:2409-2447`) |
| Calendar cache | 7-day max age (`config.py:637`), served stale on failure (`config.py:651`) |
| Operator pause | persisted in the `settings` table (`main.py:1075-1086`) and restored at boot (`main.py:228-243`) |

---

## Section 6: Current Parameters (Single Source of Truth)

**"Live" = the value actually running, from `.env`. "Default" = the value in `app/config.py` if `.env` does not override it.**

### 6.1 Core scoring constants

| Parameter | Current Value | Location (file:line) | Meaning |
|---|---|---|---|
| `EPS` | `25.0` | `app/analysis/sentiment_engine.py:215` | \|raw\| below this → tie-break, not a verdict |
| `DEFAULT_BIAS` | `BULLISH` | `app/analysis/sentiment_engine.py:219` | verdict when no signals, no history |
| `TIEBREAK_LEAN` | `2.0` | `app/analysis/sentiment_engine.py:224` | lean published when label contradicts the raw lean |
| `TIEBREAK_MAX_CONFIDENCE` | `0.30` | `app/analysis/sentiment_engine.py:228` | confidence cap for tie-break / evidence-split |
| `MIN_TOTAL_WEIGHT` | `0.15` | `app/analysis/sentiment_engine.py:236` | below this → weak-evidence branch |
| `FLIP_MIN_CONFIDENCE` | `0.30` (live & default) | `app/analysis/sentiment_engine.py:247` ← `app/config.py:238` | confidence floor to allow a flip |
| `FLIP_MARGIN` | **5.0 live** / 20.0 default | `app/analysis/sentiment_engine.py:330` ← `app/config.py:381` | score margin required to flip |
| `FLIP_HOLD_MINUTES` | **2 live** / 15 default | `app/analysis/sentiment_engine.py:331` ← `app/config.py:383` | minimum minutes between flips |
| `COOLING_MINUTES` | **5 live** / 20 default | `app/analysis/sentiment_engine.py:327` ← `app/config.py:385` | anti-whipsaw cooling window |
| `COOLING_MARGIN` | **8.0 live** / 18.0 default | `app/analysis/sentiment_engine.py:328` ← `app/config.py:387` | margin required during cooling |
| `EVIDENCE_SPLIT_RATIO` | `0.7` | `app/analysis/sentiment_engine.py:337` | bull/bear ratio above this → evidence split |
| `CONTRADICTION_FLIP_MARGIN_ADJUSTMENT` | `10.0` | `app/analysis/sentiment_engine.py:338` | margin widening on cluster conflict |
| `ASSET_SENSITIVITY_UNKNOWN` | `0.5` | `app/analysis/sentiment_engine.py:146` | sensitivity when no usable event type |
| `STRENGTH_MULT` | 1.00 / 0.72 / 0.45 / 0.20 | `app/analysis/sentiment_engine.py:129-134` | strength → magnitude multiplier |
| `STRENGTH_GATE` | {VERY_HIGH, HIGH} | `app/analysis/sentiment_engine.py:169` | impact gate band set |
| `QUALITY_MULT` | 1.00 / 0.97 / 0.94 / 0.90 | `app/analysis/sentiment_engine.py:193-198` | source-trust multiplier |
| Burst window | `900` s | `app/analysis/sentiment_engine.py:1228` | same-side headline burst window |
| Burst multiplier | `×1.02` (≥3), `×1.05` (≥5) | `app/analysis/sentiment_engine.py:1236-1243` | velocity bonus; cancelled on conflict |
| Confidence coefficients | `0.40` conviction, `0.28` breadth, `0.22` agreement | `app/analysis/sentiment_engine.py:1323` | confidence blend |
| Conviction divisor | `0.6` | `app/analysis/sentiment_engine.py:1317` | `net / 0.6`, capped at 1 |
| Breadth divisor | `8.0` | `app/analysis/sentiment_engine.py:1318` | `used / 8`, capped at 1 |
| Confidence clamp | `0.95` | `app/analysis/sentiment_engine.py:1334` | max published confidence |
| `TIEBREAK_MAX_CONFIDENCE` label thresholds | LOW `<0.30`, MEDIUM `<0.55` | `app/analysis/sentiment_engine.py:258-262` | confidence_label mapping |

### 6.2 Decay and time windows

| Parameter | Current Value | Location (file:line) | Meaning |
|---|---|---|---|
| `TIER_HALF_LIVES` | TIER_1 1.5h, TIER_2 1.25h, TIER_3 1.0h | `app/config.py:986-990` | per-tier decay half-life |
| `EVENT_HALF_LIFE_OVERRIDE` | FOMC_DECISION 4.0, NFP_PRINT 3.0, CPI_PRINT 3.0, PPI_PRINT 3.0, FOMC_MINUTES 2.0, ECB_DECISION 4.0, BOE_DECISION 4.0, GEOPOLITICAL_RISK 6.0 | `app/config.py:995-1004` | per-event half-life override |
| `ASSET_DECAY_MULTIPLIER` | GOLD 1.0, SILVER 1.0, BITCOIN 1.25, OIL 1.0 (no EQUITY) | `app/config.py:1017-1022` | per-asset half-life scale |
| `DECAY_FLOOR` | `0.05` | `app/config.py:1033` | residual weight past the cutoff |
| `TRADING_DAY_CUTOFF_HOURS` | **8 live** / 2.0 default | `app/config.py:203`, `:1028` | age at which decay floors |
| `news_window_hours` | **24 live** / 18.0 default | `app/config.py:201` | ingest admission window |
| `max_article_age_hours` | `26` | `app/config.py:207` | hard article age cap |
| `delta_window_minutes` | `60` | `app/config.py:205` | delta-60m window |
| `news_retention_days` | `2` | `app/config.py:124` | article retention |
| `stale_mass_cap_ratio` | `0.5` | `app/config.py:528` | cap on each side's stale mass vs total fresh mass |
| `update_window_hours` | `6` | `app/config.py:208` | publisher-bump window for "update" articles |
| `CATEGORY_PERSISTENCE` | Central Bank 0.90 … Other 0.40 | `app/analysis/transmission_map.py:51-72` | persistence by category |
| `CUE_SATURATION` | `9.0` | `app/analysis/transmission_map.py:43` | cue weight that equals magnitude 1.0 |
| `INTENSITY_WEIGHT` | `1.5` | `app/analysis/transmission_map.py:84` | intensity-cue weight |

### 6.3 Text quality, routing, relevance

| Parameter | Current Value | Location (file:line) | Meaning |
|---|---|---|---|
| `inversion_max_tokens` | `8` | `app/config.py:285` | subject↔verb token distance |
| `negation_window_tokens` | `4` | `app/config.py:287` | negation reach in tokens |
| `priced_in_multiplier` | `0.45` | `app/config.py:289` | priced-in downweight |
| `priced_in_floor` | `0.25` | `app/config.py:290` | priced-in floor |
| `surprise_multiplier` | `1.35` | `app/config.py:291` | surprise upweight |
| `hedge_multiplier` | `0.70` | `app/config.py:292` | hedge downweight per hit (^3) |
| `hedge_floor` | `0.35` | `app/config.py:293` | hedge floor |
| `price_action_weight` | `0.10` | `app/config.py:288` | **unused in scoring** (see §7) |
| `MIN_RELEVANCE` | `0.20` | `app/analysis/asset_relevance.py:89` | routing floor |
| `CHART_RELEVANCE_FLOOR` | `0.35` | `app/analysis/news_classifier.py:382` | relevance floor for chart-scoped synthesis |
| `QUALITY_MULTIPLIER` (ingestion) | 1.15 / 1.00 / 0.92 / 0.82 | `app/ingestion/base.py:120-125` | **second** quality table (overnight path only) |
| `RELEVANCE` matrix | 21 categories × 5 assets | `app/analysis/asset_relevance.py:18-43` | category → asset relevance |
| `DIRECT_MENTION` | per asset | `app/analysis/asset_relevance.py:46-73` | explicit-mention boost (+0.25 per hit) |
| `OIL_FALSE_FRIENDS` | 24 phrases | `app/analysis/asset_relevance.py:80-86` | oil denylist |
| `BULLISH_CUES` | ~60 phrases | `app/analysis/cue_rules.py:35-64` | bullish cue table |
| `BEARISH_CUES` | ~55 phrases | `app/analysis/cue_rules.py:65-95` | bearish cue table |
| `SPEAKER_TIER` | 1.00–1.50 by speaker | `app/analysis/cue_rules.py:104-144` | central-bank speaker multiplier |
| `GOLD_SESSION_BOOST` | see §4.1 | `app/analysis/cue_rules.py:148-172` | gold session boosts |
| `CATEGORY_KEYWORDS` | 21 categories | `app/analysis/heuristics.py:9-84` | category detection |
| `VERY_HIGH_TRIGGERS` | 32 phrases | `app/analysis/heuristics.py:119-130` | importance = VERY_HIGH |
| `HIGH_TRIGGERS` | 17 phrases | `app/analysis/heuristics.py:138-140` | importance = HIGH |
| `LOW_TRIGGERS` | 11 phrases | `app/analysis/heuristics.py:145-147` | importance = LOW |
| `RUMOR_CUES` | 12 phrases | `app/analysis/heuristics.py:155-156` | verification = UNVERIFIED |
| `PREP_CUES` / `WRAP_CUES` | 13 / 14 phrases | `app/analysis/heuristics.py:149-154` | report_kind detection |
| `NEGATION_RULES` | 24 rules | `app/analysis/text_quality.py:31-59` | negation table |
| `HEDGE_CUES` / `CONFIRMATION_CUES` | 18 / 9 | `app/analysis/text_quality.py:126-135` | certainty table |
| `PRICED_IN_CUES` / `SURPRISE_CUES` | 12 / 9 | `app/analysis/text_quality.py:162-171` | priced-in table |
| `_MAX_REPEAT_HITS` | `2` | `app/analysis/cue_rules.py:197` | repeat cap per cue |
| `_INVERSION_WEIGHT` | `2.0` | `app/analysis/cue_rules.py:200` | inversion weight |

### 6.4 Ranking, transmission, baseline

| Parameter | Current Value | Location (file:line) | Meaning |
|---|---|---|---|
| `DRIVER_RANKINGS` | GOLD/SILVER/BITCOIN/OIL | `app/config.py:1108-1300` | per-asset event-type ranks (default 0.3) |
| `EQUITY_RELEVANCE` | 21 entries | `app/config.py:1310-1331` | equity card relevance table |
| `transmission_map.json` | 66 event types × 5 assets | `app/config/transmission_map.json` | per-asset `[sign, magnitude]` |
| `EVENT_SIGNALS` | phrase → event-type table | `app/analysis/transmission_map.py:517` | event-type detection |
| `CATEGORY_FALLBACK` | all → GENERAL_MACRO | `app/analysis/transmission_map.py:961-981` | non-directional fallback |
| `FOREIGN_CB_POLICY_SCALE` | `0.30` | `app/analysis/policy_scope.py:125` | foreign central-bank policy attenuation |
| `MAX_SURPRISE_WEIGHT` | `2.0` | `app/analysis/release_surprise.py:58` | surprise-weight cap |
| `baseline_window_ticks` | `20` | `app/config.py:270` | trailing ticks in the baseline |
| `baseline_min_ticks` | `5` | `app/config.py:273` | warm-up threshold |
| `baseline_statistic` | `"median"` | `app/config.py:274` | median or mean |
| `baseline_enabled` | `True` | `app/config.py:277` | feature switch |
| `baseline_min_score` | `12.0` | `app/config.py:279` | below this the raw score is published uncentred |
| `score_damping_k` | `1.0` | `app/config.py:242` | damped-score k |
| `CONTRADICTION_DAMPENER_ENABLED` | `True` | `app/config.py:1049` | score dampener switch |
| `CONTRADICTION_RATIO` | `0.6` | `app/config.py:1051` | min/max ratio that triggers the dampener |
| `CONTRADICTION_DAMPENER_FACTOR` | `0.5` | `app/config.py:1053` | dampener multiplier |
| `CLUSTER_CONFLICT_WEIGHT_SHARE` | `0.05` | `app/config.py:1064` | conflicted-weight share threshold |
| `CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT` | `0.5` | `app/config.py:1066` | total-weight floor for the conflict flag |
| `RUMOR_MULTIPLIER` | `0.5` | `app/config.py:1096` | tier multiplier for unverified items |
| `CONFIRMED_BOOST` | `1.05` | `app/config.py:1099` | tier multiplier for confirmed items |
| `gold_silver_bleed` | `0.30` | `app/config.py:435` | gold↔silver bleed factor |
| `GLOBAL_MACRO_WEIGHT` | `0.65` | `app/analysis/equity_card.py:136` | global-macro weight on the equity card |
| `CATEGORY_SESSION_WEIGHT` | 4 sessions × categories | `app/market/session_detector.py:109-130` | session relevance curve |
| `EQUITY_SUB_SESSION_WEIGHT` | 4 sub-sessions | `app/market/session_detector.py:225-286` | US equity sub-session curve |
| `ASSET_SESSION_PROFILES` | XAUUSD metal, XAGUSD metal, BTCUSD flat, EQUITY metal | `app/market/session_detector.py:336-341` | per-asset session profile (**no USOIL entry**) |
| `WEEKEND_MULTIPLIER` | metal 0.70, flat 1.00 | `app/market/session_detector.py:343-347` | weekend discount |

### 6.5 Dedup, clustering, calendar, LLM, ops

| Parameter | Current Value | Location (file:line) | Meaning |
|---|---|---|---|
| `dedup_similarity_threshold` | `0.72` | `app/config.py:140` | ingest dedup threshold |
| `dedup_time_window_hours` | `12` | `app/config.py:141` | ingest dedup window |
| `corroboration_per_source` | `0.10` | `app/config.py:146` | per-independent-source boost |
| `corroboration_max_boost` | `0.30` | `app/config.py:147` | corroboration cap |
| `corroboration_enabled` | `True` | `app/config.py:154` | corroboration switch |
| `preview_suppression_enabled` | `True` | `app/config.py:151` | suppress previews after an actual print |
| `only_high_impact_counts` | `True` | `app/config.py:161` | impact gate switch |
| `drop_low_impact_articles` | `True` | `app/config.py:168` | one-shot deletion switch |
| `only_llm_verdict_may_drop` | `True` | `app/config.py:173` | only model-judged rows may be deleted |
| `chart_scope_isolation` | `True` | `app/config.py:183` | per-chart card isolation |
| `chart_scope_single_owner` | `False` | `app/config.py:188` | strict one-owner mode |
| `chart_scope_medium_counts` | `True` | `app/config.py:194` | MEDIUM chart-scoped stories count |
| `CALENDAR_NEWS_DUP_FACTOR` | `0.35` | `app/services/pipeline.py:288` | news duplicating a calendar print |
| `economic_calendar_enabled` | `True` | `app/config.py:558` | calendar switch |
| `economic_calendar_min_importance` | `"MEDIUM"` | `app/config.py:560` | calendar importance floor |
| `economic_calendar_horizon_hours` | `48` | `app/config.py:562` | calendar lookahead |
| `calendar_region_filter_enabled` | `True` | `app/config.py:574` | region whitelist switch |
| `calendar_providers` | **"financialjuice,faireconomy" live** / "financialjuice" default | `app/config.py:577` | provider list (**faireconomy not implemented**) |
| `financialjuice_calendar_enabled` | **True live** / False default | `app/config.py:587` | FJ calendar switch |
| `fj_calendar_poll_seconds` | `600` | `app/config.py:603` | calendar cadence |
| `fj_calendar_burst_seconds` | `60` | `app/config.py:608` | cadence near a HIGH release |
| `fj_calendar_pre_release_minutes` / `post` | `15` / `30` | `app/config.py:606-607` | burst window |
| `fj_calendar_min_rows` / `timeout_seconds` / `backoff_minutes` | `10` / `40` / `30` | `app/config.py:590,592,594` | render guards |
| `fj_calendar_render_timezone` | `"UTC"` | `app/config.py:600` | browser context TZ (must stay UTC) |
| `calendar_merge_similarity` / `time_tolerance_minutes` | `0.85` / `5` | `app/config.py:611-612` | cross-provider merge |
| `calendar_country_multipliers` | `USD=1.0,EUR=0.30,GBP=0.20,JPY=0.15,CNY=0.25,CHF=0.05,CAD=0.05,AUD=0.05,NZD=0.05` | `app/config.py:625-627` | per-country weights |
| `calendar_country_multiplier_default` | `0.05` | `app/config.py:629` | unlisted country |
| `calendar_country_asset_overrides` | `CNY:XAGUSD=0.8` | `app/config.py:633` | asset override |
| `calendar_upcoming_contributes` | `False` | `app/config.py:701` | unreleased prints score nothing |
| `calendar_awaiting_release_*` | grace 10 min, timeout 120 min, penalty 0.35 | `app/config.py:656-658` | stale expectation handling |
| `calendar_composite_*` | enabled, window 15 min, max weight 1.30, mode `per_asset` | `app/config.py:664-675` | same-timestamp print grouping |
| `calendar_polarity_enabled` / `calendar_inverse_indicators` / `calendar_core_bonus` | `True` / `unemployment rate=-1,jobless claim=-1,…` / `0.20` | `app/config.py:679-687` | per-indicator polarity |
| `surprise_std_default` / `surprise_neutral_band` / `surprise_strong_band` | `1.0` / `0.30` / `1.00` | `app/config.py:336-338` | calendar surprise bands |
| `surprise_std_table` | per-indicator σ values | `app/config.py:339-357` | **partly [ESTIMATE]** (see §7) |
| `earnings_mega_cap_names` / `_mult` / `earnings_unnamed_mult` | 16 names / `2.0` / `0.3` | `app/config.py:708-713` | earnings weighting |
| `llm_model` | `meta/llama-3.2-11b-vision-instruct` | `app/config.py:68` (live `.env`) | model id |
| `llm_base_url` | `https://integrate.api.nvidia.com/v1` | `app/config.py:64` (live `.env`) | endpoint |
| **LLM API key** | *(not printed)* | loaded from `.env` → `LLM_API_KEY` → `Settings.llm_api_key`, `app/config.py:61-63` (alias `DEEPSEEK_API_KEY`) | auth |
| `llm_timeout_seconds` | **45 live** / 60 default | `app/config.py:72` | HTTP timeout |
| `llm_max_concurrent` | **3 live** / 2 default | `app/config.py:76` | in-flight model calls |
| `llm_requests_per_minute` | `40` | `app/config.py:85` | client-side rolling-60s rate limit |
| `llm_max_tokens` | **3000 live** / 1024 default | `app/config.py:89` | response budget |
| `llm_json_mode` | `True` | `app/config.py:95` | `response_format={"type":"json_object"}` |
| `llm_disable_thinking` | **False live** / True default | `app/config.py:101` | reasoning-model toggle |
| `llm_enabled` | `True` | `app/config.py:104` | LLM switch |
| `llm_batch_limit` | `15` | `app/config.py:452` | articles classified per tick |
| `llm_tick_budget_seconds` | `90` | `app/config.py:453` | wall-clock model budget per tick |
| `llm_priority_sources` | "TradingView API News,TradingView Equity News" | `app/config.py:486` (live `.env`) | LLM budget reservation |
| `llm_priority_slots` | `2` | `app/config.py:488` | reserved slots |
| `llm_priority_max_age_hours` | **2.0 live** / 1.0 default | `app/config.py:490` | age bound on the reservation |
| `LLM_PER_ARTICLE_TIMEOUT` | `90` s | `app/analysis/news_classifier.py:60` | per-article model wall clock |
| `LLM_COOLDOWN_SECONDS` | `150.0` | `app/services/pipeline.py:92` | cooldown after zero verdicts |
| `poll_interval_seconds` | `180` | `app/config.py:109` | tick cadence |
| `HEALTH_STALE_MINUTES` | `15` | `app/services/pipeline.py:314` | staleness threshold |
| `MAX_TICK_DURATION_SECONDS` | `600` | `app/services/pipeline.py:309` | tick hard cap |
| `TICK_HEARTBEAT_SECONDS` | `60` | `app/services/pipeline.py:319` | heartbeat interval |
| `HEARTBEAT_STALE_SECONDS` | `420` | `app/services/pipeline.py:331` | hung-tick threshold |
| `_BIAS_LOOKBACK` | `500` | `app/main.py:368` | snapshot lookback for "bias since" |
| `TradingViewAPISource.PER_CHART_ITEMS` | `10` | `app/ingestion/tradingview_api.py:409` | items per chart query |
| TradingView cache TTL / min interval | `300` s / `0.5` s | `app/ingestion/tradingview_api.py:194`, `:52` | rate limiting |
| `outcome_capture_enabled` / `outcome_horizon_minutes` / `outcome_min_move_pct` / `outcome_max_lookback_minutes` / `outcome_price_retention_hours` | `True` / `30` / `0.05` / `10` / `72` | `app/config.py:543-555` | the measurement loop |
| `morning_brief_*` / `overnight_*` | on_boot True, min_gap 90 min, max_gap 24 h, wait_idle 300 s, startup_grace 15 s, counts_as_fresh True | `app/config.py:733-759` | overnight catch-up |
| `market_timezone` / `display_timezone` | `America/New_York` / `Asia/Kolkata` | `app/config.py:52-53` | "today" and display |
| `assets` | **"XAUUSD,XAGUSD,BTCUSD,USOIL" live** / "XAUUSD,XAGUSD,BTCUSD,EQUITY,USOIL" default | `app/config.py:408` | TradingView chart feeds |
| `bias_snapshot_enabled` | `True` | `app/config.py:370` | audit log switch |
| `price_data_enabled` | **True live** / False default | `app/config.py:375` | price feed switch (BTC only) |
| `alert_on_importance` / `alert_on_strength` | `"VERY_HIGH"` / `"VERY_HIGH"` | `app/config.py:390-391` | alert thresholds |
| `decision_log_path` | `logs/decisions.jsonl` | `app/config.py:496` | audit log |
| `CONFIDENCE_CALIBRATION_ENABLED` | `False` | `app/config.py:1083` | calibration switch (**off**) |
| `CONFIDENCE_CALIBRATION` | `((0.0,0.0),(1.0,1.0))` | `app/config.py:1084-1087` | identity map (**unfitted**) |
| `CONFIDENCE_CALIBRATION_MIN_SAMPLES` / `_BUCKETS` | `30` / `(0.0,0.1,0.2,0.3,0.45,0.6,0.8,1.01)` | `app/config.py:1090-1092` | fitter guards |

---

## Section 7: Known Gaps / Dead Code

### 7.1 Defined but never called

| Symbol | Location | Note |
|---|---|---|
| `sentiment_engine.score_from_signals()` | `app/analysis/sentiment_engine.py:1876-1879` | no call site anywhere in `app/`, `tools/` or `tests/` |
| `config.decay_default_hours` | `app/config.py:302` | never read |
| `config.decay_half_lives` + `Settings.half_lives` | `app/config.py:303-310`, `:849-852` | self-referential only; `config.py:298-301` states "nothing in the scoring path reads them" |
| `regime.REGIME_ASSET_BIAS` | `app/market/regime.py:39-51` | defined; never referenced outside its own definition |
| `RegimeAssessment.bias_for()` | `app/market/regime.py:61-62` | never called — the regime does not modulate any score |
| `price_data.enrich_asset_signals()` | `app/market/price_data.py:210-212` | never called |
| `price_data.get_price_reading()` | `app/market/price_data.py:161-207` | called only from `enrich_asset_signals`; the pipeline uses `_price_note()` (`pipeline.py:1436-1440`) instead |
| `config.price_action_weight` | `app/config.py:288` | never read |
| `sentiment_engine.WeightedSignal.contradiction_detected` | `app/analysis/sentiment_engine.py:651-660` | deprecated alias, still read by `equity_card.py:548-550` |
| `SentimentResult.contradiction_detected` | `app/analysis/sentiment_engine.py:740-749` | deprecated alias; persisted as a duplicate of `cluster_conflict` (`pipeline.py:2146-2147`) |

### 7.2 Duplicate / conflicting definitions

| Item | Location | Note |
|---|---|---|
| Two source-quality multiplier tables | `app/analysis/sentiment_engine.py:193-198` (1.00/0.97/0.94/0.90) and `app/ingestion/base.py:120-125` (1.15/1.00/0.92/0.82) | the scoring path uses the first; the second is reached only via `news_fetcher.quality_multiplier` → `recency_engine.py:95-101` (overnight path) |
| Duplicate `BTC_ETF_FLOW` intrinsic rule | `app/analysis/transmission_map.py:375-378` | the "outflow" tuple appears twice, identically |
| Two stopword sets | `app/analysis/event_deduplication.py:24-31` and `app/analysis/event_clustering.py:45-53` | near-identical, not shared |

### 7.3 TODO / FIXME / HACK / placeholder markers

| Marker | Location |
|---|---|
| `TODO(unify-fj): port fj_sessions onto the transmission map and the NO_BIAS contract, then delete the marker below.` | `app/main.py:884` |
| `TODO(verify-std): replace every [ESTIMATE] with the real historical standard deviation …` | `app/config.py:325` |
| `# [ESTIMATE] TODO(verify-std)` | `app/config.py:344` (nonfarm/nfp/payroll), `:345` (employment change), `:346` (jobless claim), `:350` (fomc/federal reserve/powell), `:352` (gdp/pmi/ism), `:353` (industrial production/retail sales) |
| Golden-corpus placeholders: "Every ``expected`` field is currently the placeholder ``\"TODO\"``" | `tests/golden_runner.py:21`, `:44-45`, `:258`, `:264` |

No `FIXME`, `HACK` or `XXX` markers exist in the source.

### 7.4 Listed but not scored / skipped

| Item | Status | Evidence |
|---|---|---|
| `Asset.EQUITY` | **Listed in the enum and in `settings.assets` (default), but no `SentimentSnapshot` is written.** It is served from the session-aware in-memory card. | `app/services/pipeline.py:2030-2039` (`if asset is Asset.EQUITY: ... continue`); `app/main.py:643-644` skips it in `build_payload` |
| `MARKET_PREP` / `MARKET_WRAP` scopes | **Never written.** The API serves them, so they always return the empty state. | written only for `"CURRENT"` (`pipeline.py:2103-2105`); served at `app/main.py:933-934`; `tools/verify_live_cards.py:16` reads the non-existent scope |
| `EQUITY` in the live `ASSETS` list | **Absent from the live `.env`** `ASSETS=XAUUSD,XAGUSD,BTCUSD,USOIL` — so the TradingView metals feed does not query an equity chart. Equity has its own source instead. | `.env` `ASSETS=`; `app/ingestion/tradingview_api.py:437` |
| `CoinGlass` (`.env` block) | **Not implemented.** No module, no settings field. `extra="ignore"` swallows the keys silently. | `app/config.py:37`; no `coinglass` file under `app/` |
| `faireconomy` calendar provider | **Named in `.env`** `CALENDAR_PROVIDERS=financialjuice,faireconomy` but only `financialjuice` is implemented. | `app/ingestion/economic_calendar.py:125-127` |
| `MARKET_DATA_POLL_SECONDS=60` | **No such settings field** — silently ignored. | `app/config.py:37` (`extra="ignore"`) |
| `USOIL` in `ASSET_SESSION_PROFILES` | **Missing** → falls back to `"metal"` (session curve + weekend discount). | `app/market/session_detector.py:336-341`, `:366` |
| `EQUITY` in `ASSET_DECAY_MULTIPLIER` | **Missing** (deliberate) — the equity card applies its own session logic. | `app/config.py:1015-1016` |
| `AssetImpact.signed_contribution` | Written only on the overnight path (`app/services/overnight.py:581`); always `0.0` on the live recompute path. | `app/models/db_models.py:141` |
| `AssetImpact.price_confirmation` | Default `"UNAVAILABLE"`; never written on the live path (price confirmation was removed from the math). | `app/models/db_models.py:143`; `app/analysis/text_quality.py:228-236` |
| `GOLD_SESSION_BOOST` for Silver | Applied only when `asset == "XAUUSD"`; silver gets the plain session curve. | `app/market/session_detector.py:401-404` |
| `contradiction_detected` (old flag) | "Has never fired in production" per `docs/SCORING.md:355-357` — the production path is `cluster_conflict` / `evidence_split`. | `app/analysis/sentiment_engine.py:740-749` |

### 7.5 Stale documentation inside the repo

- `docs/SCORING.md:30-33` says only three assets are scored. `Asset.OIL` is live and scored.
- `docs/SCORING.md:41` and `:64` state `assetSensitivity … 0.5 when no event type was identified`; the code also clamps to a minimum of `0.3` (`sentiment_engine.py:1050`) — the doc omits the floor.
- `docs/SCORING.md:101-103` quotes the burst bonus as `×1.20` (≥3) and `×1.30` (≥5); the code uses `×1.02` and `×1.05` (`sentiment_engine.py:1236-1243`).
- `docs/SCORING.md:62-64` quotes the half-lives and asset multipliers correctly, but omits `OIL 1.0` (added at `config.py:1021`).

---

## Section 8: Test & Validation Status

### 8.1 Tests

**Location:** `tests/` — **82 `test_*.py` files, ~1,300 test functions** (counted by `def test_`).

**Runner config** (`pytest.ini`):

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q --tb=short -m "not network"
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
markers =
    golden: Step 0 baseline harness (tests/golden) - selects the labelled headline corpus
    network: marks tests that require live network calls (run with -m network)
```
`[verbatim from pytest.ini:1-11]`

Network-marked tests are excluded from the default run; the `golden` marker is **not** excluded, so the golden corpus runs in the default suite.

**What the suites assert (selected, by file):**

| File | Tests | Asserts |
|---|---|---|
| `tests/test_sentiment_engine.py` | 29 | the core `compute()` contract: binary bias, EPS tie-break, carry-forward, stability gates, confidence |
| `tests/test_analysis.py` | 37 | classifier / analysis behaviour |
| `tests/test_display.py` | 57 | `to_display` split, ratio path, confidence shrink, label consistency |
| `tests/test_event_clustering.py` | 45 | clustering identity, title similarity, corroboration cap, same-source collapse |
| `tests/test_regional_bias.py` | 57 | region whitelist, region weights, per-region bias |
| `tests/test_fj_calendar.py` | 53 | FinancialJuice calendar parsing/rendering guards |
| `tests/test_fj_sessions.py` | 62 | the legacy FJ five-box scorer |
| `tests/test_transmission_map.py` | 37 | the transmission map: signs, magnitudes, resolution |
| `tests/test_text_quality.py` | 36 | negation, hedge, priced-in, price-action detection |
| `tests/test_today_filter.py` | 33 | the "today" predicate and rolling window |
| `tests/test_phase2_snapshots.py` | 30 | snapshot persistence contract |
| `tests/test_release_surprise.py` | 27 | ACTUAL-print parsing and surprise direction |
| `tests/test_overnight.py` | 27 | the overnight catch-up |
| `tests/test_economic_calendar.py` | 23 | calendar evaluation, polarity, composite clustering |
| `tests/test_outcome_capture.py` | 20 | PENDING → WORKED/FAILED/Mixed resolution |
| `tests/test_bias_state.py` | 21 | bias history, flip timing, warm-up |
| `tests/test_symmetry.py` / `test_polarity_symmetry.py` | 21 / 11 | bullish/bearish symmetry of the rule engine |
| `tests/test_step10_calibration.py` | 17 | the (disabled) calibration applier |
| `tests/test_step11_docs.py` | 11 | that `docs/TRANSMISSION_MAP.md` and `docs/SCORING.md` match the code, and that retired constants stay retired (`tests/test_step11_docs.py:128-147`) |
| `tests/test_usoil_recompute.py` | 5 | that `USOIL` is routable and scored, and that a non-enum symbol (`XPTUSD`) is skipped rather than crashing the tick (`:96-109`, `:188-195`) |
| `tests/test_deduplication.py` | 7 | ingest dedup similarity |
| `tests/test_step9_dedup.py` | 15 | dedup + clustering integration |
| `tests/test_stale_mass_cap.py` | 5 | the stale-mass cap |
| `tests/test_impact_gate.py` | 6 | the HIGH-only impact gate |
| `tests/test_tier_multipliers.py` | 10 | tier multipliers and half-lives |
| `tests/test_foreign_cb_policy.py` | 13 | foreign-central-bank attenuation |
| `tests/test_one_shot_llm_policy.py` | 12 | one-shot classification and deletion |
| `tests/test_llm_concurrency_phase.py` | 7 | concurrent Phase-A classification |
| `tests/test_golden_corpus` (3 tests) | — | the golden corpus (§8.2) |

Also present (not assertions, but harnesses/probes): `tests/probe_sources.py`, `tests/probe_fj_calendar.py`, `tests/probe_chromium_overlap.py`, `tests/probe_llm.py`, `tests/fixed_clock.py`, `tests/conftest.py`.

### 8.2 The golden corpus

- Corpus: `tests/golden/headlines.jsonl`.
- Runner: `tests/golden/golden_runner.py`.
- Pytest wiring: `tests/golden/test_golden_corpus.py` — one parametrised case per headline (`:44-93`), plus `test_golden_corpus_is_coherent` (`:96-136`) and `test_golden_corpus_reproduces_the_engine` (`:139-145`).
- Semantics: a row flagged `known_defect` asserts the engine's **current (wrong)** output — it passes today and turns red when the mapping is fixed; unflagged rows assert the human ground truth (`tests/golden/test_golden_corpus.py:13-18`).
- Invariants asserted: ≥60 rows; every asset represented; no duplicate title for the same asset; at least one `known_defect`; at least one clean row (`:96-136`).
- The corpus is built by `tools/build_golden_corpus.py`.

### 8.3 Backtest / IC / accuracy measurement

**There is NO backtest and NO information-coefficient (IC) computation anywhere in the repo.** A repo-wide search for `spearman`, `information coefficient`, `IC` and `backtest` across `app/`, `tools/` and `tests/` returns **zero matches**.

What *does* exist:

| Tool / module | Metric | Window | Evidence |
|---|---|---|---|
| `app/services/outcomes.py` | PENDING → WORKED / FAILED / Mixed, from the price at the call vs. the price a horizon later | `outcome_horizon_minutes` = 30 min; dead band `outcome_min_move_pct` = 0.05%; entry/exit must be within `outcome_max_lookback_minutes` = 10 min of the window edges | `app/config.py:543-555`; `app/services/outcomes.py:104-360` |
| `tools/calibration_report.py` | Hit rate per confidence bucket, joining `bias_outcome.bias_snapshot_id → bias_snapshots`; Mixed counts 0.5 | whole history in the DB | `tools/calibration_report.py:53-79`, `:46-50` |
| `tools/hitrate_report.py` | Hit rate by confidence bucket and by asset, against an **external price CSV** (`timestamp,close`) | `--horizon` in minutes, default 30 | `tools/hitrate_report.py:1-54` |
| `tools/bias_harness.py` | Bias harness, clustering over the real `EventClusterer` | — | `tools/bias_harness.py:138-145` |
| `tools/baseline_snapshot.py` | Baseline snapshot helper | — | `tools/baseline_snapshot.py` |

**Limitation, as built:** only **BITCOIN** has a reachable price feed (`app/market/price_data.py:33`), so the outcome loop can only resolve Bitcoin calls. GOLD, SILVER, OIL and EQUITY calls stay `PENDING` indefinitely (`app/services/outcomes.py` docstring, "THE HONEST LIMITS"). Consequently `CONFIDENCE_CALIBRATION_ENABLED` is `False` and `calibrate_confidence()` is an identity (`app/config.py:1083-1087`, `app/analysis/confidence_calibration.py:72-102`).

**Nothing in the measurement loop feeds back into scoring** — the engine never reads `bias_outcome` or `price_point` when deciding a bias (`app/services/outcomes.py` docstring, "NOTHING HERE FEEDS BACK INTO SCORING").

---

## Observations (No Action Taken)

- `docs/SCORING.md:30-33` states only three assets are scored; `Asset.OIL` ("USOIL") is live and is scored and persisted (`app/models/enums.py:21`, `app/services/pipeline.py:2030`).
- `docs/SCORING.md:101-103` quotes the burst bonus as ×1.20/×1.30; the code uses ×1.02/×1.05 (`app/analysis/sentiment_engine.py:1236-1243`).
- `docs/SCORING.md:41` omits the `max(0.3, …)` floor on asset sensitivity (`app/analysis/sentiment_engine.py:1050`).
- `MARKET_PREP` and `MARKET_WRAP` scopes are served by `/api/sentiment/{asset}` but are never written, so both always return the empty state (`app/main.py:933-934` vs `app/services/pipeline.py:2103-2105`).
- `MARKET_REGIME` is computed every tick and published, but `REGIME_ASSET_BIAS` / `bias_for()` are never called — the regime has no effect on any score (`app/market/regime.py:39-62`).
- The `.env` CoinGlass block (`COINGLASS_ENABLED`, `COINGLASS_POLL_SECONDS`, `COINGLASS_ASSETS`, `COINGLASS_BTC_URL`) has no implementation and no settings field; `extra="ignore"` swallows it silently (`app/config.py:37`).
- `.env` sets `CALENDAR_PROVIDERS=financialjuice,faireconomy`, but only `financialjuice` exists in code (`app/ingestion/economic_calendar.py:125-127`).
- `.env` sets `MARKET_DATA_POLL_SECONDS=60`, which is not a field on `Settings` and is silently ignored (`app/config.py:37`).
- Two different source-quality multiplier tables exist with different values (`app/analysis/sentiment_engine.py:193-198` vs `app/ingestion/base.py:120-125`).
- `config.decay_half_lives` and `config.decay_default_hours` are declared but never read by the scoring path (`app/config.py:302-310`, `:849-852`).
- `sentiment_engine.score_from_signals()` is defined but never called (`app/analysis/sentiment_engine.py:1876`).
- `price_data.enrich_asset_signals()` / `get_price_reading()` are never called by the pipeline; `_price_note()` returns a static string instead (`app/market/price_data.py:161-212`, `app/services/pipeline.py:1436-1440`).
- `config.price_action_weight` is declared but never read (`app/config.py:288`); `directional_multiplier()` is a constant 1.0 (`app/analysis/text_quality.py:228-236`).
- The `BTC_ETF_FLOW` "outflow" intrinsic rule is duplicated verbatim in the table (`app/analysis/transmission_map.py:375-378`).
- `USOIL` has no entry in `ASSET_SESSION_PROFILES`, so oil inherits the metals' session curve and weekend discount (`app/market/session_detector.py:336-341`, `:366`).
- `GOLD_SESSION_BOOST` is applied only to `XAUUSD`, not to `XAGUSD` (`app/market/session_detector.py:401-404`).
- `AssetImpact.signed_contribution` and `AssetImpact.price_confirmation` are never populated on the live recompute path (`app/models/db_models.py:141`, `:143`).
- The confidence calibration table is unfitted and the switch is off, so the 0.40/0.28/0.22 coefficients have never been fitted against outcomes (`app/config.py:1083-1087`).
- The outcome loop can only resolve Bitcoin calls because only BTC has a reachable price feed, so the other four assets' labels stay `PENDING` (`app/market/price_data.py:33`).
- There is no backtest and no information-coefficient computation anywhere in the repository.
- `tests/golden_runner.py` (the top-level harness) still uses the `"TODO"` placeholder for every `expected` field (`tests/golden_runner.py:21`, `:44-45`), while the separate `tests/golden/` corpus is fully labelled.
- `tools/verify_live_cards.py:16` reads the `MARKET_PREP` scope, which is never populated.
