# Changelog

All notable changes to the Intraday News Sentiment & Market Impact Engine.

---

## ONE LLM CALL PER ARTICLE, THEN KEEP OR DELETE — one-shot policy (2026-10-13)

### Feature — an article is classified at most once, and the verdict decides its fate
- **Files:** `app/config.py` (`drop_low_impact_articles`, `only_llm_verdict_may_drop`),
  `app/analysis/news_classifier.py` (`is_section_b_verdict`),
  `app/services/pipeline.py` (`_ingest_article`, `_guid_suppressed`,
  `_drop_low_impact_article`), `app/models/db_models.py` (`DroppedArticle`),
  `app/database.py` (`_add_missing_dropped_article_table`),
  `app/services/overnight.py`, `tests/test_one_shot_llm_policy.py` (new)
- **What it does:** The single LLM call an article gets is the one at ingest,
  and that verdict also decides its fate:
  * **HIGH / VERY_HIGH impact** (article-level `importance` OR any per-asset
    `strength`) is stored in the Section B news feed exactly as before - no
    new storage anywhere, the feed already is Section B.
  * **MEDIUM / LOW is DELETED** together with its events and impacts (the
    cascade removes `asset_impact` / `article_event`), and its guid is
    **suppressed for the rest of the trading day**, so a source re-serving the
    same story is skipped with no model call, no row and no counters.
- **Why the suppression table was needed:** `news_article` dedupes on `guid`,
  so once a dropped row is gone the guid looks brand new on the next feed
  poll. Without `DroppedArticle` the engine would pay for - and delete - the
  identical headline on every tick forever. The table is scoped by trading day
  (`dropped_day`), so a new day re-admits the story, and it is trimmed by
  `purge_old_news` on the same retention window as the articles themselves.
- **One call, ever:** the backlog re-classification loop (up to 5
  previously-heuristic articles re-sent per tick) is retired - it re-sent rows
  the ingest path had already handled and re-classified them outside this
  policy. `overnight.classify_articles` keeps its catch-up pass for rows the
  pipeline never got a verdict for (batch cap / breaker) and applies the same
  keep-or-delete rule, so each article still spends exactly one call.
- **Two guard rails:** FinancialJuice session reports (Section A prep/wrap
  boxes) are never dropped, and rows without a model verdict (heuristic
  fallback) are never dropped either - they can never reach a card, so deleting
  them would only empty the feed on a day the model is unavailable. Either
  guard is configurable.
- **Counters:** `IngestStats` gains a fifth mutually exclusive bucket,
  `llm_new_dropped_low` (plus `skipped_dropped` for free skips);
  `verify_new_article_counters()` and both counter test files now sum five
  buckets. The tick logs print `dropped_low=` / `skipped_dropped=`.
- **Tests:** `tests/test_one_shot_llm_policy.py` drives the real
  `_ingest_article` with a stubbed classifier: delete+suppress on a LOW
  verdict, no second model call for a suppressed guid, re-admission on a new
  trading day, heuristic rows kept, FJ reports exempt, flags off, retention
  purge. Full suite: 1194 passed.

---

## ONLY HIGH-IMPACT NEWS REACHES A CARD — impact gate (2026-10-13)

### Feature — MEDIUM/LOW impact news no longer counts towards any card
- **Files:** `app/config.py` (`only_high_impact_counts`),
  `app/analysis/sentiment_engine.py` (`STRENGTH_GATE`, `_passes_impact_gate`,
  `_signal_weight`, `_aggregate`), `tests/test_impact_gate.py` (new)
- **What it does:** The bullish/bearish split and the BULLISH DRIVERS /
  BEARISH DRIVERS boxes on every card are built from the same two totals
  (`bull`, `bear`) in `sentiment_engine.compute()`. `only_high_impact_counts`
  (default **true**) drops every signal whose impact band is not `VERY_HIGH`
  or `HIGH` at the ONE weight choke point — `_signal_weight()` now returns
  `0.0` for them, and `_aggregate()` already skips weight <= 0, so such a
  signal can never enter the totals, never appear in `bull_drivers` /
  `bear_drivers`, never be named as the `conflict`, never feeds the
  news-velocity burst bonus, and never breaks a tie-break. Verified on a mixed
  bundle of classified headlines: three HIGH/VERY_HIGH bullish plus two LOW
  bearish moved gold from 95%/5% (bearish box listing the two LOW headlines)
  to 100%/0% with an empty bearish box.
