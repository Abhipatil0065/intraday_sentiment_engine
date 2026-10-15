"""
STEP 4 - transmission map defects (4a-4f).

This test suite locks in the fixes for the six transmission-map defects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys
from pathlib import Path as P
sys.path.insert(0, str(P(__file__).resolve().parents[1]))

from app.analysis.cue_rules import detect_category
from app.analysis.transmission_map import (EVENT_SIGNALS, EventType,
                                           detect_event_types, resolve,
                                           transmission)
from app.models.enums import Asset

VALID_MODES = {"llm", "heuristic", "mixed"}


# ---------------------------------------------------------------------------
# 4a - CRYPTO_REGULATION
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("headline,expected_ets,expected_dir", [
    ("SEC approves spot bitcoin ETF trading",
     {"BTC_ETF_FLOW", "REGULATION_CLARITY"}, "BULLISH"),
    ("SEC charges crypto exchange with operating without registration",
     {"CRYPTO_REGULATION"}, "BEARISH"),
])
def test_crypto_regulation_fires_correctly(headline, expected_ets, expected_dir):
    cat = detect_category(headline)
    ets = {e.value for e in detect_event_types(headline, detect_category(headline))}
    r = resolve(headline, detect_category(headline))
    
    assert expected_ets <= {e.value for e in detect_event_types(headline, detect_category(headline))}
    assert r[Asset.BITCOIN].direction.value == expected_dir


def test_crypto_regulation_requires_crypto_context():
    """Bare 'SEC' without crypto context must not fire CRYPTO_REGULATION."""
    cat = detect_category("SEC filing 10-Q quarterly results")
    ets = {e.value for e in detect_event_types("SEC filing 10-Q quarterly results", detect_category("SEC filing 10-Q quarterly results"))}
    assert "CRYPTO_REGULATION" not in ets


def test_crypto_regulation_ordered_after_signed_rules():
    """CRYPTO_REGULATION must be ordered after REGULATION_CLAMP and CLARITY."""
    names = [e.value for e, _, _ in EVENT_SIGNALS]
    i_clamp = [e.value for e, _, _ in EVENT_SIGNALS].index("REGULATION_CLAMP")
    i_clarity = [e.value for e, _, _ in EVENT_SIGNALS].index("REGULATION_CLARITY")
    i_crypto = [e.value for e, _, _ in EVENT_SIGNALS].index("CRYPTO_REGULATION")
    assert i_crypto > i_clamp
    assert i_crypto > i_clarity


def test_crypto_regulation_signed_mapping():
    """CRYPTO_REGULATION must have signed mapping for BTCUSD."""
    from app.analysis.transmission_map import transmission, EventType
    from app.models.enums import Asset
    t = transmission(EventType.CRYPTO_REGULATION, Asset.BITCOIN)
    assert t.sign == -1
    assert t.magnitude > 0


def test_bare_sec_does_not_fire_crypto_regulation():
    """Bare 'SEC' with no crypto context must not fire CRYPTO_REGULATION."""
    cat = detect_category("SEC filing 10-Q quarterly results")
    ets = {e.value for e in detect_event_types("SEC filing 10-Q quarterly results", cat)}
    assert "CRYPTO_REGULATION" not in {e.value for e in detect_event_types("SEC filing 10-Q quarterly results", cat)}


# ---------------------------------------------------------------------------
# 4b - RISING_YIELDS vs REAL_YIELDS_FALLING
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("headline,expected_et", [
    ("real yields fell sharply as inflation expectations cooled", "REAL_YIELDS_FALLING"),
    ("real yields fell sharply", "REAL_YIELDS_FALLING"),
    ("real yields fell", "REAL_YIELDS_FALLING"),
    ("10y TIPS real yield fell 15bp", "REAL_YIELDS_FALLING"),
    ("real yields rose to a fresh high", "REAL_YIELDS_RISING"),
    ("real yields rose", "REAL_YIELDS_RISING"),
    ("10y TIPS real yield rose 15bp", "REAL_YIELDS_RISING"),
])
def test_real_yields_past_tense(headline, expected_et):
    """Past tense real-yield headlines must fire the correct real-yield event."""
    cat = detect_category(headline)
    ets = {e.value for e in detect_event_types(headline, detect_category(headline))}
    assert expected_et in {e.value for e in detect_event_types(headline, cat)}


def test_rising_yields_no_self_contradiction():
    """RISING_YIELDS must not fire on real-yield-fall headlines."""
    cat = detect_category("real yields fell sharply")
    ets = {e.value for e in detect_event_types("real yields fell sharply", cat)}
    # RISING_YIELDS must NOT fire (no "real yields rose" etc)
    assert "RISING_YIELDS" not in ets


def test_falling_yields_still_works():
    """FALLING_YIELDS still fires for generic yield drops."""
    cat = detect_category("yields fell sharply")
    ets = {e.value for e in detect_event_types("yields fell sharply", detect_category("yields fell sharply"))}
    assert "FALLING_YIELDS" in ets


# ---------------------------------------------------------------------------
# 4c - duplicates removed
# ---------------------------------------------------------------------------
def test_no_duplicate_event_types():
    """No event type should appear twice in EVENT_SIGNALS."""
    counts = {}
    for e, _, _ in EVENT_SIGNALS:
        counts[e.value] = counts.get(e.value, 0) + 1
    dupes = {k: v for k, v in counts.items() if v > 1}
    assert not dupes, f"Duplicate event types: {dupes}"


def test_gold_etf_flow_replaced_by_in_out():
    """GOLD_ETF_FLOW must not appear in EVENT_SIGNALS."""
    names = [e.value for e, _, _ in EVENT_SIGNALS]
    assert "GOLD_ETF_FLOW" not in [e.value for e, _, _ in EVENT_SIGNALS]


@pytest.mark.parametrize("headline", [
    "Gold drifts higher above $4,200 on surging ETF inflows",
    "Gold falls as ETF outflows accelerate",
    "Bitcoin shrugs off ETF outflows to trade above $84,000",
])
def test_one_etf_event_type_per_headline(headline):
    """One ETF headline must produce at most one ETF event type."""
    cat = detect_category(headline)
    ets = [e.value for e in detect_event_types(headline, detect_category(headline))]
    etf_events = [e for e in ets if "ETF" in e]
    assert len(etf_events) <= 1, f"Multiple ETF events for {headline}: {etf_events}"


# ---------------------------------------------------------------------------
# 4d - overlapping macro rules
# ---------------------------------------------------------------------------
def test_hawkish_repricing_no_overlap():
    """HAWKISH_POLICY and RATE_REPRICING must not share phrases."""
    names = [e.value for e, _, _ in EVENT_SIGNALS]
    hawkish = set(EVENT_SIGNALS[[e.value for e, _, _ in EVENT_SIGNALS].index("HAWKISH_POLICY")][1])
    repricing = set(EVENT_SIGNALS[[e.value for e, _, _ in EVENT_SIGNALS].index("RATE_REPRICING")][1])
    assert hawkish & repricing == set(), "HAWKISH and RATE_REPRICING must not share phrases"


def test_risk_on_stock_rally_no_overlap():
    """RISK_ON and STOCK_RALLY must not share phrases."""
    names = [e.value for e, _, _ in EVENT_SIGNALS]
    risk_on = set(EVENT_SIGNALS[[e.value for e, _, _ in EVENT_SIGNALS].index("RISK_ON")][1])
    stock_rally = set(EVENT_SIGNALS[[e.value for e, _, _ in EVENT_SIGNALS].index("STOCK_RALLY")][1])
    assert risk_on & stock_rally == set(), "RISK_ON and STOCK_RALLY must not share phrases"


def test_rate_repricing_fires_on_repricing_language():
    """RATE_REPRICING must fire on rate-repricing language."""
    headlines = [
        "Markets price a December rate hike",
        "Fed swaps futures imply two cuts",
        "Odds of a hike have doubled",
        "Rate expectations shifted",
    ]
    for h in headlines:
        cat = detect_category(h)
        ets = {e.value for e in detect_event_types(h, cat)}
        assert "RATE_REPRICING" in {e.value for e in detect_event_types(h, cat)}, f"RATE_REPRICING missing for: {h}"


def test_risk_off_still_fires():
    """RISK_OFF must still fire on safe-haven language."""
    cat = detect_category("safe haven demand surges")
    ets = {e.value for e in detect_event_types("safe haven demand surges", cat)}
    assert "RISK_OFF" in {e.value for e in detect_event_types("safe haven demand surges", cat)}


def test_stock_rally_still_fires():
    """STOCK_RALLY must still fire on equity rally language."""
    cat = detect_category("stocks rally on strong earnings")
    ets = {e.value for e in detect_event_types("stocks rally on strong earnings", cat)}
    assert "STOCK_RALLY" in {e.value for e in detect_event_types("stocks rally on strong earnings", cat)}


# ---------------------------------------------------------------------------
# 4e - missing EQUITY keys
# ---------------------------------------------------------------------------
def test_equity_keys_present():
    """All five missing EQUITY keys must be present in transmission_map.json."""
    data = json.loads((Path(__file__).resolve().parents[1] / "app" / "config" / "transmission_map.json").read_text(encoding="utf-8"))
    for event, want in [
        ("CRYPTO_LIQUIDATION", [0, 0.05]),
        ("COMMODITY_SUPPLY", [0, 0.05]),
        ("OIL_SUPPLY_TIGHTNESS", [-1, 0.30]),
        ("OIL_SUPPLY_GLUT", [1, 0.20]),
        ("GENERAL_MACRO", [0, 0.10]),
    ]:
        assert data[event].get("EQUITY") == want, f"{event}: EQUITY = {data[event].get('EQUITY')}, want {want}"


def test_all_events_map_all_four_assets():
    """Every event must map all four assets (including EQUITY)."""
    data = json.loads((Path(__file__).resolve().parents[1] / "app" / "config" / "transmission_map.json").read_text(encoding="utf-8"))
    for k, v in data.items():
        if k.startswith("_"):
            continue
        assert {"XAUUSD", "XAGUSD", "BTCUSD", "EQUITY"} <= set(v.keys()), f"{k} missing asset keys: {set(v.keys())}"


# ---------------------------------------------------------------------------
# 4f - asset sensitivity
# ---------------------------------------------------------------------------
def test_asset_sensitivity_unknown_is_half():
    """ASSET_SENSITIVITY_UNKNOWN must be 0.5."""
    from app.analysis import sentiment_engine as se
    assert se.ASSET_SENSITIVITY_UNKNOWN == 0.5


def test_flow_sensitivity_exempt_covers_all_flow_types():
    """_FLOW_SENSITIVITY_EXEMPT must cover all ETF/flow event types."""
    from app.analysis import sentiment_engine as se
    required = {
        "BTC_ETF_FLOW", "GOLD_ETF_FLOW",
        "GOLD_ETF_FLOW_IN", "GOLD_ETF_FLOW_OUT",
        "BTC_ETF_FLOW_IN", "BTC_ETF_FLOW_OUT",
        "INDEX_FLOW",
    }
    assert se._FLOW_SENSITIVITY_EXEMPT >= required


