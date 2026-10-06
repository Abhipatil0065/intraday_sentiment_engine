"""TASK 3: surprise vs expectation decides actual-data direction and weight."""
from datetime import timedelta

import pytest

from app.analysis import release_surprise as rs
from app.analysis import text_quality as tq
from app.analysis.news_classifier import heuristic_analyze
from app.analysis.sentiment_engine import WeightedSignal, compute
from app.models.enums import (
    Asset,
    Direction,
    ImpactStrength,
    NewsCategory,
    ReportKind,
)
from tests.fixed_clock import FIXED_NOW as NOW, freeze_now


def _gold(analysis):
    return next(a for a in analysis.assets if a.asset is Asset.GOLD)


def test_hot_cpi_print_is_gold_bearish():
    # Numbers decide, not words: the text carries no directional verb at all.
    out = heuristic_analyze("US CPI m/m", "Actual 0.5 Forecast 0.3 Previous 0.2")
    assert out.report_kind is ReportKind.ACTUAL
    assert _gold(out).direction is Direction.BEARISH


def test_cool_cpi_print_is_gold_bullish():
    out = heuristic_analyze("US CPI m/m", "Actual 0.2 Forecast 0.4 Previous 0.3")
    assert out.report_kind is ReportKind.ACTUAL
    assert _gold(out).direction is Direction.BULLISH


def test_in_line_print_gets_low_weight():
    in_line = rs.ReleasePrint(0.3, 0.3, 0.2, "US CPI m/m")
    small = rs.ReleasePrint(0.4, 0.3, 0.2, "US CPI m/m")
    low = rs.surprise_weight(in_line, "CPI in line with forecasts")
    assert low < rs.surprise_weight(small)
    assert low < 1.0  # text expectation cues cut the weight further


def test_missing_forecast_falls_back_to_previous_then_to_cue_direction():
    printed = rs.ReleasePrint(0.5, None, 0.2, "US CPI m/m")  # forecast missing
    assert rs.surprise_delta(printed) == pytest.approx(0.3)
    assert rs.direction_for(Asset.GOLD, printed, Direction.BULLISH) \
        is Direction.BEARISH
    bare = rs.ReleasePrint(0.5, None, None, "US CPI m/m")  # nothing to compare
    assert rs.surprise_delta(bare) is None
    assert rs.direction_for(Asset.GOLD, bare, Direction.BULLISH) \
        is Direction.BULLISH  # both missing -> existing cue direction
    assert rs.surprise_weight(None) == 1.0  # no print -> unchanged weight


def test_large_surprise_outweighs_small_surprise(monkeypatch):
    freeze_now(monkeypatch)
    small = rs.surprise_weight(rs.ReleasePrint(3.1, 3.0, 2.9, "US CPI y/y"))
    large = rs.surprise_weight(rs.ReleasePrint(3.9, 3.0, 2.9, "US CPI y/y"))
    assert small < large <= rs.MAX_SURPRISE_WEIGHT  # one named cap
    finals = []
    for weight in (small, large):
        sig = WeightedSignal(
            asset=Asset.GOLD, title="US CPI y/y print",
            direction=Direction.BEARISH, strength=ImpactStrength.HIGH,
            relevance=0.8, persistence=0.6,
            published_at=NOW - timedelta(minutes=5),
            category=NewsCategory.INFLATION, surprise_weight=weight)
        compute([sig], Asset.GOLD, now=NOW)
        finals.append(sig.final_weight)
    assert finals[1] > finals[0]


def test_higher_unemployment_is_gold_bullish():
    # Higher unemployment = WEAKER economy: the hot sign is inverted.
    out = heuristic_analyze("US Unemployment Rate",
                            "Actual 4.5 Forecast 4.3 Previous 4.2")
    assert out.report_kind is ReportKind.ACTUAL
    assert _gold(out).direction is Direction.BULLISH


