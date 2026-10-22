"""
TradingView News API source (primary).

Uses the internal TradingView news-mediator API:
https://news-mediator.tradingview.com/public/news-flow/v2/news

This replaces the Apify-based scraper with a direct API call that returns
full timestamps, provider info, and related symbols.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import settings
from app.analysis.chart_scope import chart_assets_for_symbols
from app.ingestion.base import RawArticle
from app.market.session_detector import SessionName, equity_session

log = logging.getLogger(__name__)

# API endpoint
NEWS_API_BASE = "https://news-mediator.tradingview.com/public/news-flow/v2/news"

# Default query parameters - filter must be a list for multiple values
DEFAULT_FILTERS = ["lang:en"]
DEFAULT_PARAMS = {
    "client": "landing",
    # streaming=true returns each story's LATEST UPDATE time, not the time it
    # was first published. With streaming=false a story that TradingView keeps
    # updating (a live gold price piece, for example) keeps its ORIGINAL
    # timestamp and ages out of the trading-day window while it is still the
    # freshest thing on the feed. Measured 2026-10-21: the same 30 items came
    # back either way, but the newest item was 143 minutes old with
    # streaming=false and 5 minutes old with streaming=true.
    "streaming": "true",
    "user_prostatus": "non_pro",
}

# Session for connection pooling and retries
_session: requests.Session | None = None

# --- Rate limiting & caching ---
_MIN_REQUEST_INTERVAL = 0.5  # 500ms minimum between symbol requests
_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}  # (symbol, max_items) -> (timestamp, items)
_last_request_time: float = 0.0

# --- Staleness tracking ---
_staleness: dict[str, int] = {}  # symbol -> consecutive empty count

# --- Symbol mappings (shared with legacy Apify source, now consolidated here) ---
# Configurable symbol mapping from internal symbols to TradingView exchange-qualified symbols
#
# BITCOIN: BITSTAMP:BTCUSD is the symbol TradingView actually TAGS bitcoin
# stories with - measured 2026-10-22, 29 of the 30 items returned by that
# query carry the tag. The old value, BINANCE:BTCUSD, carried it on NONE of
# them (0/30): the API answered the query with loosely related stories, so
# the bitcoin feed was never bitcoin-tagged at all.
DEFAULT_SYMBOL_MAP: dict[str, str] = {
    "XAUUSD": "OANDA:XAUUSD",
    "XAGUSD": "OANDA:XAGUSD",
    "BTCUSD": "BITSTAMP:BTCUSD",
    # Crude oil (WTI). Measured live on 2026-10-22: the TVC:USOIL query
    # returns the oil chart's own stories ("Oil Gains on Saudi Oil Field
    # Attack", "Oil Wavers Below $98 as Supply Recovers"), so the oil card
    # gets a real per-chart feed and not just the general oil headlines.
    "USOIL": "TVC:USOIL",
}

# Equity session symbol mapping
# These symbols use the format expected by TradingView news-mediator API:
# NASDAQ:SYMBOL for NASDAQ-listed stocks, AMEX:SYMBOL for ETFs like SPY
EQUITY_SESSION_SYMBOLS: dict[str, list[str]] = {
    "asia": ["NASDAQ:TSLA", "NASDAQ:NVDA"],
    "europe": ["NASDAQ:AAPL", "NYSE:JPM"],
    "us": ["NASDAQ:MSFT", "AMEX:SPY"],
}


def get_symbol_map() -> dict[str, str]:
    """Get symbol mapping, allowing override from environment."""
    symbol_map = DEFAULT_SYMBOL_MAP.copy()
    raw = getattr(settings, "tradingview_symbol_map", "") or os.environ.get("TRADINGVIEW_SYMBOL_MAP", "")
    if raw:
        for chunk in raw.split(","):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                symbol_map[k.strip().upper()] = v.strip()
    return symbol_map


def map_internal_to_tv(symbols: list[str]) -> list[str]:
    """Map internal symbols (XAUUSD, XAGUSD, BTCUSD) to TradingView format."""
    symbol_map = get_symbol_map()
    mapped = []
    for s in symbols:
        mapped.append(symbol_map.get(s.upper(), s))
    return mapped


def get_equity_symbols(session: str | SessionName) -> list[str]:
    """
    Get representative TradingView symbols for the given equity trading session.

    Args:
        session: Trading session name - "asia", "europe", or "us" (case-insensitive)
                 or a SessionName enum value

    Returns:
        List of exchange-qualified TradingView symbols for that session

    Raises:
        ValueError: If session is not one of the known sessions
    """
    if isinstance(session, SessionName):
        session_key = session.value.lower()
    else:
        session_key = session.lower().strip()
    
    if session_key not in EQUITY_SESSION_SYMBOLS:
        raise ValueError(
            f"Unknown session: {session!r}. Expected one of: {list(EQUITY_SESSION_SYMBOLS.keys())}"
        )
    return EQUITY_SESSION_SYMBOLS[session_key]


def get_current_session(utc_now: datetime | None = None) -> str:
    """
    Determine the current equity trading session using the project's session detector.

    Uses the existing `equity_session()` from app.market.session_detector which:
    - Operates in the market timezone (NY by default)
    - Handles session overlaps with newer session winning
    - Returns previous session during OFF_HOURS (carry-forward behavior)
    - Handles DST automatically via zoneinfo

    Session windows in NY time (market timezone):
    - Asia:     20:00 (prev day) - 04:00
    - Europe:   03:00 - 12:00
    - US:       08:00 - 17:00
    - OFF_HOURS: carries forward previous session

    Args:
        utc_now: Optional datetime in UTC. Defaults to now.

    Returns:
        Session name: "asia", "europe", or "us" (lowercase, matching EQUITY_SESSION_SYMBOLS keys)
    """
    session = equity_session(utc_now)
    return session.value.lower()


def _get_session() -> requests.Session:
    """Get or create a requests session with retry logic."""
    global _session
    if _session is None:
        _session = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
    return _session


def _enforce_rate_limit() -> None:
    """Enforce minimum 500ms gap between consecutive requests."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _get_cached(symbol: str, max_items: int) -> list[dict] | None:
    """Get cached result if within 5-minute TTL."""
    key = (symbol, max_items)
    if key in _cache:
        timestamp, items = _cache[key]
        if time.time() - timestamp < 300:  # 5 minutes = 300 seconds
            log.debug("Cache HIT for %s (max_items=%d)", symbol, max_items)
            return items
        else:
            del _cache[key]
    log.debug("Cache MISS for %s (max_items=%d)", symbol, max_items)
    return None


