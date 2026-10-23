"""
Central configuration.

Everything is environment-driven so the monitored website can be swapped
without touching a single line of application code (see NEWS_SOURCES).
"""

from __future__ import annotations

import os
import re
import statistics
from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.enums import NewsCategory

BASE_DIR = Path(__file__).resolve().parent.parent

QualityTier = Literal[
    "TIER_1_OFFICIAL",
    "TIER_2_MAJOR",
    "TIER_3_CRYPTO",
    "TIER_4_OTHER",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ---------------- application ----------------
    app_env: str = "development"
    log_level: str = "INFO"
    log_file: str = "logs/engine.log"

    # ---------------- database ----------------
    database_url: str = "sqlite:///./data/intraday_sentiment.db"

    # ---------------- timezones ----------------
    # MARKET_TIMEZONE defines what "TODAY" means for the today-only filter.
    market_timezone: str = "America/New_York"
    display_timezone: str = "Asia/Kolkata"

    # ---------------- LLM provider (provider-neutral) ----------------
    # Works with any OpenAI-compatible endpoint: DeepSeek, NVIDIA NIM,
    # OpenRouter, Groq, Together, Ollama, vLLM...
    #
    # Legacy DEEPSEEK_* names are still accepted as aliases, so existing
    # .env files keep working unchanged.
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("LLM_API_KEY", "DEEPSEEK_API_KEY")
    )
    llm_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("LLM_BASE_URL", "DEEPSEEK_BASE_URL"),
    )
    llm_model: str = Field(
        default="meta/llama-3.2-11b-vision-instruct",
        validation_alias=AliasChoices("LLM_MODEL", "DEEPSEEK_MODEL"),
    )
    llm_timeout_seconds: int = Field(
        default=60, validation_alias=AliasChoices(
            "LLM_TIMEOUT_SECONDS", "DEEPSEEK_TIMEOUT_SECONDS")
    )
    llm_max_concurrent: int = Field(
        default=2, validation_alias=AliasChoices(
            "LLM_MAX_CONCURRENT", "DEEPSEEK_MAX_CONCURRENT")
    )
    # Hard ceiling on outbound requests per rolling 60-second window.
    # Counted per HTTP ATTEMPT, so retries are charged against the budget too -
    # that is the number the provider actually sees. A call that would exceed
    # the budget waits for the oldest request to age out instead of firing, so
    # the key is never throttled by NVIDIA with a 429. Set 0 to disable.
    llm_requests_per_minute: int = Field(
        default=40, validation_alias=AliasChoices(
            "LLM_REQUESTS_PER_MINUTE", "DEEPSEEK_REQUESTS_PER_MINUTE")
    )
    llm_max_tokens: int = Field(
        default=1024, validation_alias=AliasChoices(
            "LLM_MAX_TOKENS", "DEEPSEEK_MAX_TOKENS")
    )
    # Some OpenAI-compatible servers reject response_format={"type":"json_object"}.
    # Set false to rely purely on prompt instructions + JSON extraction.
    llm_json_mode: bool = Field(
        default=True, validation_alias=AliasChoices("LLM_JSON_MODE")
    )
    # Reasoning models (Nemotron-3, DeepSeek-R1, QwQ...) burn the token budget
    # on internal reasoning and can return an EMPTY content field. Disabling
    # thinking keeps responses fast, cheap and reliably parseable.
    llm_disable_thinking: bool = Field(
        default=True, validation_alias=AliasChoices("LLM_DISABLE_THINKING")
    )
    llm_enabled: bool = Field(
        default=True, validation_alias=AliasChoices("LLM_ENABLED")
    )

    # ---------------- ingestion ----------------
    poll_interval_seconds: int = 180
    article_fetch_enabled: bool = True
    article_fetch_max_chars: int = 4000
    http_timeout_seconds: int = 20
    http_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
    use_mock_source: bool = False

    # ---------------- retention ----------------
    # Calendar days of news kept in the database, counted in MARKET_TIMEZONE.
    # 2 = keep today plus the previous day; anything older is deleted on the
    # next pipeline pass. Set to 0 (or a negative number) to disable purging
    # entirely and keep every article forever.
    news_retention_days: int = Field(
        default=2, validation_alias=AliasChoices("NEWS_RETENTION_DAYS")
    )

    # ---------------- news feed paging ----------------
    # How many articles one dashboard payload carries. Purely a transport
    # concern: /api/news can page through the entire day regardless of this.
    # A busy day holds several hundred rows, each with its full per-asset
    # impact breakdown, so shipping the whole day on every 30s poll would be
    # wasteful. Readers page in more via the "Load more" button.
    news_feed_page_size: int = 60
    # Ceiling on a single /api/news page, so one request can never ask the
    # server to serialise an unbounded number of rows.
    news_feed_max_page_size: int = 200

    # ---------------- deduplication / clustering ----------------
    dedup_similarity_threshold: float = 0.72
    dedup_time_window_hours: int = 12
    # A cluster counts as ONE event. Extra INDEPENDENT sources add only a small
    # capped boost with diminishing returns (1/log2(n+1)), so five outlets
    # covering one FOMC decision cannot cast five votes. Same-source repeats
    # never raise the boost. Total boost capped by CORROBORATION_MAX_BOOST.
    corroboration_per_source: float = 0.10
    corroboration_max_boost: float = 0.30
    # Task 5b: when a scheduled release (ACTUAL_DATA) arrives, earlier preview
    # or forecast headlines for the same indicator family and same day stop
    # counting (weight 0). Match by indicator family and date, not by exact text.
    preview_suppression_enabled: bool = True
    # Phase 1: when false, every signal keeps corroboration=1.0 exactly as it
    # did before the wiring fix, so the change can be switched off in one place.
    corroboration_enabled: bool = True
    # Impact gate: when true, ONLY VERY_HIGH / HIGH impact news may reach a
    # card. MEDIUM and LOW impact items used to contribute at a reduced scale
    # (STRENGTH_MULT 0.45 / 0.20); with this on they are dropped at the single
    # weight choke point in sentiment_engine._signal_weight, so they can never
    # enter the bullish/bearish totals, the driver boxes, the burst bonus or
    # the tie-break. Set false to restore the old scaled behaviour.
    only_high_impact_counts: bool = True
    # ONE-SHOT LLM POLICY: a classifier verdict decides the article's FATE at
    # ingest. HIGH / VERY_HIGH impact - the article-level importance OR any
    # per-asset impact strength - stays in the Section B news feed. MEDIUM /
    # LOW is DELETED together with its events and impacts, and its guid is
    # suppressed for the rest of the trading day, so a source re-serving the
    # same story cannot make the engine pay for it a second time.
    drop_low_impact_articles: bool = True
    # Restrict the delete rule to articles the model actually judged. A
    # heuristic row (batch cap / circuit breaker / no key) can never reach a
    # card anyway - only_high_impact_counts already excludes it - so deleting
    # those too would empty the feed exactly when the model is unavailable.
    only_llm_verdict_may_drop: bool = True
    # ---------------- news scope (region whitelist for macro/CB events) ----------------
    # Only macro data prints and central-bank events from USA/UK/China/Japan/EU
    # reach the cards. Geopolitics/risk/crypto/oil/etc. keep their normal path.
    news_scope_enabled: bool = Field(
        default=True, validation_alias="NEWS_SCOPE_ENABLED")
    # Drop out-of-scope macro prints at ingest (keeps Section B feed clean).
    news_scope_drop_out_of_region: bool = Field(
        default=True, validation_alias="NEWS_SCOPE_DROP_OUT_OF_REGION")
    # Strict: only macro/calendar/CB categories may reach cards (off by default).
    news_scope_macro_only: bool = Field(
        default=False, validation_alias="NEWS_SCOPE_MACRO_ONLY")

    # ---------------- chart scope (per-chart card isolation) ----------------
    # TradingView serves a PER-SYMBOL news feed: the stories listed on the
    # gold chart, the silver chart, the bitcoin chart. CHART_SCOPE_ISOLATION
    # makes that feed decide WHICH CARD a story may touch: a story listed on
    # the gold chart reaches the gold card and no other card, one listed on
    # two charts reaches both cards. Sources without a chart (every non-
    # TradingView feed) keep the classifier's routing exactly as before.
    # See app/analysis/chart_scope.py for the measured tag data behind this.
    chart_scope_isolation: bool = Field(
        default=True, validation_alias="CHART_SCOPE_ISOLATION")
    # Strict one-owner mode: the first match in GOLD -> SILVER -> BITCOIN ->
    # EQUITY wins and the other cards get nothing. OFF by default - a story
    # on two charts feeding both cards is TradingView's own semantics.
    chart_scope_single_owner: bool = Field(
        default=False, validation_alias="CHART_SCOPE_SINGLE_OWNER")
    # A chart-scoped story is on that chart BY TRADINGVIEW'S OWN TAGGING, so
    # a MEDIUM-strength reading of it still counts (at the MEDIUM weight)
    # instead of being dropped by the HIGH-only impact gate - otherwise most
    # chart news would land on the right card with zero weight.
    chart_scope_medium_counts: bool = Field(
        default=True, validation_alias="CHART_SCOPE_MEDIUM_COUNTS")

    # ---------------- rolling news window (item 3) ----------------
    # Replaces the "same market-timezone calendar date" gate. A rolling window
    # admits the overnight US wrap by construction, which is why the old
    # FinancialJuice / IST date carve-out is no longer needed.
    news_window_hours: float = Field(default=18.0, validation_alias="NEWS_WINDOW_HOURS")
    # Hard cap on article age, independent of the window. Items older than this get weight 0.
    trading_day_cutoff_hours: float = Field(default=2.0, validation_alias="TRADING_DAY_CUTOFF_HOURS")
    # Age within which a signal counts toward the "changed in last 60 min" delta.
    delta_window_minutes: int = 60
    # Hard cap on article age, independent of the window (0 = disabled).
    max_article_age_hours: int = 26
    update_window_hours: int = 6

    # ---------------- bias / NO_BIAS thresholds (item 2) ----------------
    # Score is -100..+100.
    #
    # STEP 11: bias_min_abs_score / bias_min_confidence / bias_score_scale
    # were the NO_BIAS-era floors. Item 2 replaced them with a BINARY bias
    # (BULLISH or BEARISH, never withheld), and the equivalents now live as
    # module constants in app/analysis/sentiment_engine.py: EPS = 25.0 is the
    # old bias_min_abs_score and TIEBREAK_MAX_CONFIDENCE = 0.30 is the old
    # bias_min_confidence. Keeping the settings copies invited reading a
    # threshold that nothing consults, so they are retired here; see
    # sentiment_engine.py's comment at the EPS definition. bias_score_scale
    # was 100.0 and is now the literal in _score_from.
    # ---- Item 5: minimum confidence before a computed flip is allowed. ----
    # A flip the stability gates say "yes" to still has to clear this floor:
    # below it the new reading is too thin to be trusted and the previous bias
    # is held instead, with the note recording the reason. This is a GATE on
    # flipping, not on publishing - the label stays binary, and a first-run
    # verdict (no previous bias to hold) is never refused. A fresh Tier-1
    # official release also ignores the floor: a confirmed release flips on
    # its own, however thin the surrounding evidence.
    #
    # WIRED UP (this used to be dead): the value lived as a hardcoded 0.30 in
    # sentiment_engine.py while THIS field sat at 0.50 and was read by
    # nothing - the two disagreed and the field was inert. The engine now
    # reads this field, and the default matches the value that was actually
    # in force, so enabling the gate changes no behaviour until the operator
    # chooses a different number.
    # DAY TRADING: lower threshold for faster flips (was 0.35)
    flip_min_confidence: float = 0.30
    # Dampening factor for display/cue strength. damped = raw * total_weight / (total_weight + k).
    # k=0 disables dampening (damped = raw). Default 0.5.
    # DAY TRADING: less dampening for more responsive scores (was 0.5)
    score_damping_k: float = Field(default=1.0, validation_alias="SCORE_DAMPING_K")

    # ---------------- per-asset baseline normalization (item 1) ----------------
    # Financial news is structurally negative-heavy, so raw cue scores carry a
    # persistent per-asset skew: the bias_snapshots log ran ~81% BEARISH. A raw
    # score is a LEVEL, not a change, so subtracting a rolling baseline of the
    # asset's own recent raw scores turns "how negative is today" into "how
    # different is today from the last few ticks" - which is what a direction
    # actually is.
    #
    # The baseline is computed from PERSISTED bias_snapshots rows only (articles
    # are purged at 48h, snapshots are the durable source), strictly from ticks
    # BEFORE the current one, so there is never any same-tick look-ahead.
    #
    # BASELINE_WINDOW_TICKS - how many prior persisted ticks form the baseline.
    # BASELINE_MIN_TICKS  - fewer than this and the baseline is not trustworthy;
    #                        the raw score is published as-is until then.
    # BASELINE_STATISTIC  - "median" (default) or "mean". Median is used because
    #                        BTCUSD's raw scores cluster in a deep negative tail
    #                        (-55..-90 for long stretches), where the MEAN is
    #                        dragged so far negative that a clearly bearish score
    #                        (-56.7 against a -65 mean) reads as BULLISH - an
    #                        inversion. The median is robust to that tail.
    # BASELINE_ENABLED    - feature switch; false publishes raw scores exactly
    #                        as before this change.
    # BASELINE_MIN_SCORE  - when |raw| is below this the score is a tie-break
    #                        band, not a verdict: publish raw as-is so borderline
    #                        calls are untouched. Mirrors the existing EPS floor.
    baseline_window_ticks: int = Field(
        default=20, validation_alias="BASELINE_WINDOW_TICKS")
    baseline_min_ticks: int = Field(
        default=5, validation_alias="BASELINE_MIN_TICKS")
    baseline_statistic: str = Field(
        default="median", validation_alias="BASELINE_STATISTIC")
    baseline_enabled: bool = Field(
        default=True, validation_alias="BASELINE_ENABLED")
    baseline_min_score: float = Field(
        default=12.0, validation_alias="BASELINE_MIN_SCORE")

    # ---------------- text quality (item 5) ----------------
    # Token distance within which a subject and verb are treated as related.
    # Replaces the old fixed 24-character regex window, which failed on long
    # phrasing and could not be tuned.
    inversion_max_tokens: int = 8
    # Tokens a negation may look ahead for its cue.
    negation_window_tokens: int = 4
    price_action_weight: float = 0.10
    priced_in_multiplier: float = 0.45
    priced_in_floor: float = 0.25
    surprise_multiplier: float = 1.35
    hedge_multiplier: float = 0.70
    hedge_floor: float = 0.35

    # ---------------- decay half-lives in hours, by category (item 7) ---
    # Exponential decay: weight = 0.5 ** (age_hours / half_life).
    # SUPERSEDED (Task 5a, retuned 2026-10-13): decay now runs on the
    # per-event-tier table TIER_HALF_LIVES scaled by ASSET_DECAY_MULTIPLIER
    # (module level, bottom of this file; see sentiment_engine.decay_weight).
    # These per-category fields are kept only for backward compatibility with
    # old .env files - nothing in the scoring path reads them.
    decay_default_hours: float = 6.0
    decay_half_lives: str = (
        "Central Bank=3.0,Interest Rates=3.0,Inflation=3.0,Employment=3.0,"
        "Treasury Yields=4.0,USD/DXY=5.0,GDP=6.0,Banking/Financial Stress=8.0,"
        "Geopolitics=8.0,Risk Sentiment=5.0,ETF Flows=24.0,Bitcoin ETF Flows=24.0,"
        "Crypto Market Flows=12.0,Crypto Regulation=72.0,China=12.0,"
        "Industrial Demand=12.0,Commodity Supply/Demand=10.0,Oil/Energy=8.0,"
        "Equities=8.0,Other=6.0"
    )

    # ---------------- economic calendar (item 8) ----------------
    # Standardised surprise = (actual - forecast) / std. Std values are per
    # indicator and configurable; a missing indicator falls back to the default.
    #
    # PROVENANCE OF EVERY VALUE BELOW
    # ================================
    # These are ORDER-OF-MAGNITUDE ESTIMATES, not measured statistics. They were
    # chosen so a typical miss lands in a sensible 0-3 z range, not fitted to any
    # dataset. Each entry is tagged:
    #
    #   [RULE-OF-THUMB]  a conventional market figure for this indicator
    #   [ESTIMATE]        a rough guess that must be replaced
    #
    # TODO(verify-std): replace every [ESTIMATE] with the real historical
    # standard deviation of the published series, computed as
    #     std(actual - forecast) over >= 10 years of releases,
    # from FRED or the issuing agency. Until then the surprise BANDS below are
    # only as trustworthy as these guesses, and SURPRISE_NEUTRAL_BAND /
    # SURPRISE_STRONG_BAND must be re-tuned in the same pass, because they are
    # expressed in exactly these std units.
    #
    # Basis for the rule-of-thumb figures: sell-side macro notes routinely quote
    # "CPI >0.1pp", "NFP >50-100k", "DXY >0.3%" as market-moving thresholds.
    # This table exists to normalise those conventions ACROSS indicators.
    surprise_std_default: float = 1.0
    surprise_neutral_band: float = 0.30
    surprise_strong_band: float = 1.00
    surprise_std_table: str = (
        # --- inflation: percentage points of the year-on-year rate ---
        "cpi=0.3,ppi=0.3,inflation=0.3,"            # [RULE-OF-THUMB]
        "core cpi=0.2,core pce=0.2,pce=0.2,deflator=0.2,"  # [RULE-OF-THUMB] core is less volatile than headline
        # --- labour: thousands of jobs (claims also in thousands) ---
        "nonfarm=120.0,nfp=120.0,payroll=120.0,"   # [ESTIMATE] TODO(verify-std)
        "employment change=120.0,"                 # [ESTIMATE] TODO(verify-std)
        "jobless claim=10.0,"                      # [ESTIMATE] TODO(verify-std)
        "unemployment rate=0.2,"                   # [RULE-OF-THUMB] percentage points
        # --- policy: percentage points of the target rate ---
        "rate decision=0.25,ecb=0.25,boe=0.25,boj=0.25,"   # [RULE-OF-THUMB] a 25bp surprise
        "fomc=0.5,federal reserve=0.5,powell=0.5," # [ESTIMATE] TODO(verify-std)
        # --- activity: index points ---
        "gdp=0.5,pmi=0.5,ism=0.5,"                 # [ESTIMATE] TODO(verify-std)
        "industrial production=0.4,retail sales=0.5,"      # [ESTIMATE] TODO(verify-std)
        # --- rates and FX ---
        "treasury yield=0.15,bond yield=0.15,10-year=0.15,"  # [RULE-OF-THUMB] ~15bp
        "dxy=0.4"                                  # [RULE-OF-THUMB] DXY index points
    )
    # A high-impact release this soon makes any standing bias unreliable.
    event_risk_lookahead_minutes: int = 45
    event_risk_min_importance: str = "HIGH"

    # ---------------- bias snapshot log (phase 2) ----------------
    # Append one row per asset per poll into ``bias_snapshots`` so a published
    # bias can be reviewed by hand. Purely additive: rows are written after
    # sentiment is persisted and are read only by scripts/export_snapshots.py,
    # so switching it off cannot affect a verdict.
    #
    # NEWS-ONLY: this log deliberately holds no price and no price-derived
    # number. Price comparison is done manually.
    bias_snapshot_enabled: bool = True

    # ---------------- price confirmation ----------------
    # Master switch for the price-action confirmation panel (GOLD/SILVER have
    # no spot feed; BITCOIN is covered by the Coinbase leg in price_data.py).
    price_data_enabled: bool = Field(default=False, validation_alias="PRICE_DATA_ENABLED")
    price_api_base_url: str = "https://api.coinbase.com/v2"

    # ---------------- stability / flip tuning ----------------
    # Minimum margin by which the opposite side must exceed the current side
    # to trigger a flip (unless a fresh Tier 1 item forces it).
    flip_margin: float = Field(default=20.0, validation_alias="FLIP_MARGIN")
    # Minimum minutes between bias flips for non-Tier-1 events.
    flip_hold_minutes: int = Field(default=15, validation_alias="FLIP_HOLD_MINUTES")
    # Anti-whipsaw cooling period after a flip (minutes).
    cooling_minutes: int = Field(default=20, validation_alias="COOLING_MINUTES")
    # Wider margin required during cooling period.
    cooling_margin: float = Field(default=18.0, validation_alias="COOLING_MARGIN")

    # ---------------- alerts ----------------
    alert_on_importance: str = "VERY_HIGH"
    alert_on_strength: str = "VERY_HIGH"

    # ---------------- sources ----------------
    # "Name|type|url|weight|quality_tier" entries separated by commas/newlines.
    news_sources: str = ""

    # TradingView symbol mapping override: "XAUUSD=FX_IDC:XAUUSD,BTCUSD=BITSTAMP:BTCUSD"
    tradingview_symbol_map: str = ""

    # ---------------- assets ----------------
    # Item 2: S&P 500 as a first-class asset. EQUITY is iterated by the pipeline
    # like the other three, persisted to sentiment_snapshot/bias_snapshots, and
    # served from its own card - it is no longer a proxy for BITCOIN.
    #
    # USOIL is crude oil (WTI). It is a first-class asset again: it gets its
    # own snapshot, its own bias history and its own card. It is listed here
    # so the TradingView per-chart news feed also queries the oil chart.
    assets: str = "XAUUSD,XAGUSD,BTCUSD,EQUITY,USOIL"

    # ---------------- session profiles (Item 3) ----------------
    # Per-asset session multiplier profile. BTCUSD is "flat" by default: it
    # trades 24 hours a day, so an Asia-session or weekend headline carries the
    # same weight as a New York one, unlike the physical metals which are
    # thinly traded outside London/New York.
    #
    # Values: "metal" (session curve + weekend discount), "flat" (1.0 always),
    # "weekend_tolerant" (session curve, weekends at full weight).
    #
    # Override per asset, e.g. BTC_SESSION_PROFILE=weekend_tolerant,
    # XAG_SESSION_PROFILE=flat. Unknown values fall back to "metal".
    asset_session_profiles: str = ""

    # ---------------- cross-asset bleed (Item 4) ----------------
    # How much a high-relevance gold story bleeds into silver (and vice versa).
    # Both relevance AND persistence are multiplied by this, so it is a single
    # knob for how much the pair shares a common driver. Lower values make
    # silver require its OWN corroboration rather than inheriting gold's read.
    #
    # WIRED UP (this used to be dead): signal_factory.build_bleed_signal
    # hardcoded 0.40 on both relevance and persistence, so this field - and
    # every "tune the bleed" instruction that pointed at it - did nothing.
    # The builder now reads this field. 0.30 is the tightening that stops
    # silver being a shadow of gold; raise it toward 1.0 to let the pair
    # share a driver more fully.
    gold_silver_bleed: float = 0.30

    # ---------------- LLM budget ----------------
    # How many articles may be classified by the model in one tick, and how
    # many SECONDS of that tick the model may take.
    #
    # The COUNT cap alone cannot keep a tick inside its interval: a slow
    # provider burns the full 60s timeout on every call. The circuit breaker
    # does not cover that case either - it counts CONSECUTIVE failures, so a
    # provider that alternates success and timeout never trips it.
    #
    # MEASURED on the live database (last 80 ticks): tick duration was
    # p50 121s, p90 589s, max 946s (15.8 minutes), and 27 of 80 ticks ran
    # past the 180s tick interval. A ten-minute tick is a ten-minute-stale
    # card, which defeats the point of an intraday engine - so the tick now
    # has a TIME allowance as well, and the remaining articles fall back to
    # deterministic heuristics once it is spent.
    llm_batch_limit: int = Field(default=15, validation_alias="LLM_BATCH_LIMIT")
    llm_tick_budget_seconds: float = Field(
        default=90.0, validation_alias="LLM_TICK_BUDGET_SECONDS")

    # ---------------- LLM priority sources ----------------
    # Source names (comma-separated, case-insensitive) that get FIRST pick of
    # the per-tick LLM budget instead of queueing behind fresher wire copy.
    #
    # WHY THIS EXISTS
    # ---------------
    # The LLM budget is small and STRICTLY FIRST-COME-FIRST-SERVED over the
    # sorted batch. Measured on the live endpoint (2026-10-21): a model call
    # costs 20-40s, so the 90s tick budget buys only 2-5 verdicts per tick -
    # the batch count cap of 15 is never reached. The order therefore decides
    # who is judged at all, and the order is tier-then-recency: a source whose
    # headlines are consistently 10-30 minutes behind the freshest wire copy
    # NEVER wins a model call, however much it contributes.
    #
    # TradingView was exactly that source: 26 rows ingested, 0 model verdicts,
    # so every TradingView headline was scored by the deterministic heuristic
    # instead - a weaker importance read, and (because only a model verdict may
    # delete an article) a MEDIUM/LOW headline that would have been deleted
    # sat in the feed instead.
    #
    # The reservation is deliberately small and age-bounded:
    #   llm_priority_slots          how many of the tick's model calls may go
    #                               to the listed sources (the rest of the
    #                               budget is untouched).
    #   llm_priority_max_age_hours  a listed-source item older than this does
    #                               NOT get the reservation, so a backlog after
    #                               a restart cannot spend the whole budget on
    #                               stale rows.
    #
    # Empty (the default) changes nothing: the sort behaves exactly as before.
    llm_priority_sources: str = Field(
        default="", validation_alias="LLM_PRIORITY_SOURCES")
    llm_priority_slots: int = Field(
        default=2, validation_alias="LLM_PRIORITY_SLOTS")
    llm_priority_max_age_hours: float = Field(
        default=1.0, validation_alias="LLM_PRIORITY_MAX_AGE_HOURS")

    # ---------------- decision log / audit (item 11) ----------------
    # Every classification and every published bias is appended here so the
    # transmission map and decay table can be tuned against real outcomes.
    decision_log_path: str = "logs/decisions.jsonl"

    # ---------------- stale mass cap ----------------
    # How much OLD news may contribute to a card, as a multiple of the total
    # FRESH news in the same card.
    #
    # The per-signal decay floors at DECAY_FLOOR (0.05) past the trading-day
    # cutoff, so old news fades but never vanishes. That is deliberate and
    # tested. The problem is that the floor is PER SIGNAL: with 130+ signals
    # a tick, hundreds of hours-old headlines each add 5% of their weight and
    # the accumulated shelf becomes the majority of the evidence.
    #
    # MEASURED on the live database - the day's surviving HIGH/VERY_HIGH
    # weight mass that is already older than the 8h cutoff, by hour of the
    # session:
    #
    #     hour  09  10  11  12  13  14  15  16  17  18  19  20  23
    #     stale 20% 29% 29% 31% 31% 56% 54% 68% 66% 69% 67% 65% 57%
    #
    # Through the US session the card is majority-driven by news that was
    # already priced in hours ago, and a headline that just landed cannot
    # move it. That is the opposite of what a DAY-TRADING tool should be.
    #
    # With 0.5, each side's stale mass is capped at half the card's total
    # fresh mass, so at least half of every card is news from the last 8
    # hours. The cap is measured against the TOTAL fresh mass, not against
    # the same side's fresh mass, so a side with no fresh news of its own
    # cannot be drowned by the other side's stale news either.
    #
    # Set to 0.0 (or less) to disable the cap - old news then counts in full.
    # Set to 1.0 to allow each side's stale mass to reach the whole fresh
    # mass.
    stale_mass_cap_ratio: float = Field(
        default=0.5, validation_alias="STALE_MASS_CAP_RATIO")

    # ---------------- outcome capture (the measurement loop) ----------------
    # The engine now scores ITSELF. Every published bias CHANGE is recorded
    # as a PENDING call, the price at the call is stored, and the call is
    # resolved to WORKED / FAILED / Mixed once the horizon has elapsed.
    #
    # Until this existed, ``bias_outcome`` could only be filled by hand - one
    # click per call, ~1,400 clicks a day - so it had zero rows for the whole
    # life of the project and every tuning decision was unfalsifiable.
    #
    # NOTHING HERE FEEDS BACK INTO SCORING. The engine never reads these
    # numbers when it decides a bias; they only tell you, afterwards,
    # whether it was right.
    outcome_capture_enabled: bool = True
    # How long a call is given to prove itself, in minutes.
    outcome_horizon_minutes: int = 30
    # Dead band, in percent. A move smaller than this is MIXED (neither
    # confirmed nor contradicted) rather than a hit or a miss.
    outcome_min_move_pct: float = 0.05
    # How far the entry and exit prices may sit from the exact window edges
    # before the call is left PENDING rather than scored against a stale
    # price.
    outcome_max_lookback_minutes: int = 10
    # How long price points are kept. The series only has to cover the
    # horizon plus slack.
    outcome_price_retention_hours: int = 72

    # ---------------- economic calendar ----------------
    economic_calendar_enabled: bool = True
    # Only Medium/High/Holiday prints are kept; Low prints are noise.
    economic_calendar_min_importance: str = "MEDIUM"
    # How far ahead an event is allowed to influence PREP (hours).
    economic_calendar_horizon_hours: int = 48

    # REGION WHITELIST for the calendar. When enabled, only events whose
    # country/currency belongs to a region listed in
    # ``app/config/calendar_bias.json`` (default: USA, Eurozone, China, Japan,
    # UK) AND whose title matches that region's event list may reach the
    # sentiment engine. Everything else is dropped at the calendar input.
    #
    # Set it to false to restore the previous behaviour exactly - useful for
    # comparing the old verdict against the region-filtered one. The
    # whitelist itself (regions, keywords, weights) lives in the JSON file,
    # never here.
    calendar_region_filter_enabled: bool = True

    # FinancialJuice is the sole calendar provider.
    calendar_providers: str = "financialjuice"

    # FinancialJuice economic calendar. Ships OFF until live-verified.
    #
    # This provider renders a public, JS-driven page with PLAIN Chromium: no
    # --disable-blink-features=AutomationControlled, no stealth plugin, no
    # fingerprint spoofing, no challenge solving. Live verification showed the
    # calendar hydrates identically with and without such flags, so none is
    # used. A Cloudflare challenge is treated as a FAILED poll and backed off
    # for 30 minutes - never retried rapidly, never solved.
    financialjuice_calendar_enabled: bool = False
    # Fewer rows than this means the SPA had not hydrated, which is a FAILURE,
    # never "no events today". The page carries 190+ rows when live.
    fj_calendar_min_rows: int = 10
    # Hard ceiling on one render, wall-clock seconds.
    fj_calendar_timeout_seconds: int = 40
    # Backoff after a detected challenge/block, minutes.
    fj_calendar_backoff_minutes: int = 30
    # The browser context is pinned to UTC because FJ renders clocks in the
    # BROWSER's timezone (verified: the same page rendered under two context
    # timezones shifted every clock by exactly their offset). Pinning to UTC
    # makes the rendered clock UTC, so this value MUST stay "UTC" or every
    # timestamp would be skewed. fj_calendar.render_tz_matches() enforces it.
    fj_calendar_render_timezone: str = "UTC"

    # FJ is a heavy render (10-17s), so it polls far less often than news.
    fj_calendar_poll_seconds: int = 600
    # ...accelerating around a HIGH impact release: every 60s from this many
    # minutes before, until this many minutes after.
    fj_calendar_pre_release_minutes: int = 15
    fj_calendar_post_release_minutes: int = 30
    fj_calendar_burst_seconds: int = 60

    # Cross-provider merge tuning.
    calendar_merge_similarity: float = 0.85
    calendar_merge_time_tolerance_minutes: int = 5

    # Phase 8 cache: serve the last good payload when the provider fails.
