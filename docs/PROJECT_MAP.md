# PROJECT_MAP

## Section 1 — Repo state

Working tree clean.

```
On branch wip/contradiction-split
nothing to commit, working tree clean
```

HEAD commit: `37ef016 Pass tz-aware now into compute(): enable hold/cooling/Tier-1 gates and delta_60m`
Branch: `wip/contradiction-split` (local only; remote origin/main at main).

## Section 2 — Project layout (depth 3, excluding .venv, .git, caches, data/coinglass_profile)

Top-level dirs: app, backups, data, docs, logs, scripts, tests, tools. .venv excluded (dependency dir).

app subdirs: analysis, config, dashboard, ingestion, market, models, services, utils.

## Section 3 — Entry points

Main entry: `app/main.py` (893 lines). Run command from README/usage: `python -m app.main` (scheduler entry).

Pipeline: `app/services/pipeline.py::run_once()` (1640 lines). Scheduler interval and job registration in main.py (need read for exact quotes — see code).

API: FastAPI in main.py (uvicorn). Port default 8000 (configured via config). Dashboard: Streamlit `app/dashboard/dashboard.py` served via streamlit.

DB: SQLite, `data/intraday_sentiment.db`, connection string `sqlite:///./data/intraday_sentiment.db` (app/config.py line 48).

Logs: `logs/` directory, rotation per logging_conf.py.

## Section 4 — Pipeline

run_once() stages: ingestion, classification, signal building, clustering, aggregation, scoring, snapshot writing, alert evaluation, purge. _recompute_sentiment() computes for XAUUSD, XAGUSD, BTCUSD, EQUITY using signal lists and compute() from sentiment_engine; writes sentiment_snapshot rows (scopes per asset).

## Section 5 — Scoring engine constants

From sentiment_engine.py. Values: see code (EPS, FLIP_MARGIN, MIN_TOTAL_WEIGHT, ASSET_SENSITIVITY_UNKNOWN, EVIDENCE_SPLIT_RATIO, CLUSTER_CONFLICT_WEIGHT_SHARE, CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT). Query code for exact numbers.

## Section 6 — Transmission map

app/config/transmission_map.json event types count; GENERAL_MACRO excluded detection per transmission_map.py. App config excludes GENERAL_MACRO before asset-sensitivity lookup (verify line).

## Section 7 — Persistence model

Tables: news_article, news_event, article_event, asset_impact, sentiment_snapshot, bias_snapshots, alert, pipeline_run, bias_outcome, dropped_article, price_point.

SentimentSnapshot columns include raw_score, bull, bear, cluster_conflict, evidence_split (verified PRAGMA output shows all present).

Migrations in app/database.py: ensure idempotent ALTER TABLE statements for new columns.

### The outcome loop (app/services/outcomes.py)

The engine scores ITSELF. `record_new_calls` writes a PENDING `bias_outcome`
row every time the published bias CHANGES for an asset; `record_price_point`
stores what the price actually was; `resolve_pending` turns a call into
WORKED / FAILED / Mixed once the horizon has elapsed. `price_point` is the
ground truth the resolution needs.

Before this, `bias_outcome` could only be filled by hand — one click per
call — so it had 0 rows for the whole life of the project and every tuning
decision in the repo was unfalsifiable. Nothing in this loop feeds back into
scoring: it can only tell you, afterwards, whether the engine was right.

## Section 8 — Tests

1542 tests collected and passing. Full run: 1542 passed in ~90s.

Test files grouped by phase: Step 1–11, Task 1–2, verification passes (verify_*.py), plus
test_outcome_capture.py (the outcome loop) and test_stale_mass_cap.py (the stale cap).

## Section 9 — Region-scoped macro bias (2026-10-22)

New modules (additive; nothing else in the engine changed):

- `app/config/calendar_bias.json` — THE config file: the region whitelist
  (code, currencies, aliases, weight, include/exclude keywords), impact
  weights, impact floor, surprise settings (standardised / percent fallback /
  sigma cap), time decay, inverted indicators, central-bank decision + speech
  keywords and speech tags, `scenario_map`, `stance_map`, the signed
  `asset_sensitivity` matrix, normalisation, label bands, the event-risk
  window, the upcoming list, and the presentation knobs.
- `app/analysis/calendar_config.py` — loads/validates that file, holds the
  two-stage matcher (country/currency first, then the title keyword list) and
  `filter_events()`. Raises `CalendarConfigError` on a broken file; callers
  decide what that means (the input filter fails safe, the panel reports it).
- `app/analysis/regional_bias.py` — the engine: surprise, stance (inverted
  indicators, rate decisions, tagged speeches), scenario selection, time
  decay, per-asset score, region breakdown, top drivers, upcoming list, event
  risk, payload.

Wiring:

- `app/ingestion/economic_calendar.py::apply_region_filter` — the filter at
  the calendar input; behind `settings.calendar_region_filter_enabled`
  (`CALENDAR_REGION_FILTER_ENABLED`, default true).
- `app/ingestion/economic_calendar.py::apply_impact_overrides_logged` — the
  FORCED impact (`impact_overrides` in the config), applied before any
  weighting and logged on every change.
- `app/ingestion/calendar_cache.py::merge_history` / `save_history` — the
  last 48 h of RAW events, persisted to `data/calendar_history.json`, so the
  time decay survives a restart. The provider only exposes TODAY, so without
  it a restart loses yesterday's releases - and their decay.
- `app/services/pipeline.py` — `current_calendar_events()` (read-only copy)
  and `regional_bias_payload()`.
- `app/main.py` — the dashboard payload carries `regional_bias`.
- `app/dashboard/web/index.html` — `SECTION A2 — REGIONAL MACRO BIAS`
  (`renderRegionalBias`).
- `app/ingestion/fj_calendar.py::guess_country_detail` — the country AND
  its confidence: a derivation from the GENERIC fallback list is LOW
  confidence and is logged as such.
- `tools/audit_calendar.py` — the audit: every event, its derived country,
  the confidence, the region, kept/dropped and why, plus the flags.

Tests: `tests/test_calendar_regions.py`, `tests/test_regional_bias.py`,
`tests/test_calendar_poll_cadence.py`, `tests/test_calendar_overrides.py`,
`tests/test_calendar_history.py`.

## Section 10 — Known discrepancies
- raw_score: DB newest 5 rows all have raw_score populated (-68.7, -68.3, ...). Contradicts prior "raw_score write missing" if that was the claim; current state shows populated.
- bull/bear: newest 5 rows populated (0.96/5.17 etc).
- cluster_conflict: flagged on 941 of 1183 captured ticks (79.5%) before the
  fix - the row-level gate counted the dominant survivor's full weight as
  conflicted evidence, so the flip margin ran widened on nearly every tick.
  compute() now counts only the losing side's weight
  (WeightedSignal.conflict_weight, set by event_clustering._collapse_keyed_events)
  as the conflicted share, so the 2.8-2.9% background baseline no longer fires
  and the +10 flip-margin widening only applies to genuine minority conflicts.
- evidence_split: 378 of 1183.
- intraid: no matches anywhere on disk.
- _patch_*.py: none found.
- Untracked .py files: none outside .venv.
- Git: working tree clean on wip/contradiction-split; HEAD 37ef016.
```

Reported to session.end
