# Changelog

All notable changes to the Intraday News Sentiment & Market Impact Engine.

---

## FORCED IMPACT, A WORKING SPEECH TAGGER, AND A DECAY THAT SURVIVES A RESTART (2026-10-22)

### Feature — `impact_overrides`: the impact you decide, not the one the provider guesses
- **Files:** `app/config/calendar_bias.json` (`impact_overrides`),
  `app/analysis/calendar_config.py` (`ImpactOverride`, `override_impact`,
  `apply_impact_overrides`), `app/ingestion/economic_calendar.py`
  (`apply_impact_overrides_logged`), `app/analysis/regional_bias.py`
  (the override is applied before the weighting there too)
- **The rule:** a release listed in `impact_overrides` is forced to High
  BEFORE any impact weighting, so it carries the High weight (3, not 2),
  appears in the upcoming high-impact list, and opens the event-risk window.
  FOMC rate decision / statement / press conference, Powell, Lagarde, Ueda,
  Bailey, every central-bank rate decision, CPI, Core CPI, NFP and PCE are
  forced to High by default; a rule may carry `exclude_keywords` so a survey
  ("CPI Expectations") is not mistaken for a print.
- **Logged:** every change names the event, the old impact, the new one and
  the keyword that forced it.
- **Interaction, stated plainly:** an overridden release also escapes the
  impact floor - if the config says CPI is High, it is used even when the
  provider tagged it Low. (In production the provider drops Low rows before
  this point, so the case is hypothetical.)

### Fix — the speech tagger now actually reaches the engine
- **Files:** `app/analysis/regional_bias.py` (`_read_events`,
  `speech_stance`), `app/analysis/calendar_config.py` (`speech_magnitude`),
  `app/config/calendar_bias.json` (`central_bank.speech_tags`,
  `central_bank.speech_magnitude`)
- **The bug:** a speech has no number, so it has no surprise - and the engine
  skipped every event without an actual/forecast BEFORE it looked at the
  stance. The keyword tagger was therefore dead code in the engine: a tagged
  speech never scored. The stance is now resolved first, and a tagged speech
  or press conference is worth `central_bank.speech_magnitude` sigma - a
  configured number, never an invented surprise.
- **Also:** the tagger is config-driven end to end, reports the keywords it
  matched, logs them, and shows them on the card. An UNTAGGED speech still
  scores nothing, and a title that matches both a hawkish and a dovish word
  is treated as untagged.

### Feature — the whitelist grows, and the auction rows go
- **File:** `app/config/calendar_bias.json`
- **UK:** added `ppi`, `rpi`, `retail sales`. UK PPI (input and output
  prices), UK RPI and UK Core Retail Sales are UK releases, not filler.
- **USA:** the Treasury AUCTION rows are out - `bid-to-cover`, `high yield`,
  `bill`, `bond`, `tips`, `auction` are now the USA `exclude_keywords`. A
  genuine "US 10-Year Treasury Yield" stays on the list; "US 20-Year Bond
  High Yield" and its siblings do not.

### Feature — the time decay survives a restart
- **Files:** `app/ingestion/calendar_cache.py` (`save_history`,
  `load_history`, `merge_history`), `app/ingestion/economic_calendar.py`
  (`merge_with_history`), `app/config.py` (`calendar_history_hours`)
- **The problem:** the provider only ever exposes TODAY, and the calendar was
  in-memory only, so every restart lost yesterday's releases - and with them
  the decay, exactly when it still matters (a print six hours old is still
  worth 90% of its weight).
- **The fix:** the last 48 h of events are persisted to
  `data/calendar_history.json` and merged back into every poll. The history
  holds the RAW feed, so the whitelist and the forced impact are re-applied
  to it on every poll: a config edit still takes effect immediately instead
  of resurrecting a release the user has since excluded.

### Feature — a LOW-CONFIDENCE country derivation is marked, not hidden
- **Files:** `app/ingestion/fj_calendar.py` (`guess_country_detail`,
  `GENERIC_HINTS`, the parse-time log), `tools/audit_calendar.py` (new)
