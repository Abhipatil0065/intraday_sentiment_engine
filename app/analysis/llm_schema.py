"""
STRICT LLM RESPONSE SCHEMA (improvement item 9).

The LLM is the PRIMARY engine. Its output is therefore the one input most able
to corrupt the aggregate, so it is validated rather than trusted:

* every field is typed and range-checked (magnitude 0-1, horizon is an enum)
* an asset entry naming an unknown asset is REJECTED, not coerced
* a malformed response raises, so the caller can retry once and then fall
  back to the deterministic heuristic engine
* ``assets: []`` is explicitly VALID - it means "this story does not move any
  of the three assets", which is the honest answer for most articles and must
  never be discarded or upgraded into a directional call
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.enums import Asset, NewsCategory, Verification

Horizon = Literal["intraday", "session", "multi_day"]


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
    metal_character: Optional[Literal["PRECIOUS", "INDUSTRIAL", "BOTH"]] = None

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
    return LLMAnalysisSchema.model_validate(data)