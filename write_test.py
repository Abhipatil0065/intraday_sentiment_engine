content = """\"\"\"Task 4 verification: tier multiplier applied exactly once, plus rumor/confirmed discounts and the equity card reusing the same table.\"\"\"

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.analysis.sentiment_engine import (
    Asset, Bias, Direction, EconomicImportance, ImpactStrength,
    NewsCategory, ReportKind, WeightedSignal, compute,
)

from app.analysis.equity_card import build_equity_card
from app.market.session_detector import SessionName
from app.models.enums import Verification


def _s(
    title: str,
    source_quality: str = "TIER_1_OFFICIAL",
    direction=Direction.BEARISH,
    category: NewsCategory = NewsCategory.INFLATION,
) -> WeightedSignal:
    return WeightedSignal(
        asset=Asset.GOLD,
        title=title,
        direction=direction,
        strength=ImpactStrength.MEDIUM,
        importance=EconomicImportance.HIGH,
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        relevance=0.9,
        persistence=0.7,
        source_quality=source_quality,
        session_multiplier=1.0,
        is_primary=True,
        event_key="e1",
        cluster_id="c1",
        corroboration=1.0,
        category=category,
        reason="",
        transmission="",
        report_kind=ReportKind.GENERAL,
        mode="heuristic",
        surprise_weight=1.0,
    )


# --- 1. Fresh Tier 1 bearish headline beats three Tier 3 bullish headlines ---
def test_tier1_bear_beats_three_tier3_bull():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    tier1 = _s("CPI report shows hotter than expected inflation", source_quality="TIER_1_OFFICIAL", direction=Direction.BEARISH)
    t3a = _s("analyst opinion: inflation may ease", source_quality="TIER_3_CRYPTO", direction=Direction.BULLISH)
    t3b = _s("forecast said inflation would cool", source_quality="TIER_3_CRYPTO", direction=Direction.BULLISH)
    t3c = _s("commentary: rates could stay high", source_quality="TIER_3_CRYPTO", direction=Direction.BULLISH)

    r1 = compute([tier1], Asset.GOLD, now=now)
    r3 = compute([t3a, t3b, t3c], Asset.GOLD, now=now)

    assert r1.bias == Bias.BEARISH
    assert r3.bias == Bias.BULLISH
    assert abs(r1.score) >= abs(r3.score)
    assert r1.confidence >= r3.confidence


# --- 2. Rumor < confirmed on the same headline ---
def test_rumor_is_discounted_below_confirmed():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    rumor = _s("Sources say Fed may cut rates next month", source_quality="TIER_1_OFFICIAL", direction=Direction.BEARISH)
    confirmed = _s("Fed holds rates steady", source_quality="TIER_1_OFFICIAL", direction=Direction.BEARISH)

    assert compute([rumor], Asset.GOLD, now=now).score > compute([confirmed], Asset.GOLD, now=now).score


# --- 3. Exact-once tier multiplier: Tier 2 contributes 0.45x of Tier 1 ---
def test_exact_once_tier_multiplier_weight_ratio():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    base = _s("Fed raises rates by 50bp", source_quality="TIER_1_OFFICIAL", direction=Direction.BEARISH)
    tier1 = _s("CPI prints hotter than expected", source_quality="TIER_2_MAJOR", direction=Direction.BEARISH)

    r_base = compute([base], Asset.GOLD, now=now)
    r_tier = compute([tier1], Asset.GOLD, now=now)

    ratio = r_tier.drivers[0].contribution / r_base.drivers[0].contribution
    assert ratio == pytest.approx(0.45 / 1.00, abs=1e-6)


# --- 4. Equity card reuses the same tier table ---
def test_equity_card_respects_tiers():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    tier1 = _s("Nonfarm payrolls beat expectations", source_quality="TIER_1_OFFICIAL", direction=Direction.BULLISH, category=NewsCategory.EQUITIES)
    t3 = _s("analyst preview: companies may guide lower", source_quality="TIER_3_CRYPTO", direction=Direction.BEARISH, category=NewsCategory.EQUITIES)

    card1 = build_equity_card([tier1], session=SessionName.US, now=now)
    card3 = build_equity_card([t3], session=SessionName.US, now=now)

    assert card1.bias == Bias.BULLISH.value
    assert card3.bias == Bias.BULLISH.value
    assert abs(card1.score) >= abs(card3.score)
"""
with open("tests/test_tier_multipliers.py", "w", encoding="utf-8") as f:
    f.write(content)
print("written")
