# Changelog

All notable changes to the Intraday News Sentiment & Market Impact Engine.

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