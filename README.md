# Real-Time Intraday News Sentiment & Market Impact Engine

News-based intraday sentiment engine for **XAUUSD (Gold)**, **XAGUSD (Silver)**
and **BTCUSD (Bitcoin)**.

It continuously polls financial news sources, keeps **only articles published
today**, classifies each one, works out **how** it transmits to each asset,
weights it by recency / importance / relevance / persistence, and publishes a
live dashboard with **Market Prep**, **Market Wrap** and a full news-impact feed.

> **This is news-based sentiment, not a price prediction.** It is not a trading
> signal and not financial advice.

---

## 1. Quick start

```bash
# 1. Install Python 3.10+ (this machine: winget install Python.Python.3.12)
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 2. Dependencies
pip install -r requirements.txt

# 3. Configuration
copy .env.example .env          # Windows (cp on macOS/Linux)
# Optional but recommended for full analysis:
#   set LLM_API_KEY=...      in .env (NVIDIA, DeepSeek, OpenRouter...)

# 4. Run
python run.py --api             # API + dashboard + background pipeline
python run.py --streamlit       # optional Streamlit UI (2nd terminal)
```

Open **http://127.0.0.1:8000** for the live dashboard.

### How to run

The simplest way is the start script (uses the project's `.venv` and runs
`uvicorn app.main:app` on `127.0.0.1:8000`):

```bash
# Windows: double-click start_server.bat, or run it from a terminal
start_server.bat

# macOS / Linux: ./start_server.sh
```

Or run uvicorn directly after activating the venv:

```bash
# Windows
.venv\Scripts\activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# macOS / Linux
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

> Opening the project folder in VS Code starts the server automatically via a
> `folderOpen` task (see `.vscode/tasks.json`). If VS Code asks to allow
> automatic tasks the first time, click **Allow** (or run
> "Tasks: Manage Automatic Tasks in Folder" from the Command Palette and choose
> "Allow Automatic Tasks").

### Keep the dashboard up after a restart (no more ERR_CONNECTION_REFUSED)

`python run.py --api` is a normal console process, so it dies on every reboot.
If your browser restores the `127.0.0.1:8000` tab, it then fails with
`ERR_CONNECTION_REFUSED (-102)` because nothing is listening on the port.

Install the scheduled task once and it never happens again:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_autostart.ps1
```

That registers a per-user task `IntradaySentimentDashboard` which runs
`tools\dashboard_service.ps1 -Action Watch` hidden in the background:

| | |
|---|---|
| At logon (5s delay) | the API is already serving before you open the browser |
| Every 5 minutes | watchdog relaunches the server if it ever dies |
| On failure | Task Scheduler restarts it (up to 999 times) |
| Other | runs on battery, no execution time limit, never a second copy |

No admin rights and no console window are needed. The watchdog also recovers
from crashes mid-session, so the tab only ever sees a brief blip, never a
persistent error.

**Day-to-day commands**

```powershell
# is it up?
powershell -ExecutionPolicy Bypass -File .\tools\dashboard_service.ps1 -Action Status

# start it now / restart it / stop it
powershell -ExecutionPolicy Bypass -File .\tools\dashboard_service.ps1 -Action Start
powershell -ExecutionPolicy Bypass -File .\tools\dashboard_service.ps1 -Action Restart
powershell -ExecutionPolicy Bypass -File .\tools\dashboard_service.ps1 -Action Stop

# turn auto-start back off
powershell -ExecutionPolicy Bypass -File .\tools\install_autostart.ps1 -Uninstall
```

`Start` is idempotent - it does nothing if the server is already healthy, so it
is safe to run at any time. Everything is logged to `logs\service.log`.

> If you change `API_PORT` in `.env`, also pass `-Port <new port>` when running
> these scripts directly.

### Other entry points

| Command | Purpose |
|---|---|
| `python run.py --demo` | Offline demo with synthetic news. No key, no network. |
| `python run.py --once` | One pipeline pass against live sources. |
| `python run.py --api --reload` | Uvicorn autoreload for development. |
| `python -m tests.probe_sources` | Check how many articles each feed has **today**. |
| `python -m pytest` | Run the test suite. |

---

## 2. Two operating modes

| Mode | When | Behaviour |
|---|---|---|
| **LLM** | `LLM_API_KEY` is set | The configured model classifies each article (category, importance, per-asset direction, strength, transmission chain). |
| **Heuristic** | No key | Deterministic keyword + transmission rules. Always available. |

