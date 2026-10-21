# How the sentiment engine actually works

_Last updated after Steps 1–11. This describes the code as it is, not as any
older design document claimed._

## The pipeline in one pass

```
feeds ─► ingest ─► classify (LLM or heuristic) ─► dedup ─► impact rows
                                                                  │
                          ┌───────────────────────────────────────┘
                          ▼
        _recompute_sentiment()  (every poll_interval_seconds, default 180s)
                          │
            1. load today's NewsArticle rows  (news_day_predicate)
            2. build one WeightedSignal per (article × asset impact row)
            3. gold↔silver bleed signals for eligible macro stories
            4. economic-calendar signals + bleed
            5. session-change reweight of session_multiplier (in memory)
            6. suppress previews behind a released ACTUAL print
            7. down-weight news duplicating a calendar print
            8. cluster_signals() — one vote per event per direction
            9. event_risk_check() — unreleased high-impact print
           10. PER ASSET: compute() → CURRENT / MARKET_PREP / MARKET_WRAP
           11. persist 3 SentimentSnapshot rows per asset
           12. write BiasSnapshot diagnostics rows
           13. build the session-aware equity card (in memory)
```

Only **three** assets are scored: `XAUUSD`, `XAGUSD`, `BTCUSD`. `Asset.EQUITY`
exists as the equity card's internal proxy but no `SentimentSnapshot` is
written for it (Step 6, Option A). The fourth dashboard box renders the
session-aware equity card instead.

## One signal's weight

`app/analysis/sentiment_engine.py::_signal_weight`

```
impact gate   — only VERY_HIGH / HIGH pass (config.only_high_impact_counts)
decay         — 0.5 ** (age_hours / half_life)
half_life     — TIER_HALF_LIVES[tier] × ASSET_DECAY_MULTIPLIER[asset]
                 overridden per EventType by config.EVENT_HALF_LIFE_OVERRIDE
floor         — age past trading_day_cutoff_hours → DECAY_FLOOR (0.05)
                 published_at is None → DECAY_FLOOR
relevance     — clamp(s.relevance, 0, 1)
persistence   — clamp(s.persistence, 0.05, 1)
sourceQuality — QUALITY_MULT[source_quality]  (1.00 / 0.97 / 0.94 / 0.90)
                 forced to 1.00 for BITCOIN_ETF_FLOWS
session       — clamp(session_multiplier, 0.5, 1.5), then a BTC clock multiplier
corroboration — max(1, corroboration), capped at +0.30 by event_clustering
surprise      — clamp(surprise_weight, 0, 2.0)
tier          — TIER_MULTIPLIERS × 0.5 (rumor) or × 1.05 (confirmed)
metalMult     — 1.10 for SILVER with PRECIOUS or INDUSTRIAL character
earningsMult  — 2.0 mega-cap named / 0.3 unnamed
assetSensitivity — clamp(max transmission magnitude, 0.3, 1.0)
                   0.5 when no event type was identified (Step 4f)
                   skipped for earnings, *_ETF_FLOW* and INDEX_FLOW
speakerTier   — s.speaker_tier
```

Half-lives: **TIER_1 1.5h, TIER_2 1.25h, TIER_3 1.0h.**
Overrides: FOMC 4h, NFP/CPI/PPI 3h, FOMC minutes 2h, GEOPOLITICAL_RISK 6h.
Asset multipliers: GOLD 1.0, SILVER 1.0, BITCOIN 1.25. No EQUITY entry.

### The stale mass cap

Past `trading_day_cutoff_hours` the per-signal weight floors at
`DECAY_FLOOR` (0.05) rather than zero — old news fades, it never vanishes.
That floor is **per signal**, so with 130+ signals a tick the accumulated
shelf of hours-old headlines becomes the majority of the evidence. Measured on
the live database, the share of the day's surviving weight mass that was
already older than the 8h cutoff:

```
hour  09  10  11  12  13  14  15  16  17  18  19  20  23
stale 20% 29% 29% 31% 31% 56% 54% 68% 66% 69% 67% 65% 57%
```

So through the US session the card was majority-driven by news that had
already been priced in hours ago, and a headline that just landed could not
move it. `_aggregate` therefore caps each side's stale mass at
`stale_mass_cap_ratio` (default **0.5**) times the card's **total fresh
mass** — measured against the total, not against the same side's fresh mass,
so a side with no fresh news of its own cannot be drowned by the other side's
stale news either. With the default, at least half of every card is news
from the last 8 hours. Set `STALE_MASS_CAP_RATIO=0` to disable the cap.

## From signals to a score

`_aggregate` (`sentiment_engine.py`)

