"""
STEP 4F - asset sensitivity runtime behaviour.

These tests verify that ``_signal_weight`` applies the asset-sensitivity rules:

    * no detectable event type        -> ``0.5`` fallback
    * GENERAL_MACRO (sign-0) only     -> ``0.5`` fallback
    * *_ETF_FLOW* / INDEX_FLOW        -> skip the lookup entirely, ``1.0``
    * a normal macro event            -> transmission magnitude clamped to
      ``[0.3, 1.0]``

The signal under test is built from a single fixed title so every factor that
is not the detected event type - relevance, persistence, tier, verification,
session, surprise - is identical for baseline and test. Only
``detect_event_types`` and ``transmission`` are patched, which is exactly the
surface ``_signal_weight`` uses for asset sensitivity.
"""

from __future__ import annotations

import unittest.mock
from datetime import datetime, timezone

from app.analysis.signal_factory import build_signal
from app.analysis.sentiment_engine import _signal_weight
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    ReportKind,
)

# One fixed headline: detect_verification() must be CONFIRMED and its category
# must not be a special-cased one, so it contributes a constant non-sensitivity
# weight to baseline and test alike.
_FIXED_TITLE = "Neutral headline"

# A flow event that skips the transmission lookup and yields sensitivity 1.0.
_FLOW_BASELINE_EVENT = "GOLD_ETF_FLOW_IN"

# Pin the clock used by _signal_weight. Passing None makes each call read the
# wall clock independently, so baseline and test would differ by microseconds in
# the decay factor and break exact ratio assertions on a slow machine.
_NOW = datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc)


def _make_signal(
    *,
    title: str,
    asset: Asset = Asset.GOLD,
    direction: Direction = Direction.BULLISH,
    strength: ImpactStrength = ImpactStrength.HIGH,
    importance: EconomicImportance = EconomicImportance.VERY_HIGH,
    relevance: float = 0.9,
    persistence: float = 0.8,
    published_at=None,
):
    """Build a WeightedSignal with every non-title factor held constant."""
    from app.analysis.cue_rules import detect_category

    if published_at is None:
        published_at = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    return build_signal(
        asset=asset,
        title=title,
        direction=direction,
        strength=strength,
        importance=importance,
        published_at=published_at,
        relevance=relevance,
        persistence=persistence,
        source_quality="TIER_2_MAJOR",
        source_name="",
        category=detect_category(title),
        independent_sources=1,
        report_kind=ReportKind.GENERAL,
        session_multiplier=1.0,
        surprise_weight=1.0,
        metal_character=None,
        speaker_tier=1.0,
        age_reference_utc=None,
    )


def _patch_detect(monkeypatch, event_type_names):
    """Force detect_event_types to return the named event types."""
    from app.analysis.transmission_map import EventType

    members = [EventType(n) for n in event_type_names]
    monkeypatch.setattr(
        "app.analysis.transmission_map.detect_event_types",
        lambda title, category: list(members),
    )


def _flow_baseline_weight(monkeypatch):
    """
    Sensitivity-1.0 baseline: a flow event, computed with the same fixed title
    and the same patched detect_event_types surface.
    """
    _patch_detect(monkeypatch, [_FLOW_BASELINE_EVENT])
    return _signal_weight(_make_signal(title=_FIXED_TITLE), _NOW)


def _test_weight(monkeypatch, event_type_names):
    return _signal_weight(_make_signal(title=_FIXED_TITLE), _NOW)


def test_unknown_event_types_get_half_sensitivity(monkeypatch):
    """No event types detected -> asset_sensitivity = 0.5, not 1.0."""
    from app.analysis.transmission_map import EventType

    baseline_weight = _flow_baseline_weight(monkeypatch)

    # Same helper surface, but no event type is returned.
    _patch_detect(monkeypatch, [])
    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - 0.5) < 1e-9, (
        f"expected asset_sensitivity 0.5 for unknown event types, "
        f"got {weight_ratio}"
    )


def test_btc_etf_flow_skips_lookup_and_returns_one(monkeypatch):
    """BTC_ETF_FLOW must skip the transmission lookup entirely."""
    baseline_weight = _flow_baseline_weight(monkeypatch)

    # Patch the lookup AFTER the baseline (which is flow and never calls it),
    # then assert it is not called for the BTC flow signal.
    _patch_detect(monkeypatch, ["BTC_ETF_FLOW"])
    mock_transmission = unittest.mock.MagicMock()
    monkeypatch.setattr(
        "app.analysis.transmission_map.transmission", mock_transmission
    )

    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    assert mock_transmission.call_count == 0, (
        "transmission() must not be called for BTC_ETF_FLOW"
    )

    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - 1.0) < 1e-9, (
        f"expected asset_sensitivity 1.0 for BTC_ETF_FLOW, got {weight_ratio}"
    )


def test_index_flow_skips_lookup_and_returns_one(monkeypatch):
    """INDEX_FLOW must skip the transmission lookup entirely."""
    baseline_weight = _flow_baseline_weight(monkeypatch)

    _patch_detect(monkeypatch, ["INDEX_FLOW"])
    mock_transmission = unittest.mock.MagicMock()
    monkeypatch.setattr(
        "app.analysis.transmission_map.transmission", mock_transmission
    )

    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    assert mock_transmission.call_count == 0, (
        "transmission() must not be called for INDEX_FLOW"
    )

    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - 1.0) < 1e-9, (
        f"expected asset_sensitivity 1.0 for INDEX_FLOW, got {weight_ratio}"
    )


