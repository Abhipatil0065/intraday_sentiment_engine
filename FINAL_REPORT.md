# FINAL REPORT: Intraday News Sentiment & Market Impact Engine Audit

## Overall Health

The engine is **structurally sound** with a clean per-asset transmission map, deterministic heuristic engine, and comprehensive test suite (946 tests, all passing). The primary bug — equity card using metal direction — has been fixed and hardened with 26 new tests.

**Key metric: 946 tests pass (base + venv identical), zero regressions.**

---

## Area / Test / Result Summary

| Area | Test | Result | Notes |
|------|------|--------|-------|
| **1. Bug fix** | 3 failing bias scenarios | FIXED | Scenarios 3, 7, 8 now pass |
| **1. Bug fix** | Full test suite | 946 pass, 0 fail | No regressions |
| **1. Bug fix** | venv vs base interpreter | Identical | No difference |
| **2. Hardening** | 12+ equity card tests | 26 pass | Metal vs equity direction |
| **2. Hardening** | Keyword weakness docs | 3 documented | See bugs table below |
| **3. Ingestion** | Mock source (offline) | 12 articles, 7 today | Working |
| **3. Ingestion** | Fed RSS (live) | 20 articles fetched | All older than today |
| **3. Ingestion** | Bloomberg RSS (live) | 20 articles, 8 today | Working |
| **3. Ingestion** | FJ Calendar (live) | 62 events, 121 rows | No challenge/block |
| **3. Ingestion** | Faireconomy (live) | 9 events | Working |
| **3. Ingestion** | Calendar merge | Merge logic correct | See weakness below |
| **3. Ingestion** | Newsquawk | FILE MISSING | Stale tab reference |
| **4. Sentiment** | 25 headline table | 16 pass, 8 mismatch | All mismatches pre-existing |
| **4. Aggregation** | 3-signal math | Correct | Tie-break BEARISH, EPS=12 |
| **4. Aggregation** | Empty signals | DEFAULT_BIAS=BULLISH | Correct |
| **4. Aggregation** | Single signal | score=100.0 | Correct |
| **5. API** | 10 endpoints | All respond | 2 with expected 404 |
| **5. API** | Secret leakage | NONE found | sk- was false positive |
| **5. API** | Empty/error states | Proper | stale=True, DEFAULT_BIAS |
| **6. HTML Dashboard** | `/` returns | 200, 53KB HTML | Cache-Control: no-store |
| **6. HTML Dashboard** | Display consistency | 0 mismatches | API display == to_display |
| **6. Streamlit** | render_block() | NOT CALLED | Dead code, never invoked |
| **6. Streamlit** | Equity card | NOT REFERENCED | Completely omitted |
| **6. Streamlit** | Dead code | Lines 207-212 | Undefined var + nonexistent fields |
| **7. Contradictions** | Transmission vs cues | 6 contradictions | 4 BY-DESIGN, 2 need attention |

---

## Bugs Found

### 1. CRITICAL (pre-existing, FIXED): Equity card used metal direction
- **File:line**: `app/analysis/equity_card.py:207` (original)
- **Cause**: `build_equity_card` used `s.direction` (metal direction from transmission map) for non-ACTUAL signals. Metal and equity directions diverge for geopolitics (safe-haven gold vs risk-off equities), NFP (hawkish metals vs strong-economy equities), unemployment, etc.
- **Fix**: Added `equity=True` parameter to `detect_direction()` which applies subject-aware inversion (equity subjects + UP_VERBs = bullish, equity subjects + DOWN_VERBS = bearish, "lower guidance" context = bearish). Falls back to `s.direction` when cues return NEUTRAL/MIXED.
- **Tests**: `tests/test_equity_card_hardened.py` (26 tests)

### 2. HIGH (pre-existing, FIXED): "eased" cue doesn't match "eases"
- **File:line**: `app/analysis/cue_rules.py:38`
- **Cause**: BULLISH_CUES had `("eased", 2)` but `contains_cue` only appends inflection suffixes, never strips them. "eases" (3rd person) never matches "eased".
- **Fix**: Changed cue from "eased" to "ease" (matches ease/eases/eased/easing via inflection)
- **Tests**: Updated `tests/test_symmetry.py` known_gaps

### 3. MEDIUM (pre-existing, FIXED): "beats expectations" doesn't match "beat expectations"
- **File:line**: `app/analysis/cue_rules.py:34`
- **Cause**: BULLISH_CUES had `("beats expectations", 2)` but headlines often say "beat expectations" (without 's'). `contains_cue` inflection is append-only.
- **Fix**: Added `("beat expectations", 2)` and `("beat forecast", 2)` and `("beats forecast", 2)` alongside the existing cue
- **Tests**: `tests/test_bias_scenarios.py::test_scenario_8_strong_nfp`

