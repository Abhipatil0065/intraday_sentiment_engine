# STEP 0 — UNDERSTANDING REPORT (read-only, no code changed)

Repo: `C:\Users\HP\.vscode\intraday_sentiment_engine`
Baseline: `python -m pytest` → **1227 passed** (5m19s).
Baseline captured 2026-10-13. Nothing in this report changed any file.

> ⚠️ Run pytest with `workdir = C:\Users\HP\.vscode\intraday_sentiment_engine`.
> Running it from `C:\Users\HP\.vscode` picks up an unrelated `baseline_check/`
> folder and pytest aborts with an INTERNALERROR.

---

## 1. Data flow: ingestion → final daily bias

```
rss_reader / website_monitor / financialjuice / kitco / economic_calendar
      / market_data (tvDatafeed: DXY, IEF) / coinglass_scraper
        v  RawArticle{title, published_raw, updated_raw, source, url, body}
  ── TODAY / AGE FILTER ────────────────────────────────────────────────
  timestamp_parse.parse_timestamp -> aware UTC, or REJECTED (never fabricate)
  within_rolling_window(NEWS_WINDOW_HOURS=24 .env) + within_max_age(26h)
  an article re-stamped today -> is_updated_article
        v
  ── EVENT DEDUPLICATION ───────────────────────────────────────────────
  EventDeduplicator (0.5*jaccard + 0.5*containment, ≥0.72, 12h window)
  one cluster = one NewsEvent row (event_key, article_count = corroboration)
        v
  ── CLASSIFICATION (ONE LLM call max per article) ─────────────────────
  news_classifier.analyze_article -> importance, strength, direction PER
  ASSET, category, report_kind (PREP/WRAP/GENERAL/ACTUAL_DATA)
  MEDIUM/LOW verdict from the model => row DELETED + guid suppressed today
        v
  ── PERSIST ───────────────────────────────────────────────────────────
  NewsArticle 1─* AssetImpact (one row per asset per article)
        v
  ── RECOMPUTE (every tick, POLL_INTERVAL_SECONDS=180) ─────────────────
  _recompute_sentiment() reads today's rows -> build_signal() per
  (article x asset) => WeightedSignal  [+ gold/silver bleed signals]
  + calendar signals (RELEASED prints only by default)
  + market_data signals (DXY / IEF %, tvDatafeed)
  + CoinGlass signals (CVD / footprint / heatmap)
  session-change reweight of session_multiplier (in memory only)
  _suppress_previews()  ->  _downweight_news_duplicating_calendar() (x0.35)
  -> cluster_signals(window_hours=0.5)
        v
  ── SCORING ───────────────────────────────────────────────────────────
  sentiment_engine.compute(signals, asset)  per asset  (see §2)
  market_prep.build()   -> "MARKET_PREP" scope  (re-runs compute())
  market_wrap.build()   -> "MARKET_WRAP" scope   (re-runs compute())
  equity_card.build_equity_card(signals) -> one region card (in memory)
        v
  ── PERSIST / PUBLISH ─────────────────────────────────────────────────
  SentimentSnapshot row per (asset x scope x tick)
  write_bias_snapshots(AssetVerdict)  + DecisionLog JSONL (logs/decisions.jsonl)
  /api/dashboard, /api/sentiment/{asset}, /api/financialjuice/sessions, WS /ws
```

Three separate "today" definitions coexist (a known drift source):
1. **market-day predicate** — `MARKET_TIMEZONE` (`America/New_York`) calendar day,
   SQL twin of `timeutils.market_day_start_utc()`.
2. **rolling window** — `NEWS_WINDOW_HOURS` (.env **24**, code default 18) +
   `MAX_ARTICLE_AGE_HOURS=26`.
3. **IST calendar day** — `fj_sessions.py` hard-codes `Asia/Kolkata`, which is
   why the FinancialJuice source is carved out of the ET date test.

## 2. Where sentiment is scored

| File | Role |
|---|---|
| `app/analysis/sentiment_engine.py` | **The only scoring path.** `_signal_weight()` (decay + all multipliers), `_aggregate()` (bull/bear sums + burst bonus), `compute()` (score, confidence, binary bias, hysteresis). |
| `app/analysis/recency_engine.py` | **Legacy bucket table.** `recency_weight` is now only used by `market_wrap` (fresh vs older split) and `overnight` (persisted weights). *Not* the scoring decay. |
| `app/analysis/equity_card.py` | Region-filtered wrapper that re-tags signals `asset=Asset.EQUITY` and calls the same `compute()`. |
| `app/analysis/fj_sessions.py` | A **parallel, independent** scoring system for the FJ five-box report. Reads no settings. (Known bug: line 302 overwrites `bear_score` with `bull_score`, so all five boxes publish 50/50.) |

Weight formula (one magnitude, one event weight — nothing is counted twice):

```
weight = decay
       x relevance x persistence x source_quality
       x session_multiplier (x BTC UTC-hour curve)
       x corroboration x surprise x tier_multiplier
       x metal_mult x earnings_mult x asset_sensitivity x speaker_tier

contribution = weight x STRENGTH_MULT[direction side]
score        = 100 * (bull - bear) / (bull + bear)      -> -100..+100
confidence   = f(conviction, breadth, agreement, certainty)
bias         = sign(score)  (BULLISH | BEARISH only)
```

Aggregation of the same signal list happens in exactly one place
(`_aggregate`), called by `compute`, `market_prep`, `market_wrap` and
`equity_card` — all four therefore share the identical weights.