def test_gold_etf_flow_in_out_skip_lookup_and_return_one(monkeypatch):
    """GOLD_ETF_FLOW_IN and GOLD_ETF_FLOW_OUT must skip the lookup."""
    baseline_weight = _flow_baseline_weight(monkeypatch)

    _patch_detect(monkeypatch, ["GOLD_ETF_FLOW_IN"])
    mock_transmission = unittest.mock.MagicMock()
    monkeypatch.setattr(
        "app.analysis.transmission_map.transmission", mock_transmission
    )
    test_signal_in = _make_signal(title=_FIXED_TITLE)
    test_weight_in = _signal_weight(test_signal_in, _NOW)
    assert mock_transmission.call_count == 0, (
        "transmission() must not be called for GOLD_ETF_FLOW_IN"
    )
    ratio_in = test_weight_in / baseline_weight
    assert abs(ratio_in - 1.0) < 1e-9, (
        f"expected asset_sensitivity 1.0 for GOLD_ETF_FLOW_IN, got {ratio_in}"
    )

    mock_transmission.reset_mock()
    _patch_detect(monkeypatch, ["GOLD_ETF_FLOW_OUT"])
    test_signal_out = _make_signal(title=_FIXED_TITLE)
    test_weight_out = _signal_weight(test_signal_out, _NOW)
    assert mock_transmission.call_count == 0, (
        "transmission() must not be called for GOLD_ETF_FLOW_OUT"
    )
    ratio_out = test_weight_out / baseline_weight
    assert abs(ratio_out - 1.0) < 1e-9, (
        f"expected asset_sensitivity 1.0 for GOLD_ETF_FLOW_OUT, got {ratio_out}"
    )


def test_bare_gold_etf_flow_skip_lookup_and_returns_one(monkeypatch):
    """The duplicate GOLD_ETF_FLOW variant must skip the lookup too."""
    baseline_weight = _flow_baseline_weight(monkeypatch)

    # GOLD_ETF_FLOW was removed from live detection by Step 4c; inject it.
    _patch_detect(monkeypatch, ["GOLD_ETF_FLOW"])
    mock_transmission = unittest.mock.MagicMock()
    monkeypatch.setattr(
        "app.analysis.transmission_map.transmission", mock_transmission
    )

    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    assert mock_transmission.call_count == 0, (
        "transmission() must not be called for GOLD_ETF_FLOW"
    )

    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - 1.0) < 1e-9, (
        f"expected asset_sensitivity 1.0 for GOLD_ETF_FLOW, got {weight_ratio}"
    )


def test_normal_macro_signal_uses_transmission_map(monkeypatch):
    """A normal macro event must read the map and use the clamped magnitude."""
    from app.analysis.transmission_map import transmission as real_transmission

    baseline_weight = _flow_baseline_weight(monkeypatch)

    _patch_detect(monkeypatch, ["STRONG_USD"])

    # Wrap the real lookup so we can prove it ran while still using the map.
    call_count = {"n": 0}

    def counting_transmission(et, asset):
        call_count["n"] += 1
        return real_transmission(et, asset)

    monkeypatch.setattr(
        "app.analysis.transmission_map.transmission", counting_transmission
    )

    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    assert call_count["n"] > 0, "transmission() must be called for a macro event"

    # STRONG_USD on GOLD is sign -1, magnitude 0.95 - clamped to 0.95.
    expected_sensitivity = min(1.0, max(0.3, 0.95))
    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - expected_sensitivity) < 1e-9, (
        f"expected asset_sensitivity {expected_sensitivity} for STRONG_USD, "
        f"got {weight_ratio}"
    )


def test_general_macro_returns_half_sensitivity(monkeypatch):
    """GENERAL_MACRO (sign-zero) only -> asset_sensitivity = 0.5, not 1.0."""
    baseline_weight = _flow_baseline_weight(monkeypatch)

    _patch_detect(monkeypatch, ["GENERAL_MACRO"])
    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - 0.5) < 1e-9, (
        f"expected asset_sensitivity 0.5 for GENERAL_MACRO, got {weight_ratio}"
    )


def test_floor_clamp(monkeypatch):
    """A transmission magnitude below 0.3 is clamped up to 0.3."""
    from app.analysis.transmission_map import Transmission

    baseline_weight = _flow_baseline_weight(monkeypatch)

    _patch_detect(monkeypatch, ["HAWKISH_POLICY"])
    monkeypatch.setattr(
        "app.analysis.transmission_map.transmission",
        lambda et, asset: Transmission(sign=-1, magnitude=0.2),
    )

    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - 0.3) < 1e-9, (
        f"expected floor clamp 0.3 for magnitude 0.2, got {weight_ratio}"
    )


def test_ceiling_clamp(monkeypatch):
    """A transmission magnitude above 1.0 is clamped down to 1.0."""
    from app.analysis.transmission_map import Transmission

    baseline_weight = _flow_baseline_weight(monkeypatch)

    _patch_detect(monkeypatch, ["HAWKISH_POLICY"])
    monkeypatch.setattr(
        "app.analysis.transmission_map.transmission",
        lambda et, asset: Transmission(sign=-1, magnitude=2.0),
    )

    test_signal = _make_signal(title=_FIXED_TITLE)
    test_weight = _signal_weight(test_signal, _NOW)

    weight_ratio = test_weight / baseline_weight
    assert abs(weight_ratio - 1.0) < 1e-9, (
        f"expected ceiling clamp 1.0 for magnitude 2.0, got {weight_ratio}"
    )
