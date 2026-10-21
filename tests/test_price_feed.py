"""
PRICE FEED - the base URL that made every price read 404.

THE DEFECT
----------
``PRICE_API_BASE_URL`` points at Coinbase's v2 REST API
(``https://api.coinbase.com/v2``), but the ticker and stats endpoints live on
the EXCHANGE host (``https://api.exchange.coinbase.com``) with no version
prefix. Building the ``/products/...`` URL straight from the configured base
therefore returned 404 on every call.

The visible symptom was the price-action confirmation: every live
``sentiment_snapshot`` row carried "Price-action confirmation unavailable."
- 32,286 rows, every single one. The leg was not failing gracefully because
the data was missing; it was failing because the URL was wrong.

The same wrong URL is what the outcome loop's ``get_spot_price`` would have
used, so the measurement loop could not have recorded a single price either.
"""

from __future__ import annotations

import pytest


def test_the_products_base_uses_the_exchange_host():
    """The configured v2 base must be rewritten to the exchange host."""
    from app.config import settings
    from app.market.price_data import _products_base

    original = settings.price_api_base_url
    settings.price_api_base_url = "https://api.coinbase.com/v2"
    try:
        base = _products_base()
    finally:
        settings.price_api_base_url = original

    assert base == "https://api.exchange.coinbase.com", (
        "the /products/... endpoints are on the exchange host, not the v2 API")


def test_an_already_correct_base_is_left_alone():
    from app.config import settings
    from app.market.price_data import _products_base

    original = settings.price_api_base_url
    settings.price_api_base_url = "https://api.exchange.coinbase.com"
    try:
        assert _products_base() == "https://api.exchange.coinbase.com"
    finally:
        settings.price_api_base_url = original


def test_a_custom_host_is_used_exactly_as_configured():
    """A proxy or a mirror must not be rewritten."""
    from app.config import settings
    from app.market.price_data import _products_base

    original = settings.price_api_base_url
    settings.price_api_base_url = "https://prices.internal.example/v1"
    try:
        assert _products_base() == "https://prices.internal.example/v1"
    finally:
        settings.price_api_base_url = original


def test_both_response_shapes_are_accepted():
    """The v2 API wraps the payload in {"data": ...}; the exchange does not."""
    from app.market.price_data import _unwrap

    assert _unwrap({"data": {"price": "1.5"}}) == {"price": "1.5"}
    assert _unwrap({"price": "1.5"}) == {"price": "1.5"}
    assert _unwrap(None) == {}
    assert _unwrap("nonsense") == {}


def test_the_products_url_is_not_built_from_the_v2_base(monkeypatch):
    """
    The regression guard: the URL the price reader actually requests must
    not contain the v2 API host.
    """
    from app.config import settings
    from app.market import price_data

    seen: list[str] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"price": "83000.0"}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, timeout=None):
            seen.append(url)
            return _Resp()

    import httpx

    monkeypatch.setattr(price_data.httpx, "AsyncClient", lambda: _Client())
    monkeypatch.setattr(settings, "price_data_enabled", True, raising=False)

    import asyncio

    price, source = asyncio.run(
        price_data.get_spot_price(price_data.Asset.BITCOIN))

    assert price == pytest.approx(83000.0)
    assert source == "Coinbase"
    assert seen, "no price request was made"
    assert "api.exchange.coinbase.com/products/BTC-USD/ticker" in seen[0], (
        f"unexpected price URL: {seen[0]}")