The active mode is shown in the dashboard header and stored per-article, so
heuristic output is never presented as model-derived. Any LLM failure (network,
malformed JSON, validation) falls back to heuristics automatically and is
logged — the pipeline never dies.

---

## 3. Architecture

```
DATA INGESTION       rss_reader.py / website_monitor.py / mock_source.py
        v
NEWS EXTRACTION      headline, timestamp, source, url, article text
        v
TODAY FILTER         app/utils/timeutils.py  (strict market-timezone test)
        v
EVENT DEDUPLICATION  event_deduplication.py   (one event = one signal)
        v
NLP / LLM ANALYSIS   news_classifier.py       (LLM + heuristic fallback)
        v
RECENCY ENGINE       recency_engine.py       (time decay buckets)
        v
ASSET IMPACT ENGINE  asset_relevance.py + per-asset weights
        v
SENTIMENT AGGREGATOR sentiment_engine.py      (bull/bear/neutral = 100%)
        v
MARKET PREP / WRAP   market_prep.py, market_wrap.py
        v
DATABASE             SQLite (dev) / PostgreSQL (prod)
        v
REAL-TIME DASHBOARD  FastAPI + WebSocket + Streamlit
```
```
intraday_sentiment_engine/
├── app/
│   ├── main.py               FastAPI, WebSocket, scheduler, HTML dashboard
│   ├── config.py             env-driven settings (pydantic-settings)
│   ├── database.py           engine/session/WAL setup
│   ├── logging_conf.py       console + rotating file, API-key redaction
│   ├── ingestion/            base, rss_reader, website_monitor, mock_source, news_fetcher
│   ├── analysis/             llm_client, news_classifier, heuristics, cue_rules,
│   │                         asset_relevance, recency_engine,
│   │                         event_deduplication, sentiment_engine
│   ├── market/               session_detector, regime, market_prep, market_wrap,
│   │                         price_data
│   ├── services/             pipeline (orchestrator), alerts
│   ├── dashboard/            dashboard.py (Streamlit), web/index.html
│   ├── models/               enums, db_models (ORM), schemas (API)
│   └── utils/                timeutils, timestamp_parse
├── tools/
│   ├── dashboard_service.ps1  start/stop/status/watch the API server
│   └── install_autostart.ps1  register the auto-start scheduled task
├── tests/
├── requirements.txt
├── .env.example
├── pytest.ini
├── run.py
└── README.md
```

---

## 4. How sentiment is calculated

```
FINAL NEWS WEIGHT = Recency x Economic Importance x Asset Relevance
                    x Expected Persistence x Source Quality x Session Relevance
```

* **Recency** — 100% (0-15 min), 90% (15-30), 80% (30-60), 65% (1-2 h),
  50% (2-4 h), 30% (4-8 h), 15% (8 h+).
  Recency alone never decides importance: a major central-bank announcement
  from hours ago outweighs a minor headline from five minutes ago.
* **Economic importance** — VERY_HIGH 1.00 / HIGH 0.70 / MEDIUM 0.42 / LOW 0.18.
* **Asset relevance** — per (category x asset) matrix, boosted when the asset is
  named explicitly. Below 0.20 the asset is not analysed at all.
* **Persistence** — how long the item should keep influencing *today's* session.
* **Source quality** — TIER_1_OFFICIAL 1.15 ... TIER_4_OTHER 0.82.
* **Session relevance** — e.g. US data matters more in the US session; China
  data matters more in Asia.

Sentiment is **binary: BULLISH or BEARISH**. Bullish and Bearish percentages are
summed, normalised, and **always sum to exactly 100%** - there is no third
"neutral" view. Where the two sides are close, the leading side is still named
but **confidence drops** and the opposing drivers are listed under `conflicts`,
so a weak call reads as weak rather than hiding behind a neutral label.

Sentiment is recalculated from scratch on every pipeline pass so impact
**decays** correctly over the session.

### Conflicting news
Opposing signals are never hidden. The verdict still names one of the two sides,
but **confidence falls** as the two sides converge, and `conflicts` lists the top
bullish driver against the top bearish driver with the weight of each, so you
can always see what is fighting the call.

