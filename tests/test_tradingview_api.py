"""Tests for TradingView API news source and shared utilities."""

from __future__ import annotations

import os
import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from app.ingestion.tradingview_api import (
    fetch_tradingview_news_api,
    TradingViewAPISource,
    get_current_session,
    get_equity_symbols,
    map_internal_to_tv,
    EQUITY_SESSION_SYMBOLS,
)
from app.ingestion.base import parse_source_config
from app.market.session_detector import SessionName


# Sample API response matching the real TradingView news-mediator API
SAMPLE_API_RESPONSE = {
    "items": [
        {
            "paywall": True,
            "id": "tag:reuters.com,2026:newsml_L1N4660RM:0",
            "title": "Swiss gold exports rose 6% in September as shipments to UK surged",
            "published": 1792513758,
            "urgency": 2,
            "permission": "headline",
            "relatedSymbols": [
                {"symbol": "OANDA:XAUUSD", "logoid": "metal/gold"}
            ],
            "storyPath": "/news/reuters.com,2026:newsml_L1N4660RM:0-swiss-gold-exports-rose-6-in-september-as-shipments-to-uk-surged/",
            "provider": {
                "id": "reuters",
                "name": "Reuters",
                "logo_id": "reuters"
            }
        },
        {
            "paywall": False,
            "id": "te_news:593481:0",
            "title": "Gold Slips as Rising Oil Prices Fuel Rate Hike Expectations",
            "published": 1792502905,
            "urgency": 2,
            "permission": "preview",
            "link": "https://tradingeconomics.com/commodity/gold",
            "relatedSymbols": [
                {"symbol": "OANDA:XAUUSD", "logoid": "metal/gold"}
            ],
            "storyPath": "/news/te_news:593481:0-gold-slips-as-rising-oil-prices-fuel-rate-hike-expectations/",
            "provider": {
                "id": "trading-economics",
                "name": "Trading Economics",
                "logo_id": "te"
            }
        },
        {
            "paywall": True,
            "id": "tag:reuters.com,2026:newsml_L1N4660OS:0",
            "title": "Gold steady as investors await fed rate signals",
            "published": 1792510771,
            "urgency": 2,
            "permission": "headline",
            "relatedSymbols": [
                {"symbol": "OANDA:XAUUSD", "logoid": "metal/gold"},
                {"symbol": "TVC:GOLD", "logoid": "metal/gold"}
            ],
            "storyPath": "/news/reuters.com,2026:newsml_L1N4660OS:0-gold-steady-as-investors-await-fed-rate-signals/",
            "provider": {
                "id": "reuters",
                "name": "Reuters",
                "logo_id": "reuters"
            }
        }
    ],
    "pagination": {"cursor": "..."}
}

EMPTY_API_RESPONSE = {"items": [], "pagination": {}}


def test_fetch_tradingview_news_api_parses_all_fields():
    """Test that all required fields are correctly parsed from API response."""
    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_API_RESPONSE
        mock_session.get.return_value = mock_response
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD"], max_items=10)

        assert len(items) == 3
        
        # Check first item
        item = items[0]
        assert item["news_id"] == "tag:reuters.com,2026:newsml_L1N4660RM:0"
        assert item["title"] == "Swiss gold exports rose 6% in September as shipments to UK surged"
        assert item["url"] == "https://www.tradingview.com/news/reuters.com,2026:newsml_L1N4660RM:0-swiss-gold-exports-rose-6-in-september-as-shipments-to-uk-surged/"
        assert item["published_at"] == "2026-10-20T16:29:18+00:00"
        assert item["source"] == "Reuters"
        assert item["symbols"] == ["OANDA:XAUUSD"]
        assert item["is_flash"] is True  # urgency >= 2

        # Check second item (preview, no paywall)
        item = items[1]
        assert item["news_id"] == "te_news:593481:0"
        assert item["source"] == "Trading Economics"
        assert item["is_flash"] is True

        # Check third item (multiple symbols)
        item = items[2]
        assert item["symbols"] == ["OANDA:XAUUSD", "TVC:GOLD"]


def test_fetch_tradingview_news_api_published_at_conversion():
    """Test Unix timestamp to ISO 8601 UTC conversion."""
    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_API_RESPONSE
        mock_session.get.return_value = mock_response
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD"])

        # Verify all published_at are valid ISO 8601 UTC
        for item in items:
            assert item["published_at"] is not None
            # Parse back to verify
            dt = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
            assert dt.tzinfo is not None
            assert dt.tzinfo.utcoffset(dt) == timezone.utc.utcoffset(dt)

        # Specific check for known timestamp
        # 1792513758 = 2026-10-20T16:29:18+00:00
        assert items[0]["published_at"] == "2026-10-20T16:29:18+00:00"
        # 1792502905 = 2026-10-20T13:28:25+00:00
        assert items[1]["published_at"] == "2026-10-20T13:28:25+00:00"
        # 1792510771 = 2026-10-20T15:39:31+00:00
        assert items[2]["published_at"] == "2026-10-20T15:39:31+00:00"


