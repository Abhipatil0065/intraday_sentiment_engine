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

Half-lives: **TIER_1 1.5h, TIER_2 1.25h, TIER 0.25h.**
Overrides: FOMC 4h, NFP/CPI/PPI 3h, FOMC minutes 2h, GEOPOLITICAL_RISK 6h.
Asset multipliers: GOLD 1.0, SILVER 1.0, BITCOIN 1.25. No EQUITY entry.

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
|raw| ≥ EPS (12.0)    → bias = sign(raw)
otherwise             → bias = _tie_break_bias(...)   # heaviest, newest signal

then _should_flip():
  1. no current bias                     → take the computed bias
  2. bull + bear ≤ 0                     → hold
  3. fresh TIER_1 opposite (< 1h)        → flip immediately, bypasses all gates
  4. inside COOLING_MINUTES (10)         → margin widens to COOLING_MARGIN (12)
  5. hold_elapsed < FLIP_HOLD_MINUTES (5)→ hold
  6. score has not crossed by FLIP_MARGIN (8) → hold
     (margin +10 when contradiction_detected)

score = payload_score(raw, final_bias)   # |raw| < EPS → ±TIEBREAK_LEAN (2.0)
```

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

`tools/calibration_report.py` reads the human WORKED / FAILED / Mixed labels,
buckets them by the engine's own confidence, and reports the observed hit rate.
It prints a `CONFIDENCE_CALIBRATION` block to paste into `app/config.py`.

**Currently unfitted by design**: `bias_outcome` has 0 rows, so there is nothing
to fit and any mapping would be an invented formula.
`calibrate_confidence()` is therefore an identity and the flag is off.

The fitter joins `bias_snapshots` for the confidence —
`bias_outcome.confidence` is written as a literal `0.0` by the POST endpoint.

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
| add an event type | `transmission_map.py::EVENT_SIGNALS` + `transmission_map.json` + `DRIVER_RANKINGS` |
| change a mapping's strength | `app/config/transmission_map.json` |
| change what the card shows | `display.py::to_display` |
| regenerate the map reference | `python tools/generate_transmission_docs.py` |
| see whether confidence is honest | `python tools/calibration_report.py` |
