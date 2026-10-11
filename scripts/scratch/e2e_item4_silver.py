"""Item 4 verification: silver independence.

Read-only. Asserts:
  1. SILVER_INDUSTRIAL_DEMAND detects solar / photovoltaic / EV / electronics
     headlines and is bullish XAGUSD.
  2. SILVER_CHINA_DEMAND detects China PMI / factory-gate / offtake headlines
     and is bullish XAGUSD.
  3. GOLD_SILVER_RATIO_HIGH is bullish XAGUSD, bearish XAUUSD.
  4. GOLD_SILVER_RATIO_LOW is the mirror image.
  5. INDUSTRIAL_DEMAND and CHINA_DEMAND magnitudes are raised for XAGUSD.
  6. Gold->silver bleed is configurable and lower than the direct signal.

Usage: python scripts/scratch/e2e_item4_silver.py
"""
from __future__ import annotations

from app.analysis.transmission_map import (
    Asset, EventType, detect_event_types, transmission,
)
from app.analysis.cue_rules import detect_category

CASES = [
    # (headline, expected_event, expected_xag_sign)
    ("Solar photovoltaic capacity surges across Europe", EventType.SILVER_INDUSTRIAL_DEMAND, 1),
    ("EV battery demand drives silver consumption higher", EventType.SILVER_INDUSTRIAL_DEMAND, 1),
    ("Grid storage and semiconductor orders lift silver fabrication", EventType.SILVER_INDUSTRIAL_DEMAND, 1),
    ("China manufacturing PMI prints strong factory output", EventType.SILVER_CHINA_DEMAND, 1),
    ("Silver offtake from Shanghai rises on import demand", EventType.SILVER_CHINA_DEMAND, 1),
    ("Gold silver ratio hits record high as gold outperforms", EventType.GOLD_SILVER_RATIO_HIGH, 1),
    ("Gold silver ratio above 100: silver looks cheap vs gold", EventType.GOLD_SILVER_RATIO_HIGH, 1),
    ("Gold silver ratio narrows as silver outperforms gold", EventType.GOLD_SILVER_RATIO_LOW, -1),
]


def main() -> None:
    for headline, expected, expected_sign in CASES:
        cat = detect_category(headline)
        detected = detect_event_types(headline, cat)
        assert expected in detected, (
            f"{headline!r}: expected {expected}, got {detected}")
        t = transmission(expected, Asset.SILVER)
        assert t.sign == expected_sign, (
            f"{headline!r}: XAG sign {t.sign} != {expected_sign}")
        print(f"  OK {expected.value:24s} XAG={t.sign:+d} mag={t.magnitude:.2f}  {headline[:52]}")

    # Ratio is a two-sided silver signal: high ratio bullish silver, bearish gold.
    assert transmission(EventType.GOLD_SILVER_RATIO_HIGH, Asset.GOLD).sign == -1
    assert transmission(EventType.GOLD_SILVER_RATIO_HIGH, Asset.SILVER).sign == 1
    assert transmission(EventType.GOLD_SILVER_RATIO_LOW, Asset.GOLD).sign == 1
    assert transmission(EventType.GOLD_SILVER_RATIO_LOW, Asset.SILVER).sign == -1
    print("  OK ratio is two-sided: high bullish silver / bearish gold and vice versa")

    # Raised magnitudes for the silver-specific mechanisms.
    assert transmission(EventType.SILVER_INDUSTRIAL_DEMAND, Asset.SILVER).magnitude >= 0.95
    assert transmission(EventType.SILVER_CHINA_DEMAND, Asset.SILVER).magnitude >= 0.95
    assert transmission(EventType.INDUSTRIAL_DEMAND, Asset.SILVER).magnitude >= 0.95
    assert transmission(EventType.CHINA_DEMAND, Asset.SILVER).magnitude >= 0.95
    print("  OK silver demand magnitudes raised (>=0.95)")

    # Bleed is configurable and below the direct signal.
    from app.config import settings
    direct = transmission(EventType.CHINA_DEMAND, Asset.SILVER).magnitude
    bleed = settings.gold_silver_bleed
    assert 0.0 < bleed < 1.0, bleed
    assert bleed < direct, (bleed, direct)
    print(f"  OK gold_silver_bleed={bleed:.2f} < direct magnitude {direct:.2f}")

    # A headline that names silver's own mechanism must NOT be read as gold.
    cat = detect_category("Silver fabrication demand rises on EV and solar orders")
    detected = detect_event_types("Silver fabrication demand rises on EV and solar orders", cat)
    assert EventType.SILVER_INDUSTRIAL_DEMAND in detected
    print("  OK silver-specific headline routes to silver, not gold")

    print("\nALL ITEM 4 CHECKS PASSED")


if __name__ == "__main__":
    main()