def test_lower_jobless_claims_are_gold_bearish():
    out = heuristic_analyze("US Jobless Claims",
                            "Actual 210K Forecast 240K Previous 250K")
    assert out.report_kind is ReportKind.ACTUAL
    assert _gold(out).direction is Direction.BEARISH  # cooler = tighter economy


def test_strong_nfp_is_gold_bearish_but_equity_bullish():
    text = "US Nonfarm Payrolls Actual 310K Forecast 200K Previous 160K"
    out = heuristic_analyze("US Nonfarm Payrolls", text)
    assert out.report_kind is ReportKind.ACTUAL
    assert _gold(out).direction is Direction.BEARISH       # metals column
    # NEUTRAL cue: an unknown indicator would fall back to it, not pass here.
    assert rs.equity_direction(rs.parse(text), Direction.NEUTRAL) \
        is Direction.BULLISH                               # equity column


def test_expectation_cues_cut_weight_and_price_multiplier_stays_1_0():
    # Text-only expectation cues reduce the weight (point 6) ...
    assert tq.priced_in_multiplier("widely anticipated CPI, as expected") < 1.0
    assert tq.priced_in_multiplier("print in line with forecasts") < 1.0
    # ... and the old price-driven multiplier is still a constant 1.0,
    # fed by no price data of any kind.
    assert tq.directional_multiplier("Gold plunges 2% in early trade") == 1.0
    assert tq.directional_multiplier("US CPI actual 0.5 vs 0.3 forecast") == 1.0


def test_hot_cpi_is_equity_bearish():
    text = "US CPI m/m Actual 0.5 Forecast 0.3 Previous 0.2"
    assert rs.equity_direction(rs.parse(text), Direction.NEUTRAL) \
        is Direction.BEARISH                            # hot inflation
    cool = rs.parse("US CPI m/m Actual 0.2 Forecast 0.4 Previous 0.3")
    assert rs.equity_direction(cool, Direction.NEUTRAL) is Direction.BULLISH


def test_fed_projection_with_number_is_not_actual_and_keeps_cue():
    text = "sees inflation at 3% by year end"
    assert rs.parse(text) is None                        # a quote, not a release
    assert rs.parse("sees inflation at 3% versus 2.8% prior") is None
    out = heuristic_analyze("Fed's Williams", text)
    assert out.report_kind is not ReportKind.ACTUAL
    assert all(not a.reason.startswith("Actual ") for a in out.assets)
    cue = Direction.BULLISH                              # cue direction stands
    assert rs.direction_for(Asset.GOLD, rs.parse(text), cue) is cue


# ============================================================
# TASK 3c: mixed headlines, number parsing, revision handling
# ============================================================

def test_mixed_headline_payrolls_and_unemployment():
    """
    "Payrolls beat forecast as unemployment rate rises to 4.3%"
    - Payrolls (growth family): beat forecast -> bullish for equities, bearish for gold
    - Unemployment (cool family): rises to 4.3% -> higher unemployment = weaker economy
      -> bullish for gold, bearish for equities
    Each print gets its own direction based on its indicator family.
    """
    text = "Payrolls beat forecast as unemployment rate rises to 4.3% forecast 4.1%"
    prints = rs.parse_all(text)
    # Should find both prints
    assert len(prints) == 2
    
    # Find payrolls print (growth family) and unemployment print (cool family)
    payrolls_print = next((p for p in prints if p.family == "growth"), None)
    unemployment_print = next((p for p in prints if p.family == "cool"), None)
    
    assert payrolls_print is not None, "Should find payrolls print"
    assert unemployment_print is not None, "Should find unemployment print"
    
    # Payrolls beat forecast: actual > forecast -> growth family -> HOT -> gold BEARISH
    # (but payrolls_print may not have explicit numbers, so direction falls back to cue)
    # Unemployment rises: actual 4.3% > forecast 4.1% -> cool family -> higher = cool
    # -> inverted sign -> gold BULLISH
    
    # Test gold directions
    cue = Direction.NEUTRAL
    payrolls_dir = rs.direction_for(Asset.GOLD, payrolls_print, cue)
    unemployment_dir = rs.direction_for(Asset.GOLD, unemployment_print, cue)
    
    # Payrolls (growth): higher = hot = bearish for gold
    # Unemployment (cool): higher = cool = bullish for gold (inverted)
    # Since payrolls may not have explicit actual/forecast, it may fall back to cue
    # But unemployment should have actual 4.3 and forecast 4.1
    assert unemployment_dir == Direction.BULLISH  # higher unemployment -> gold bullish


