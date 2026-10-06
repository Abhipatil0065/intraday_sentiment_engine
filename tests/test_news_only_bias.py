"""NEWS-ONLY bias (Task 2): price data never moves score/conf/label."""
from datetime import timedelta
from app.analysis import text_quality as tq
from app.analysis import transmission_map as tm
from app.analysis.news_classifier import heuristic_analyze
from app.analysis.sentiment_engine import (
    WeightedSignal, clear_bias_history, compute,
)
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength,
    NewsCategory,
)
from app.models.schemas import AssetImpactOut
from tests.fixed_clock import FIXED_NOW as NOW, freeze_now
from app.config import settings


def _sig(**kw):
    base = dict(asset=Asset.GOLD, title="t",
                direction=Direction.BULLISH,
                strength=ImpactStrength.HIGH,
                importance=EconomicImportance.HIGH,
                published_at=NOW - timedelta(minutes=5),
                relevance=0.8, persistence=0.7,
                category=NewsCategory.CENTRAL_BANK)
    base.update(kw)
    return WeightedSignal(**base)


def test_price_action_flag_is_constant_off(monkeypatch):
    freeze_now(monkeypatch)
    r = tm.resolve("Gold plunges 2% in early trade",
                   NewsCategory.COMMODITY_SUPPLY_DEMAND)
    assert r[Asset.GOLD].priced_in is False


def test_price_confirmation_has_no_weight_effect():
    assert tq.directional_multiplier("Gold plunges 2%") == 1.0
    assert tq.directional_multiplier("Fed holds rates steady") == 1.0


def test_same_news_identical_verdict_without_price(monkeypatch):
    freeze_now(monkeypatch)
    clear_bias_history()
    a = compute([_sig(title="bull 1"), _sig(title="bull 2")],
                Asset.GOLD, now=NOW)
    clear_bias_history()
    b = compute([_sig(title="bull 1"), _sig(title="bull 2")],
                Asset.GOLD, now=NOW)
    assert (a.score, a.confidence, a.bias) == (b.score, b.confidence,
                                               b.bias)


def test_per_impact_price_confirmation_is_constant():
    out = AssetImpactOut(asset=Asset.GOLD, direction=Direction.BULLISH,
                         strength=ImpactStrength.HIGH)
    assert out.price_confirmation == "UNAVAILABLE"
    assert heuristic_analyze("Gold plunges 2% in early trade").assets


def test_verdict_identical_across_price_settings(monkeypatch):
    freeze_now(monkeypatch)
    sigs = [_sig(title="bull 1"), _sig(title="bear 1",
                                       direction=Direction.BEARISH)]
    outs = []
    for enabled, weight in ((False, 0.10), (True, 0.10), (True, 0.90)):
        monkeypatch.setattr(settings, "price_data_enabled", enabled)
        monkeypatch.setattr(settings, "price_action_weight", weight)
        clear_bias_history()
        outs.append(compute(sigs, Asset.GOLD, now=NOW))
    assert all((o.score, o.confidence, o.bias) ==
               (outs[0].score, outs[0].confidence, outs[0].bias)
               for o in outs[1:])