- **Scope:** the gate reads the per-asset **impact band**
  (`AssetImpact.strength`, what actually scales the card), not the
  article-level `importance`. The burst counts — which read the raw signal
  list rather than `_signal_weight` — were the one bypass and are now filtered
  by the same gate, so three fresh MEDIUM headlines no longer multiply their
  side by 1.2x/1.3x. With the flag false the old scaled behaviour
  (`STRENGTH_MULT` 0.45 / 0.20) returns unchanged.
- **Thin days:** when nothing in the window is HIGH/VERY_HIGH the totals are
  zero, so the card takes the existing weak-evidence branch (empty driver
  boxes, label carried forward, split near 50/50) instead of inventing weight
  for noise. `MIN_TOTAL_WEIGHT` is unchanged.
- **Tests:** `tests/test_impact_gate.py` covers both sides of the flag. The
  suite's autouse fixture (`tests/conftest.py`) runs existing tests with the
  gate **off**, because they predate it and use MEDIUM signals as generic
  evidence while asserting decay/tier/polarity behaviour. Full suite: 1180
  passed.

---

## NEWS IMPACT NO LONGER FADES IN MINUTES — decay retune + per-asset clocks (2026-10-13)

### Fix — event-tier half-lives were sized in minutes, not the trading day
- **File:** `app/config.py` (`TIER_HALF_LIVES`)
- **What it does:** The DAY TRADING retune (0.5/0.25/0.1h) halved a
  headline's weight every 30/15/6 minutes, so a 12:30 UTC NFP print sat at
  **6%** weight by the US close and an 08:45 UTC ECB speech was at 0.4% by the
  13:30 US window — gold kept trading both for hours. Half-lives are now sized
  to how long each tier actually drives price intraday:
  `TIER_1 = 1.5h` (63% at 2h, 40% at 4h), `TIER_2 = 0.75h` (47% at 1h),
  `TIER_3 = 0.25h` (25% at 30min, 6% at 1h — dead within the hour, but alive
  long enough to combine with same-day cluster corroboration).

### Feature — per-asset decay multipliers (GOLD/SILVER/BITCOIN/EQUITY)
- **File:** `app/config.py` (`ASSET_DECAY_MULTIPLIER`),
  `app/analysis/sentiment_engine.py` (`decay_weight`, `_signal_weight`)
- **What it does:** `decay_weight()` takes the signal's asset and scales its
  tier half-life by `ASSET_DECAY_MULTIPLIER`: `BITCOIN ×1.25` (24/7 — no
  overnight reset, so a 03:00 UTC headline still matters at the London open),
  `EQUITY ×0.75` (cash equities ~16h/day; earnings reacts pre-market and fades
  into the close), `GOLD/SILVER ×1.0`. The equity card inherits it
  automatically: `equity_card.py` already proxies signals with
  `asset=Asset.EQUITY` before scoring. Only one call site changed.

### Docs — the decay comments were lying
- **Files:** `app/config.py`, `app/analysis/sentiment_engine.py`,
  `.env.example`, `AUDIT.md`
- **What it does:** Comments/docstrings claimed "TIER_1~6h, TIER_2~2h,
  TIER_3~1h" (code) and "TIER_1~30min..." — both wrong. Now state the real
  values. The dead per-category `DECAY_HALF_LIVES` fields are marked
  SUPERSEDED (nothing reads them), and `test_decay_applied_once_weight_ratio`
  now derives its expected factors from `TIER_HALF_LIVES` instead of
  hardcoded exponents, so the next retune cannot break the test.

---

## SESSION-AWARE EQUITY CARD — switch the moment a session opens