def test_fetch_tradingview_news_api_deduplication_across_symbols():
    """Test that duplicate news_ids across symbols are deduplicated."""
    # Clear cache before test to avoid interference from other tests
    from app.ingestion.tradingview_api import _cache
    _cache.clear()
    # Two symbols returning overlapping items
    response_symbol1 = {
        "items": [
            {
                "id": "news_1",
                "title": "Shared News",
                "published": 1792513758,
                "urgency": 2,
                "relatedSymbols": [{"symbol": "OANDA:XAUUSD"}],
                "storyPath": "/news/shared/",
                "provider": {"name": "Reuters"}
            },
            {
                "id": "news_2",
                "title": "Symbol 1 Only",
                "published": 1792510771,
                "urgency": 1,
                "relatedSymbols": [{"symbol": "OANDA:XAUUSD"}],
                "storyPath": "/news/symbol1/",
                "provider": {"name": "Reuters"}
            }
        ],
        "pagination": {}
    }
    response_symbol2 = {
        "items": [
            {
                "id": "news_1",  # Same ID - should be deduplicated
                "title": "Shared News",
                "published": 1792513758,
                "urgency": 2,
                "relatedSymbols": [{"symbol": "OANDA:XAGUSD"}],
                "storyPath": "/news/shared/",
                "provider": {"name": "Reuters"}
            },
            {
                "id": "news_3",
                "title": "Symbol 2 Only",
                "published": 1792502905,
                "urgency": 1,
                "relatedSymbols": [{"symbol": "OANDA:XAGUSD"}],
                "storyPath": "/news/symbol2/",
                "provider": {"name": "Trading Economics"}
            }
        ],
        "pagination": {}
    }

    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        # First call for XAUUSD, second for XAGUSD
        mock_response1 = Mock()
        mock_response1.status_code = 200
        mock_response1.json.return_value = response_symbol1
        
        mock_response2 = Mock()
        mock_response2.status_code = 200
        mock_response2.json.return_value = response_symbol2
        
        mock_session.get.side_effect = [mock_response1, mock_response2]
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD", "OANDA:XAGUSD"], max_items=10)

        # Should have 3 unique items (news_1, news_2, news_3)
        assert len(items) == 3
        news_ids = {item["news_id"] for item in items}
        assert news_ids == {"news_1", "news_2", "news_3"}


def test_fetch_tradingview_news_api_empty_response():
    """Test that empty response returns empty list."""
    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = EMPTY_API_RESPONSE
        mock_session.get.return_value = mock_response
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD"])
        assert items == []


def test_fetch_tradingview_news_api_http_error():
    """Test that HTTP 500 returns empty list."""
    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 500
        mock_session.get.return_value = mock_response
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD"])
        assert items == []


def test_fetch_tradingview_news_api_request_exception():
    """Test that request exception returns empty list."""
    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        import requests
        mock_session.get.side_effect = requests.RequestException("Connection failed")
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD"])
        assert items == []


def test_fetch_tradingview_news_api_max_items_limit():
    """Test that max_items limits results."""
    many_items = {
        "items": [
            {"id": f"news_{i}", "title": f"News {i}", "published": 1792513758 + i,
             "urgency": 2, "relatedSymbols": [{"symbol": "OANDA:XAUUSD"}],
             "storyPath": f"/news/item{i}/", "provider": {"name": "Reuters"}}
            for i in range(20)
        ],
        "pagination": {}
    }

    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = many_items
        mock_session.get.return_value = mock_response
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD"], max_items=5)
        assert len(items) == 5


def test_fetch_tradingview_news_api_splits_budget_across_symbols():
    """Every symbol must get its share of the budget.

    The first symbol's 30-item window used to fill the whole cap, so the
    later symbols (silver, crypto) were never fetched at all.
    """
    def _payload(prefix: str, symbol: str) -> dict:
        return {
            "items": [
                {"id": f"{prefix}_{i}", "title": f"{prefix} {i}",
                 "published": 1792513758 + i, "urgency": 2,
                 "relatedSymbols": [{"symbol": symbol}],
                 "storyPath": f"/news/{prefix}{i}/",
                 "provider": {"name": "Reuters"}}
                for i in range(30)
            ],
            "pagination": {},
        }

    symbols = ["OANDA:XAUUSD", "OANDA:XAGUSD", "BINANCE:BTCUSD"]
    payloads = [_payload("gold", symbols[0]), _payload("silver", symbols[1]),
                _payload("btc", symbols[2])]

    from app.ingestion.tradingview_api import _cache
    _cache.clear()

    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        mock_session.get.side_effect = [
            Mock(status_code=200, json=Mock(return_value=p)) for p in payloads
        ]
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(symbols, max_items=30)

    # 30 total, split 10 / 10 / 10.
    assert len(items) == 30
    counts = {}
    for item in items:
        key = item["news_id"].split("_")[0]
        counts[key] = counts.get(key, 0) + 1
    assert counts == {"gold": 10, "silver": 10, "btc": 10}