# Shared Chromium render slot. Measured concurrent news + calendar renders
    # cost +12 processes and ~1.0 GB RSS at peak, so the two serialise. A
    # waiter that cannot get the slot within this many seconds skips its render
    # and leaves it to the next poll, rather than stalling the pipeline.
    chromium_lock_wait_seconds: float = 60.0

    # Phase 5: per-country weight on a calendar signal. A Japanese print moves
    # Japanese assets; it does not move gold as much as a US one. Values are
    # multipliers in 0..1 applied to relevance, keyed by country code.
    calendar_country_multipliers_enabled: bool = True
    calendar_country_multipliers: str = (
        "USD=1.0,EUR=0.30,GBP=0.20,JPY=0.15,CNY=0.25,CHF=0.05,CAD=0.05,AUD=0.05,NZD=0.05"
    )
    # Anything not listed above.
    calendar_country_multiplier_default: float = 0.05
    # Asset-specific overrides, keyed "COUNTRY:ASSET". CNY needs this because
    # silver and oil follow China's industrial cycle far more closely than
    # gold or bitcoin do.
    calendar_country_asset_overrides: str = "CNY:XAGUSD=0.8"

    # Phase 8 cache: serve the last good payload when every provider fails.
    calendar_cache_enabled: bool = True
    calendar_cache_max_age_days: int = 7

    # How far back the PERSISTED calendar history reaches, in hours. The
    # provider only ever exposes TODAY, so without this the time decay of
    # yesterday's releases would be lost on every restart. The history holds
    # the RAW feed; the region whitelist and the forced impact are re-applied
    # to it on every poll, so a config edit still takes effect immediately.
    calendar_history_hours: int = 48

    # Phase 8: conditional requests where a provider supports them. FJ is a
    # browser render and cannot send ETag.
    calendar_conditional_requests: bool = True
    # Serve a stale cache when every provider fails. Never for older than
    # calendar_cache_max_age_days.
    calendar_serve_stale_on_failure: bool = True

    # Phase 6: a scheduled time that has passed with no actual yet. After the
    # grace period the EXPECTED signal is damped, and after the timeout it is
    # dropped entirely - a stale expectation is not evidence.
    calendar_awaiting_release_grace_minutes: int = 10
    calendar_awaiting_release_timeout_minutes: int = 120
    calendar_awaiting_release_penalty: float = 0.35
    calendar_awaiting_release_enabled: bool = True

    # Phase 6: several prints share one timestamp (NFP + unemployment + wages).
    # They are combined into one composite release so a single macro moment is
    # not counted three times.
    calendar_composite_cluster_enabled: bool = True
    calendar_composite_window_minutes: int = 15
    calendar_composite_max_weight: float = 1.30

    # How the composite cap is applied:
    #   "per_asset" - cap each asset independently, so one asset hitting the
    #                 limit never drags the others down. A single CPI release
    #                 can then carry the full 1.30 for gold.
    #   "global"    - old behaviour: scale every signal in the cluster by one
    #                 shared factor, so four assets share the 1.30 between
    #                 them and each ends up near 0.33.
    calendar_composite_cap_mode: str = "per_asset"

    # Phase 3: per-indicator polarity. An unemployment rate above forecast is a
    # WEAKER economy, the opposite of payrolls above forecast.
    calendar_polarity_enabled: bool = True
    # "title-keyword=+1|-1" where -1 means a higher-than-forecast print is COLD.
    calendar_inverse_indicators: str = (
        "unemployment rate=-1,jobless claim=-1,initial claim=-1,"
        "initial jobless claim=-1,continuing claim=-1,"
        "consumer confidence=1,business confidence=1"
    )
    # Core measures are the better read of the same release.
    calendar_core_bonus: float = 0.20

    # PHASE 7: DOES A SCHEDULED PRINT MOVE A CARD BEFORE IT HAPPENS?
    #
    #   false (default) - an UPCOMING print is a RISK, not evidence. It is
    #     still listed in the economic-calendar panel and it still raises the
    #     EVENT RISK note ("this bias may be invalidated by the print"), but
    #     it never adds bull/bear weight to a card. The card moves only when
    #     the real number is published and the surprise is known.
    #   true - the previous behaviour: a consensus expectation contributes at
    #     reduced persistence (0.35 instead of 0.75) and is damped/dropped
    #     once it goes stale. It also means an UNPUBLISHED print scores on
    #     the CONSENSUS direction, which for a growth survey can invert the
    #     sign of the read.
    calendar_upcoming_contributes: bool = False
    # When a release publishes both m/m and y/y, keep one row per asset.
    calendar_dedupe_mom_yoy: bool = True

    # Earnings mega-cap / unnamed multipliers (Item 6).
    # Applied in _signal_weight() when event type is an earnings type.
    # Mega-cap names get 2x weight; unnamed/small-cap get 0.3x.
    earnings_mega_cap_names: str = (
        "apple,microsoft,nvidia,amazon,alphabet,google,meta,tesla,"
        "broadcom,jpmorgan,berkshire,eli lilly,visa,walmart,exxon"
    )
    earnings_mega_cap_mult: float = 2.0
    earnings_unnamed_mult: float = 0.3

    # ---------------- overnight catch-up ("morning brief") ----------------
    # The engine only runs while the laptop is on. Everything published in the
    # shutdown gap is judged in ONE catch-up pass on the next boot: every
    # article in the resolved off-window is sent through the LLM for a HIGH /
    # MEDIUM / LOW impact verdict and the stored classification is refreshed
    # (see app/services/overnight.py).
    #
    # MORNING_BRIEF_ON_BOOT              - run the pass automatically when the
    #                                      app starts (also available as
    #                                      `python run.py --morning-brief`,
    #                                      which the Windows logon task calls).
    # MORNING_BRIEF_MIN_GAP_MINUTES      - a gap shorter than this is a plain
    #                                      restart, not an overnight - skip.
    # OVERNIGHT_MAX_GAP_HOURS            - hard ceiling on the window so a
    #                                      stale marker can never turn the
    #                                      pass into an all-day crawl.
    # MORNING_BRIEF_WAIT_IDLE_SECONDS    - how long the final rebuild pass
    #                                      waits for an in-flight tick.
    morning_brief_on_boot: bool = Field(
        default=True, validation_alias="MORNING_BRIEF_ON_BOOT")
    morning_brief_min_gap_minutes: int = Field(
        default=90, validation_alias="MORNING_BRIEF_MIN_GAP_MINUTES")
    overnight_max_gap_hours: int = Field(
        default=24, validation_alias="OVERNIGHT_MAX_GAP_HOURS")
    morning_brief_wait_idle_seconds: int = Field(
        default=300, validation_alias="MORNING_BRIEF_WAIT_IDLE_SECONDS")
    # Short pause before the pass starts, so a web process booting at the same
    # moment can prime (and finish) its first ingest tick first. SQLite runs
    # WAL with a 30s busy timeout, so this is politeness, not a lock.
    morning_brief_startup_grace_seconds: int = Field(
        default=15, validation_alias="MORNING_BRIEF_STARTUP_GRACE_SECONDS")
    # OVERNIGHT COUNTS AS FRESH
    # -------------------------
    # The brief keeps the off-window's headlines, but the market-day boundary
    # (00:00 ET) has usually already rolled over by boot time, so those rows
    # would otherwise never reach that morning's boxes. With this on, the
    # covered off-window is ADMITTED into the day's rows (feed, boxes, alerts)
    # for the rest of that market day, and each kept headline measures its
    # freshness from the window's END - the moment the engine came back and
    # the model actually judged it - instead of from its publication time
    # while the laptop was off. Without this the 8h trading-day cutoff zeroes
    # the whole overnight batch before it can move a score. Set false to fall
    # back to pure publication-time ageing (headlines appear, weigh ~0).
    overnight_counts_as_fresh: bool = Field(
        default=True, validation_alias="OVERNIGHT_COUNTS_AS_FRESH")

    # ---------------- ports ----------------
    dashboard_port: int = 8501
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # ---------------- derived helpers ----------------
    @field_validator("market_timezone", "display_timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {v!r}") from exc
        return v

    def model_post_init(self, __context) -> None:
        """
        Warn when the environment shadows the .env file.

        pydantic-settings gives real OS environment variables priority over
        .env. A stale exported LLM_API_KEY in the shell therefore silently
        overrides the key in .env and every request fails with an opaque 401.
        """
        import os

        for env_name, file_value in (
            ("LLM_API_KEY", self.llm_api_key),
            ("DEEPSEEK_API_KEY", self.llm_api_key),
        ):
            os_value = os.environ.get(env_name)
            if os_value and file_value and os_value != file_value:
                import sys

                print(
                    f"WARNING: environment variable {env_name} is set and does "
                    f"NOT match the value in .env - the environment value wins. "
                    f"If you get 401 errors, run:\n"
                    f"  Remove-Item Env:\\{env_name}\n"
                    f"or unset {env_name} and restart.",
                    file=sys.stderr,
                )
                break

    @property
    def supported_assets(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    @property
    def asset_session_profile_map(self) -> dict[str, str]:
        """
        Per-asset session multiplier profile, parsed from ``asset_session_profiles``.

        Format: ``BTC=flat,XAG=weekend_tolerant``. Unknown values fall back to
        the hardcoded default in ``ASSET_SESSION_PROFILES``.
        """
        out: dict[str, str] = {}
        for chunk in (self.asset_session_profiles or "").split(","):
            if "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            key = key.strip().upper()
            value = value.strip().lower()
            if key and value:
                out[key] = value
        return out

    def parsed_table(self, raw: str) -> dict:
        """
        Parse a ``k=v,k=v`` config string into a lowercase-keyed dict.

        Used for the decay half-life table and the calendar std-dev table so
        both stay editable as one line in ``.env`` instead of being hard-coded
        in the modules that read them.
        """
        out: dict = {}
        for chunk in (raw or "").split(","):
            if "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            key = key.strip().lower()
            try:
                out[key] = float(value.strip())
            except ValueError:
                continue
        return out

    @property
    def half_lives(self) -> dict:
        """Half-life in hours per NewsCategory value."""
        return self.parsed_table(self.decay_half_lives)

    @property
    def surprise_stds(self) -> dict:
        """Historical standard deviation per calendar indicator keyword."""
        return self.parsed_table(self.surprise_std_table)

    @property
    def baseline_statistic_fn(self):
        """The aggregation function used for the rolling baseline (item 1).

        Defaults to the median. ``BASELINE_STATISTIC=mean`` selects the mean,
        which is exposed for comparison but is NOT recommended: BTCUSD's raw
        scores cluster in a deep negative tail, and the mean is dragged far
        enough below them that a clearly bearish score reads as bullish.
        """
        return statistics.mean if self.baseline_statistic.lower() == "mean" \
            else statistics.median

    @property
    def calendar_provider_list(self) -> list[str]:
        """Provider names in PRIORITY order (first wins conflicts)."""
        return [p.strip().lower() for p in self.calendar_providers.split(",")
                if p.strip()]

    @property
    def fj_render_tz(self) -> timezone:
        """Timezone the FJ page renders its clock times in, as a UTC offset.

        Parsed from ``fj_calendar_render_timezone`` ("UTC+05:30"), verified
        against faireeconomy's copy of the same event rather than assumed.
        A malformed value falls back to UTC and logs nothing silently wrong.
        """
        raw = (self.fj_calendar_render_timezone or "UTC").strip().upper()
        if raw in ("UTC", "Z", "+00:00"):
            return timezone.utc
        match = re.match(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", raw)
        if not match:
            return timezone.utc
        sign = 1 if match.group(1) == "+" else -1
        hours = int(match.group(2))
        minutes = int(match.group(3) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=minutes))

    @property
    def country_multipliers(self) -> dict:
        """Per-country signal-weight multipliers (phase 5), keyed by code."""
        return self.parsed_table(self.calendar_country_multipliers)

    @property
    def has_llm(self) -> bool:
        """LLM is usable only if enabled AND a key is present."""
        return bool(self.llm_enabled and self.llm_api_key.strip())

    def resolved_log_file(self) -> Path:
        p = Path(self.log_file)
        return p if p.is_absolute() else BASE_DIR / p

    def resolved_database_url(self) -> str:
        """Make relative sqlite paths resolve against BASE_DIR, not cwd."""
        if self.database_url.startswith("sqlite:///./"):
            tail = self.database_url[len("sqlite:///./"):]
            return f"sqlite:///{(BASE_DIR / tail).as_posix()}"
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# Ensure the directories the app writes to exist at import time.
# Ensure the directories the app writes to exist at import time.
for _d in (BASE_DIR / "data", BASE_DIR / "logs"):
    os.makedirs(_d, exist_ok=True)

# ---------------------------------------------------------------------------
# Task 8: stability (no whipsaw) constants - module-level for backward compat.
# These read from the instantiated settings object so .env values are respected.
# ---------------------------------------------------------------------------
FLIP_MARGIN = settings.flip_margin
FLIP_HOLD_MINUTES = settings.flip_hold_minutes
COOLING_MINUTES = settings.cooling_minutes
COOLING_MARGIN = settings.cooling_margin

# ---------------------------------------------------------------------------
# Source trust tiers (Task 4).
#
# Tier 1 = highest-trust, confirmed/official items. Tier 3 = lowest-trust,
# analyst opinion / forecast / rumor. The tier multiplier multiplies the
# importance/strength/relevance/persistence factors inside
# sentiment_engine._signal_weight exactly once.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Event tiers (Task 4b). Tier comes from the EVENT (importance + category +
# report_kind), never from the outlet. Source quality stays a separate narrow
# trust factor (0.9..1.0).
#
#   TIER_1 = VERY_HIGH importance, ACTUAL_DATA prints for CPI/PPI/PCE/NFP/FOMC
#            / rate decisions, confirmed geopolitical shocks, ETF flow data.
#   TIER_2 = central-bank speeches, secondary data, official statements.
#   TIER_3 = analyst opinion, previews, commentary, forecasts.
# ---------------------------------------------------------------------------
TIER_MULTIPLIERS = {
    "TIER_1": 1.00,
    "TIER_2": 0.45,
    "TIER_3": 0.15,
}

# Task 5a: intraday recency decay half-lives by EVENT TIER (not category).
# Exponential decay: weight = 0.5 ** (age_hours / half_life).
# Tier 1 ~90min, Tier 2 ~45min, Tier 3 ~15min.
# Retuned 2026-10-13: the DAY TRADING values (0.5/0.25/0.1h) faded real news
# out in minutes - a 12:30 UTC NFP print was at 6% weight by the US close,
# even though gold trades that print for hours. Tier 2 speeches from London
# open were dead by the 13:30 US window. Half-lives are now sized to how long
# each event tier actually drives price intraday:
#   TIER_1 (CPI/FOMC/NFP/geopolitical): 1.5h  -> 63% at 2h, 40% at 4h
#   TIER_2 (speeches/secondary data):    1.25h -> 74% at 1h, 55% at 2h
#   TIER_3 (commentary/previews):        1.0h  -> 71% at 30min, 50% at 1h
#
# TIER_2 and TIER_3 were both wrong in the comment above for a while - it
# said 0.75h and 0.25h, which are half the live TIER_2 value and a quarter
# of the live TIER_3 value. Commentary news was therefore living in the
# scoring window four times longer than the comment claimed. The table below
# is the truth; the comment above it now matches.
#
# TRADING_DAY_CUTOFF_HOURS: any item older than this does not get weight 0 -
# it gets the DECAY_FLOOR (0.05), and the accumulated stale mass is capped
# in _aggregate (see settings.stale_mass_cap_ratio).
TIER_HALF_LIVES = {
    "TIER_1": 1.5,
    "TIER_2": 1.25,
    "TIER_3": 1.0,
}

# STEP 7: Explicit half-life override per EventType (optional).
# Allows FOMC decisions, NFP prints, etc. to carry custom half-lives
# independent of their tier. If absent, the tier's half-life is used.
EVENT_HALF_LIFE_OVERRIDE: dict[str, float] = {
    "FOMC_DECISION": 4.0,       # FOMC decision drives price for hours
    "NFP_PRINT": 3.0,           # NFP print trades for hours
    "CPI_PRINT": 3.0,           # CPI print trades for hours
    "PPI_PRINT": 3.0,           # PPI print trades for hours
    "FOMC_MINUTES": 2.0,        # Minutes release has medium persistence
    "ECB_DECISION": 4.0,        # ECB decision
    "BOE_DECISION": 4.0,        # BoE decision
    "GEOPOLITICAL_RISK": 6.0,   # Wars/escalations last days
}

# Per-asset decay multipliers applied ON TOP of the tier half-life
# (half_life *= ASSET_DECAY_MULTIPLIER[asset]). Decay is about TIME, and the
# instruments have genuinely different clocks:
#   GOLD / SILVER: 22h/day with a daily break; the 1.0 baseline matches the
#                  tier table above.
#   BITCOIN:       24/7 - no overnight reset, so a 03:00 UTC headline still
#                  matters at the London open. Longer half-life (+25%).
#   OIL:           crude trades ~23h/day with a daily break, like the metals,
#                  so it keeps the 1.0 baseline.
#   EQUITY:       REMOVED (Step 6 Option A). The equity card handles its own
#                 decay via its own session-aware logic.
ASSET_DECAY_MULTIPLIER = {
    "GOLD": 1.0,
    "SILVER": 1.0,
    "BITCOIN": 1.25,
    "OIL": 1.0,
}

# Hard cap on article age, independent of the window. Reads from the
# instantiated settings so the .env value is respected; exposed at module level
# for backward compatibility with callers importing the constant.
# DAY TRADING: .env ships 8h for live trading; the declared default is 2.0.
TRADING_DAY_CUTOFF_HOURS = settings.trading_day_cutoff_hours

# STEP 7: decay floor - items older than cutoff don't hit zero, they hit
# this floor (0.05 = 5% residual weight). Ensures very old news still
# contributes a tiny signal rather than vanishing entirely.
DECAY_FLOOR = 0.05

# ---------------------------------------------------------------------------
# STEP 8: contradiction dampener (applied in sentiment_engine._score_from).
#
# When the two sides are genuinely close, the news is fighting itself, and
# min/max above CONTRADICTION_RATIO means the difference is mostly which side
# landed the heavier single headline rather than a real lean. The score is
# multiplied by CONTRADICTION_DAMPENER_FACTOR there, so the displayed split
# reads as a lean rather than a verdict.
#
# Unlike `contradiction_detected` (which widens the FLIP margin at bull > 80
# AND bear > 80), this fires on a much smaller imbalance and scales the score
# rather than gating a label change - it is about how strongly to speak, not
# about whether to flip.
# ---------------------------------------------------------------------------
CONTRADICTION_DAMPENER_ENABLED = True
# min(bull, bear) / max(bull, bear) above this is treated as a real fight.
CONTRADICTION_RATIO = 0.6
# Multiplier applied to the score when that ratio is exceeded.
CONTRADICTION_DAMPENER_FACTOR = 0.5

# ---------------------------------------------------------------------------
# CONTRADICTION SPLIT: row-level cluster_conflict gate.
#
# The signal-level flag means one event cluster collapsed opposing readings.
# The row-level flag must not fire merely because one low-weight conflicted
# cluster exists somewhere in a 3000-signal tick. These constants gate it by
# the conflicted signals' share of the same weight the score aggregates.
# ---------------------------------------------------------------------------
# Fraction of total signal weight carried by cluster_conflict=True signals.
CLUSTER_CONFLICT_WEIGHT_SHARE = 0.05
# Absolute total-weight floor; below this the tick is negligible evidence.
CLUSTER_CONFLICT_MIN_TOTAL_WEIGHT = 0.5

# ---------------------------------------------------------------------------
# STEP 10: confidence calibration.
#
# The confidence blend in sentiment_engine._confidence_from uses fixed
# hand-picked coefficients that were never checked against real outcomes. This
# table remaps its output onto the hit rates observed in tools/calibration_report.py,
# so a reported 0.30 means "right about 30% of the time".
#
# UNFITTED ON PURPOSE: bias_outcome is empty (no calibration label has ever been
# recorded), so any mapping placed here would be an invented formula rather than
# evidence. Until a fit exists the applier is an identity and the flag stays off.
# Run  python tools/calibration_report.py  and paste its printed block to enable.
#
# Points are (raw_confidence, calibrated_confidence), ascending in raw.
# ---------------------------------------------------------------------------
CONFIDENCE_CALIBRATION_ENABLED = False
CONFIDENCE_CALIBRATION: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (1.0, 1.0),
)
# A bucket with fewer than this many labels is reported but not fitted: a hit
# rate over 4 samples is noise.
CONFIDENCE_CALIBRATION_MIN_SAMPLES = 30
# Bucket edges used by the fitter, in ascending order.
CONFIDENCE_CALIBRATION_BUCKETS = (0.0, 0.1, 0.2, 0.3, 0.45, 0.6, 0.8, 1.01)

