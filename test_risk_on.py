#!/usr/bin/env python3
"""
Test RISK_ON change for XAGUSD.
"""
import sys
sys.path.insert(0, r"C:\Users\HP\.vscode\intraday_sentiment_engine")

from datetime import datetime, timezone, timedelta

from app.analysis.news_classifier import heuristic_analyze
from app.analysis.signal_factory import build_signal
from app.analysis.sentiment_engine import compute, clear_bias_history
from app.analysis.transmission_map import detect_event_types
from app.models.enums import Asset, ImpactStrength, EconomicImportance, NewsCategory, ReportKind, Direction

NOW = datetime.now(timezone.utc)

HEADLINES = [
    "Stocks rally to record highs on strong risk appetite",
    "Fed hikes rates 25bp, signals higher for longer",
    "US dollar index falls to 3-month low",
]

def analyze_headline(headline: str):
    clear_bias_history()
    analysis = heuristic_analyze(headline, "")
    
    event_types = detect_event_types(headline, analysis.category)
    event_type_names = [et.value for et in event_types]
    
    print(f"=== {headline} ===")
    print(f"  Event types: {event_type_names}")
    
    for asset in [Asset.GOLD, Asset.SILVER, Asset.EQUITY]:
        aa = None
        for a in analysis.assets:
            if a.asset == asset:
                aa = a
                break
        if aa is None:
            print(f"  {asset.value}: no signal")
            continue
        
        sig = build_signal(
            asset=aa.asset,
            title=headline,
            direction=aa.direction,
            strength=aa.strength,
            importance=analysis.importance,
            published_at=NOW,
            relevance=aa.relevance,
            persistence=aa.persistence,
            source_quality="TIER_1_OFFICIAL",
            source_name="TestSource",
            category=analysis.category,
            session_multiplier=1.0,
            is_primary=True,
            event_key=f"test_{headline[:30]}",
            cluster_id="",
            independent_sources=1,
            report_kind=analysis.report_kind,
            reason=aa.reason,
            transmission=aa.transmission,
            mode="heuristic",
            surprise_weight=1.0,
            metal_character=aa.metal_character,
        )
        
        result = compute([sig], asset, now=datetime.now(timezone.utc))
        print(f"  {asset.value}: contrib={sig.contribution:.4f}, bias={result.bias.value}")
    print()

def main():
    for headline in HEADLINES:
        clear_bias_history()
        analyze_headline(headline)

if __name__ == "__main__":
    main()