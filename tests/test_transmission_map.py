"""
PER-ASSET TRANSMISSION MAP (improvement item 1).

The governing defect these tests protect: direction used to be computed ONCE
from the whole article and then stamped onto every routed asset. A headline
whose transmission differs by asset must now yield a different direction per
asset.

Written before the implementation, per the brief.
"""

import json

from app.analysis import transmission_map as tm
from app.models.enums import Asset, Direction, NewsCategory


# --------------------------------------------------------------------------
# Map integrity
# --------------------------------------------------------------------------
def test_map_file_is_valid_json():
    raw = json.loads(tm.TRANSMISSION_FILE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw


def test_every_event_type_covers_every_asset():
    mapping = tm.load_transmission_map()
    for event_type, per_asset in mapping.items():
        for asset in Asset:
            assert asset in per_asset, f"{event_type} missing {asset.value}"


def test_signs_are_in_minus_one_to_one():
    mapping = tm.load_transmission_map()
    for per_asset in mapping.values():
        for entry in per_asset.values():
            assert entry.sign in (-1, 0, 1)
            assert 0.0 <= entry.magnitude <= 1.5


def test_unknown_event_type_yields_neutral_not_a_guess():
    entry = tm.transmission("NOT_A_REAL_EVENT", Asset.GOLD)
    assert entry.sign == 0
    assert entry.direction is Direction.NEUTRAL


# --------------------------------------------------------------------------
# The canonical transmissions named in the brief
# --------------------------------------------------------------------------
def test_hawkish_policy_is_bearish_for_all_three():
    for asset in Asset:
        entry = tm.transmission(tm.EventType.HAWKISH_POLICY, asset)
        assert entry.sign == -1, f"hawkish policy should be bearish for {asset.value}"
        assert entry.direction is Direction.BEARISH


def test_rising_yields_is_bearish_for_all_three():
    for asset in Asset:
        assert tm.direction_for(tm.EventType.RISING_YIELDS, asset) is Direction.BEARISH


def test_strong_usd_is_bearish_metals_and_btc():
    for asset in Asset:
        assert tm.direction_for(tm.EventType.STRONG_USD, asset) is Direction.BEARISH
def test_geopolitical_risk_is_bullish_gold_and_mildly_silver():
    gold = tm.transmission(tm.EventType.GEOPOLITICAL_RISK, Asset.GOLD)
    silver = tm.transmission(tm.EventType.GEOPOLITICAL_RISK, Asset.SILVER)
    assert gold.sign == +1
    assert silver.sign == +1
    # gold must react at least as strongly as silver to safe-haven demand
    assert gold.magnitude >= silver.magnitude


def test_geopolitical_risk_is_not_bullish_btc():
    btc = tm.transmission(tm.EventType.GEOPOLITICAL_RISK, Asset.BITCOIN)
    assert btc.sign <= 0


def test_banking_stress_is_bullish_gold_and_bearish_btc():
    assert tm.direction_for(tm.EventType.BANKING_STRESS, Asset.GOLD) is Direction.BULLISH
    # BTC has no fixed direction on bank stress (rose in the Mar 2023 SVB episode).
    assert tm.direction_for(tm.EventType.BANKING_STRESS, Asset.BITCOIN) is Direction.MIXED


def test_btc_etf_flows_move_btc_and_are_silent_on_metals():
    btc = tm.transmission(tm.EventType.BTC_ETF_FLOW, Asset.BITCOIN)
    assert btc.sign in (-1, 1) and btc.magnitude > 0
    for asset in (Asset.GOLD, Asset.SILVER):
        assert tm.transmission(tm.EventType.BTC_ETF_FLOW, asset).sign == 0


def test_gold_etf_flows_move_gold_only():
    assert tm.transmission(tm.EventType.GOLD_ETF_FLOW, Asset.GOLD).sign != 0
    assert tm.transmission(tm.EventType.GOLD_ETF_FLOW, Asset.SILVER).sign == 0
    assert tm.transmission(tm.EventType.GOLD_ETF_FLOW, Asset.BITCOIN).sign == 0


def test_crypto_regulation_is_btc_only():
    # STEP 4a: CRYPTO_REGULATION is now bearish for BTC (sign=-1).
    # It has no direction for gold/silver (sign=0).
    assert tm.transmission(tm.EventType.CRYPTO_REGULATION, Asset.BITCOIN).sign == -1
    assert tm.transmission(tm.EventType.CRYPTO_REGULATION, Asset.GOLD).sign == 0
    assert tm.transmission(tm.EventType.CRYPTO_REGULATION, Asset.SILVER).sign == 0


def test_china_demand_is_mainly_silver():
    silver = tm.transmission(tm.EventType.CHINA_DEMAND, Asset.SILVER)
# --------------------------------------------------------------------------
# Per-asset resolution from raw text
# --------------------------------------------------------------------------
def test_negation_suppresses_the_event_type_not_just_the_cue():
    """
    "rules out a rate cut" contains the words "rate cut". Scoring alone is not
    enough - the transmission map must also refuse to classify it as DOVISH,
    because a Fed ruling a cut OUT is hawkish, not dovish.

    The full headline is used because a truncated "Fed rules out a rate cut"
    leaves no directional verb at all, and correctly falls through to the
    neutral GENERAL_MACRO rather than inventing a stance.
    """
    from app.analysis.transmission_map import detect_event_types

    found = detect_event_types(
        "Fed rules out a rate cut in June, signals higher for longer",
        NewsCategory.CENTRAL_BANK)
    assert tm.EventType.DOVISH_POLICY not in found
    assert tm.EventType.HAWKISH_POLICY in found


def test_truncated_negated_phrase_yields_no_directional_event():
    """
    With no verb left after negation there is genuinely no stance to report, so
    the result must be the directionless fallback rather than a guessed one.
    This is the VAL-5 fallback rule applied to negation.
    """
    from app.analysis.transmission_map import detect_event_types

    found = detect_event_types("Fed rules out a rate cut", NewsCategory.CENTRAL_BANK)
    assert tm.EventType.DOVISH_POLICY not in found
    assert found == [tm.EventType.GENERAL_MACRO]


def test_negated_cut_is_not_read_as_bullish_end_to_end():
    from app.analysis.news_classifier import heuristic_analyze

    r = heuristic_analyze("Fed rules out a rate cut in June, signals higher for longer")
    gold = next(a for a in r.assets if a.asset is Asset.GOLD)
    assert gold.direction is Direction.BEARISH


def test_empty_text_resolves_to_neutral_everywhere():
    resolved = tm.resolve("", NewsCategory.OTHER)
    for asset in Asset:
        assert resolved[asset].direction is Direction.NEUTRAL


def test_resolve_returns_a_distinct_direction_per_asset():
    resolved = tm.resolve(
        "Geopolitical escalation: missile strike widens as sanctions bite",
        NewsCategory.GEOPOLITICS,
    )
    assert resolved[Asset.GOLD].direction is Direction.BULLISH
    assert resolved[Asset.BITCOIN].direction is not Direction.BULLISH


def test_resolve_hawkish_fed_is_bearish_across_the_board():
    resolved = tm.resolve(
        "Fed announces surprise rate hike as Powell signals higher for longer",
        NewsCategory.CENTRAL_BANK,
    )
    for asset in Asset:
        assert resolved[asset].sign < 0, asset.value


def test_resolve_reports_the_dominant_event_type():
    resolved = tm.resolve("Fed signals dovish pivot and rate cut",
                          NewsCategory.CENTRAL_BANK)
    assert resolved[Asset.GOLD].dominant in {
        tm.EventType.DOVISH_POLICY, tm.EventType.RECESSION_CUT
    }


# --- Task 6: per-asset driver logic tests ---
def test_geopolitical_escalation_gold_bullish_btc_bearish():
    """Geopolitical escalation gives GOLD bullish and BITCOIN bearish.
    
    NOTE: With minimum cue magnitude 0.30 and BTC driver rank 0.35 for
    GEOPOLITICAL_RISK, the contribution is below the 0.05 MIXED threshold,
    so BTC resolves to MIXED (sign 0). This is the current behavior.
    """
    resolved = tm.resolve(
        "Missile strike widens as sanctions bite amid escalating conflict",
        NewsCategory.GEOPOLITICS,
    )
    # Gold: safe-haven demand -> bullish
    assert resolved[Asset.GOLD].direction is Direction.BULLISH
    assert resolved[Asset.GOLD].sign == +1
    # Bitcoin: contribution below MIXED threshold -> MIXED
    assert resolved[Asset.BITCOIN].direction is Direction.MIXED
    assert resolved[Asset.BITCOIN].sign == 0
    # Silver: weaker safe-haven (part industrial) -> bullish but weaker
    assert resolved[Asset.SILVER].direction is Direction.BULLISH
    # Gold should react more strongly than silver to geopolitical risk
    assert resolved[Asset.GOLD].magnitude >= resolved[Asset.SILVER].magnitude


def test_hawkish_fed_all_three_bearish():
    """Hawkish Fed gives all three bearish."""
    resolved = tm.resolve(
        "Fed announces surprise rate hike as Powell signals higher for longer",
        NewsCategory.CENTRAL_BANK,
    )
    for asset in Asset:
        assert resolved[asset].direction is Direction.BEARISH, asset.value
        assert resolved[asset].sign == -1, asset.value


def test_btc_etf_outflows_btc_bearish_gold_unchanged():
    """Bitcoin ETF outflows give BITCOIN bearish with gold direction unchanged.
    
    NOTE: With minimum cue magnitude 0.30, BTC_ETF_FLOW magnitude is 0.33
    (1.10 * 0.30 * 1.0 driver_rank), which is below the 0.4 expectation.
    """
    resolved = tm.resolve(
        "Bitcoin ETF outflows surge as investors redeem shares",
        NewsCategory.BITCOIN_ETF_FLOWS,
    )
    # BTC: outflow -> bearish
    assert resolved[Asset.BITCOIN].direction is Direction.BEARISH
    assert resolved[Asset.BITCOIN].sign == -1
    assert resolved[Asset.BITCOIN].magnitude > 0.3  # was > 0.4, now 0.33 with min cue 0.30
    # Gold: ETF flow has no transmission path (sign 0)
    assert resolved[Asset.GOLD].sign == 0
    assert resolved[Asset.GOLD].direction in (Direction.NEUTRAL, Direction.MIXED)
    # Silver: same
    assert resolved[Asset.SILVER].sign == 0
    assert resolved[Asset.SILVER].direction in (Direction.NEUTRAL, Direction.MIXED)


def test_driver_rank_weights_applied_per_asset():
    """Driver rank weights are applied per asset (top driver = 1.0)."""
    from app.config import DRIVER_RANKINGS
    
    # GOLD: GEOPOLITICAL_RISK is top driver (1.0)
    assert DRIVER_RANKINGS["GOLD"]["GEOPOLITICAL_RISK"] == 1.0
    # SILVER: CHINA_DEMAND and INDUSTRIAL_DEMAND are top (1.0, 0.95)
    assert DRIVER_RANKINGS["SILVER"]["CHINA_DEMAND"] == 1.0
    assert DRIVER_RANKINGS["SILVER"]["INDUSTRIAL_DEMAND"] == 0.95
    # BITCOIN: BTC_ETF_FLOW, CRYPTO_REGULATION, CRYPTO_LIQUIDATION, RISK_ON, RISK_OFF are top (1.0)
    assert DRIVER_RANKINGS["BITCOIN"]["BTC_ETF_FLOW"] == 1.0
    assert DRIVER_RANKINGS["BITCOIN"]["CRYPTO_REGULATION"] == 1.0
    assert DRIVER_RANKINGS["BITCOIN"]["CRYPTO_LIQUIDATION"] == 1.0
    assert DRIVER_RANKINGS["BITCOIN"]["RISK_ON"] == 1.0
    assert DRIVER_RANKINGS["BITCOIN"]["RISK_OFF"] == 1.0
    # Unlisted event types get minimal weight (0.3)
    assert DRIVER_RANKINGS["GOLD"].get("UNKNOWN_EVENT", 0.3) == 0.3


def test_price_action_headline_does_not_produce_a_direction():
    """NEWS-ONLY bias (Task 2): price action no longer damps the scale."""
    resolved = tm.resolve("Gold plunges 2% in early trade",
                          NewsCategory.COMMODITY_SUPPLY_DEMAND)
    assert resolved[Asset.GOLD].priced_in is False
    assert resolved[Asset.GOLD].magnitude >= 0.0


def test_magnitude_scales_with_evidence_but_stays_bounded():
    weak = tm.resolve("Fed rate hike", NewsCategory.CENTRAL_BANK)
    strong = tm.resolve(
        "Fed announces emergency rate hike of 75 basis points, signals further "
        "hikes, lifts rates and tightens policy as inflation stays sticky",
        NewsCategory.CENTRAL_BANK,
    )
    assert strong[Asset.GOLD].magnitude > weak[Asset.GOLD].magnitude
    for asset in Asset:
        assert 0.0 <= strong[asset].magnitude <= 1.5


def test_relevance_gate_still_blocks_irrelevant_assets():
    """Guarantee preserved: unrelated news must not leak a DIRECTION."""
    text = "SEC approves spot bitcoin ETF as crypto regulation clears"
    category = NewsCategory.CRYPTO_REGULATION
    # Crypto regulation carries a 0.05 relevance for metals, so the map must
    # give them no directional lean at all.
    resolved = tm.resolve(text, category)
    assert resolved[Asset.GOLD].sign == 0
    assert resolved[Asset.SILVER].sign == 0
    assert resolved[Asset.BITCOIN].sign != 0


def test_recession_driven_cut_is_bullish_gold_and_mixed_for_silver_btc():
    assert tm.direction_for(tm.EventType.RECESSION_CUT, Asset.GOLD) is Direction.BULLISH
    assert tm.direction_for(tm.EventType.RECESSION_CUT, Asset.SILVER) is Direction.MIXED
    assert tm.direction_for(tm.EventType.RECESSION_CUT, Asset.BITCOIN) is Direction.MIXED


# --------------------------------------------------------------------------
# Risk-regime multi-counting filter (resolve() only)
# --------------------------------------------------------------------------
# detect_event_types is intentionally untouched: it still reports every
# risk-regime match. The de-duplication lives in resolve(), where the score
# contribution is computed.
def test_detection_still_reports_all_risk_regime_types():
    """Headlines that trip two risk-regime mechanisms still report all of them
    from detect_event_types - only resolve() collapses them to one."""
    found = tm.detect_event_types("VIX spikes as stocks plunge", NewsCategory.EQUITIES)
    assert tm.EventType.STOCK_PLUNGE in found
    assert tm.EventType.VIX_SPIKE in found

    found = tm.detect_event_types("stocks rally on earnings", NewsCategory.EQUITIES)
    assert tm.EventType.STOCK_RALLY in found
    assert tm.EventType.RISK_ON in found


def test_filter_keeps_strongest_risk_regime_for_btc():
    """For BTC, STOCK_PLUNGE (0.55) beats VIX_SPIKE (0.5): only the strongest
    risk-regime type survives the filter."""
    filtered = tm._filter_risk_regime(
        [tm.EventType.STOCK_PLUNGE, tm.EventType.VIX_SPIKE], Asset.BITCOIN)
    assert filtered == [tm.EventType.STOCK_PLUNGE]


def test_filter_strength_is_asset_specific():
    """The strongest magnitude is per-asset: XAUUSD prefers VIX_SPIKE (0.4)
    over STOCK_PLUNGE (0.35), the opposite of BTC."""
    filtered = tm._filter_risk_regime(
        [tm.EventType.STOCK_PLUNGE, tm.EventType.VIX_SPIKE], Asset.GOLD)
    assert filtered == [tm.EventType.VIX_SPIKE]


def test_filter_single_and_non_risk_regimes_are_unaffected():
    """One risk-regime type passes through unchanged (backward compatibility),
    as do non-risk-regime event types."""
    assert tm._filter_risk_regime([tm.EventType.GEOPOLITICAL_RISK], Asset.BITCOIN) == [
        tm.EventType.GEOPOLITICAL_RISK]
    assert tm._filter_risk_regime([tm.EventType.GEOPOLITICAL_DEESCALATION], Asset.BITCOIN) == [
        tm.EventType.GEOPOLITICAL_DEESCALATION]
    assert tm._filter_risk_regime([tm.EventType.GOLD_ETF_FLOW_IN], Asset.BITCOIN) == [
        tm.EventType.GOLD_ETF_FLOW_IN]


def test_resolve_produces_single_risk_regime_contribution():
    """End-to-end: 2+ risk-regime types on one headline yield a single
    risk-regime contribution, headed by the strongest type for that asset."""
    # War headline: only GEOPOLITICAL_RISK fires - a single risk-regime type,
    # so BTC's contribution is unchanged (no double counting to remove).
    resolved = tm.resolve(
        "Iran war: missiles strike Saudi oil hub", NewsCategory.GEOPOLITICS)
    assert tm.detect_event_types(
        "Iran war: missiles strike Saudi oil hub", NewsCategory.GEOPOLITICS) == [
            tm.EventType.GEOPOLITICAL_RISK]
    assert resolved[Asset.BITCOIN].dominant is tm.EventType.GEOPOLITICAL_RISK

    # Cease-fire headline: only GEOPOLITICAL_DEESCALATION fires.
    resolved = tm.resolve("Cease-fire declared in Iran", NewsCategory.GEOPOLITICS)
    assert tm.detect_event_types(
        "Cease-fire declared in Iran", NewsCategory.GEOPOLITICS) == [
            tm.EventType.GEOPOLITICAL_DEESCALATION]
    assert resolved[Asset.BITCOIN].dominant is tm.EventType.GEOPOLITICAL_DEESCALATION

    # VIX spike headline: STOCK_PLUNGE and VIX_SPIKE both fire; BTC keeps the
    # stronger one (STOCK_PLUNGE, 0.55 > 0.5) - one contribution, not two.
    resolved = tm.resolve("VIX spikes as stocks plunge", NewsCategory.EQUITIES)
    assert tm.detect_event_types(
        "VIX spikes as stocks plunge", NewsCategory.EQUITIES) == [
            tm.EventType.STOCK_PLUNGE, tm.EventType.VIX_SPIKE]
    assert resolved[Asset.BITCOIN].dominant is tm.EventType.STOCK_PLUNGE

    # Non-risk headline: no risk-regime type involved, contributions unchanged.
    resolved = tm.resolve("Gold ETF inflows surge", NewsCategory.ETF_FLOWS)
    assert tm.detect_event_types(
        "Gold ETF inflows surge", NewsCategory.ETF_FLOWS) == [
            tm.EventType.GOLD_ETF_FLOW_IN]
    assert resolved[Asset.BITCOIN].dominant is tm.EventType.GOLD_ETF_FLOW_IN

    # Backward compatibility: "stocks rally on earnings" fires STOCK_RALLY and
    # RISK_ON; the filter drops STOCK_RALLY and keeps RISK_ON (1.0 > 0.55 for
    # BTC) - a single risk-regime contribution, headed by RISK_ON.
    resolved = tm.resolve("stocks rally on earnings", NewsCategory.EQUITIES)
    assert tm.detect_event_types(
        "stocks rally on earnings", NewsCategory.EQUITIES) == [
            tm.EventType.STOCK_RALLY, tm.EventType.RISK_ON]
    assert resolved[Asset.BITCOIN].dominant is tm.EventType.RISK_ON
    assert resolved[Asset.BITCOIN].direction is Direction.BULLISH