"""
STRICT LLM SCHEMA + RETRY/FALLBACK (improvement item 9).

The LLM is the primary engine, so its output must be validated rather than
trusted. These tests protect the failure path: a bad response is retried once
and then falls back to heuristics, never propagated into the aggregate.
"""

import pytest

from app.analysis.llm_schema import validate_llm_payload
from app.models.enums import Asset


def good_payload(**overrides):
    payload = {
        "category": "Central Bank",
        "importance": "HIGH",
        "verification": "CONFIRMED",
        "event_title": "Fed holds rates",
        "event_summary": "The Fed held its policy rate.",
        "relevance_score": 0.8,
        "assets": [
            {
                "asset": "XAUUSD",
                "direction": "BEARISH",
                "magnitude": 0.7,
                "horizon": "session",
                "priced_in": False,
                "reason": "Higher real yields raise the opportunity cost.",
                "transmission": "Hawkish -> higher yields -> gold down",
            }
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_valid_payload_parses():
    result = validate_llm_payload(good_payload())
    assert len(result.assets) == 1
    assert result.assets[0].asset is Asset.GOLD
    assert result.assets[0].horizon == "session"


def test_empty_assets_is_a_valid_answer():
    result = validate_llm_payload(good_payload(assets=[]))
    assert result.assets == []


def test_magnitude_is_clamped_to_the_valid_range():
    with pytest.raises(Exception):
        validate_llm_payload(good_payload(assets=[{
            "asset": "XAUUSD", "direction": "BULLISH", "magnitude": 1.7}]))
    with pytest.raises(Exception):
        validate_llm_payload(good_payload(assets=[{
            "asset": "XAUUSD", "direction": "BULLISH", "magnitude": -0.2}]))


# --------------------------------------------------------------------------
# Rejection paths
# --------------------------------------------------------------------------
def test_missing_assets_key_is_rejected():
    payload = good_payload()
    payload.pop("assets")
    with pytest.raises(ValueError):
        validate_llm_payload(payload)


def test_unknown_asset_is_rejected_not_coerced():
    with pytest.raises(Exception):
        validate_llm_payload(good_payload(assets=[{
            "asset": "PETROL", "direction": "BULLISH", "magnitude": 0.5}]))


def test_invalid_direction_is_rejected():
    with pytest.raises(Exception):
        validate_llm_payload(good_payload(assets=[{
            "asset": "XAUUSD", "direction": "VERY BULLISH", "magnitude": 0.5}]))


def test_invalid_horizon_is_rejected():
    with pytest.raises(Exception):
        validate_llm_payload(good_payload(assets=[{
            "asset": "XAUUSD", "direction": "BULLISH", "magnitude": 0.5,
            "horizon": "next decade"}]))


def test_duplicate_asset_entries_are_rejected():
    with pytest.raises(ValueError):
        validate_llm_payload(good_payload(assets=[
            {"asset": "XAUUSD", "direction": "BULLISH", "magnitude": 0.5},
            {"asset": "XAUUSD", "direction": "BEARISH", "magnitude": 0.5},
        ]))


def test_non_object_response_is_rejected():
    with pytest.raises(ValueError):
        validate_llm_payload(["not", "an", "object"])


def test_more_than_three_assets_is_rejected():
    with pytest.raises(Exception):
        validate_llm_payload(good_payload(assets=[
            {"asset": a, "direction": "BULLISH", "magnitude": 0.5}
            for a in ("XAUUSD", "XAGUSD", "BTCUSD", "XAUUSD")]))


def test_relevance_score_out_of_range_is_rejected():
    with pytest.raises(Exception):
        validate_llm_payload(good_payload(relevance_score=1.4))


# --------------------------------------------------------------------------
# Retry-then-fallback behaviour
# --------------------------------------------------------------------------
async def test_analyze_retries_once_then_falls_back(monkeypatch):
    """One bad response -> one retry -> heuristic fallback, never a crash."""
    import asyncio

    from app.analysis import news_classifier
    from app.analysis.llm_client import circuit_breaker

    # Reset circuit breaker before test
    await circuit_breaker().reset()

    calls = {"n": 0}

    async def flaky(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("malformed response")
        return {"ok": True}   # second attempt also bad shape for _parse

    monkeypatch.setattr(news_classifier, "_llm_call", flaky, raising=False)

    analysis, llm_ok, skip_reason = await news_classifier.analyze_article(
        "Fed signals dovish pivot and rate cut")
    assert llm_ok is False
    assert skip_reason == "error"
    assert analysis.mode == "heuristic"


async def test_analyze_never_raises_on_llm_failure(monkeypatch):
    from app.analysis import news_classifier
    from app.analysis.llm_client import circuit_breaker

    # Reset circuit breaker before test
    await circuit_breaker().reset()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(news_classifier, "_llm_call", boom, raising=False)
    analysis, llm_ok, skip_reason = await news_classifier.analyze_article(
        "Gold rallies as haven demand returns")
    assert llm_ok is False
    assert skip_reason == "error"
    assert analysis.mode == "heuristic"
    assert isinstance(analysis.assets, list)