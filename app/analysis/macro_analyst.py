"""
MACRO NEWS ANALYST - standalone LLM-based intraday bias engine.

Implements the SYSTEM PROMPT verbatim: takes a news event plus market context
(prices, DXY, yields, VIX, oil, key levels, upcoming events, previous bias) and
returns a per-asset directional bias with confidence, reaction type, horizon,
reason, invalidation level, plus cross-asset check, risk flags and next events.

Two modes:
  * ANALYZE  - news just hit; classify, measure surprise, transmit to each
               asset, decide bias per asset, flag risks.
  * EVALUATE - was the earlier bias right?  Verdict per asset, error type,
               confidence calibration, lesson; running scorecard when history
               is provided.

This module does NOT modify any existing code. It is a separate analysis path
that uses the existing :mod:`app.analysis.llm_client` to call the model with the
provided system prompt, sends inputs as JSON user messages, and reads JSON
output.  Missing input fields are sent as null.

The module never writes to the database, never changes the pipeline, and never
alters any existing feature.  When the LLM is not configured it raises a clear
RuntimeError so the caller can decide what fallback to use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.analysis import llm_client
from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# SYSTEM PROMPT (verbatim from the macro news analyst specification).
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """ROLE
You are a real-time macro news analyst for intraday traders. You trade-focus on: GOLD (XAU),
SILVER (XAG), BITCOIN (BTC), and GLOBAL EQUITIES (S&P 500, Nasdaq, Nifty 50, Bank Nifty,
Europe, Asia). Your job: when news hits, decide what it means for each asset over the next
15 min to 4 hours and give a directional BIAS for day trading. You give bias and
invalidation, not guaranteed entries. The trader confirms entries with price action.

MODES
You receive a JSON input with "mode": "ANALYZE" or "EVALUATE".

------------------------------------------------------------
MODE 1: ANALYZE (news just hit)
Inputs may include: headline, source, timestamp, actual/forecast/previous values (if a data
release), current prices and % change since news for each asset, DXY, US10Y, US real yield,
VIX, oil, key levels, upcoming events in next 24h, and the previous bias you gave.

STEP 1 - CLASSIFY the news
- Type: scheduled data (CPI, NFP, PMI, GDP) / central bank (Fed, ECB, RBI, BoJ) / geopolitical /
  earnings / regulation or crypto-specific / commodity supply / other
- Impact: HIGH / MEDIUM / LOW for each asset.
- Is it new information, or a repeat of what the market already knows?

STEP 2 - MEASURE THE SURPRISE
- Compare actual vs forecast (and vs previous). Surprise matters, not the headline.
- If the data matched expectations, say the surprise is ~zero and expect a muted or
  "sell the news" reaction.
- Judge whether the news is already priced in using the price move since the headline.

STEP 3 - TRANSMISSION to each asset
- GOLD: up on lower real yields, weaker USD, geopolitical stress, dovish central banks, safe-haven
  demand. Down on higher real yields, stronger USD, hawkish surprises, de-escalation.
- SILVER: follows gold direction with higher beta, plus industrial demand (China/global PMI,
  growth). Usually moves more than gold in both directions.
- BITCOIN: up on liquidity/easing, risk-on, positive regulation, ETF inflows, weak USD. Down on
  hawkish surprises, risk-off, regulatory crackdowns, large liquidations/hacks. High
  sentiment sensitivity, 24/7 trading.
- GLOBAL EQUITIES: up on growth-positive + rate-neutral/dovish news, strong earnings. Down
  on rate shocks, recession fear, geopolitical escalation. Note the "bad news is good news"
  regime (weak data = rate cuts = rally) vs the "growth scare" regime, and state which
  regime is currently active.
- Always check cross-asset confirmation: DXY, yields, VIX, oil. If cross-asset moves
  disagree with your thesis, lower your confidence.

STEP 4 - DECIDE BIAS per asset
- BIAS: BULLISH / BEARISH / NEUTRAL / NO-TRADE
- CONFIDENCE: 0-100 (be honest; 50-60 is normal, above 75 needs clear surprise + confirmation)
- Time horizon: how long the bias is valid (e.g., 30 min, 2 hours, session)
- Reaction type: CONTINUATION / FADE (sell the news / buy the rumor) / CHOP
- Invalidation: a specific condition that proves you wrong (price level, yield move, or
  follow-up headline). If no clear invalidation exists, set NO-TRADE.

STEP 5 - RISK FLAGS
- Upcoming high-impact events within 2 hours -> recommend caution/reduce size.
- Thin liquidity (Asia late session, holidays) -> warn of spikes and fakeouts.
- Conflicting headlines or unconfirmed/rumor source -> lower confidence, do not chase.
- First 1-5 minutes after high-impact news are usually noise; flag if the move is a spike
  and advise waiting for the retest before acting.

HARD RULES
- Never invent data, prices, or events. If an input is missing, say "UNKNOWN" and lower
  confidence.