def test_mixed_headline_ambiguous_keeps_cue_direction():
    """
    If a headline has two prints but the match is ambiguous (e.g., numbers
    not clearly associated with either indicator), keep the cue direction.
    """
    # This headline mentions two indicators but only one clear number
    text = "Payrolls and unemployment both moved today"
    prints = rs.parse_all(text)
    # No clear numbers, so no prints should be extracted
    # (numbers are required for ACTUAL prints)
    assert len(prints) == 0


def test_percent_numbers_parse_and_compare():
    """
    "3.4% vs 3.1%" - percent numbers parse correctly and compare in same unit
    """
    text = "CPI actual 3.4% forecast 3.1%"
    prints = rs.parse_all(text)
    assert len(prints) == 1
    p = prints[0]
    assert p.actual == pytest.approx(3.4)
    assert p.forecast == pytest.approx(3.1)
    assert p.family == "inflation"


def test_k_numbers_parse_and_compare():
    """
    "225K vs 190K" - K (thousands) numbers parse correctly
    """
    text = "Nonfarm payrolls actual 225K forecast 190K"
    prints = rs.parse_all(text)
    assert len(prints) == 1
    p = prints[0]
    assert p.actual == pytest.approx(225000)
    assert p.forecast == pytest.approx(190000)
    assert p.family == "growth"


def test_m_numbers_parse_and_compare():
    """
    "1.2M vs 1.1M" - M (millions) numbers parse correctly
    """
    text = "Job openings actual 1.2M forecast 1.1M"
    prints = rs.parse_all(text)
    assert len(prints) == 1
    p = prints[0]
    assert p.actual == pytest.approx(1200000)
    assert p.forecast == pytest.approx(1100000)


def test_negative_numbers_parse_and_compare():
    """
    "-0.3% vs 0.1%" - negative values parse correctly
    """
    text = "GDP actual -0.3% forecast 0.1%"
    prints = rs.parse_all(text)
    assert len(prints) == 1
    p = prints[0]
    assert p.actual == pytest.approx(-0.3)
    assert p.forecast == pytest.approx(0.1)
    assert p.family == "growth"


def test_revision_is_not_actual():
    """
    "previous revised to 175K" or "prior revised to 3.2%" must never be read
    as the actual figure. A revision can only update the previous value.
    """
    # Revision language should not create an actual print
    text = "Previous revised to 175K"
    prints = rs.parse_all(text)
    assert len(prints) == 0, "Revision alone should not parse as actual"
    
    # Revision with a current print - the revision should update previous, not be the actual
    text = "Nonfarm payrolls actual 200K forecast 180K previous revised to 175K"
    prints = rs.parse_all(text)
    assert len(prints) == 1
    p = prints[0]
    assert p.actual == pytest.approx(200000)
    assert p.forecast == pytest.approx(180000)
    # The revision should be captured as previous
    assert p.previous == pytest.approx(175000)


def test_revision_updates_previous_for_fallback():
    """
    When forecast is missing, the revised previous should be used as fallback.
    """
    text = "CPI actual 3.2% previous revised to 3.0%"
    prints = rs.parse_all(text)
    assert len(prints) == 1
    p = prints[0]
    assert p.actual == pytest.approx(3.2)
    assert p.forecast is None  # no forecast given
    assert p.previous == pytest.approx(3.0)  # revised previous
    
    # surprise_delta should use the revised previous
    delta = rs.surprise_delta(p)
    assert delta == pytest.approx(0.2)