# Task 3: one multiplier for rumors/unconfirmed items ("reportedly",
# "sources say", "may", "could", "might", opinion pieces).
RUMOR_MULTIPLIER = 0.5

# Task 3: slight boost for confirmed/official items.
CONFIRMED_BOOST = 1.05

# ---------------------------------------------------------------------------
# Per-asset driver rankings (Task 6).
# Each asset has its own ranked list of event types (drivers) by importance.
# The driver rank weight scales the transmission magnitude: top driver = 1.0,
# second = 0.9, third = 0.8, etc. This is applied ONCE in the weight path.
# Event types not listed for an asset get a minimal weight (0.3).
# ---------------------------------------------------------------------------
DRIVER_RANKINGS: dict[str, dict[str, float]] = {
    # GOLD: Fed path / real yields / USD first, then geopolitical risk-off (bullish),
    # then ETF and central-bank buying.
    "GOLD": {
        "HAWKISH_POLICY": 1.00,
        "DOVISH_POLICY": 1.00,
        "RECESSION_CUT": 0.95,
        "RISING_YIELDS": 0.95,
        "FALLING_YIELDS": 0.95,
        "STRONG_USD": 0.90,
        "WEAK_USD": 0.90,
        "HOT_INFLATION": 0.90,
        "COOL_INFLATION": 0.85,
        "STRONG_JOBS": 0.85,
        "WEAK_JOBS": 0.80,
        "GEOPOLITICAL_RISK": 1.00,   # risk-off = bullish gold
        "GEOPOLITICAL_DEESCALATION": 1.00,   # de-escalation = bearish gold
        "BANKING_STRESS": 0.95,      # safe-haven demand
        "RISK_OFF": 0.90,            # flight to quality
        "RISK_ON": 0.75,             # risk-on hurts gold (weaker than RISK_OFF)
        "GOLD_ETF_FLOW": 0.95,
        "BTC_ETF_FLOW": 0.05,
        "CRYPTO_REGULATION": 0.05,
        "CRYPTO_LIQUIDATION": 0.10,
        "CRYPTO_HACK": 0.05,
        "CHINA_DEMAND": 0.40,
        "INDUSTRIAL_DEMAND": 0.20,
        "COMMODITY_SUPPLY": 0.60,
        "OIL_SUPPLY_TIGHTNESS": 0.15,
        "OIL_SUPPLY_GLUT": 0.10,
        "GENERAL_MACRO": 0.30,
    },
    # SILVER: gold's drivers plus industrial demand and China data.
    "SILVER": {
        "HAWKISH_POLICY": 0.95,
        "DOVISH_POLICY": 0.95,
        "RECESSION_CUT": 0.85,
        "RISING_YIELDS": 0.90,
        "FALLING_YIELDS": 0.90,
        "STRONG_USD": 0.85,
        "WEAK_USD": 0.85,
        "HOT_INFLATION": 0.85,
        "COOL_INFLATION": 0.80,
        "STRONG_JOBS": 0.80,
        "WEAK_JOBS": 0.75,
        "GEOPOLITICAL_RISK": 0.70,   # weaker safe-haven (part industrial)
        "GEOPOLITICAL_DEESCALATION": 0.70,   # de-escalation = bearish silver
        "BANKING_STRESS": 0.60,      # weaker safe-haven
        "RISK_OFF": 0.55,
        "RISK_ON": 0.55,
        "GOLD_ETF_FLOW": 0.20,       # minor read-through
        "BTC_ETF_FLOW": 0.10,
        "CRYPTO_REGULATION": 0.05,
        "CRYPTO_LIQUIDATION": 0.10,
        "CRYPTO_HACK": 0.05,
        "CHINA_DEMAND": 1.00,        # top driver for silver (industrial)
        "INDUSTRIAL_DEMAND": 0.95,   # top driver for silver
        # ---- Item 4: silver's OWN industrial and China-demand mechanisms. ----
        # SILVER_INDUSTRIAL_DEMAND is the XAGUSD-specific supply-chain mechanism
        # (solar / photovoltaic / EV / electronics / grid), ranked as a top
        # driver because silver is both a precious and an industrial metal and
        # the industrial half was previously under-served. SILVER_CHINA_DEMAND
        # is the China PMI / factory-gate / offtake mechanism, also a top
        # driver: China is the world's largest physical silver buyer.
        "SILVER_INDUSTRIAL_DEMAND": 1.00,
        "SILVER_CHINA_DEMAND": 1.00,
        # ---- Item 4: gold-silver ratio. ----
        # A historically HIGH ratio is bullish silver (money rotates into the
        # cheaper metal); a LOW ratio is bearish silver. Both are top drivers
        # because the ratio is one of the few silver-specific price signals.
        "GOLD_SILVER_RATIO_HIGH": 1.00,
        "GOLD_SILVER_RATIO_LOW": 1.00,
        "COMMODITY_SUPPLY": 0.75,
        "OIL_SUPPLY_TIGHTNESS": 0.15,
        "OIL_SUPPLY_GLUT": 0.10,
        "GENERAL_MACRO": 0.30,
    },
    # BITCOIN: ETF flows, liquidity / rate path, regulation, risk sentiment.
    "BITCOIN": {
        "HAWKISH_POLICY": 0.85,
        "DOVISH_POLICY": 0.85,
        "RECESSION_CUT": 0.80,
        "RISING_YIELDS": 0.90,
        "FALLING_YIELDS": 0.85,
        "STRONG_USD": 0.80,
        "WEAK_USD": 0.75,
        "HOT_INFLATION": 0.90,
        "COOL_INFLATION": 0.80,
        "STRONG_JOBS": 0.85,
        "WEAK_JOBS": 0.80,
        "GEOPOLITICAL_RISK": 0.35,   # risk-off = bearish btc
        "GEOPOLITICAL_DEESCALATION": 0.35,   # de-escalation = bullish btc
        "BANKING_STRESS": 0.90,      # liquidity stress
        "RISK_ON": 1.00,             # top driver: risk-on = bullish btc
        "RISK_OFF": 1.00,            # top driver: risk-off = bearish btc
        "GOLD_ETF_FLOW": 0.05,
        "BTC_ETF_FLOW": 1.00,        # top driver: ETF flows
        "CRYPTO_REGULATION": 1.00,   # top driver: regulation
        "CRYPTO_LIQUIDATION": 1.00,  # top driver: liquidations
        "CRYPTO_HACK": 1.00,  # top driver: exchange hacks
        # Item 3: the regulation category split into two mechanisms. A clamp
        # (ban/enforcement/litigation) is a top bearish driver; clarity
        # (approval/framework/ETF approval) is a top bullish driver. Both rank
        # 1.0 because they are the most market-moving crypto-specific events.
        "REGULATION_CLAMP": 1.00,
        "REGULATION_CLARITY": 1.00,
        # Item 3: QE/QT/balance-sheet/TGA moves reach BTC through the liquidity
        # channel, so they rank as top drivers rather than background macro
        # inputs. Easing and tightening are opposite directions, so they are
        # separate event types, each ranking 1.0.
        "LIQUIDITY_EASING": 1.00,
        "LIQUIDITY_TIGHTENING": 1.00,
        # BTC-specific on-chain / derivatives mechanisms (intraday)
        "BTC_FUNDING_RATE_SPIKE": 1.00,
        "BTC_FUNDING_RATE_RESET": 0.90,
        "BTC_OPEN_INTEREST_SURGE": 0.85,
        "BTC_OPEN_INTEREST_DROP": 0.85,
        "BTC_LONG_LIQUIDATION": 1.00,
        "BTC_SHORT_SQUEEZE": 1.00,
        "BTC_WHALE_ALERT": 0.70,
        "BTC_EXCHANGE_INFLOW": 0.90,
        "BTC_EXCHANGE_OUTFLOW": 0.90,
        "BTC_STABLECOIN_MINT": 0.80,
        "BTC_STABLECOIN_REDEEM": 0.80,
        "CHINA_DEMAND": 0.25,
        "INDUSTRIAL_DEMAND": 0.20,
        "COMMODITY_SUPPLY": 0.10,
        "OIL_SUPPLY_TIGHTNESS": 0.10,
        "OIL_SUPPLY_GLUT": 0.05,
        "GENERAL_MACRO": 0.30,
    },
    # OIL (crude, WTI): its OWN supply mechanisms first - a crude draw, an
    # OPEC+ cut or a Hormuz closure is the single most direct thing that can
    # move the oil price. Then geopolitics (oil carries a supply risk premium,
    # so escalation is BULLISH oil - the opposite of gold's safe-haven read),
    # then the demand/industrial cycle (China is the largest crude importer),
    # then the USD/rate path, which is oil's dominant macro channel because
    # crude is priced in dollars.
    "OIL": {
        # --- oil's own supply mechanisms: the top drivers ---
        "OIL_SUPPLY_TIGHTNESS": 1.00,
        "OIL_SUPPLY_GLUT": 1.00,
        "COMMODITY_SUPPLY": 0.95,
        # --- geopolitics: supply risk premium ---
        "GEOPOLITICAL_RISK": 1.00,       # escalation = bullish oil
        "GEOPOLITICAL_DEESCALATION": 1.00,   # de-escalation = bearish oil
        # --- demand / industrial cycle ---
        "INDUSTRIAL_DEMAND": 0.85,
        "CHINA_DEMAND": 0.80,
        "SILVER_CHINA_DEMAND": 0.70,
        "SILVER_INDUSTRIAL_DEMAND": 0.55,
        # --- USD and the rate path (oil's dominant macro channel) ---
        "STRONG_USD": 0.75,
        "WEAK_USD": 0.75,
        "HAWKISH_POLICY": 0.70,
        "DOVISH_POLICY": 0.70,
        "RECESSION_CUT": 0.65,
        "HOT_INFLATION": 0.60,
        "COOL_INFLATION": 0.55,
        "RISING_YIELDS": 0.50,
        "FALLING_YIELDS": 0.50,
        "STRONG_JOBS": 0.50,
        "WEAK_JOBS": 0.50,
        "RATE_REPRICING": 0.50,
        "REAL_YIELDS_RISING": 0.45,
        "REAL_YIELDS_FALLING": 0.45,
        # --- risk regime (oil is a growth / risk proxy) ---
        "RISK_OFF": 0.70,
        "RISK_ON": 0.65,
        "BANKING_STRESS": 0.65,
        "VIX_SPIKE": 0.55,
        "VIX_COMPRESSION": 0.50,
        "STOCK_RALLY": 0.45,
        "STOCK_PLUNGE": 0.55,
        # --- liquidity ---
        "LIQUIDITY_EASING": 0.45,
        "LIQUIDITY_TIGHTENING": 0.45,
        # --- equities read-through ---
        "EARNINGS_BEAT": 0.25,
        "EARNINGS_MISS": 0.30,
        "GUIDANCE_RAISED": 0.25,
        "GUIDANCE_CUT": 0.30,
        "INDEX_FLOW": 0.25,
        "CORP_EARNINGS_SEASON": 0.20,
        # --- no path to crude ---
        "GOLD_ETF_FLOW": 0.05,
        "BTC_ETF_FLOW": 0.05,
        "CRYPTO_REGULATION": 0.05,
        "CRYPTO_LIQUIDATION": 0.05,
        "CRYPTO_HACK": 0.05,
        "GENERAL_MACRO": 0.30,
    },
}