### Bug fix — session overlap was decided in list order, not priority
- **File:** `app/market/session_detector.py` (`detect_session`)
- **What it does:** `SESSION_WINDOWS` is declared `[ASIA, EUROPE, US]` and
  `detect_session` walked it in that order, so the FIRST-matching session won
  every shared hour. Asia's 20:00-04:00 tail swallowed 03:00-04:00 (Europe's
  first hour) and Europe's 03:00-12:00 window swallowed 08:00-12:00 — the
  whole first half of the US session. The windows are now checked in
  priority order (US, then EUROPE, then ASIA), matching the comment that was
  already in the function: the newest/highest-liquidity session wins, so the
  switch happens when the next session OPENS. Asia owns 20:00-03:00,
  Europe owns 03:00-08:00, US owns 08:00-17:00, 17:00-20:00 is off hours and
  freezes on the previous session.

### Bug fix — `next_session()` returned the current session
- **File:** `app/market/session_detector.py` (`next_session`)
- **What it does:** `SESSION_ORDER[idx % len]` is just `SESSION_ORDER[idx]`,
  so the "next" label always echoed the current session (e.g. EUROPE ->
  EUROPE). Now advances one step (`idx + 1`), wrapping to ASIA in OFF_HOURS.

### Robustness — `equity_title(OFF_HOURS)` is reference-time aware
- **File:** `app/market/session_detector.py` (`equity_title`)
- **What it does:** OFF_HOURS resolves through `previous_session(now)` for the
  passed reference time instead of wall-clock `now`, so the fallback title is
  deterministic off-hours (US EQUITY) rather than depending on when it runs.