def _set_cache(symbol: str, max_items: int, items: list[dict]) -> None:
    """Store result in cache with current timestamp."""
    _cache[(symbol, max_items)] = (time.time(), items)


def get_staleness_report() -> dict[str, int]:
    """Return consecutive empty fetch counts per symbol."""
    return _staleness.copy()


def _record_empty(symbol: str) -> None:
    """Increment empty counter for a symbol, log warning at 3."""
    _staleness[symbol] = _staleness.get(symbol, 0) + 1
    if _staleness[symbol] == 3:
        log.warning(
            "TradingView API: symbol %s has returned empty results 3 consecutive times",
            symbol,
        )


def _record_success(symbol: str) -> None:
    """Reset empty counter on successful fetch."""
    if symbol in _staleness:
        del _staleness[symbol]


def _map_item(item: dict) -> dict:
    """Raw API item -> internal schema."""
    from datetime import timezone
    return {
        "news_id": item.get("id"),
        "title": item.get("title", ""),
        "url": "https://www.tradingview.com" + item.get("storyPath", ""),
        "published_at": datetime.fromtimestamp(
            item["published"], tz=timezone.utc).isoformat()
            if item.get("published") else None,
        "source": item.get("provider", {}).get("name", "TradingView"),
        # The story's OWN TradingView tags (relatedSymbols). Kept for the
        # audit trail and the feed badge - they are NOT the routing key:
        # TradingView cross-tags freely (16 of the 30 stories on the gold
        # chart also carry a silver-futures tag), so routing by tag would
        # push half the gold feed onto the silver card. The routing key is
        # ``origin_symbols`` below - the charts that actually LISTED the
        # story.
        "symbols": [rs["symbol"] for rs in item.get("relatedSymbols", [])
                    if rs.get("symbol")],
        "origin_symbols": [],
        "is_flash": item.get("urgency", 0) >= 2,
    }


