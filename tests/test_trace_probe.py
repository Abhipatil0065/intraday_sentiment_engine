#!/usr/bin/env python
"""
Test script to run 25 headlines through the sentiment engine and capture
detailed evidence for all four assets (Gold, Silver, Bitcoin, Equity).
"""

import sys
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis.sentiment_engine import (
    compute, WeightedSignal, SentimentResult,
    _signal_weight, _tie_break_bias, _confidence_from, payload_score,
    EPS, DEFAULT_BIAS, FLIP_MARGIN, FLIP_HOLD_MINUTES,
    COOLING_MINUTES, COOLING_MARGIN, TIEBREAK_LEAN, TIEBREAK_MAX_CONFIDENCE,
    STRENGTH_MULT, QUALITY_MULT
)
from app.analysis.transmission_map import (
    resolve, detect_event_types, measure_cue_magnitude,
    EVENT_SIGNALS, CATEGORY_FALLBACK,
    CATEGORY_PERSISTENCE
)
from app.analysis.event_polarity import POLARITY_PAIRS
from app.config import (
    DRIVER_RANKINGS, TIER_MULTIPLIERS, TIER_HALF_LIVES, TRADING_DAY_CUTOFF_HOURS,
    RUMOR_MULTIPLIER, CONFIRMED_BOOST
)
from app.analysis.cue_rules import (
    BULLISH_CUES, BEARISH_CUES, detect_direction, detect_category,
    detect_importance, detect_report_kind, detect_verification,
    detect_metal_character
)
from app.analysis.asset_relevance import relevance_for, relevant_assets
from app.analysis.signal_factory import build_signal, build_bleed_signal
from app.config import settings, DRIVER_RANKINGS as CONFIG_DRIVER_RANKINGS
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength,
    NewsCategory, ReportKind, Verification
)

# Test headlines
HEADLINES = [
    "Fed hikes rates 25bp, signals higher for longer",
    "Fed cuts rates by 25bp, signals more easing ahead",
    "US CPI comes in hotter than expected at 3.8%",
    "US CPI comes in cooler than expected at 2.6%",
    "US dollar index falls to 3-month low",
    "10-year Treasury yield jumps to 5%",
    "Missile strike escalates Middle East conflict, oil jumps",
    "Regional bank collapses, regulators step in",
    "Spot Bitcoin ETFs see $500 million net inflows",
    "Spot Bitcoin ETFs see $500 million net outflows",
    "SEC approves new crypto framework, regulatory clarity improves",
    "Major crypto exchange hacked, $300 million stolen",
    "Apple beats earnings expectations, raises guidance",
    "Small-cap retailer XYZ beats earnings expectations",
    "Nvidia misses revenue estimates, shares plunge",
    "China factory output surges, solar demand booms",
    "Global stocks plunge as VIX spikes to 35",
    "Stocks rally to record highs on strong risk appetite",
    "Fed did NOT raise rates as expected",
    "Sources say Fed may be considering a rate cut",
    "Gold prices rise as dollar weakens",
    "Yields rise sharply",
    "Markets quiet ahead of data, little changed",
    "Fed hikes rates",
    "Fed hikes rates",  # Duplicate for decay test (10 hours ago)
]