## 3. Timestamps and time zones

* All timestamps are **aware UTC**; `tzdata` is installed and `ZoneInfo` is used
  everywhere. `MARKET_TIMEZONE=America/New_York` defines "today",
  `DISPLAY_TIMEZONE=Asia/Kolkata` is display-only.
* SQLite strips `tzinfo`, so every read path funnels through `_ensure_aware()`
  before subtracting (that guard is why `sentiment_engine._age_hours` and
  `_now_aware` exist).
* Both timestamps exist on a row: `published_at_utc` and `created_at`.
  **Age is always measured from `published_at_utc`; `created_at` is never used
  for age** (`decay_weight(s.published_at, ...)`). This already satisfies the
  Step 2 rule.
* Unparseable publisher timestamps are rejected at ingest, never guessed.
  Timestamps >2h in the future are rejected as a broken feed/clock.

## 4. Existing decay and weighting (what Step 1 replaces / sits next to)

| Mechanism | Where | Formula | Status |
|---|---|---|---|
| **`decay_weight`** | `sentiment_engine.py:524-551` | `0.5 ** (age_h / (TIER_HALF_LIVES[tier] * ASSET_DECAY_MULTIPLIER[asset]))`, tiers `1.5/0.75/0.25 h`, asset `XAU 1.0, XAG 1.0, BTC 1.25, EQUITY 0.75`, hard zero at `trading_day_cutoff_hours` (.env **8h**) | **active, single-speed** |
| `recency_weight` | `recency_engine.py:21-44` | step buckets 1.00/0.90/0.80/0.65/0.50/0.30 → 0.15 floor (≤15/30/60/120/240/480 min) | legacy, wrap+overnight only |
| `signed_contribution` fade | `recency_engine.py:116-135` | `1 - progress*(1-mag)` when `mag < 0.8` | only via `compute_final_weight` (overnight) |
| event tier | `event_tier()` | importance/category/report_kind → TIER_1/2/3 → multiplier 1.00/0.45/0.15 | active |
| session | `session_detector.py` | NY-hour windows ASIA 20-04, EUROPE 03-12, US 08-17 (highest-liquidity wins); equity sub-sessions PRE/REGULAR/AH; weekend x0.70 for metals | active |
| surprise | `release_surprise.py` | `(actual-forecast)/std` → `1 + 1*(|z|/(1+|z|))`, capped 2.0 | active |
| dedup / corroboration | `event_deduplication.py`, `event_clustering.py` | blended similarity 0.72 / 30 min window + `corroboration_multiplier` | active |
| event risk | `calendar_risk.py:89-123` | 45-min lookahead warning + note. **No NO_TRADE / blackout state anywhere** | active but advisory |
| cross-asset confirmation | `market_data.py` (DXY/IEF → signals, not a confidence gate) | explicit-ish | active as signals only |
| hysteresis / stability | `sentiment_engine.py:348-423` | FLIP_MARGIN (8, .env 5), COOLING_MARGIN, FLIP_HOLD_MINUTES, Tier-1 bypass | active |

Config is **`.env` via pydantic-settings + one JSON file**
(`app/config/transmission_map.json`). There is **no YAML anywhere** in the repo
(PyYAML 6.0.3 is installed but unused).

## 5. What the requested feature needs, and what already exists

Present and reusable: `WeightedSignal` pool, per-asset/per-region history keys,
hysteresis primitives, surprise maths, dedup/clustering, `tvDatafeed` channel for
futures/VIX/currency, decision log + `bias_snapshots` + `hitrate_report` for
scoring realised outcomes.

Absent (all new work): two-speed (`short+long`) decay, `config/decay.yaml`,
**effective-minutes** market clock (open-minutes only + holidays + overnight
carry), the event-type decay table, `tanh` bounded score on -1..+1, three
*simultaneous* equity region scores with rotation/carry-over, region-relevance
tags, same-topic override, pre-event blackout → `NO_TRADE`, `fresh_share` /
`valid_until` / `top_drivers` / `next_high_impact_event` output, calibration and
backtest scripts.

## 6. Environment facts that shape the plan

| Fact | Consequence |
|---|---|
Installed in `.venv`: `pandas 2.3.3`, `numpy 2.5.3`, `PyYAML 6.0.3`, `tvDatafeed`, `feedparser`, `SQLAlchemy`, `APScheduler`.
**Not installed:** `scipy`, `pandas_market_calendars`, `exchange_calendars`. | Step 2's "holiday calendars per exchange" needs either a `pip install` or a built-in holiday table. |
`tvDatafeed` already drives the live DXY/IEF signals. | Step 4's index futures / VIX / USDJPY / EURUSD inputs can ride the same channel that is already proven in this repo. |
No git repo. | Recommend `git init` + one baseline commit before Step 1, so every step is revertible. |
`.env` `ASSETS = XAUUSD,XAGUSD,BTCUSD` (EQUITY excluded). | Equity cards are built from the free-form signal pool, not from `ASSETS`, so they still run — but the new engine's asset list must not read `ASSETS` naively. |
`TRADING_DAY_CUTOFF_HOURS=8`, `NEWS_WINDOW_HOURS=24` in `.env`. | The new engine's own horizon must be explicit in `decay.yaml`, not inherited from the old wall. |
`PRICE_DATA_ENABLED=true` → only Coinbase BTC-USD is live; gold/silver report unavailable by design. | Step 5 cross-asset confirmation must degrade to "confidence neutral" for assets with no price feed, never flip a bias. |