### 4. HIGH (new, NOT FIXED): Streamlit dashboard is incomplete
- **File:line**: `app/dashboard/dashboard.py:61` (render_block defined but never called), `:7` (equity never rendered)
- **Cause**: `render_block()` and `bar_figure()` functions are defined but never invoked. The equity card data (`data["equity"]`) is fetched from the API but never displayed. Only status metrics, alerts, and news feed are shown.
- **Impact**: Streamlit dashboard doesn't show the core sentiment cards (asset or equity)
- **Fix needed**: Add calls to `render_block()` for each asset scope, and add equity card rendering

### 5. MEDIUM (new, NOT FIXED): Dead code after st.rerun()
- **File:line**: `app/dashboard/dashboard.py:207-212`
- **Cause**: After `st.rerun()` (line 205), code at lines 207-212 references undefined variable `b` and nonexistent API fields `risks` and `conflicts`
- **Impact**: If ever reached (impossible due to rerun), would raise NameError
- **Fix needed**: Remove dead code

### 6. LOW (new, NOT FIXED): Equity card payload missing `display` field
- **File:line**: `app/analysis/equity_card.py:281-297` (to_payload)
- **Cause**: `to_payload()` for EquityCardResult doesn't include `display` field, unlike `_block()` for asset cards in main.py
- **Impact**: HTML dashboard JS must compute display client-side for equity card; if JS doesn't handle this, equity display is broken

### 7. MEDIUM (pre-existing, NOT FIXED): Calendar merge similarity too strict
- **File:line**: `app/analysis/calendar_merge.py`
- **Cause**: `title_similarity("US CPI m/m", "CPI m/m (Median)")` = 0.417, below 0.85 threshold. "median" and "us" not in `_MATCH_NOISE`, reducing similarity.
- **Impact**: Same economic release from different providers may not merge, causing duplicate voting
- **Fix suggested**: Add "median" to `_MATCH_NOISE`, or lower threshold for same-event-type

### 8. LOW (pre-existing, NOT FIXED): Newsquawk file doesn't exist
- **File:line**: `app/ingestion/newsquawk.py` (reference only)
- **Cause**: File referenced in tabs but not present in codebase. Not in NEWS_SOURCES config.
- **Impact**: No impact (not configured as a source)

---

## Remaining Risks

1. **Cue-based equity direction is a heuristic approximation**: `detect_direction(equity=True)` uses keyword cues, not the full transmission map. When cues return NEUTRAL, the equity card falls back to the metal direction, which can be wrong (e.g., banking stress = BULLISH metal but BEARISH equity, but detect_direction returns NEUTRAL → falls back to BULLISH).

2. **Subject-aware inversion doesn't check semantic scope**: "Risk-off sentiment grips stocks as war fears rise" triggers a false BULLISH equity inversion because "stocks" and "rise" are both in the text, but the verb applies to "war fears", not "stocks".

3. **"Hot" keyword weakness**: "CPI comes in hot" doesn't match "hotter than expected" cue → NEUTRAL. Relies on ACTUAL surprise logic for correct direction.

4. **Streamlit dashboard incomplete**: Core sentiment visualization missing. Users relying on Streamlit miss asset/equity sentiment cards.

5. **Negation edge cases**: "Rate cut hopes fade" and "on hold" are not recognized as negators → "rate cut" BULLISH_CUE fires → BULLISH (wrong for metals when it's actually a no-cut scenario).

---

## Improvements Ranked by Impact

| Rank | Improvement | Impact | Effort |
|------|-------------|--------|--------|
| 1 | **Fix Streamlit dashboard**: Call `render_block()` for each asset scope, render equity card | HIGH | MEDIUM |
| 2 | **Remove dead code** in `dashboard.py` (lines 207-212) | LOW | LOW |
| 3 | **Add `display` field** to equity card `to_payload()` for HTML dashboard consistency | MEDIUM | LOW |
| 4 | **Fix calendar merge thresholds**: Add "median" to `_MATCH_NOISE` | MEDIUM | LOW |
| 5 | **Add negation phrases**: "hopes fade", "on hold", "unlikely to" to negation patterns | MEDIUM | MEDIUM |
| 6 | **Add "hot" as context-sensitive cue** (not standalone) for inflation | LOW | MEDIUM |
| 7 | **Create `app/ingestion/newsquawk.py`** if Newsquawk integration is still needed | LOW | HIGH |
| 8 | **Add equity direction to transmission map**: Create an EQUITY column in transmission_map.json so the equity card can resolve direction via the map (not just cues) | HIGH | HIGH |

---

## Files Changed

```
app/analysis/cue_rules.py         - Added equity=True param, EQUITY_SUBJECTS, _EQUITY_LOWER_CONTEXT, "beat expectations" cue, "eased"→"ease"
app/analysis/equity_card.py       - Re-derive equity direction via detect_direction(equity=True) for non-ACTUAL signals
tests/test_symmetry.py            - Updated known_gaps: "eased" → "ease"
tests/test_equity_card_hardened.py - NEW: 26 tests for equity card direction (metal vs equity)
```