```
magnitude_i = weight_i × STRENGTH_MULT[strength_i]
              VERY_HIGH 1.00 · HIGH 0.72 · MEDIUM 0.45 · LOW 0.20

bull = Σ magnitude over BULLISH signals
bear = Σ magnitude over BEARISH signals
directionless = Σ magnitude × 1.4 (MIXED)  |  × 1.0 (NEUTRAL)

burst bonus — ≥3 same-side signals inside 900s → that side ×1.20; ≥5 → ×1.30
              cancelled when both sides have ≥2
```

`_score_from` — **Step 8 added the contradiction dampener**

```
score = round(100 × (bull − bear) / (bull + bear), 1)

if CONTRADICTION_DAMPENER_ENABLED:
    if min(bull,bear) / max(bull,bear) > 0.6:      # CONTRADICTION_RATIO
        score = round(score × 0.5, 1)              # CONTRADICTION_DAMPENER_FACTOR
```

So a 67/33 split becomes 54/46, and a perfectly balanced pair becomes 0.

`_confidence_from`

```
net        = |bull − bear| / (bull + bear)
conviction = min(1, net / 0.6)
breadth    = min(1, used / 8)
agreement  = 1 − 2·min(bull,bear)/(bull+bear)
certainty  = (bull+bear) / (bull+bear+directionless)

confidence = clamp((0.40·conviction + 0.28·breadth + 0.22·agreement) × certainty,
                  0, 0.95)
confidence = calibrate_confidence(confidence)      # Step 10, identity today
```

Confidence **never** scales the score. It only gates flips
(`FLIP_MIN_CONFIDENCE = 0.30`) and now shrinks the displayed split (Step 5).

## Choosing the label

`compute` (`sentiment_engine.py`)

```
no signals            → carry the last bias forward, stale = True
total < MIN_TOTAL_WEIGHT (0.15)
                       → bias = heavier side; score = raw × total/0.15
|raw| ≥ EPS (25.0)    → bias = sign(raw)
otherwise             → bias = _tie_break_bias(...)   # heaviest, newest signal

then _should_flip():
  1. no current bias                     → take the computed bias
  2. bull + bear ≤ 0                     → hold
  3. fresh TIER_1 opposite (< 1h)        → flip immediately, bypasses all gates
  4. inside COOLING_MINUTES              → margin widens to COOLING_MARGIN
  5. hold_elapsed < FLIP_HOLD_MINUTES    → hold
  6. confidence < FLIP_MIN_CONFIDENCE    → hold (a thin reading cannot move a badge)
  7. score has not crossed by FLIP_MARGIN → hold
      (margin +10 when contradiction_detected)

score = payload_score(raw, final_bias)   # label-consistent; a disagreement
                                         # becomes ±TIEBREAK_LEAN (2.0)
```

The gate constants are **read from `settings`**, so the `.env` values are the
values that run: `FLIP_MARGIN` (default 5.0), `COOLING_MINUTES` (5),
`COOLING_MARGIN` (8.0), `FLIP_HOLD_MINUTES` (2) and `FLIP_MIN_CONFIDENCE`
(0.30). They used to be read through `hasattr(settings, 'FLIP_MARGIN')`,
which is always False on a pydantic-settings object because the fields are
lowercase — so every one of those knobs was inert and the engine ran on
hardcoded fallbacks.

### The published score never contradicts the badge

