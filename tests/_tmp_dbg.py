"""Temporary debug for Task 1b equity path (delete after use)."""
from datetime import timedelta

import pytest

from app.analysis.equity_card import build_equity_card, region_signals
from app.analysis.sentiment_engine import WeightedSignal, _signal_weight
from app.market.session_detector import SessionName, equity_keywords
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
)
from tests.fixed_clock import FIXED_NOW as NOW, freeze_now


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    freeze_now(monkeypatch)
    yield


def test_dbg():
    def eq(direction, strength, weight, minutes_ago, cluster, title):
        return WeightedSignal(
            asset=Asset.BITCOIN, title=title, direction=direction,
            strength=strength, importance=EconomicImportance.HIGH,
            published_at=NOW - timedelta(minutes=minutes_ago),
            relevance=weight, persistence=0.7,
            category=NewsCategory.EQUITIES, cluster_id=cluster)

    sigs = [
        eq(Direction.BEARISH, ImpactStrength.VERY_HIGH, 1.0, 2, "bear",
           "Nasdaq slides as yields jump"),
        eq(Direction.BULLISH, ImpactStrength.HIGH, 0.85, 40, "b1",
           "Nasdaq climbs on earnings"),
        eq(Direction.BULLISH, ImpactStrength.HIGH, 0.85, 40, "b2",
           "S&P 500 rises on strong data"),
    ]
    print("US keywords:", equity_keywords(SessionName.US))
    print("NOW:", NOW)
    for s in sigs:
        print("  sig", s.title, s.direction, s.published_at,
              "w(NOW)=%.4f" % _signal_weight(s, NOW),
              "w(None)=%.4f" % _signal_weight(s, None))
    matched = region_signals(sigs, SessionName.US)
    print("matched:", [(s.title, s.direction.value) for s in matched])
    card = build_equity_card(sigs, now=NOW, session=SessionName.US)
    print("card score=%s raw=%s bias=%s arts=%s"
          % (card.score, card.raw_score, card.bias, card.contributing_articles))