- Never claim certainty. Never promise profit. This is analysis, not financial advice.
- Do not give position size or exact entry/stop prices unless levels were provided; give
  conditions instead (e.g., "bullish while holding above X / if US10Y falls below Y").
- Prefer NO-TRADE over a forced opinion when the signal is unclear or already priced in.
- Keep reasoning short and factual.

OUTPUT (strict JSON only, no extra text):
{
  "mode": "ANALYZE",
  "timestamp": "",
  "news_summary": "",
  "news_type": "",
  "surprise": {"actual": null, "forecast": null, "direction": "HAWKISH|DOVISH|RISK_ON|RISK_OFF|NEUTRAL", "size": "LARGE|MEDIUM|SMALL|NONE"},
  "already_priced_in": "YES|PARTLY|NO|UNKNOWN",
  "market_regime": "",
  "assets": {
    "GOLD":   {"bias": "", "confidence": 0, "reaction_type": "", "horizon": "", "reason": "", "invalidation": ""},
    "SILVER": {"bias": "", "confidence": 0, "reaction_type": "", "horizon": "", "reason": "", "invalidation": ""},
    "BITCOIN":{"bias": "", "confidence": 0, "reaction_type": "", "horizon": "", "reason": "", "invalidation": ""},
    "EQUITIES":{"bias": "", "confidence": 0, "reaction_type": "", "horizon": "", "reason": "", "invalidation": ""}
  },
  "cross_asset_check": "CONFIRMS|CONFLICTS|MIXED",
  "risk_flags": [],
  "next_events_to_watch": [],
  "overall_summary": ""
}

------------------------------------------------------------
MODE 2: EVALUATE (was the earlier bias right?)
Inputs: your previous ANALYZE output, the price at the time of the bias, and prices at
+15 min, +60 min, +240 min (and high/low during each window).

For each asset:
- Verdict: CORRECT / WRONG / PARTLY / INCONCLUSIVE (move too small = INCONCLUSIVE)
- Did price hit the invalidation level before the target direction? 
- Error type if wrong: MISREAD_SURPRISE / ALREADY_PRICED_IN / IGNORED_CROSS_ASSET /
  SELL_THE_NEWS / NEW_HEADLINE_CHANGED_VIEW / THIN_LIQUIDITY / OTHER
- Was the confidence appropriate? (high confidence + wrong = overconfidence)
- One concrete lesson to apply next time.

Also report a running scorecard if history is provided: hit rate per asset, per news type,
and average confidence when right vs wrong. Do not change rules automatically; only suggest.

OUTPUT (strict JSON only):
{
  "mode": "EVALUATE",
  "evaluations": {
    "GOLD":   {"verdict": "", "hit_invalidation_first": false, "error_type": null, "confidence_calibration": "", "lesson": ""},
    "SILVER": {...}, "BITCOIN": {...}, "EQUITIES": {...}
  },
  "scorecard": {"overall_hit_rate": null, "by_asset": {}, "by_news_type": {}},
  "suggested_improvements": []
}
"""


# ---------------------------------------------------------------------------
# Input / output helpers
# ---------------------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _null_if_missing(value: Any) -> Any:
    """Return the value as-is, or None when it is absent / empty string."""
    if value is None or value == "":
        return None
    return value


def _get(event: dict[str, Any], *names: str) -> Any:
    """Return the first non-missing value for any of the given field names."""
    for name in names:
        if name in event and event[name] is not None and event[name] != "":
            return event[name]
    return None


def _normalize_prices(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Normalize per-asset prices into a flat dict keyed by asset.

    Accepts either:
      * ``current_prices`` / ``prices`` as a dict of asset -> {price, pct_change_since_news}
      * A flat dict with asset keys directly
    Returns None when no price data is present.
    """
    raw = _get(event, "current_prices", "prices")
    if raw is None or not isinstance(raw, dict):
        return None

    out: dict[str, Any] = {}
    for asset_key in ("GOLD", "SILVER", "BITCOIN", "EQUITIES"):
        entry = raw.get(asset_key)
        if isinstance(entry, dict):
            out[asset_key] = {
                "price": entry.get("price"),
                "pct_change_since_news": entry.get("pct_change_since_news"),
            }
        elif isinstance(entry, (int, float)):
            out[asset_key] = {"price": entry, "pct_change_since_news": None}
    return out or None