def fetch_tradingview_news_api(symbols: list[str], max_items: int = 30) -> list[dict]:
    """
    Fetch news from TradingView news-mediator API.

    The budget is split EVENLY across the requested symbols. The API returns
    up to 30 items per symbol call, and the first symbol alone fills that
    window, so an early break on "max_items reached" starves every later
    symbol: with [XAUUSD, XAGUSD, BTCUSD] the source delivered gold headlines
    and NOTHING for silver or crypto, and the engine's XAGUSD/BTCUSD cards
    never saw a TradingView line. Each symbol now gets max_items // len(symbols)
    of the budget (10 of 30 for three symbols).

    Args:
        symbols: List of TradingView exchange-qualified symbols (e.g., ["OANDA:XAUUSD", "BINANCE:BTCUSD"])
        max_items: Maximum number of news items to return in total

    Returns:
        List of mapped news items with keys:
        news_id, title, url, published_at, source, symbols, origin_symbols,
        is_flash

    ORIGIN SYMBOLS. ``origin_symbols`` lists every chart query that returned
    the story. A story found by both the gold and the silver query is listed
    on BOTH charts, so it belongs to both cards (TradingView's own
    semantics). The duplicate is therefore MERGED into the first copy's
    origin list rather than dropped - dropping it would lose the fact that
    the story is on the silver chart too.
    """
    session = _get_session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    all_items: list[dict] = []
    # news_id -> the mapped item, so a story returned by several chart
    # queries keeps every origin instead of being silently dropped.
    by_id: dict[str, dict] = {}
    per_symbol = max(1, max_items // max(1, len(symbols)))

    def _take(item: dict, origin: str) -> None:
        """Add a story, or merge its origin when it is already known."""
        news_id = item.get("id")
        if not news_id:
            return
        existing = by_id.get(news_id)
        if existing is not None:
            if origin not in existing["origin_symbols"]:
                existing["origin_symbols"].append(origin)
            return
        mapped = _map_item(item)
        mapped["origin_symbols"] = [origin]
        by_id[news_id] = mapped
        all_items.append(mapped)

    for symbol in symbols:
        if len(all_items) >= max_items:
            break
        taken_for_symbol = 0
        # Check cache first
        cached = _get_cached(symbol, max_items)
        if cached is not None:
            for item in cached:
                if taken_for_symbol >= per_symbol:
                    break
                before = len(all_items)
                _take(item, symbol)
                if len(all_items) > before:
                    taken_for_symbol += 1
            continue

        # Enforce rate limit before making request
        _enforce_rate_limit()

        # Build params with multiple filter values (API expects multiple filter params)
        params = DEFAULT_PARAMS.copy()
        params["filter"] = DEFAULT_FILTERS + [f"symbol:{symbol}"]

        try:
            log.debug("Fetching TradingView news for symbol=%s", symbol)
            resp = session.get(NEWS_API_BASE, params=params, headers=headers, timeout=10)

            if resp.status_code != 200:
                # The streaming feed is the fresh one; if it is unavailable for
                # any reason, fall back to the non-streaming window rather than
                # losing the symbol entirely.
                log.warning(
                    "TradingView API error for %s: HTTP %d - retrying without "
                    "streaming", symbol, resp.status_code)
                fallback_params = dict(params)
                fallback_params["streaming"] = "false"
                try:
                    resp = session.get(NEWS_API_BASE, params=fallback_params,
                                       headers=headers, timeout=10)
                except requests.RequestException:
                    _record_empty(symbol)
                    continue
                if resp.status_code != 200:
                    log.warning("TradingView API error for %s: HTTP %d",
                                symbol, resp.status_code)
                    _record_empty(symbol)
                    continue

            data = resp.json()
            items = data.get("items", [])

            # Cache the raw items for potential reuse
            _set_cache(symbol, max_items, items)

            if not items:
                _record_empty(symbol)
                continue

            _record_success(symbol)

            for item in items:
                if taken_for_symbol >= per_symbol:
                    break
                before = len(all_items)
                _take(item, symbol)
                if len(all_items) > before:
                    taken_for_symbol += 1

        except requests.RequestException as exc:
            log.warning("TradingView API request failed for %s: %s", symbol, exc)
            _record_empty(symbol)
            continue
        except Exception as exc:
            log.exception("Unexpected error fetching TradingView news for %s", symbol)
            _record_empty(symbol)
            continue

    log.info("Fetched %d unique news items from TradingView API (symbols=%s, per_symbol=%d)",
             len(all_items), symbols, per_symbol)
    return all_items[:max_items]


class TradingViewAPISource:
    """
    NewsSource-compatible wrapper for TradingView API news.

    This is the primary TradingView news source (replaces Apify-based scraper).
    Uses the news-mediator API which returns full timestamps and metadata.
    """

    kind = "tradingview_api"

    #: Items each CHART query gets. The fetch budget is split evenly across
    #: the queried charts, so the TOTAL is ``PER_CHART_ITEMS * len(symbols)``.
    #: The per-chart share is fixed on purpose: adding the oil chart must not
    #: take items away from the gold / silver / bitcoin charts.
    PER_CHART_ITEMS = 10

    def __init__(self, spec=None):
        self.name = spec.name if spec else "TradingView API News"
        self.quality = spec.quality if spec else "TIER_1_PRIMARY"
        self.url = spec.url if spec else "https://www.tradingview.com/news/"
        self.max_items = 30
        # Determine if this is an equity session-aware source
        self._is_equity = spec and "equity" in spec.name.lower() if spec else False

    async def fetch(self) -> list[RawArticle]:
        """Fetch and convert to RawArticle objects.

        CHART SCOPE. Every symbol queried here is a CHART, and the stories a
        chart query returns are the stories listed on that chart. The origin
        symbols of each story are therefore carried into the article as
        ``chart_assets`` - the set of cards that story may touch. A story
        returned by both the gold and the silver query is on both charts and
        reaches both cards; a story only the gold query returned reaches the
        gold card alone.
        """
        # Determine symbols based on source type
        if self._is_equity:
            session = get_current_session()
            symbols = get_equity_symbols(session)
            log.info("TradingView API Equity: fetching for session=%s symbols=%s", session, symbols)
        else:
            # Default to our three assets mapped to TradingView format
            symbols = map_internal_to_tv(settings.supported_assets)
            log.info("TradingView API: fetching for symbols=%s", symbols)

        items = fetch_tradingview_news_api(
            symbols, self.PER_CHART_ITEMS * len(symbols))
        articles = []
        for item in items:
            origins = item.get("origin_symbols") or []
            chart_assets = chart_assets_for_symbols(origins, equity=self._is_equity)
            # Use news_id as GUID for deduplication
            article = RawArticle(
                guid=item["news_id"],
                source=self.name,
                source_quality=self.quality,
                url=item["url"],
                title=item["title"],
                body="",  # API doesn't provide full body
                summary=item["title"][:300],
                published_raw=item["published_at"],
                symbols=list(item.get("symbols") or []),
                chart_assets=[a.value for a in chart_assets],
            ).ensure_guid()
            articles.append(article)
        return articles

    @staticmethod
    def available() -> bool:
        """API source is always available (no external token needed)."""
        return True