- **Why:** FJ renders no country column, so the country comes from the
  title. A country derived from the GENERIC fallback list ("cpi", "pmi",
  "gdp", ...) is attributed to the US because the title names an indicator and
  no country. That answer is usually right and always unproven - "Swiss CPI"
  and "US CPI" contain the same words. Such a derivation is now marked LOW
  confidence in the engine log and in the audit output.
- **The audit tool:** `python tools/audit_calendar.py [--live]` prints every
  event with its derived country, the confidence, the assigned region, whether
  it was kept or dropped and why - and flags every untrustworthy derivation.

### Report — the latest day, end to end
- On the live feed (21 Oct, 72 events): 31 kept by the whitelist, of which
  **7 had both an actual and a forecast**, and **4 contributed a non-zero
  score**. The other 27 scored zero for a stated reason: 16 have no number
  yet, 8 are untagged speeches, 2 are the m/m twin of a release already
  counted, and 1 printed exactly on forecast (no surprise, no evidence).

### Tests
- 37 new tests: 16 for the forced impact and the country confidence, 12 for
  the speech tagger, 5 for the history and the decay across a restart, and 4
  for the whitelist changes. Suite: 1742 tests, 0 failures.

---

## CALENDAR: WIDER WHITELIST, OTHER-REGION RISK, REAL POLL CADENCE (2026-10-22)

### Feature — the whitelist now admits the releases it was missing
- **File:** `app/config/calendar_bias.json` (data only)
- **USA:** added `consumer confidence`, `umich sentiment`, `umich`,
  `university of michigan`, `university michigan`, `jolts`. (`powell` and
  `average hourly earnings` were already there.)
- **Japan:** added `pmi`, `purchasing managers` - Japanese PMI was being
  dropped even though the region was whitelisted.
- The live audit caught a wording gap: FJ renders the Michigan sentiment
  releases as "University Michigan 1 Yr Inflation Final", which contains
  neither "umich" nor "umich sentiment" - hence the two extra spellings.

### Feature — a dropped event still warns you, labelled "other region"
- **Files:** `app/analysis/calendar_risk.py` (`event_risk_check` gained
  `other_events`), `app/ingestion/economic_calendar.py`
  (`filtered_out_events()`), `app/services/pipeline.py` (the recompute passes
  the dropped events through)
- **The rule:** a high-impact release from a region OUTSIDE the whitelist -
  a Swiss CPI, an RBA decision - still moves the market, so it still raises
  the EVENT RISK note, labelled **"other region"**. It contributes **ZERO** to
  every score: it is not evidence for a dollar, only a warning that a standing
  bias may be about to be invalidated.
- **Tests:** the dropped events are recorded with their reasons, the note says
  "other region" (and only for those), a whitelisted release keeps the plain
  "EVENT RISK:" wording, and an unwhitelisted release scores 0.

### Fix — `FJ_CALENDAR_POLL_SECONDS` is now a real setting
- **Files:** `app/services/pipeline.py` (`_calendar_poll_due`,
  `_calendar_release_window`, the gated fetch in `run_once`),
  `.env.example` (the FJ calendar block, which was missing entirely)
- **Before:** the knob was read by no code. The FinancialJuice calendar was
  re-rendered on EVERY pipeline tick (180s by default) even though the render
  costs 10-20s and the site is Cloudflare-sensitive.
- **Now:** the calendar has its own cadence. Inside a high-impact release's
  window - from `FJ_CALENDAR_PRE_RELEASE_MINUTES` before the release until
  `FJ_CALENDAR_POST_RELEASE_MINUTES` after it - the cadence tightens to
  `FJ_CALENDAR_BURST_SECONDS`, so the actual number is picked up within a tick
  of the print instead of waiting out the full interval. The four knobs that
  were dead (POLL_SECONDS, PRE_RELEASE_MINUTES, POST_RELEASE_MINUTES,
  BURST_SECONDS) are now all live.
- **Tests:** seven, covering the cadence gate, the burst window, the
  "first poll is always due" rule and the "cadence 0 means every tick" case.

### Audit — the calendar, event by event
- A live FinancialJuice poll (123 rows -> 72 events) was audited: every
  event with its derived country, its assigned region, whether the whitelist
  kept it and why. Result: 28 kept, 44 dropped, 0 unresolved countries, and
  TWO derivation flags - both the Michigan sentiment releases, whose country
  comes from the GENERIC fallback ("inflation") rather than from a country
  marker. They are US releases, so the answer is right, but it is right by
  luck: the title names no country. The fix is the two extra Michigan
  spellings in the USA keyword list.

