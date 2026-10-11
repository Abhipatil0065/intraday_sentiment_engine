"""Item 3 verification: regulation split, liquidity events, BTC session profile.

Read-only against a temp DB. Asserts:
  1. REGULATION_CLAMP (ban/enforce/litigation) -> BTC bearish, metals neutral.
  2. REGULATION_CLARITY (approve/framework/ETF approval) -> BTC bullish.
  3. Ambiguous "regulation" alone -> GENERAL_MACRO (no guessed sign).
  4. LIQUIDITY_EASING (QE/TGA draw) -> BTC bullish; LIQUIDITY_TIGHTENING
     (QT/runoff) -> BTC bearish; both neutral on metals.
  5. BTC session profile is flat: 1.0 in every session and on weekends.
  6. Metals keep the session curve + weekend discount.

Usage: python scripts/scratch/e2e_item3_bitcoin.py
"""
from __future__ import annotations

from datetime import datetime

from app.analysis.transmission_map import (
    Asset, EventType, detect_event_types, direction_for, transmission,
)
from app.market.session_detector import (
    SessionName, asset_session_multiplier,
)

CASES = [
    # (headline, expected_event, expected_btc_sign)
    ("SEC bans crypto exchanges from offering staking", EventType.REGULATION_CLAMP, -1),
    ("SEC sues Coinbase over unregistered securities", EventType.REGULATION_CLAMP, -1),
    ("Congress crackdown on offshore crypto venues", EventType.REGULATION_CLAMP, -1),
    ("CFTC enforcement action against leverage protocol", EventType.REGULATION_CLAMP, -1),
    ("SEC approves spot Bitcoin ETF", EventType.REGULATION_CLARITY, 1),
    ("Bitcoin ETF approval brings legal clarity", EventType.REGULATION_CLARITY, 1),
    ("Congress passes crypto framework bill", EventType.REGULATION_CLARITY, 1),
    ("Regulator issues clear guidance for custody", EventType.REGULATION_CLARITY, 1),
    ("Fed launches quantitative easing programme", EventType.LIQUIDITY_EASING, 1),
    ("Balance sheet runoff begins as QT takes hold", EventType.LIQUIDITY_TIGHTENING, -1),
    ("Treasury general account drawdown injects liquidity", EventType.LIQUIDITY_EASING, 1),
]


def main() -> None:
    from app.config import settings

    # 1. Detection + direction for every case.
    for headline, expected, expected_sign in CASES:
        from app.analysis.cue_rules import detect_category
        cat = detect_category(headline)
        detected = detect_event_types(headline, cat)
        assert expected in detected, (
            f"{headline!r}: expected {expected}, got {detected}")
        t = transmission(expected, Asset.BITCOIN)
        assert t.sign == expected_sign, (
            f"{headline!r}: BTC sign {t.sign} != {expected_sign}")
        print(f"  OK {expected.value:22s} BTC={t.sign:+d} mag={t.magnitude:.2f}  {headline[:55]}")

    # 2. Ambiguous bare "regulation" must NOT be guessed at.
    from app.analysis.cue_rules import detect_category
    cat = detect_category("Crypto regulation debate continues")
    detected = detect_event_types("Crypto regulation debate continues", cat)
    assert EventType.REGULATION_CLAMP not in detected, detected
    assert EventType.REGULATION_CLARITY not in detected, detected
    print("  OK bare 'regulation' falls through to GENERAL_MACRO, not guessed")

    # 3. BTC session profile is flat.
    for session in (SessionName.ASIA, SessionName.EUROPE, SessionName.US,
                    SessionName.OFF_HOURS):
        m = asset_session_multiplier("BTCUSD", session, "Bitcoin ETF Flows")
        assert m == 1.0, (session, m)
    for day, label in ((0, "Mon"), (4, "Fri"), (5, "Sat"), (6, "Sun")):
        m = asset_session_multiplier(
            "BTCUSD", SessionName.US, "Bitcoin ETF Flows", datetime(2026, 10, 5 + day))
        assert m == 1.0, (label, m)
    print("  OK BTCUSD flat: 1.0 in every session and on every weekday")

    # 4. Metals keep the session curve + weekend discount.
    assert asset_session_multiplier(
        "XAUUSD", SessionName.ASIA, "Central Bank") < 1.0
    assert asset_session_multiplier(
        "XAUUSD", SessionName.US, "Central Bank", datetime(2026, 10, 10)) < 1.0
    print("  OK XAUUSD keeps session curve + weekend discount")

    # 5. Liquidity events reach metals weakly and equities moderately, and are
    #    opposite directions for BTC (easing bullish, tightening bearish).
    for asset in (Asset.GOLD, Asset.SILVER):
        assert transmission(EventType.LIQUIDITY_EASING, asset).sign == 0
        assert transmission(EventType.LIQUIDITY_TIGHTENING, asset).sign == 0
    assert transmission(EventType.LIQUIDITY_EASING, Asset.BITCOIN).sign == 1
    assert transmission(EventType.LIQUIDITY_TIGHTENING, Asset.BITCOIN).sign == -1
    print("  OK LIQUIDITY_EASING/TIGHTENING: metals neutral, BTC opposite signs")

    # 6. .env override works. weekend_tolerant keeps the session curve but drops
    #    the weekend discount, so it differs from BOTH flat (always 1.0) and
    #    metal (curve + discount). Verify it actually follows the curve and
    #    beats metal on a weekend.
    settings.asset_session_profiles = "BTC=weekend_tolerant"
    curve = asset_session_multiplier(
        "BTCUSD", SessionName.ASIA, "Bitcoin ETF Flows")
    weekend = asset_session_multiplier(
        "BTCUSD", SessionName.ASIA, "Bitcoin ETF Flows", datetime(2026, 10, 10))
    assert curve < 1.0, curve            # session curve still applies
    assert weekend == curve, (weekend, curve)  # no weekend discount
    metal_weekend = asset_session_multiplier(
        "XAUUSD", SessionName.ASIA, "Central Bank", datetime(2026, 10, 10))
    assert metal_weekend < weekend, (metal_weekend, weekend)
    settings.asset_session_profiles = "BTC=flat"
    assert asset_session_multiplier(
        "BTCUSD", SessionName.ASIA, "Bitcoin ETF Flows", datetime(2026, 10, 10)) == 1.0
    print("  OK BTC_SESSION_PROFILE=weekend_tolerant override works")

    print("\nALL ITEM 3 CHECKS PASSED")


if __name__ == "__main__":
    main()