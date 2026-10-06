"""Scratch: does decay_weight honour now=?"""
from datetime import timedelta

from app.analysis.sentiment_engine import _signal_weight, WeightedSignal
from app.models.enums import (
    Direction, EconomicImportance, ImpactStrength, NewsCategory,
)
from app.utils import timeutils as T
from tests.fixed_clock import FIXED_NOW as NOW

pub_2 = NOW - timedelta(minutes=2)
pub_40 = NOW - timedelta(minutes=40)

print("FIXED_NOW       :", NOW)
print("age_hours(2m,now):", T.age_hours(pub_2, now=NOW))
print("age_hours(40m,now):", T.age_hours(pub_40, now=NOW))
print("age_hours(2m,None):", T.age_hours(pub_2))

from app.analysis.sentiment_engine import decay_weight
print("decay(2m, now=NOW) :", decay_weight(pub_2, NewsCategory.EQUITIES, NOW))
print("decay(40m, now=NOW):", decay_weight(pub_40, NewsCategory.EQUITIES, NOW))

bear = WeightedSignal(
    asset="BTCUSD", title="t", direction=Direction.BEARISH,
    strength=ImpactStrength.VERY_HIGH, importance=EconomicImportance.HIGH,
    published_at=pub_2, relevance=1.0, persistence=0.7,
    category=NewsCategory.EQUITIES, cluster_id="bear")
bull = WeightedSignal(
    asset="BTCUSD", title="t", direction=Direction.BULLISH,
    strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
    published_at=pub_40, relevance=0.85, persistence=0.7,
    category=NewsCategory.EQUITIES, cluster_id="b1")
print("w(bear, now=NOW):", _signal_weight(bear, NOW))
print("w(bull, now=NOW):", _signal_weight(bull, NOW))
print("w(bear, now=None):", _signal_weight(bear, None))