`payload_score(raw, bias)` returns the raw score when its sign already
agrees with the label, and a small lean of the **label's** sign when it does
not. The disagreement is real: the stability gates can hold the previous bias
while the aggregate has already turned, and a fresh TIER_1 release can flip
the badge while the day's aggregate has not caught up. Before this rule the
card published a badge and its opposite number side by side ("BEARISH
+40.0") — 24% of live ticks carried that contradiction, and 86% of those were
TIER_1 override flips. The aggregate stays on `raw_score` for the audit
trail, and the card note records why the two differ.

The **drivers follow the published label**, not the computed one, for the
same reason: 39.7% of live ticks used to publish a badge whose #1 driver
pointed the opposite way. `bull_drivers` and `bear_drivers` still carry both
sides, and `conflict` still names the strongest opposing headline.

The engine is **binary**: it always names BULLISH or BEARISH, never withholds.
A contradiction caps confidence at `TIEBREAK_MAX_CONFIDENCE` (0.30) and forces
`confidence_label = "LOW"` (Step 8).

### cluster_conflict vs evidence_split routing

The two conflict-related flags are NOT interchangeable and neither one caps
confidence on its own:

* **`cluster_conflict`** (cluster-level, from Step 9.3): two readings of one
  event were collapsed into a single vote after disagreeing. It widens the
  flip margin only, via `effective_flip_margin()` (+`CONTRADICTION_FLIP_MARGIN_ADJUSTMENT`
  when true). It does **not** read or write confidence.
* **`evidence_split`** (aggregate-ratio): the bull/bear totals are balanced
  above `EVIDENCE_SPLIT_RATIO`. This is the flag that caps confidence at
  `TIEBREAK_MAX_CONFIDENCE` (0.30) via `cap_confidence_for_evidence_split()`.

So `cluster_conflict=true` never lowers confidence, and `evidence_split=true`
never widens the flip margin. A BTCUSD/EQUITY tick can legitimately show higher
median confidence on `cluster_conflict=true` ticks: the flag signals a wider
flip margin, not uncertainty in the verdict itself.

## From the score to the displayed percentage

`app/analysis/display.py::to_display(score, bias, confidence, tie_break)`

```
bull_pct = clamp(round(50 + score/2), 0, 100)
bear_pct = 100 − bull_pct

if confidence is not None:                    # Step 5
    shrink  = min(1, confidence / 0.30)       # TIEBREAK_MAX_CONFIDENCE
    bull_pct = round(50 + (bull_pct − 50) × shrink)

if tie_break and bull_pct == 50:              # Step 8, label = NEUTRAL 50/50
    return NEUTRAL, 50, 50, 50

if label == BULLISH and bull_pct < 50:  bull_pct = 51   # never contradict a name
if label == BEARISH and bear_pct < 50:  bull_pct = 49
```

The `confidence` shrink is why a 0.14-confidence card no longer reads 58/42.

## Which number reaches the card

`app/main.py::_block` reads `snap.raw_score` **directly** (Step 1 — the
`getattr(snap, "raw_score", snap.score)` fallback is gone) and projects that,
not the published `score`. `raw_score` and `baseline_score` are declared
columns on `SentimentSnapshot`; the live SQLite schema already had them from
`_add_missing_sentiment_columns()`, but without the `Mapped` attributes
SQLAlchemy never mapped them.

`tie_break` is derived from the same raw value: `abs(raw) < EPS`.

The strip cards render the **CURRENT** scope (Step 2) — `renderPrepStrip` reads
`a.current`, not `a.market_prep`.

## The equity card

`app/analysis/equity_card.py::build_equity_card` is the single equity
computation. It keeps only signals whose category is in `EQUITY_CATEGORIES`
AND whose title matches the active region's keywords, collapses cross-asset
copies by `event_key`, re-derives direction with `equity=True`, then calls the
same `compute()` with `history_key = "EQUITY:<region>"`.

| Session (NY) | Card | Symbol |
|---|---|---|
| ASIA 20:00–04:00 | ASIA EQUITY | NIKKEI / HSI |
| EUROPE 03:00–12:00 | EUROPE EQUITY | DAX / STOXX50 |
| US 08:00–17:00 | US EQUITY | SPX / NDX |
| otherwise | previous session | — |

No regional news → `neutral_card()` carries the region's bias forward.

## The classification mode

`_mode_for` aggregates `WeightedSignal.mode`, which `build_signal` sets from
`NewsArticle.analysis_mode` (Step 3). All-llm → `llm`, all-heuristic →
`heuristic`, a blend → `mixed`, nothing → `heuristic`. Before Step 3 the
pipeline never passed it, so every card read `heuristic`.

## One story, one vote

Three layers, in order:

1. **Ingest dedup** — `event_deduplication.py`. Token-set Jaccard +
   containment over a 12h window, pruned against the **tick's clock** (Step
   9.1). The index is in-memory and reseeded from the DB once per process
   (Step 9.2), so a restart cannot re-admit today's headlines.
2. **Clustering** — `event_clustering.py`. Identity first: one `event_key` is
   one vote for the whole trading day. **Step 9.3** made the group key the
   event alone rather than `(event, direction)`, so opposite readings of one
   event collapse to the dominant direction, flagged `contradiction_detected`.
   Title similarity (≥0.72, 30 min / 90 min for anchors) is the fallback for
   identity-less signals.
3. **Burst bonus** — rewards genuinely concurrent coverage, and is cancelled
   when both sides surge.

## Confidence calibration

`tools/calibration_report.py` reads the labels, buckets them by the engine's
own confidence, and reports the observed hit rate. It prints a
`CONFIDENCE_CALIBRATION` block to paste into `app/config.py`.

The labels are now produced **automatically**. `app/services/outcomes.py`
records a PENDING `bias_outcome` row every time the published bias **changes**
for an asset, stores the price at the call in `price_point`, and resolves the
call to WORKED / FAILED / Mixed once the horizon has elapsed. A move inside
`outcome_min_move_pct` is Mixed, which the fitter counts as half a hit.

```
python tools/calibration_report.py      # the calibration table
python tools/hitrate_report.py --from-db   # the hit rate, by confidence and asset
```