### Economic calendar
Scheduled macro releases (CPI, payrolls, FOMC, PMI, ...) are fetched from a
keyless public feed and weighed **into the same per-asset verdict** as the news
flow - they are not a separate view. Each release is matched to a transmission
profile that explains how it reaches gold, silver and bitcoin, e.g.
*"hot CPI -> higher rate expectations -> stronger USD and real yields -> bearish
for metals and crypto"*.

The engine uses two modes and **never invents a print**:

| Mode | When | Direction comes from |
|---|---|---|
| `RELEASED` | an `actual` value exists | the realised surprise vs forecast |
| `EXPECTED` | not released yet | published consensus (forecast vs previous), labelled *(expected)* |

A print that lands exactly on forecast is reported as no material surprise, and
an unrecognised event is skipped entirely rather than force-fitted. Configure in
`.env`:

```
ECONOMIC_CALENDAR_ENABLED=true
ECONOMIC_CALENDAR_URL=https://nfs.faireconomy.media/ff_calendar_thisweek.json
ECONOMIC_CALENDAR_MIN_IMPORTANCE=MEDIUM   # drop Low-impact filler
ECONOMIC_CALENDAR_HORIZON_HOURS=48        # how far ahead it can influence PREP
```

Calendar failures never stop the pipeline - they are logged and the engine falls
back to news-only sentiment.

---

## 5. Asset-specific logic

The three assets are **not** scored with one generic rule set:

* **Gold (XAUUSD)** — Fed, rate expectations, **real yields**, Treasury yields,
  USD/DXY, inflation, employment, recession risk, geopolitics, safe-haven
  demand, central-bank purchases, gold ETF flows, China/India demand.
* **Silver (XAGUSD)** — everything gold responds to, **plus** industrial demand:
  China, manufacturing PMIs, copper, solar/electronics. Every silver item is
  labelled `PRECIOUS` / `INDUSTRIAL` / `BOTH` / neither.
* **Bitcoin (BTCUSD)** — monetary policy, real yields, USD, liquidity, equities /
  Nasdaq, spot Bitcoin ETF flows, regulation, stablecoin liquidity, liquidations.

Relationships are treated as contextual, not absolute. For example the
heuristic engine applies subject-aware inversion so *"Treasury yields climb"*
reads bearish (higher yields pressure non-yielding gold) rather than matching a
naive "climb = up = bullish" keyword rule.

---

## 6. News is not price

Price-action confirmation is **disabled by default**. With no reliable keyless
intraday feed for XAUUSD/XAGUSD, the engine reports exactly:

```
Price-action confirmation unavailable.
```

It never invents prices, yields, DXY or ETF flows. Enable with
`PRICE_DATA_ENABLED=true` to attach BTC spot confirmation from a public
endpoint; XAUUSD/XAGUSD continue to report unavailable unless you configure a
real feed.

---

## 7. Changing the monitored website

---

## 8. The TODAY-ONLY filter

This is the most important correctness guarantee.

1. The trading date is derived from the **market timezone**
   (`MARKET_TIMEZONE`, default `America/New_York`) using the real system clock —
   **no date is ever hard-coded**.
2. Each article's publisher timestamp is parsed (ISO-8601, RFC-822,
   `time.struct_time`, common formats) into an aware UTC datetime.
3. An article is kept only if its **market-local calendar date equals today**
   and it is within `MAX_ARTICLE_AGE_HOURS`.
4. Timestamps that cannot be parsed are **rejected** — a publication time is
   never fabricated.
5. An old article re-stamped today is flagged `is_updated_article` rather than
   treated as a new event.

Every date calculation funnels through `app/utils/timeutils.py`, so there is
exactly one definition of "today" and one place to audit it.

---

## 9. API

| Endpoint | Description |
|---|---|
| `GET /` | Live HTML dashboard (WebSocket auto-refresh) |
| `GET /api/health` | Liveness + active analysis mode |
| `GET /api/dashboard` | Full dashboard payload |
| `GET /api/news?limit=60` | Today's news feed only |
| `GET /api/sentiment/{asset}` | CURRENT / PREP / WRAP for one asset |
| `GET /api/alerts` | High-impact alerts |
| `POST /api/run` | Trigger a pipeline pass now |
| `GET /api/console` | Plain-text dashboard (section 20 layout) |
| `WS /ws` | Push channel — clients refresh on new news |

Interactive docs: http://127.0.0.1:8000/docs

---

## 10. Testing

