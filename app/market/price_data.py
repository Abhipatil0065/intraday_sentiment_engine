"""
PRICE-ACTION CONFIRMATION (section 12).

News is not price. When market data is available this compares the news
direction against the actual reaction. When it is not, it returns an
explicit UNAVAILABLE verdict - the engine never invents prices.

Coverage:
   - BITCOIN-> Coinbase BTC-USD spot (the historical crypto reference,
               kept unchanged).
   - GOLD / SILVER: no spot feed (the TradingView/tvDatafeed leg was
               retired), so they read UNAVAILABLE and the engine relies on
               the news flow alone.

Every failure path below ends in an UNAVAILABLE reading, never in a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import Asset, Direction, PriceConfirmation

log = get_logger(__name__)

UNAVAILABLE_NOTE = "Price-action confirmation unavailable."

# Coinbase spot endpoint - free and keyless, but BTC-only.
_COINBASE_PRODUCTS = {Asset.BITCOIN: "BTC-USD"}


def _products_base() -> str:
    """
    The base URL for Coinbase's ``/products/...`` endpoints.

    The configured ``PRICE_API_BASE_URL`` points at the v2 REST API
    (``https://api.coinbase.com/v2``), but the ticker and stats endpoints live
    on the EXCHANGE host (``https://api.exchange.coinbase.com``) with no
    version prefix. Building the products URL straight from the configured
    base therefore returned 404 for every call - which is why the
    price-action confirmation read "Price-action confirmation unavailable." on
    all 32,286 live snapshot rows, and why the outcome loop could not record
    a single price.

    Only the known Coinbase hosts are rewritten. A different configured host
    (a proxy, a mirror) is used exactly as given.
    """
    configured = (settings.price_api_base_url or "").rstrip("/")
    if configured.endswith("/v2"):
        configured = configured[:-3]
    if "api.exchange.coinbase.com" in configured:
        return configured
    if "api.coinbase.com" in configured:
        return configured.replace("api.coinbase.com",
                                  "api.exchange.coinbase.com")
    return configured


def _unwrap(payload) -> dict:
    """
    The data object from a Coinbase response.

    The v2 API wraps it in ``{"data": {...}}``; the exchange endpoints return
    the object itself. Both shapes are accepted so the reader does not depend
    on which host answered.
    """
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    if isinstance(inner, dict):
        return inner
    return payload


async def get_spot_price(asset: Asset) -> tuple[float | None, str]:
    """
    The current spot price for the asset, as ``(price, source)``.

    ``(None, "")`` when there is no feed for the asset or the fetch failed -
    never a guess. This is the price the OUTCOME LOOP records as ground
    truth, so it is a single timestamped quote rather than the 24-hour
    change the confirmation leg uses.

    Coverage mirrors ``get_price_reading``: BTC only. GOLD and SILVER have
    no reachable spot feed here, so their outcomes stay PENDING until the
    operator supplies a price series.
    """
    if not settings.price_data_enabled:
        return None, ""
    product = _COINBASE_PRODUCTS.get(asset)
    if not product:
        return None, ""
    url = f"{_products_base()}/products/{product}/ticker"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
            data = _unwrap(resp.json())
            price = float(data.get("price") or data.get("last"))
    except Exception as exc:  # noqa: BLE001 - any failure means unavailable
        log.debug("Spot price fetch failed for %s: %s", asset, exc)
        return None, ""
    if price <= 0:
        return None, ""
    return price, "Coinbase"


@dataclass
class PriceReading:
    available: bool
    confirmation: PriceConfirmation = PriceConfirmation.UNAVAILABLE
    note: str = UNAVAILABLE_NOTE
    change_pct: float | None = None
    last_price: float | None = None
    source: str = ""


async def _fetch_spot(client: httpx.AsyncClient, product: str) -> dict | None:
    """24h ticker from a public exchange endpoint."""
    url = f"{_products_base()}/products/{product}/stats"
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        return _unwrap(resp.json())
    except Exception as exc:  # noqa: BLE001 - any failure means unavailable
        log.debug("Spot fetch failed for %s: %s", product, exc)
        return None


def _note(prefix: str, change_pct: float | None,
          confirmation: PriceConfirmation) -> str:
    if change_pct is None:
        return UNAVAILABLE_NOTE
    verdict = "consistent with" if confirmation is PriceConfirmation.CONFIRMED \
        else "diverging from"
    return f"{prefix}{change_pct:+.2f}% - {verdict} the news signal."


def classify(news_direction: Direction, change_pct: float | None) -> PriceConfirmation:
    """
    Compare news direction with the realised move.

    A small threshold avoids calling a 0.05% wiggle a confirmation or a
    contradiction.
    """
    if change_pct is None or news_direction in (Direction.NEUTRAL, Direction.MIXED):
        return PriceConfirmation.UNAVAILABLE
    threshold = 0.15  # percent
    if abs(change_pct) < threshold:
        return PriceConfirmation.DIVERGENT
    if (news_direction is Direction.BULLISH and change_pct > 0) or (
            news_direction is Direction.BEARISH and change_pct < 0):
        return PriceConfirmation.CONFIRMED
    return PriceConfirmation.DIVERGENT


async def get_price_reading(asset: Asset,
                            news_direction: Direction = Direction.NEUTRAL) -> PriceReading:
    """
    Best-effort price reaction for the asset.

    BITCOIN goes to the Coinbase map (BTC-only). When no feed covers the
    instrument - or the reachable one returns nothing - the reading is
    explicitly UNAVAILABLE rather than guessed.
    """
    if not settings.price_data_enabled:
        return PriceReading(available=False)

    product = _COINBASE_PRODUCTS.get(asset)
    if not product:
        return PriceReading(
            available=False,
            note=(
                "Price-action confirmation unavailable. "
                f"No spot feed reachable for {asset.value}."
            ),
        )

    try:
        async with httpx.AsyncClient() as client:
            stats = await _fetch_spot(client, product)
    except Exception as exc:  # noqa: BLE001
        log.debug("Price lookup failed for %s: %s", asset, exc)
        return PriceReading(available=False)

    if not stats:
        return PriceReading(available=False)

    # Coinbase 24h stats expose last and open; compute the 24h change.
    try:
        last = float(stats.get("last", 0))
        open_px = float(stats.get("open", 0))
        change = ((last - open_px) / open_px * 100.0) if open_px else None
    except (TypeError, ValueError):
        change = None

    confirmation = classify(news_direction, change)
    note = _note("Coinbase ", change, confirmation)
    return PriceReading(
        available=True, confirmation=confirmation, note=note,
        change_pct=change, last_price=float(stats.get("last")) if stats.get("last") else None,
        source="Coinbase",
    )


async def enrich_asset_signals(asset: Asset, direction: Direction) -> PriceReading:
    """Convenience wrapper used by the pipeline."""
    return await get_price_reading(asset, direction)