def test_fetch_tradingview_news_api_falls_back_when_streaming_fails():
    """A streaming failure must not lose the symbol."""
    def _payload(prefix: str) -> dict:
        return {
            "items": [
                {"id": f"{prefix}_0", "title": f"{prefix} 0", "published": 1792513758,
                 "urgency": 2, "relatedSymbols": [{"symbol": "OANDA:XAUUSD"}],
                 "storyPath": f"/news/{prefix}0/", "provider": {"name": "Reuters"}}
            ],
            "pagination": {},
        }

    from app.ingestion.tradingview_api import _cache
    _cache.clear()

    with patch("app.ingestion.tradingview_api._get_session") as mock_get_session:
        mock_session = Mock()
        mock_session.get.side_effect = [
            Mock(status_code=503, json=Mock(return_value={})),
            Mock(status_code=200, json=Mock(return_value=_payload("gold"))),
        ]
        mock_get_session.return_value = mock_session

        items = fetch_tradingview_news_api(["OANDA:XAUUSD"], max_items=30)

    assert len(items) == 1
    assert items[0]["news_id"] == "gold_0"


def test_tradingview_api_source_wrapper():
    """Test the TradingViewAPISource wrapper class."""
    spec = parse_source_config("TradingView API News|tradingview_api|https://www.tradingview.com/news/|1.0|TIER_1_PRIMARY")[0]
    source = TradingViewAPISource(spec)

    assert source.name == "TradingView API News"
    assert source.quality == "TIER_1_PRIMARY"
    assert source.kind == "tradingview_api"
    assert source.available() is True


def test_tradingview_api_source_equity_symbols():
    """Test that equity source uses session-aware symbols."""
    spec = parse_source_config("TradingView API News|tradingview_api|https://www.tradingview.com/news/|1.0|TIER_1_PRIMARY")[0]
    # The wrapper detects equity based on "equity" in name (case-insensitive)
    source = TradingViewAPISource(spec)
    assert source._is_equity is False  # "TradingView API News" doesn't contain "equity"
    
    # Test with equity in name
    spec2 = parse_source_config("TradingView Equity News|tradingview_api|https://www.tradingview.com/news/|1.0|TIER_1_PRIMARY")[0]
    source2 = TradingViewAPISource(spec2)
    assert source2._is_equity is True


# --- Shared function tests (moved from test_tradingview.py) ---

def test_symbol_mapping():
    """Test internal to TradingView symbol mapping."""
    # Test default mapping
    internal = ["XAUUSD", "XAGUSD", "BTCUSD"]
    mapped = map_internal_to_tv(internal)
    # BITSTAMP:BTCUSD is the symbol TradingView actually TAGS bitcoin stories
    # with: measured against the live API on 2026-10-22, 29 of the 30 items
    # returned by that query carry the tag. BINANCE:BTCUSD carried it on NONE
    # of them (0/30), so the bitcoin feed was never bitcoin-tagged.
    assert mapped == ["OANDA:XAUUSD", "OANDA:XAGUSD", "BITSTAMP:BTCUSD"]

    # Test with override via environment
    os.environ["TRADINGVIEW_SYMBOL_MAP"] = "XAUUSD=FX_IDC:XAUUSD,BTCUSD=BITSTAMP:BTCUSD"
    # Need to reimport to pick up new env var
    from importlib import reload
    import app.ingestion.tradingview_api as tv_module
    reload(tv_module)

    mapped2 = tv_module.map_internal_to_tv(internal)
    assert mapped2 == ["FX_IDC:XAUUSD", "OANDA:XAGUSD", "BITSTAMP:BTCUSD"]