```bash
python -m pytest              # full suite
python -m pytest -v           # verbose
python -m pytest tests/test_today_filter.py   # single file
```

Coverage focuses on the guarantees that matter:

* `test_today_filter.py` — yesterday/2-days-ago rejected, unparseable timestamps
  rejected, future timestamps rejected, update-vs-new detection.
* `test_recency_engine.py` — every documented recency bucket, and that
  importance can outweigh recency.
* `test_sentiment_engine.py` — percentages always sum to 100, no-signal honesty,
  conflict handling, duplicate damping, per-asset isolation.
* `test_deduplication.py` — near-duplicate headlines merge, unrelated stay apart,
  numbers ignored, window expiry.
* `test_analysis.py` — canonical transmissions (hawkish Fed → bearish gold),
  subject-aware inversion, asset routing, session relevance.
* `probe_sources.py` — live feed freshness (not a pytest test).

---

## 11. Configuration reference

See `.env.example` for the full annotated list. Key values:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_KEY` | *(empty)* | Empty → heuristic mode |
| `LLM_REQUESTS_PER_MINUTE` | `40` | Hard cap per rolling minute (`0` = off) |
| `MARKET_TIMEZONE` | `America/New_York` | Defines "today" |
| `DISPLAY_TIMEZONE` | `Asia/Kolkata` | Dashboard display |
| `POLL_INTERVAL_SECONDS` | `180` | Ingestion cadence |
| `MAX_ARTICLE_AGE_HOURS` | `26` | Hard cap on article age |
| `NEWS_RETENTION_DAYS` | `2` | Days of news kept in the DB (today + previous day); `0` keeps everything |
| `DEDUP_SIMILARITY_THRESHOLD` | `0.72` | Event merge threshold |
| `PRICE_DATA_ENABLED` | `false` | Price-action confirmation |
| `USE_MOCK_SOURCE` | `false` | Offline synthetic demo |
| `DATABASE_URL` | `sqlite:///./data/...` | Swap for PostgreSQL in prod |

For PostgreSQL:
```
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/intraday
```

---

## 12. Known limitations

* **No LLM key → heuristic mode.** The heuristics are deterministic and
  reasonable but cannot perform the contextual reasoning the LLM prompt
  describes. The UI always states which mode produced a given result.
* **Price data is off by default** because no free, keyless, reliable intraday
  feed exists for XAUUSD/XAGUSD.
* **Free RSS feeds go stale or die.** Outlets freeze, kill or paywall their
  free feeds — `CNBC Markets`, `Yahoo Finance` and `WSJ Markets` were dropped
  from `NEWS_SOURCES` for exactly this reason. The today-filter drops stale
  items automatically; use `probe_sources` to monitor what is left.
* **Scraping is best-effort.** `WebsiteSource` is intentionally polite
  (one request per page, concurrency-limited). Respect each site's terms.
* **Sentiment ≠ probability.** The percentages are the model's news-derived
  directional read. They are not a guaranteed chance of a price move.

---

## 13. Disclaimer

The output of this system is **news-based intraday sentiment**. It is not a
price prediction, not a trading signal, and not financial advice. No figure
produced by this tool is a guarantee of future price movement.
Sources are **configuration only** — no code changes needed. In `.env`:

```
NEWS_SOURCES=Name|type|url|weight|quality_tier, Name2|type|url|weight|quality_tier
```

* `type` = `rss` | `atom` | `html` | `kitco` | `fj` | `mock`
* `quality_tier` = `TIER_1_OFFICIAL` | `TIER_2_MAJOR` | `TIER_3_CRYPTO` | `TIER_4_OTHER`

> Keep `NEWS_SOURCES` on a **single line** — `.env` parsers do not support
> multi-line values.

Validate any feed before trusting it:

```bash
python -m tests.probe_sources
```

```
STATUS  SOURCE                TOTAL  TODAY  LATEST
LIVE    CNBC Economy             30      3  2026-10-01T07:23:01+00:00
STALE   <some other feed>        20      0  2025-01-27T19:26:00+00:00
```

`STALE` means the feed serves no article dated today; the today-filter rejects
all of them, which is correct behaviour, not a bug.

### Adding a genuinely new backend
Implement the `NewsSource` protocol (`name`, `quality`, `url`, `kind`,
`async fetch() -> list[RawArticle]`) and register it in
`app/ingestion/news_fetcher.py:build_sources`.

