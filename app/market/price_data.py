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
    url = f"{settings.price_api_base_url.rstrip('/')}/products/{product}/stats"
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        return resp.json().get("data")
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
