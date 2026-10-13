"""GOLD/SILVER price-action confirmation over TradingView (section 12)."""
from app.market import price_data as pd
from app.models.enums import Asset, Direction, PriceConfirmation
from app.config import settings


def _use_tv(monkeypatch, quote, calls=None):
    """Swap the blocking TradingView fetch for a canned quote."""
    monkeypatch.setattr(settings, "price_data_enabled", True)
    monkeypatch.setattr(settings, "price_tv_enabled", True)

    def fake(exchange, symbol):
        if calls is not None:
            calls.append((exchange, symbol))
        return quote

    monkeypatch.setattr(pd, "_fetch_tv_quote", fake)


async def test_gold_confirms_bullish_news(monkeypatch):
    _use_tv(monkeypatch, (2400.50, 0.52))
    r = await pd.get_price_reading(Asset.GOLD, Direction.BULLISH)
    assert r.available is True
    assert r.confirmation is PriceConfirmation.CONFIRMED
    assert r.last_price == 2400.50
    assert r.change_pct == 0.52
    assert r.source == "TradingView OANDA:XAUUSD"
    assert "TradingView OANDA:XAUUSD" in r.note
    assert "consistent with" in r.note


async def test_gold_diverges_from_bullish_news(monkeypatch):
    _use_tv(monkeypatch, (2380.00, -0.61))
    r = await pd.get_price_reading(Asset.GOLD, Direction.BULLISH)
    assert r.available is True
    assert r.confirmation is PriceConfirmation.DIVERGENT
    assert "diverging from" in r.note


async def test_silver_uses_oanda_silver_pair(monkeypatch):
    calls = []
    _use_tv(monkeypatch, (28.90, -0.35), calls)
    r = await pd.get_price_reading(Asset.SILVER, Direction.BEARISH)
    assert calls == [("OANDA", "XAGUSD")]
    assert r.available is True
    assert r.confirmation is PriceConfirmation.CONFIRMED
    assert r.source == "TradingView OANDA:XAGUSD"


async def test_tv_outage_leaves_reading_unavailable(monkeypatch):
    _use_tv(monkeypatch, None)
    r = await pd.get_price_reading(Asset.GOLD, Direction.BULLISH)
    assert r.available is False
    assert r.confirmation is PriceConfirmation.UNAVAILABLE
    assert pd.UNAVAILABLE_NOTE in r.note


async def test_tv_leg_disabled_skips_tradingview(monkeypatch):
    calls = []
    _use_tv(monkeypatch, (2400.50, 0.52), calls)
    monkeypatch.setattr(settings, "price_tv_enabled", False)
    r = await pd.get_price_reading(Asset.GOLD, Direction.BULLISH)
    assert calls == []
    assert r.available is False


async def test_master_switch_off(monkeypatch):
    _use_tv(monkeypatch, (2400.50, 0.52))
    monkeypatch.setattr(settings, "price_data_enabled", False)
    r = await pd.get_price_reading(Asset.GOLD, Direction.BULLISH)
    assert r.available is False
    assert pd.UNAVAILABLE_NOTE in r.note


async def test_btc_still_uses_coinbase(monkeypatch):
    monkeypatch.setattr(settings, "price_data_enabled", True)
    monkeypatch.setattr(settings, "price_tv_enabled", True)

    async def fake_spot(client, product):
        assert product == "BTC-USD"
        return {"last": "95000.0", "open": "94000.0"}

    monkeypatch.setattr(pd, "_fetch_spot", fake_spot)
    r = await pd.get_price_reading(Asset.BITCOIN, Direction.BULLISH)
    assert r.available is True
    assert r.confirmation is PriceConfirmation.CONFIRMED
    assert r.source == "Coinbase"
    assert abs(r.change_pct - 1.0638) < 0.01


def test_classify_threshold_unchanged():
    # Moves inside the 0.15% noise band are DIVERGENT, not confirmations.
    assert pd.classify(Direction.BULLISH, 0.10) is PriceConfirmation.DIVERGENT
    assert pd.classify(Direction.BULLISH, None) is PriceConfirmation.UNAVAILABLE
    assert pd.classify(Direction.NEUTRAL, 1.0) is PriceConfirmation.UNAVAILABLE


def test_tv_quote_parses_bars():
    """The bar parser turns first-open -> last-close into a % change."""
    import pandas as pd_

    class FakeTv:
        def get_hist(self, symbol, exchange, interval, n_bars):
            return pd_.DataFrame(
                {"open": [100.0, 101.0], "close": [100.5, 101.25]},
            )

    monkeyed = pd._tv_client
    pd._tv_client = FakeTv()
    try:
        last, change = pd._fetch_tv_quote("OANDA", "XAUUSD")
    finally:
        pd._tv_client = monkeyed
    assert last == 101.25
    assert abs(change - 1.25) < 1e-9
