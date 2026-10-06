"""
Central configuration.

Everything is environment-driven so the monitored website can be swapped
without touching a single line of application code (see NEWS_SOURCES).
"""

from __future__ import annotations

import os
import re
from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
        default="deepseek-chat",
        validation_alias=AliasChoices("LLM_MODEL", "DEEPSEEK_MODEL"),
    )
    llm_timeout_seconds: int = Field(
        default=60, validation_alias=AliasChoices(
            "LLM_TIMEOUT_SECONDS", "DEEPSEEK_TIMEOUT_SECONDS")
    )
    llm_max_concurrent: int = Field(
        default=4, validation_alias=AliasChoices(
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
        default=1800, validation_alias=AliasChoices(
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
    # capped boost, so five outlets covering one FOMC decision cannot cast
    # five votes. Same-source repeats never raise the boost.
    corroboration_per_source: float = 0.10
    corroboration_max_boost: float = 0.30
    # Phase 1: when false, every signal keeps corroboration=1.0 exactly as it
    # did before the wiring fix, so the change can be switched off in one place.
    corroboration_enabled: bool = True

    # ---------------- rolling news window (item 3) ----------------
    # Replaces the "same market-timezone calendar date" gate. A rolling window
    # admits the overnight US wrap by construction, which is why the old
    # FinancialJuice / IST date carve-out is no longer needed.
    news_window_hours: float = 18.0
    # Age within which a signal counts toward the "changed in last 60 min" delta.
    delta_window_minutes: int = 60
    # Hard cap on article age, independent of the window (0 = disabled).
    max_article_age_hours: int = 26
    update_window_hours: int = 6

    # ---------------- bias / NO_BIAS thresholds (item 2) ----------------
    # Score is -100..+100. Below either floor the engine says NO_BIAS rather
    # than inventing a direction from noise.
    bias_min_abs_score: float = 12.0
    bias_min_confidence: float = 0.30
    bias_score_scale: float = 100.0

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
    price_data_enabled: bool = False
    price_api_base_url: str = "https://api.coinbase.com/v2"

    # ---------------- alerts ----------------
    alert_on_importance: str = "VERY_HIGH"
    alert_on_strength: str = "VERY_HIGH"

    # ---------------- sources ----------------
    # "Name|type|url|weight|quality_tier" entries separated by commas/newlines.
    news_sources: str = ""

    # ---------------- assets ----------------
    assets: str = "XAUUSD,XAGUSD,BTCUSD"

    # ---------------- decision log / audit (item 11) ----------------
    # Every classification and every published bias is appended here so the
    # transmission map and decay table can be tuned against real outcomes.
    decision_log_path: str = "logs/decisions.jsonl"

    # ---------------- economic calendar ----------------
    economic_calendar_enabled: bool = True
    economic_calendar_url: str = (
        "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    )
    economic_calendar_nextweek_url: str = ""
    # Only Medium/High/Holiday prints are kept; Low prints are noise.
    economic_calendar_min_importance: str = "MEDIUM"
    # How far ahead an event is allowed to influence PREP (hours).
    economic_calendar_horizon_hours: int = 48

    # ---------------- calendar providers (phase 1/2) ----------------
    # Order is PRIORITY: the first provider holding a value for a field wins
    # and the others only fill gaps. Merging is by country + normalised title +
    # time proximity, so priority decides which value is authoritative.
    calendar_providers: str = "financialjuice,faireconomy"

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

    # Phase 1 follow-up: faireeconomy rate-limits (HTTP 429) under repeated
    # polling. Back off rather than retrying into the limit - FinancialJuice
    # and the cache cover the gap. A Retry-After header wins when present.
    calendar_fe_429_backoff_minutes: int = 15
    # Ceiling applied to a Retry-After header, so a hostile or absurd value
    # cannot park the calendar for a day.
    calendar_fe_429_max_retry_after_minutes: int = 60
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
        "USD=1.0,EUR=0.6,GBP=0.4,JPY=0.4,CNY=0.5,CHF=0.3,CAD=0.3,"
        "AUD=0.3,NZD=0.3"
    )
    # Anything not listed above.
    calendar_country_multiplier_default: float = 0.25
    # Asset-specific overrides, keyed "COUNTRY:ASSET". CNY needs this because
    # silver and oil follow China's industrial cycle far more closely than
    # gold or bitcoin do.
    calendar_country_asset_overrides: str = "CNY:XAGUSD=0.8"

    # Phase 8 cache: serve the last good payload when every provider fails.
    calendar_cache_enabled: bool = True
    calendar_cache_max_age_days: int = 7

    # Phase 8: conditional requests where a provider supports them. FJ is a
    # browser render and cannot send ETag; faireconomy can.
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
    # When a release publishes both m/m and y/y, keep one row per asset.
    calendar_dedupe_mom_yoy: bool = True

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
    def supported_assets(self) -> List[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

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
    def calendar_provider_list(self) -> List[str]:
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
for _d in (BASE_DIR / "data", BASE_DIR / "logs"):
    os.makedirs(_d, exist_ok=True)