---

## REGION-SCOPED MACRO BIAS (2026-10-22)

### Feature — a five-region calendar whitelist, and a REGIONAL MACRO BIAS panel
- **Files:** `app/config/calendar_bias.json` (new), `app/analysis/calendar_config.py`
  (new), `app/analysis/regional_bias.py` (new), `app/ingestion/economic_calendar.py`
  (`apply_region_filter`), `app/config.py` (`calendar_region_filter_enabled`),
  `app/analysis/calendar_risk.py` (`matched_std`), `app/services/pipeline.py`
  (`current_calendar_events`, `regional_bias_payload`), `app/main.py` (payload key
  `regional_bias`), `app/dashboard/web/index.html` (SECTION A2),
  `tests/test_calendar_regions.py` (new), `tests/test_regional_bias.py` (new),
  `README.md`, `.env.example`
- **The problem:** the calendar carried every scheduled release the provider
  publishes - for every country - and each one could reach a dollar verdict at a
  weight as low as 0.05. A Swiss CPI print was treated as macro evidence for
  gold.
- **The filter:** matching is two-stage and strict. The event's country/currency
  must map to a region listed in `app/config/calendar_bias.json` (default: USA,
  Eurozone, China, Japan, UK), AND its title must match that region's
  `include_keywords`. The filter runs at the calendar input
  (`fetch_calendar()`), so the verdict, the calendar panel and the event-risk
  check all see the same whitelisted set. It is behind
  `CALENDAR_REGION_FILTER_ENABLED` (default true): with it off the previous
  behaviour is restored exactly. A broken config file fails SAFE - it logs and
  serves the calendar unfiltered rather than silently changing a published
  verdict.
- **The panel:** `SECTION A2 - REGIONAL MACRO BIAS` is a separate view, not a
  change to the news-weighted verdict. It scores Gold, Silver, Bitcoin and
  Equities independently on -100..+100 from
  `|surprise| x impact x region x asset_sensitivity x time_decay`, and shows the
  top 3 driving releases, the per-region contribution breakdown, and the next
  24 h of high-impact releases with a countdown.
- **What it refuses to do:** an upcoming print adds NO score (it is an event-risk
  flag, and inside its window the neutral zone is widened); a speech with no
  direction tag scores NOTHING; a print with no actual or no forecast is
  SKIPPED, never guessed at; an inverted indicator (unemployment rate, jobless
  claims) flips its sign; and one release cannot swamp the panel - the surprise
  is capped at `surprise.max_sigma` because the std table holds rule-of-thumb
  estimates. Every contributing event is logged with its region, weights,
  surprise and final contribution.
- **Tests:** 67 new tests - region filtering (both stages, the switch, the
  fail-safe), inverted-event handling, surprise calculation (standardised,
  percent fallback, the sigma cap, missing values), time decay (full window,
  linear decay, the horizon, upcoming events), score normalisation and labels,
  the event-risk window, the m/m + y/y dedupe, and the API payload.

---

## THE CARD SPLIT IS THE RATIO OF THE EVIDENCE (2026-10-21)

### Fix — "bull 2.8 · bear 1.2" no longer publishes as "Bullish 49% / Bearish 51%"
- **Files:** `app/analysis/display.py` (`split_from_totals`, `to_display`),
  `app/main.py` (`_block`), `app/analysis/equity_card.py` (`to_payload`),
  `app/services/bias_snapshots.py` (`AssetVerdict`, `build_rows`),
  `app/services/pipeline.py` (both `AssetVerdict` build sites),
  `app/analysis/fj_sessions.py` (`bullish_bearish_from_score`),
  `app/dashboard/web/index.html` (`disp`, `chipText`, `msplit`),
  `tests/test_display.py`, `tests/test_fj_sessions.py`
