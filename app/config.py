"""
Central configuration.

Everything is environment-driven so the monitored website can be swapped
without touching a single line of application code (see NEWS_SOURCES).
"""

from __future__ import annotations

import os
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

    # ---------------- today-only filter ----------------
    max_article_age_hours: int = 26
    update_window_hours: int = 6

    # ---------------- deduplication ----------------
    dedup_similarity_threshold: float = 0.72
    dedup_time_window_hours: int = 12

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