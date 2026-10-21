"""
STRICT LLM RESPONSE SCHEMA (improvement item 9).

The LLM is the PRIMARY engine. Its output is therefore the one input most able
to corrupt the aggregate, so it is validated rather than trusted:

* every field is typed and range-checked (magnitude 0-1, horizon is an enum)
* an asset entry naming an unknown asset is DROPPED, not coerced into one of
  ours - and it does not invalidate the rest of the response
* a malformed response raises, so the caller can retry once and then fall
  back to the deterministic heuristic engine
* ``assets: []`` is explicitly VALID - it means "this story does not move any
  of the three assets", which is the honest answer for most articles and must
  never be discarded or upgraded into a directional call
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.logging_conf import get_logger
from app.models.enums import Asset, NewsCategory, Verification

log = get_logger(__name__)

Horizon = Literal["intraday", "session", "multi_day"]

# Asset names the model is allowed to use, normalised for lookup.
_VALID_ASSET_NAMES = {a.value.upper(): a.value for a in Asset}


class LLMPricesInSchema(BaseModel):
    """An explicit "this was already priced in" declaration."""

    priced_in: bool = False


class LLMAssetResult(BaseModel):
    """One asset's verdict. Nothing optional except the explanations."""

    asset: Asset
    direction: Literal["BULLISH", "BEARISH", "NEUTRAL", "MIXED"]
    # 0.0-1.0 conviction for THIS asset.
    magnitude: float = Field(ge=0.0, le=1.0)
    horizon: Horizon = "intraday"
    priced_in: bool = False
    reason: str = Field(default="", max_length=400)
    transmission: str = Field(default="", max_length=400)
    metal_character: Literal["PRECIOUS", "INDUSTRIAL", "BOTH"] | None = Field(
        default=None,
    )

    @field_validator("metal_character", mode="before")
    @classmethod
    def _normalise_metal_character(cls, v):
        """Accept the literal string "null" or "none" from the model and coerce
        it to a real None so validation does not raise."""
        if v is None:
            return None
        if isinstance(v, str) and v.strip().lower() in ("null", "none", ""):
            return None
        return v

    @field_validator("magnitude")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v:  # NaN
            raise ValueError("magnitude must be a real number")
        return round(float(v), 4)


class LLMAnalysisSchema(BaseModel):
    """The whole response. ``assets`` may legitimately be empty."""

    category: NewsCategory = NewsCategory.OTHER
    importance: Literal["VERY_HIGH", "HIGH", "MEDIUM", "LOW"] = "LOW"
    verification: Verification = Verification.CONFIRMED
    event_title: str = ""
    event_summary: str = ""
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)
    assets: list[LLMAssetResult] = Field(default_factory=list, max_length=3)

    @field_validator("assets")
    @classmethod
    def _unique_assets(cls, v: list[LLMAssetResult]) -> list[LLMAssetResult]:
        """The same asset must not be scored twice in one response."""
        seen: set[str] = set()
        for entry in v:
            if entry.asset.value in seen:
                raise ValueError(f"duplicate asset entry: {entry.asset.value}")
            seen.add(entry.asset.value)
        return v


def _drop_unknown_assets(data: dict) -> dict:
    """
    Remove asset entries that name something outside our universe.

    The model is told which assets it may use, but it sometimes slips in a
    neighbouring instrument - "USD/DXY", "USOIL", "SPX". REJECTING the whole
    response for that threw away a COMPLETED model call: the category, the
    importance, the relevance score and the event title were all perfectly
    usable, and with a provider this slow (20-40s a call) that is an
    expensive way to learn nothing.

    An unknown asset is not evidence about OUR assets, so the entry is dropped
    rather than coerced into one of them. If the model only spoke about
    assets outside the universe, the response is equivalent to the explicitly
    valid "no material intraday impact" answer.

    Returns the payload unchanged when nothing was dropped.
    """
    assets = data.get("assets")
    if not isinstance(assets, list):
        return data

    kept: list = []
    dropped: list[str] = []
    for entry in assets:
        name = entry.get("asset") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip().upper() in _VALID_ASSET_NAMES:
            kept.append(entry)
        else:
            dropped.append(str(name))

    if not dropped:
        return data

    log.warning(
        "LLM response named %d asset(s) outside our universe (%s) - dropped "
        "those entries, kept the rest of the verdict",
        len(dropped), ", ".join(dropped[:4]))
    return {**data, "assets": kept}


def validate_llm_payload(data) -> LLMAnalysisSchema:
    """
    Validate a decoded LLM response. Raises on anything malformed.

    Kept as a named function so the retry-then-fallback path in
    ``news_classifier.llm_analyze`` reads clearly and can be unit tested
    without a live provider.
    """
    if not isinstance(data, dict):
        raise ValueError(f"LLM response must be an object, got {type(data).__name__}")
    if "assets" not in data:
        raise ValueError(f"LLM response has no 'assets' key. Keys={sorted(data)}")
    return LLMAnalysisSchema.model_validate(_drop_unknown_assets(data))