def _build_analyze_user_message(event: dict[str, Any]) -> str:
    """
    Build the JSON user message for ANALYZE mode from the raw input event.

    Every field the system prompt lists is included; missing fields are sent
    as null (per the instruction "If an input field is unavailable, send null").
    Field names are matched case-insensitively and accept common aliases
    (e.g. ``DXY`` / ``dxy``, ``prices`` / ``current_prices``).
    """
    payload: dict[str, Any] = {
        "mode": "ANALYZE",
        "headline": _get(event, "headline"),
        "source": _get(event, "source"),
        "timestamp": _get(event, "timestamp"),
        "actual": _get(event, "actual"),
        "forecast": _get(event, "forecast"),
        "previous": _get(event, "previous"),
        "current_prices": _normalize_prices(event),
        "dxy": _get(event, "dxy", "DXY"),
        "us10y": _get(event, "us10y", "US10Y"),
        "us_real_yield": _get(event, "us_real_yield", "US_real_yield"),
        "vix": _get(event, "vix", "VIX"),
        "oil": _get(event, "oil", "OIL"),
        "key_levels": _get(event, "key_levels"),
        "upcoming_events": _get(event, "upcoming_events"),
        "previous_bias": _get(event, "previous_bias"),
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_evaluate_user_message(
    previous: dict[str, Any],
    prices: dict[str, Any],
) -> str:
    """
    Build the JSON user message for EVALUATE mode.

    ``previous`` is the earlier ANALYZE output; ``prices`` carries the price at
    the time of the bias and at +15 / +60 / +240 min (plus high/low per window).
    """
    payload: dict[str, Any] = {
        "mode": "EVALUATE",
        "previous_analysis": _null_if_missing(previous),
        "price_at_bias": _null_if_missing(prices.get("price_at_bias")),
        "prices": {
            "+15min": _null_if_missing(prices.get("plus_15min")),
            "+60min": _null_if_missing(prices.get("plus_60min")),
            "+240min": _null_if_missing(prices.get("plus_240min")),
        },
        "history": _null_if_missing(prices.get("history")),
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def analyze(event: dict[str, Any]) -> dict[str, Any]:
    """
    ANALYZE mode: classify a news event and produce a per-asset intraday bias.

    Parameters
    ----------
    event : dict
        Any subset of: headline, source, timestamp, actual, forecast, previous,
        current_prices, pct_change_since_news, dxy, us10y, us_real_yield, vix,
        oil, key_levels, upcoming_events, previous_bias.  Missing fields become
        null in the user message.

    Returns
    -------
    dict
        The parsed JSON output from the model, matching the system prompt's
        ANALYZE schema exactly.

    Raises
    ------
    RuntimeError
        If the LLM is not configured or the call fails after retries.
    """
    if not settings.has_llm:
        raise RuntimeError(
            "LLM not configured - set LLM_API_KEY and LLM_BASE_URL in .env "
            "to use the macro news analyst."
        )

    user_message = _build_analyze_user_message(event)
    log.debug(
        "macro_analyst ANALYZE: headline=%r source=%r",
        event.get("headline"), event.get("source"),
    )

    try:
        result = await llm_client.chat_json(
            SYSTEM_PROMPT,
            user_message,
            max_tokens=settings.llm_max_tokens,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clear error
        log.warning("macro_analyst ANALYZE LLM call failed: %s", exc)
        raise RuntimeError(f"macro analyst analyze failed: {exc}") from exc

    if result.get("mode") != "ANALYZE":
        log.warning(
            "macro_analyst ANALYZE: model returned mode=%r, expected ANALYZE",
            result.get("mode"),
        )
    return result


async def evaluate(
    previous_analysis: dict[str, Any],
    prices: dict[str, Any],
) -> dict[str, Any]:
    """
    EVALUATE mode: score an earlier bias against realised price action.

    Parameters
    ----------
    previous_analysis : dict
        The earlier ANALYZE output (including bias, confidence, invalidation
        per asset).
    prices : dict
        Price at the time of the bias ("price_at_bias") and at +15 / +60 /
        +240 min ("plus_15min", "plus_60min", "plus_240min"), each optionally
        carrying high/low.  Optional "history" for the running scorecard.

    Returns
    -------
    dict
        The parsed JSON output from the model, matching the system prompt's
        EVALUATE schema exactly.

    Raises
    ------
    RuntimeError
        If the LLM is not configured or the call fails after retries.
    """
    if not settings.has_llm:
        raise RuntimeError(
            "LLM not configured - set LLM_API_KEY and LLM_BASE_URL in .env "
            "to use the macro news analyst."
        )

    user_message = _build_evaluate_user_message(previous_analysis, prices)
    log.debug(
        "macro_analyst EVALUATE: previous_mode=%r",
        previous_analysis.get("mode") if isinstance(previous_analysis, dict) else None,
    )

    try:
        result = await llm_client.chat_json(
            SYSTEM_PROMPT,
            user_message,
            max_tokens=settings.llm_max_tokens,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - surface as a clear error
        log.warning("macro_analyst EVALUATE LLM call failed: %s", exc)
        raise RuntimeError(f"macro analyst evaluate failed: {exc}") from exc

    if result.get("mode") != "EVALUATE":
        log.warning(
            "macro_analyst EVALUATE: model returned mode=%r, expected EVALUATE",
            result.get("mode"),
        )
    return result


def system_prompt() -> str:
    """Return the system prompt string (useful for testing / introspection)."""
    return SYSTEM_PROMPT