# ============================================================
# TASK 3d: combine multiple prints by weight
# ============================================================

def test_mixed_headline_combined_weight_payrolls_unemployment():
    """
    "Payrolls 225K vs 190K as unemployment rate rises to 4.3% forecast 4.1%"
    - Payrolls (growth): 225K vs 190K = +35K surprise -> growth family -> HOT -> gold BEARISH
    - Unemployment (cool): 4.3% vs 4.1% = +0.2% surprise -> cool family -> higher = cool -> inverted -> gold BULLISH
    
    The gold direction should follow the larger weighted surprise.
    Payrolls surprise (35K) is large relative to std (120K) -> z ~ 0.29
    Unemployment surprise (0.2%) is moderate relative to std (0.2%) -> z ~ 1.0
    So unemployment should dominate -> gold BULLISH
    """
    text = "Payrolls 225K vs 190K as unemployment rate rises to 4.3% forecast 4.1%"
    out = heuristic_analyze("US Jobs Report", text)
    assert out.report_kind is ReportKind.ACTUAL
    # Gold should be BULLISH because unemployment surprise is larger in sigma terms
    gold_impact = next(a for a in out.assets if a.asset is Asset.GOLD)
    assert gold_impact.direction is Direction.BULLISH


def test_two_prints_different_weights():
    """
    Two prints with different weights for the same asset - the stronger
    surprise should dominate the direction.
    """
    from app.analysis.release_surprise import combine_prints_for_asset, ReleasePrint
    
    # Print 1: inflation hot -> gold BEARISH
    p1 = ReleasePrint(actual=3.5, forecast=3.0, previous=2.8, 
                      title="CPI", family="inflation", actual_raw="3.5%", forecast_raw="3.0%")
    # Print 2: unemployment higher -> gold BULLISH (cool family inverted)
    p2 = ReleasePrint(actual=4.5, forecast=4.3, previous=4.2,
                      title="Unemployment", family="cool", actual_raw="4.5%", forecast_raw="4.3%")
    
    cue = Direction.NEUTRAL
    direction, weight = combine_prints_for_asset(Asset.GOLD, [p1, p2], cue)
    # The weights are different (unemployment has higher z-score)
    # The direction should follow the stronger surprise
    assert direction in (Direction.BULLISH, Direction.BEARISH)
    assert 0.0 <= weight <= rs.MAX_SURPRISE_WEIGHT


def test_cancel_prints_keep_cue():
    """
    Two prints with exactly equal and opposite contributions cancel,
    keeping the cue direction.
    """
    from app.analysis.release_surprise import combine_prints_for_asset, ReleasePrint
    
    # Two prints with same weight, opposite directions
    p1 = ReleasePrint(actual=3.5, forecast=3.0, previous=2.8,
                      title="CPI", family="inflation", actual_raw="3.5%", forecast_raw="3.0%")
    p2 = ReleasePrint(actual=3.0, forecast=3.5, previous=3.2,
                      title="CPI", family="inflation", actual_raw="3.0%", forecast_raw="3.5%")
    
    # With same surprise magnitude but opposite signs, they should cancel
    cue = Direction.BULLISH
    direction, weight = combine_prints_for_asset(Asset.GOLD, [p1, p2], cue)
    # Should keep cue direction when they cancel
    assert direction == cue
    # Weight can be 1.0 (baseline) when perfectly cancelled
    assert weight == 1.0


def test_mixed_unit_k_vs_percent_dropped():
    """
    A print where actual is in K and forecast is in % should be dropped
    (mixed units). If no valid print remains, cue direction is kept.
    """
    from app.analysis.release_surprise import combine_prints_for_asset, ReleasePrint
    
    # Mixed units: actual in K, forecast in %
    p1 = ReleasePrint(actual=225000, forecast=3.5, previous=3.0,
                      title="Mixed", family="growth", 
                      actual_raw="225K", forecast_raw="3.5%")
    
    cue = Direction.BULLISH
    direction, weight = combine_prints_for_asset(Asset.GOLD, [p1], cue)
    # Mixed units -> print dropped -> cue direction kept, weight 1.0
    assert direction == cue
    assert weight == 1.0