# ---------------------------------------------------------------------------
# Equity card relevance matrix (Task 7) - DAY TRADING OPTIMIZED.
# The equity card has its OWN relevance table, separate from BITCOIN's slot.
# Relevant categories: EQUITIES, central bank, macro data, geopolitical,
# banking stress, risk sentiment. All scaled 0.0-1.0.
# Day trading: higher relevance for actionable macro prints (CPI, NFP, FOMC,
# yields, USD), lower for noisy/lagging categories (geopolitics, risk sentiment).
# ---------------------------------------------------------------------------
EQUITY_RELEVANCE: dict[str, float] = {
    NewsCategory.EQUITIES: 1.00,
    NewsCategory.CENTRAL_BANK: 0.95,
    NewsCategory.INTEREST_RATES: 0.95,
    NewsCategory.INFLATION: 0.95,          # raised from 0.90 - CPI/PCE critical intraday
    NewsCategory.EMPLOYMENT: 0.95,         # raised from 0.90 - NFP/jobless claims critical
    NewsCategory.GDP: 0.85,
    NewsCategory.TREASURY_YIELDS: 1.00,    # raised from 0.95 - real yields drive intraday
    NewsCategory.USD_DXY: 0.95,            # raised from 0.90
    NewsCategory.GEOPOLITICS: 0.50,        # lowered from 0.70 - less actionable intraday
    NewsCategory.RISK_SENTIMENT: 0.60,     # lowered from 0.80
    NewsCategory.BANKING_FINANCIAL_STRESS: 0.70,  # lowered from 0.85
    NewsCategory.ETF_FLOWS: 0.70,          # raised from 0.55 - intraday flow data actionable
    NewsCategory.COMMODITY_SUPPLY_DEMAND: 0.30,
    NewsCategory.INDUSTRIAL_DEMAND: 0.25,
    NewsCategory.CHINA: 0.40,
    NewsCategory.CRYPTO_REGULATION: 0.10,
    NewsCategory.BITCOIN_ETF_FLOWS: 0.05,
    NewsCategory.CRYPTO_MARKET_FLOWS: 0.05,
    NewsCategory.OIL_ENERGY: 0.35,
    NewsCategory.OTHER: 0.10,
}