def create_weighted_signal(headline: str, asset: Asset, published_at: datetime = None) -> WeightedSignal:
    """Create a WeightedSignal from a headline for a specific asset."""
    if published_at is None:
        published_at = datetime.now(timezone.utc)
    
    # Detect category and other properties
    category = detect_category(headline)
    importance = detect_importance(headline, category)
    report_kind = detect_report_kind(headline)
    verification = detect_verification(headline)
    direction = detect_direction(headline, equity=(asset == Asset.EQUITY))
    metal_character = detect_metal_character(headline) if asset == Asset.SILVER else None
    
    # Get relevance for this asset
    relevance = relevance_for(category, asset, headline)
    
    # Get event types
    event_types = detect_event_types(headline, category)
    
    # Get cue magnitude
    cue_magnitude = measure_cue_magnitude(headline)
    
    # Resolve transmission for this asset
    resolved = resolve(headline, category, cue_magnitude)
    impact = resolved[asset]
    
    # Determine strength from importance
    strength_map = {
        EconomicImportance.VERY_HIGH: ImpactStrength.VERY_HIGH,
        EconomicImportance.HIGH: ImpactStrength.HIGH,
        EconomicImportance.MEDIUM: ImpactStrength.MEDIUM,
        EconomicImportance.LOW: ImpactStrength.LOW,
    }
    strength = strength_map.get(importance, ImpactStrength.MEDIUM)
    
    # Get persistence from category
    persistence = CATEGORY_PERSISTENCE.get(category.value, 0.5)
    
    # Session multiplier
    from app.market.session_detector import asset_session_multiplier, detect_session
    session_mult = asset_session_multiplier(asset.value, detect_session(), category.value)
    
    # Build signal using the signal factory
    signal = build_signal(
        asset=asset,
        title=headline,
        direction=direction if direction != Direction.NEUTRAL else impact.direction,
        strength=strength,
        importance=importance,
        published_at=published_at,
        relevance=relevance,
        persistence=persistence,
        source_quality="TIER_1_OFFICIAL",
        source_name="Test Source",
        category=category,
        session_multiplier=session_mult,
        is_primary=True,
        event_key=f"test_{headline[:30]}",
        report_kind=report_kind,
        reason=f"Test: {category.value}, {importance.value}",
        transmission=impact.transmission if hasattr(impact, 'transmission') else "",
        mode="heuristic",
        surprise_weight=1.0,
        metal_character=metal_character,
    )
    
    return signal, {
        'category': category,
        'importance': importance,
        'report_kind': report_kind,
        'verification': verification,
        'direction': direction,
        'event_types': event_types,
        'cue_magnitude': cue_magnitude,
        'relevance': relevance,
        'persistence': persistence,
        'impact': impact,
        'strength': strength,
        'session_mult': session_mult,
    }

def print_signal_breakdown(signal: WeightedSignal, meta: Dict, asset: Asset, now: datetime):
    """Print detailed breakdown of a signal's weight calculation."""
    print(f"\n  --- {asset.value} Signal Breakdown ---")
    print(f"  Category: {meta['category'].value}")
    print(f"  Importance: {meta['importance'].value}")
    print(f"  Report Kind: {meta['report_kind'].value}")
    print(f"  Verification: {meta['verification'].value}")
    print(f"  Direction: {meta['direction'].value}")
    print(f"  Event Types: {[et.value for et in meta['event_types']]}")
    print(f"  Cue Magnitude: {meta['cue_magnitude']:.3f}")
    print(f"  Relevance: {meta['relevance']:.3f}")
    print(f"  Persistence: {meta['persistence']:.3f}")
    print(f"  Strength: {meta['strength'].value}")
    print(f"  Session Multiplier: {meta['session_mult']:.3f}")
    print(f"  Impact: direction={meta['impact'].direction.value}, sign={meta['impact'].sign}, magnitude={meta['impact'].magnitude:.3f}, dominant={meta['impact'].dominant.value if meta['impact'].dominant else None}")
    
    # Calculate _signal_weight components
    from app.analysis.sentiment_engine import event_tier
    from app.config import TIER_MULTIPLIERS, TIER_HALF_LIVES, TRADING_DAY_CUTOFF_HOURS
    from app.analysis.cue_rules import detect_verification
    from app.analysis.release_surprise import MAX_SURPRISE_WEIGHT
    
    tier_key = event_tier(signal)
    print(f"  Event Tier: {tier_key}")
    
    # Decay
    from app.analysis.sentiment_engine import decay_weight
    decay = decay_weight(signal.published_at, tier_key, now)
    print(f"  Decay: {decay:.4f} (tier={tier_key}, half_life={TIER_HALF_LIVES.get(tier_key, 2.0)}h)")
    
    # Source quality
    qual = QUALITY_MULT.get((signal.source_quality or "").upper(), 0.90)
    if signal.category is NewsCategory.BITCOIN_ETF_FLOWS:
        qual = 1.0
    print(f"  Source Quality: {qual:.3f} ({signal.source_quality})")
    
    # Corroboration
    corr = max(1.0, float(signal.corroboration or 1.0))
    print(f"  Corroboration: {corr:.3f}")
    
    # Surprise
    surprise = max(0.0, min(MAX_SURPRISE_WEIGHT, float(signal.surprise_weight or 0.0)))
    print(f"  Surprise Weight: {surprise:.3f}")
    
    # Tier multiplier
    tier = TIER_MULTIPLIERS.get(tier_key, 0.45)
    if detect_verification(signal.title) is Verification.UNVERIFIED:
        tier *= RUMOR_MULTIPLIER
    elif detect_verification(signal.title) is Verification.CONFIRMED:
        tier *= CONFIRMED_BOOST
    print(f"  Tier Multiplier: {tier:.3f} (base={TIER_MULTIPLIERS.get(tier_key, 0.45):.2f})")
    
    # Metal multiplier
    metal_mult = 1.0
    if signal.asset is Asset.SILVER and signal.metal_character:
        metal_mult = 1.10 if signal.metal_character in ("PRECIOUS", "INDUSTRIAL") else 1.0
    print(f"  Metal Multiplier: {metal_mult:.3f}")
    
    # Final weight
    rel = max(0.0, min(1.0, signal.relevance))
    per = max(0.05, min(1.0, signal.persistence))
    sess = max(0.5, min(1.5, signal.session_multiplier))
    
    weight = decay * rel * per * qual * sess * corr * surprise * tier * metal_mult
    print(f"  Final Weight: {weight:.6f}")
    print(f"    = decay({decay:.4f}) * rel({rel:.3f}) * per({per:.3f}) * qual({qual:.3f}) * sess({sess:.3f}) * corr({corr:.3f}) * surprise({surprise:.3f}) * tier({tier:.3f}) * metal({metal_mult:.3f})")
    
    # Magnitude
    magnitude = weight * STRENGTH_MULT.get(signal.strength, 0.45)
    print(f"  Magnitude (weight * strength_mult): {magnitude:.6f}")
    print(f"  Contribution (signed): {signal.contribution:.6f}")