- **The bug:** the card's evidence line printed the aggregate
  (`score 39.8 · bull 2.8 · bear 1.2` — a 70/30 bullish split) while the boxes
  under it published "Bullish 49% / Bearish 51%". The 49/51 was not a
  calculation: `to_display()` projected the score
  (`bull_pct = round(50 + score / 2)`), and when the badge contradicted the
  projected split it REPLACED the whole split with a hard-coded one-point lean
  (`LEAN_BULL_PCT = 51`). That path fires whenever a fresh Tier-1 release
  flips the badge while the day's aggregate still leans the other way —
  `tier1_override` — which is why the card could say "BEARISH 51%" over
  evidence that was 70/30 bullish.
- **The rule now:** the split IS the ratio of the two captured totals,
  `bull_pct = round(100 * bull / (bull + bear))`, on every card — the asset
  cards, the EQUITY card and the five FinancialJuice boxes. The badge keeps
  the engine's verdict; where the verdict owns the split it also carries the
  percentage, and where it does not (the Tier-1 override case) the badge
  withholds the percentage instead of faking one, and the card's existing note
  explains the disagreement. The FJ boxes lost their 0.34 conviction cap for
  the same reason: a ratio cannot fabricate conviction, because a ratio IS the
  evidence — and the same 2.8-vs-1.2 evidence no longer reads 59/41 on one
  card and 70/30 on another.
- **Also changed:** the confidence shrink no longer applies to the ratio path
  (confidence already has its own badge, and shrinking toward 50/50 would
  re-create the very "boxes say 49/51, evidence says 70/30" problem); the
  client's "Flipped …" clock now tracks the engine's bias instead of the
  split's dominant side, because a ratio crosses 50% regularly; and
  `bias_snapshots` rows carry `bull` / `bear` so the logged split is
  reproducible from the logged evidence. Legacy rows with no captured totals
  fall back to the score projection, so no migration is needed.
- **Tests:** `test_display.py` locks the ratio split, the withheld percentage
  on a contradicting verdict, the "no synthetic 49/51" case, the legacy
  fallback, the JS fallback mirroring the server rule, and the FJ box's
  ratio. `test_fj_sessions.py` replaces the cap assertions with ratio
  assertions. Full suite: 1460 passed.

---

## EQUITY MARKETS IS THE FOURTH CARD IN EVERY FINANCIALJUICE SESSION BOX (2026-10-13)

### Feature — the FJ session boxes score four instruments, not three
- **Files:** `app/analysis/fj_sessions.py` (`FJ_ASSETS`, `FJ_CARD_LABELS`,
  `fj_card_label`, `_empty_box`, `analyse_report_text`, `build_session_analysis`),
  `app/main.py` (endpoint docstring), `app/dashboard/web/index.html`
  (`.fjgrid` 4-up + the 1250px 2-up step, `fjAsset` comment),
  `tests/test_fj_sessions.py`
- **What it does:** All five session boxes (US Market Wrap, Asian Market Wrap,
  Europe Market Prep, Europe Market Wrap, US Session Prep) now render a fourth
  card, **Equity Markets**, directly beside the Bitcoin card, so the equity
  half of a session report is read on its own card instead of being inferred
  through bitcoin's cues. Order is fixed: Gold → Silver → Bitcoin → Equity
  Markets, in found boxes and in the "no report yet" 50/50 boxes alike.
  `Asset.EQUITY` was already scored by `analyse_report_text` (it has its own
  cue family in `ASSET_CUES`, added when the S&P 500 became a first-class
  asset), so the fourth card reuses the same scorer - no second model, no new
  payload keys, and the 100% Bullish + Bearish invariant is unchanged because
  every card still flows through `bullish_bearish_from_score`.
- **Name only here:** `Asset.EQUITY.label` stays `"S&P 500"` everywhere else in
  the engine; the FJ section overrides the display name to `"Equity Markets"`
  via `FJ_CARD_LABELS` because each box is a regional session (Asia / Europe /
  US), not just the US tape.
- **Tests:** `test_fj_sessions.py` asserts the four-asset order on every box,
  the quarter-card's name, the untouched global label, and that an
  equities-only report yields equity drivers (`s&p` / `nasdaq` / `stocks`)
  while the three price cards honestly report "No asset-specific detail in
  this report". The no-content-stays-even case now covers EQUITY too. Full
  suite: 1221 passed.

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