### Dashboard — the 4th strip card is the SESSION-aware equity card
- **File:** `app/dashboard/web/index.html` (`equityCard`, `renderPrepStrip`)
- **What it does:** Item 2 made EQUITY a fourth asset, so the top strip was
  rendering the EQUITY asset's MARKET PREP card ("S&P 500"), which aggregates
  every region's news at once and can never follow a session. The strip now
  renders the three metal/crypto prep cards plus the session-aware card from
  the server's `equity` block: title `ASIA EQUITY` / `EUROPE EQUITY` /
  `US EQUITY` with `NIKKEI / HSI`, `DAX / STOXX50`, `SPX / NDX`, verdict and
  drivers re-scored for the active region. Flip state is keyed
  `EQUITY:<region>` so each region keeps its own clock (mirroring the
  backend's per-region bias state), and the card gains a region-coloured
  accent (`.mcard.equity.region-asia|europe|us`). The strip grid moves to
  four columns; the existing responsive steps are unchanged.

### Latency — the card no longer waits for the next tick to switch
- **Files:** `app/services/pipeline.py`, `app/main.py` (`build_payload`)
- **What it does:** the equity card is rebuilt during the pipeline recompute,
  which only runs every `poll_interval_seconds` (180s), so a session boundary
  used to show the previous region for up to three minutes. The pipeline now
  keeps the signal list behind the card (`current_equity_signals()`), and
  `build_payload` re-scores the card from it whenever the active region
  differs from the cached card's region. The switch therefore lands within
  one dashboard poll (30s) of the session opening; a rebuild failure keeps
  the last good card and never breaks the payload.

### Tests
- `tests/test_analysis.py`: session detection now pins the overlap rule at the
  boundaries (03:00 -> EUROPE, 08:00 -> US while Europe runs, 17:00-20:00 ->
  OFF_HOURS); `test_session_ordering` asserts prev/current/next per hour and
  that `next_session` is never the current session.
- `tests/test_equity_card.py`: end-to-end session switch — with all three
  regions present in one signal pool, the card follows
  `equity_session(now)` at 21:00/02:00 (ASIA), 03:00/07:00 (EUROPE) and
  08:00/14:00 (US), including title/symbol and one-region-per-card isolation.

---

## BIAS CALCULATION IMPROVEMENTS — Round 2

### Improvement 1 — News velocity burst bonus
- **File:** `app/analysis/sentiment_engine.py` (`_aggregate`, lines ~622-655)
- **What it does:** A cluster of same-direction headlines in the last 15
  minutes is treated as a momentum cue and multiplies that side's total so it
  can punch through a marginal flip margin. Conflicting bursts (both sides
  >= 2 in the window) cancel the bonus entirely — that is a tug-of-war, not a
  trend. Exposed on the dashboard as `burst_bull` / `burst_bear`.

### Improvement 2 — High-contradiction holds the bias stronger
- **File:** `app/analysis/sentiment_engine.py` (`compute`, lines ~877-885)
- **What it does:** When both sides are heavily weighted (bull > 80 AND bear
  > 80) the news is genuinely torn, so a flip is held to a wider margin
  (`FLIP_MARGIN + 10`). Flagged on the payload as
  `contradiction_detected` so the dashboard can show a torn market.

### Improvement 3 — Fresh-signal recency weighting
- **File:** `app/analysis/sentiment_engine.py` (`_signal_weight`, lines ~548-574)
- **What it does:** Fresh headlines get an extra boost on top of the existing
  decay curve: <= 10 minutes gets +20%, <= 30 minutes gets +10%, the current
  hour is flat, and older than an hour is slightly penalised. Purely
  multiplicative; the existing decay calculation is untouched.

### Improvement 4 — Capped corroboration boost on clusters
- **File:** `app/analysis/event_clustering.py`
- **What it does:** Five outlets covering one FOMC decision used to cast five
  votes into the aggregate. Clusters now count as ONE event, with only a small
  capped boost for independent corroboration (diminishing returns,
  `1/log2(n+1)`). Representative selection uses the same weight formula the
  aggregate uses downstream.

### Improvement 5 — Gold and Silver cross-asset bleed
- **File:** `app/analysis/signal_factory.py` (`build_bleed_signal`,
  `BLEED_CATEGORIES`, `BLEED_PAIR`) and `app/services/pipeline.py`
- **What it does:** A high-relevance (>= 0.70), high-strength (HIGH / VERY_HIGH)
  central-bank, rates, inflation, USD, treasury or banking-stress story moves
  BOTH precious metals in the same direction. After the primary signal is
  built for XAUUSD/XAGUSD, a secondary copy is built for the PAIR, down-weighted
  0.40 on relevance AND persistence so it never outranks a direct silver story,
  and flagged `is_primary=False` so the dashboard can tell a bleed from a real
  silver headline. Bitcoin and the equity proxy are excluded (they are not in
  `BLEED_PAIR`, so `build_bleed_signal()` returns None for them). Wired into
  both the news-impact path and the calendar path in `pipeline.py`.

### Improvement 6 — Anti-whipsaw cooling period after a flip
- **File:** `app/analysis/sentiment_engine.py` (`_should_flip`,
  `COOLING_MINUTES`, `COOLING_MARGIN`)
- **What it does:** After a bias flips, the market needs 30 minutes before
  another flip is allowed, and during that window the score must clear by
  COOLING_MARGIN (25, wider than the normal FLIP_MARGIN of 15) so a re-test
  of the old level does not immediately flip it back. The Tier-1 override
  still bypasses this entirely — it is checked first in `_should_flip()` and
  returns immediately.

### Dashboard / payload additions (Round 2)
- **Files:** `app/models/db_models.py`, `app/database.py`, `app/main.py`,
  `app/analysis/sentiment_engine.py`, `app/analysis/equity_card.py`,
  `tests/test_migration_idempotent.py`
- **What it does:** `sentiment_snapshot` gains `contradiction_detected`,
  `burst_bull` and `burst_bear` columns (added by the existing idempotent
  migration, so old databases keep working). The dashboard block exposes all
  three alongside the existing `signal_count`. The binary-bias contract is
  unchanged: every asset reports BULLISH or BEARISH only — never NEUTRAL,
  MIXED or NO_BIAS.

### Bug fixes found while wiring the above
- `app/analysis/sentiment_engine.py` (`_now_aware`, `_signal_weight`):
  `now=None` now resolves through `app.utils.timestamp_parse.datetime` rather
  than the module's own `datetime`, so the test suite's frozen clock is
  honoured and naive/aware datetime subtraction no longer raises TypeError.