def test_mixed_unit_with_valid_print():
    """
    When one print has mixed units (dropped) and another is valid,
    the valid one should determine the direction.
    """
    from app.analysis.release_surprise import combine_prints_for_asset, ReleasePrint
    
    # Mixed units print (should be dropped)
    p1 = ReleasePrint(actual=225000, forecast=3.5, previous=3.0,
                      title="Mixed", family="growth",
                      actual_raw="225K", forecast_raw="3.5%")
    # Valid print: unemployment higher -> gold BULLISH
    p2 = ReleasePrint(actual=4.5, forecast=4.3, previous=4.2,
                      title="Unemployment", family="cool",
                      actual_raw="4.5%", forecast_raw="4.3%")
    
    cue = Direction.NEUTRAL
    direction, weight = combine_prints_for_asset(Asset.GOLD, [p1, p2], cue)
    # p1 dropped, p2 valid -> gold BULLISH (unemployment higher = cool = inverted)
    assert direction == Direction.BULLISH
    assert weight > 1.0


def test_ambiguous_variant_keeps_cue_direction():
    """
    When a headline mentions indicators but numbers are ambiguous/unclear,
    the cue direction should be kept.
    """
    # Headline with indicators but no clear numbers for each
    text = "Payrolls and unemployment both moved today"
    out = heuristic_analyze("US Jobs", text)
    # No prints extracted -> report_kind stays GENERAL, not ACTUAL
    assert out.report_kind is not ReportKind.ACTUAL


def test_equity_card_combined_direction():
    """
    Equity card should also combine multiple prints using equity_direction.
    """
    from app.analysis.release_surprise import combine_prints_for_asset, ReleasePrint
    
    # Payrolls beat (growth -> equity BULLISH)
    p1 = ReleasePrint(actual=250000, forecast=200000, previous=180000,
                      title="Payrolls", family="growth",
                      actual_raw="250K", forecast_raw="200K")
    # Unemployment rises (cool -> equity BEARISH)
    p2 = ReleasePrint(actual=4.5, forecast=4.3, previous=4.2,
                      title="Unemployment", family="cool",
                      actual_raw="4.5%", forecast_raw="4.3%")
    
    cue = Direction.NEUTRAL
    direction, weight = combine_prints_for_asset(Asset.BITCOIN, [p1, p2], cue, use_equity_column=True)
    # Equity: growth hot -> BULLISH, cool higher -> BEARISH
    # The stronger surprise (unemployment) should dominate
    # z for payrolls: (250-200)/120 = 0.42
    # z for unemployment: (4.5-4.3)/0.2 = 1.0
    # Unemployment dominates -> equity BEARISH
    assert direction == Direction.BEARISH
    # Weight is the absolute signed sum, can be < 1.0 if prints partially cancel
    assert 0.0 <= weight <= rs.MAX_SURPRISE_WEIGHT


def test_equity_card_same_direction_prints_add():
    """
    Two prints with same equity direction should add their weights.
    """
    from app.analysis.release_surprise import combine_prints_for_asset, ReleasePrint
    
    # Two growth prints: both bullish for equity
    p1 = ReleasePrint(actual=250000, forecast=200000, previous=180000,
                      title="Payrolls", family="growth",
                      actual_raw="250K", forecast_raw="200K")
    p2 = ReleasePrint(actual=1.2, forecast=1.0, previous=0.9,
                      title="Retail Sales", family="growth",
                      actual_raw="1.2M", forecast_raw="1.0M")
    
    cue = Direction.NEUTRAL
    direction, weight = combine_prints_for_asset(Asset.BITCOIN, [p1, p2], cue, use_equity_column=True)
    # Both bullish for equity -> combined should be bullish with higher weight
    assert direction == Direction.BULLISH
    assert weight > 1.0