Before this, `bias_outcome` could only be filled by hand — one click per
call, ~1,400 clicks a day — so it had 0 rows for the whole life of the
project, `calibrate_confidence()` was an identity *by design*, and the
0.40 / 0.28 / 0.22 confidence coefficients had never been checked against
anything.

**Still unfitted by design.** The labels are now produced automatically, but
the table has to accumulate first, and until it does `calibrate_confidence()`
remains an identity and the flag stays off. An empty table cannot support a
mapping, and inventing one is exactly the unfounded formula this step exists
to avoid.

Only BITCOIN has a reachable price feed, so the other assets stay PENDING
until an operator supplies a price series.

The fitter joins `bias_snapshots` for the confidence — `bias_outcome.confidence`
is written as a literal `0.0` by the manual POST endpoint.

## The baseline is CONTEXT, not a transform

`bias_baseline` computes the asset's trailing median of its own recent raw
scores. The obvious use — subtract it and publish the centred value — was
implemented and then **measured against the live history**, and it does not do
what it was built to do:

* It barely changes the bias mix. The live database ran BTCUSD at 86.6%
  BEARISH and EQUITY at 90.8% BEARISH with zero bull-dominant ticks; with the
  baseline applied the label changes on only 0.7–3.4% of ticks, because the
  baseline IS the trailing level and the day's level IS the trailing level.
* It costs the card its information. The sign rule fires on only 9.7–25.4% of
  ticks instead of ~66%, so 75–90% of every card would become a tie-break
  verdict: LOW confidence, a 50/50 bar, and a label chosen by the single
  heaviest headline. "Always bearish" would become "always uninformative".

The reason is structural: a trailing baseline is a CHANGE detector, so it
removes the change and leaves the level. A **structural** skew — a corpus
whose vocabulary is negative — lives in the level, and no amount of centring
will remove it. The measured cause of the skew is upstream of the score: the
article-level mix for BTCUSD is 2.4:1 bearish, but among VERY_HIGH strength
articles it is 7:1, and for XAGUSD the article mix is balanced (61 bearish / 60
bullish) while the bearish articles still carry 4.2× the final weight. The
amplification happens in the strength and event-tier classification, and that
is where a fix belongs — validated by the outcome loop above.

So the baseline is published as **context**: the card carries the level (what
the label and the bar are built from) and the asset's own recent norm, and
the note says whether the reading is in line with that norm or unusually far
from it.

## Things this document deliberately does not claim

- **MARKET_PREP is not a distinct scope.** `market_prep.build` folds its prep
  signals back together with the general flow, so its input set is identical to
  CURRENT's. It is published in the API for other consumers but the strip does
  not render it (Step 2).
- **`contradiction_detected` has never fired in production.** It needs
  `bull > 80 AND bear > 80`; across 21,231 live snapshot rows it was 0 every
  time. The Step 8 confidence cap is therefore live only via the clustered flag
  from Step 9.3. The `CONFLICTING SIGNALS` chip is wired but dark in practice.
- **Step 9.4 is not done.** `news_day_predicate` still OR-branches around the
  date test for any FinancialJuice row, which lets multi-day-old FJ articles
  satisfy the day filter. The carve-out is documented and deliberate (the US
  Market Wrap lands on the previous ET day), so removing it needs a decision
  about where FJ session reports should live.
- **`news_day_predicate`'s `is_today` clause is dead for ordinary news.** It is
  written once at ingest and never recomputed; the OR-clauses do the real work.
  For FJ rows it is the governing flag.

## Where to change what

| I want to… | Edit |
|---|---|
| change a weight factor | `sentiment_engine.py::_signal_weight` |
| change how long news lives | `config.py::TIER_HALF_LIVES`, `EVENT_HALF_LIFE_OVERRIDE` |
| change how much old news may count | `config.py::STALE_MASS_CAP_RATIO` |
| change how hard it is to flip | `.env` `FLIP_MARGIN`, `COOLING_MINUTES`, `COOLING_MARGIN`, `FLIP_HOLD_MINUTES`, `FLIP_MIN_CONFIDENCE` |
| change how much the gold/silver bleed counts | `.env` `GOLD_SILVER_BLEED` |
| change the LLM budget | `.env` `LLM_BATCH_LIMIT`, `LLM_TICK_BUDGET_SECONDS` |
| change the outcome horizon | `.env` `OUTCOME_HORIZON_MINUTES`, `OUTCOME_MIN_MOVE_PCT` |
| add an event type | `transmission_map.py::EVENT_SIGNALS` + `transmission_map.json` + `DRIVER_RANKINGS` |
| change a mapping's strength | `app/config/transmission_map.json` |
| change what the card shows | `display.py::to_display` |
| regenerate the map reference | `python tools/generate_transmission_docs.py` |
| see whether confidence is honest | `python tools/calibration_report.py` |
| see the hit rate | `python tools/hitrate_report.py --from-db` |
