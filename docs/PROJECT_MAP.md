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

Tables: news_article, news_event, article_event, asset_impact, sentiment_snapshot, bias_snapshots, alert, pipeline_run, bias_outcome, dropped_article.

SentimentSnapshot columns include raw_score, bull, bear, cluster_conflict, evidence_split (verified PRAGMA output shows all present).

Migrations in app/database.py: ensure idempotent ALTER TABLE statements for new columns.

## Section 8 — Tests

1450 tests collected. Full run timed out at ~49% (suspected hang on network-dependent test). No final pass count available from this run. Collected count verified: 1450.

Test files grouped by phase: Step 1–11, Task 1–2, verification passes (verify_*.py).

## Section 9 — Known discrepancies

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
