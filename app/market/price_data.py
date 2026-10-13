"""
PRICE-ACTION CONFIRMATION (section 12).

News is not price. When market data is available this compares the news
direction against the actual reaction. When it is not, it returns an
explicit UNAVAILABLE verdict - the engine never invents prices.

Coverage:
  - GOLD   -> TradingView OANDA:XAUUSD via tvDatafeed (free, keyless, the
              same symbols+exchange the CoinGlass scraper already trusts
              for its spot resolution).
  - SILVER -> TradingView OANDA:XAGUSD, same path.
  - BITCOIN-> Coinbase BTC-USD spot (the historical crypto reference,
              kept unchanged).

Both legs degrade independently: the TradingView flag can be switched off
(PRICE_TV_ENABLED=false) without losing the BTC feed and vice versa. Every
failure path below ends in an UNAVAILABLE reading, never in a guess.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.logging_conf import get_logger
from app.models.enums import Asset, Direction, PriceConfirmation

log = get_logger(__name__)

UNAVAILABLE_NOTE = "Price-action confirmation unavailable."

# Coinbase spot endpoint - free and keyless, but BTC-only.
_COINBASE_PRODUCTS = {Asset.BITCOIN: "BTC-USD"}

# TradingView (exchange, symbol) pairs for the metals. OANDA is the
# convention already validated by the CoinGlass scraper's spot resolver.
TV_PAIRS: dict[Asset, tuple[str, str]] = {
    Asset.GOLD: ("OANDA", "XAUUSD"),
    Asset.SILVER: ("OANDA", "XAGUSD"),
}

# Bars fetched per quote: 1-minute bars, so the change is measured from the
# window's FIRST OPEN to the last close - a "last 30 minutes" move, not a
# 1-minute tick (which would almost always sit inside classify()'s 0.15%
# noise band and read as DIVERGENT).
TV_WINDOW_BARS = 30

# tvDatafeed is synchronous: one fetch per call, so the client is cached.
# Handshake failures are retried after this cooldown instead of being
# cached forever.
_TV_RETRY_COOLDOWN_SECONDS = 60.0
_TV_LOCK = threading.Lock()
_tv_client: object | None = None
_tv_unavailable_until: float = 0.0


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


def _get_tv_client() -> object | None:
    """
    Cached tvDatafeed instance, or None while unavailable.

    One instance is reused on purpose: every ``TvDatafeed()`` call opens its
    own websocket and TradingView caps concurrent connections, so building a
    client per quote would eventually fail with "no free connection". A
    failure is not sticky - the next retry is only deferred by
    ``_TV_RETRY_COOLDOWN_SECONDS``.
    """
    global _tv_client, _tv_unavailable_until
    with _TV_LOCK:
        if _tv_client is not None:
            return _tv_client
        if time.monotonic() < _tv_unavailable_until:
            return None
        try:
            from tvDatafeed import TvDatafeed

            _tv_client = TvDatafeed()
        except Exception as exc:  # noqa: BLE001 - init failure is expected
            log.debug("tvDatafeed unavailable: %s", exc)
            _tv_unavailable_until = time.monotonic() + _TV_RETRY_COOLDOWN_SECONDS
            return None
        return _tv_client


def _fetch_tv_quote(exchange: str, symbol: str) -> tuple[float, float] | None:
    """
    (last_price, window change %) from TradingView, or None.

    BLOCKING I/O (tvDatafeed is synchronous) - call via ``asyncio.to_thread``
    from the async path. The tvDatafeed logger is muted: a missing quote is
    an ordinary, non-fatal condition here.
    """
    tv = _get_tv_client()
    if tv is None:
        return None
    try:
        from tvDatafeed import Interval

        # tvDatafeed logs its connection errors at ERROR; that noise is
        # already handled by the unavailable path below.
        tv_logger = logging.getLogger("tvDatafeed")
        previous = tv_logger.level
        tv_logger.setLevel(logging.CRITICAL)
        try:
            df = tv.get_hist(symbol=symbol, exchange=exchange,
                             interval=Interval.in_1_minute,
                             n_bars=TV_WINDOW_BARS)
        finally:
            tv_logger.setLevel(previous)
        if df is None or not len(df):
            return None
        last = float(df["close"].iloc[-1])
        first_open = float(df["open"].iloc[0])
        if first_open <= 0 or last <= 0:
            return None
        return last, (last - first_open) / first_open * 100.0
    except Exception as exc:  # noqa: BLE001 - any failure means unavailable
        log.debug("TradingView quote failed for %s:%s: %s", exchange, symbol, exc)
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

    GOLD/SILVER go to TradingView first (see ``TV_PAIRS``); every other
    asset falls through to the Coinbase map (BTC-only). When no feed covers
    the instrument - or the reachable one returns nothing - the reading is
    explicitly UNAVAILABLE rather than guessed.
    """
    if not settings.price_data_enabled:
        return PriceReading(available=False)

    tv_pair = TV_PAIRS.get(asset)
    if tv_pair and settings.price_tv_enabled:
        exchange, symbol = tv_pair
        try:
            quote = await asyncio.to_thread(_fetch_tv_quote, exchange, symbol)
        except Exception as exc:  # noqa: BLE001 - a worker crash is the same as no data
            log.debug("TradingView lookup failed for %s: %s", asset, exc)
            quote = None
        if quote is not None:
            last_price, change = quote
            confirmation = classify(news_direction, change)
            return PriceReading(
                available=True,
                confirmation=confirmation,
                note=_note(f"TradingView {exchange}:{symbol} ", change,
                           confirmation),
                change_pct=change,
                last_price=last_price,
                source=f"TradingView {exchange}:{symbol}",
            )

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