def test_get_current_session():
    """Test get_current_session function against session detector logic."""
    # Session windows in NY time (market timezone):
    # - Asia:     20:00 (prev day) - 04:00 NY
    # - Europe:   03:00 - 12:00 NY
    # - US:       08:00 - 17:00 NY
    # - OFF_HOURS: carries forward previous session
    #
    # Overlap rule: NEWER session wins shared hours
    # - 03:00-04:00 NY: Europe wins over Asia
    # - 08:00-12:00 NY: US wins over Europe
    #
    # In Jan (EST, UTC-5): NY = UTC - 5
    # In July (EDT, UTC-4): NY = UTC - 4

    # Test with January dates (EST, UTC-5)
    test_cases_jan = [
        # (UTC hour, expected_session, description)
        (1, "asia", "01:00 UTC = 20:00 NY -> Asia"),
        (4, "asia", "04:00 UTC = 23:00 NY -> Asia"),
        (8, "europe", "08:00 UTC = 03:00 NY -> Europe (wins 03-04 overlap)"),
        (12, "europe", "12:00 UTC = 07:00 NY -> Europe"),
        (13, "us", "13:00 UTC = 08:00 NY -> US (wins 08-12 overlap)"),
        (16, "us", "16:00 UTC = 11:00 NY -> US"),
        (21, "us", "21:00 UTC = 16:00 NY -> US"),
        (22, "us", "22:00 UTC = 17:00 NY -> OFF_HOURS -> carry forward US"),
        (23, "us", "23:00 UTC = 18:00 NY -> OFF_HOURS -> carry forward US"),
        (0, "us", "00:00 UTC = 19:00 NY -> OFF_HOURS -> carry forward US"),
    ]

    for utc_hour, expected, desc in test_cases_jan:
        dt = datetime(2026, 1, 15, utc_hour, 0, 0, tzinfo=timezone.utc)
        result = get_current_session(dt)
        assert result == expected, f"{desc} -> {result} (expected {expected})"

    # Test Asia session start (20:00 NY = 01:00 UTC next day)
    dt = datetime(2026, 1, 16, 1, 0, 0, tzinfo=timezone.utc)  # 01:00 UTC = 20:00 NY
    result = get_current_session(dt)
    assert result == "asia", f"01:00 UTC (20:00 NY next day) -> {result} (expected asia)"

    # Test DST (July - EDT, UTC-4)
    # 08:00 UTC = 04:00 NY -> Europe (Asia ends at 04:00, Europe 03:00-12:00 wins overlap)
    dt = datetime(2026, 7, 15, 8, 0, 0, tzinfo=timezone.utc)  # 08:00 UTC = 04:00 NY
    result = get_current_session(dt)
    assert result == "europe", f"08:00 UTC (04:00 NY) -> {result} (expected europe - overlap Europe wins)"
    
    # 12:00 UTC = 08:00 NY -> US (wins 08-12 overlap)
    dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)  # 12:00 UTC = 08:00 NY
    result = get_current_session(dt)
    assert result == "us", f"12:00 UTC (08:00 NY) -> {result} (expected us)"

    # 07:00 UTC = 03:00 NY -> Europe
    dt = datetime(2026, 7, 15, 7, 0, 0, tzinfo=timezone.utc)  # 07:00 UTC = 03:00 NY
    result = get_current_session(dt)
    assert result == "europe", f"07:00 UTC (03:00 NY) -> {result} (expected europe)"

    # 11:00 UTC = 07:00 NY -> Europe
    dt = datetime(2026, 7, 15, 11, 0, 0, tzinfo=timezone.utc)  # 11:00 UTC = 07:00 NY
    result = get_current_session(dt)
    assert result == "europe", f"11:00 UTC (07:00 NY) -> {result} (expected europe)"

    # Checklist verification: post-market carry-forward (21:00+ UTC -> returns "us")
    # In Jan (EST, UTC-5): 21:00 UTC = 16:00 NY (US session), 22:00 UTC = 17:00 NY (OFF_HOURS -> carry US)
    # So 22:00 and 23:59 UTC should return "us"
    assert get_current_session(datetime(2025, 1, 1, 22, 0, tzinfo=timezone.utc)) == "us", "22:00 UTC should be us"
    assert get_current_session(datetime(2025, 1, 1, 23, 59, tzinfo=timezone.utc)) == "us", "23:59 UTC should be us"


def test_get_equity_symbols():
    """Test get_equity_symbols function for all sessions and invalid input."""
    # Test valid sessions (lowercase matching EQUITY_SESSION_SYMBOLS keys)
    sessions = ["asia", "europe", "us"]

    for session in sessions:
        symbols = get_equity_symbols(session)
        assert len(symbols) == 2, f"Expected 2 symbols for {session}, got {len(symbols)}"
        # Verify they're exchange-qualified
        for s in symbols:
            assert ":" in s, f"Expected exchange-qualified symbol, got {s}"

    # Test with SessionName enum
    assert get_equity_symbols(SessionName.ASIA) == get_equity_symbols("asia")
    assert get_equity_symbols(SessionName.EUROPE) == get_equity_symbols("europe")
    assert get_equity_symbols(SessionName.US) == get_equity_symbols("us")

    # Test case-insensitive
    assert get_equity_symbols("ASIA") == get_equity_symbols("asia")
    assert get_equity_symbols("Europe") == get_equity_symbols("europe")
    assert get_equity_symbols("US") == get_equity_symbols("us")

    # Test invalid session raises ValueError
    with pytest.raises(ValueError):
        get_equity_symbols("invalid")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])