def run_test():
    """Run all test headlines and print detailed output."""
    now = datetime.now(timezone.utc)
    
    # For decay test, create a signal 10 hours old
    old_time = now - timedelta(hours=10)
    
    print("=" * 80)
    print("SENTIMENT ENGINE TRACE PROBE - 25 TEST HEADLINES")
    print("=" * 80)
    
    for i, headline in enumerate(HEADLINES):
        is_decay_test = (i == 24)  # Last headline is the decay test
        pub_time = old_time if is_decay_test else now
        
        print(f"\n{'='*80}")
        print(f"HEADLINE #{i+1}: {headline}")
        print(f"{'='*80}")
        
        if is_decay_test:
            print("  [DECAY TEST - published 10 hours ago]")
        
        # Create signals for all four assets
        signals = []
        meta_info = {}
        
        for asset in [Asset.GOLD, Asset.SILVER, Asset.BITCOIN, Asset.EQUITY]:
            signal, meta = create_weighted_signal(headline, asset, pub_time)
            signals.append(signal)
            meta_info[asset] = meta
            print_signal_breakdown(signal, meta, asset, now)
        
        # Run compute() for each asset
        print(f"\n  --- COMPUTE() RESULTS ---")
        for asset in [Asset.GOLD, Asset.SILVER, Asset.BITCOIN, Asset.EQUITY]:
            asset_signals = [s for s in signals if s.asset == asset]
            result = compute(asset_signals, asset, now=now)
            
            print(f"\n  {asset.value}:")
            print(f"    Score: {result.score:.1f}")
            print(f"    Raw Score: {result.raw_score:.1f}")
            print(f"    Damped Score: {result.damped_score:.1f}")
            print(f"    Confidence: {result.confidence:.3f}")
            print(f"    Bias: {result.bias.value}")
            print(f"    Mode: {result.mode}")
            print(f"    Contributing Articles: {result.contributing_articles}")
            print(f"    Contributing Clusters: {result.contributing_clusters}")
            print(f"    Signal Count: {result.signal_count}")
            print(f"    Stale: {result.stale}")
            print(f"    Contradiction Detected: {result.contradiction_detected}")
            print(f"    Burst Bull: {result.burst_bull}, Burst Bear: {result.burst_bear}")
            print(f"    Delta 60m: {result.delta_60m:.1f}")
            print(f"    Event Risk: {result.event_risk}")
            print(f"    Note: {result.note}")
            
            if result.drivers:
                print(f"    Top Drivers:")
                for d in result.drivers:
                    print(f"      {d.contribution:+.4f}  {d.title[:60]} (weight={d.weight:.4f})")
            
            if result.conflict:
                print(f"    Conflict: {result.conflict.contribution:+.4f}  {result.conflict.title[:60]} (weight={result.conflict.weight:.4f})")
            
            if result.bull_drivers:
                print(f"    Bull Drivers:")
                for d in result.bull_drivers:
                    print(f"      {d.contribution:+.4f}  {d.title[:60]} (weight={d.weight:.4f})")
            
            if result.bear_drivers:
                print(f"    Bear Drivers:")
                for d in result.bear_drivers:
                    print(f"      {d.contribution:+.4f}  {d.title[:60]} (weight={d.weight:.4f})")

if __name__ == "__main__":
    run_test()