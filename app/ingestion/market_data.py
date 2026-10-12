"""
Synthetic Market Data Signals via TradingView (section 12 extension).

Polls liquid, high-frequency proxies for the two biggest gold/silver drivers:
  - DXY (US Dollar Index) via TradingView TVC:DXY -> STRONG_USD / WEAK_USD
  - IEF (iShares 7-10Y Treasury Bond ETF) via NASDAQ:IEF -> REAL_YIELDS_RISING / REAL_YIELDS_FALLING

IEF price action is used as a real-yield proxy:
  - IEF price DOWN = yields UP = real yields rising = bearish gold/silver
  - IEF price UP = yields DOWN = real yields falling = bullish gold/silver

These are injected as WeightedSignal objects directly into the sentiment
recompute, so price moves *without headlines* still shift the bias.

Uses tvDatafeed (unofficial TradingView API) - free, no API key required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.analysis.sentiment_engine import WeightedSignal
from app.config import settings
from app.models.enums import Asset, Direction, ImpactStrength, NewsCategory
from app.logging_conf import get_logger

log = get_logger(__name__)

# TradingView symbols
DXY_SYMBOL = "DXY"
DXY_EXCHANGE = "TVC"
IEF_SYMBOL = "IEF"
IEF_EXCHANGE = "NASDAQ"

# Minimum absolute change (in %) to trigger a signal
DXY_MIN_CHANGE_PCT = 0.05        # 0.05% = 5 bps
IEF_MIN_CHANGE_PCT = 0.03        # 0.03% = 3 bps

# Signal weights (relevance * persistence)
DXY_RELEVANCE = 0.85
DXY_PERSISTENCE = 0.60
REAL_YIELD_RELEVANCE = 0.90
REAL_YIELD_PERSISTENCE = 0.70

# Cooldown between signals for same asset/direction (seconds)
SIGNAL_COOLDOWN_SECONDS = 300    # 5 minutes

# Track last signal time per (asset, event_type) to avoid spam
_last_signal_time: dict[tuple[str, str], datetime] = {}

# Cached tvDatafeed instance
_tv_datafeed = None


def _get_tv_datafeed():
    """Get or create tvDatafeed instance."""
    global _tv_datafeed
    if _tv_datafeed is None:
        try:
            from tvDatafeed import TvDatafeed
            _tv_datafeed = TvDatafeed()
        except Exception as exc:
            log.warning("Failed to initialize tvDatafeed: %s", exc)
            _tv_datafeed = False
    return _tv_datafeed


def _should_emit(asset: Asset, event_type: str) -> bool:
    """Rate-limit: don't emit same event_type for same asset more than once per cooldown."""
    key = (asset.value, event_type)
    now = datetime.now(timezone.utc)
    last = _last_signal_time.get(key)
    if last is None:
        _last_signal_time[key] = now
        return True
    if (now - last).total_seconds() >= SIGNAL_COOLDOWN_SECONDS:
        _last_signal_time[key] = now
        return True
    return False


def _fetch_tv_change(symbol: str, exchange: str) -> Optional[float]:
    """Return % change since last bar for a TradingView symbol, or None on error."""
    tv = _get_tv_datafeed()
    if not tv:
        return None
    
    try:
        from tvDatafeed import Interval
        # Get last 3 bars to compute change
        df = tv.get_hist(symbol=symbol, exchange=exchange, interval=Interval.in_1_minute, n_bars=3)
        if df is None or len(df) < 2:
            log.debug("Insufficient data for %s:%s (got %d bars)", symbol, exchange, len(df) if df is not None else 0)
            return None
        
        # Use last two closes
        last_close = float(df["close"].iloc[-1])
        prev_close = float(df["close"].iloc[-2])
        if prev_close == 0:
            return None
        pct_change = (last_close - prev_close) / prev_close * 100.0
        return pct_change
    except Exception as exc:
        log.debug("TradingView fetch failed for %s:%s: %s", symbol, exchange, exc)
        return None


def _fetch_dxy_change() -> Optional[float]:
    """Return DXY % change since last poll."""
    return _fetch_tv_change(DXY_SYMBOL, DXY_EXCHANGE)


def _fetch_ief_change() -> Optional[float]:
    """Return IEF (7-10Y Treasury ETF) % change since last poll.
    
    IEF price UP = yields DOWN = real yields FALLING = bullish gold
    IEF price DOWN = yields UP = real yields RISING = bearish gold
    """
    return _fetch_tv_change(IEF_SYMBOL, IEF_EXCHANGE)


@dataclass
class MarketDataSignal:
    """One synthetic signal derived from market data."""
    asset: Asset
    event_type: str              # e.g., "STRONG_USD", "REAL_YIELDS_RISING"
    direction: Direction
    strength: ImpactStrength
    relevance: float
    persistence: float
    reason: str
    transmission: str
    source_quality: str = "TIER_2_MAJOR"
    category: NewsCategory = NewsCategory.OTHER


def build_market_data_signals() -> list[MarketDataSignal]:
    """
    Poll DXY and IEF via TradingView, return list of MarketDataSignal objects if thresholds crossed.
    
    Called once per pipeline tick (or on a separate schedule).
    """
    signals: list[MarketDataSignal] = []
    now = datetime.now(timezone.utc)

    # --- DXY (USD strength) ---
    dxy_change = _fetch_dxy_change()
    if dxy_change is not None and abs(dxy_change) >= DXY_MIN_CHANGE_PCT:
        if dxy_change > 0:
            # DXY rising = STRONG_USD = bearish for gold/silver
            event_type = "STRONG_USD"
            direction = Direction.BEARISH
            reason = f"DXY rose {dxy_change:.2f}% (TradingView TVC:DXY)"
        else:
            # DXY falling = WEAK_USD = bullish for gold/silver
            event_type = "WEAK_USD"
            direction = Direction.BULLISH
            reason = f"DXY fell {abs(dxy_change):.2f}% (TradingView TVC:DXY)"

        for asset in (Asset.GOLD, Asset.SILVER):
            if _should_emit(asset, event_type):
                signals.append(MarketDataSignal(
                    asset=asset,
                    event_type=event_type,
                    direction=direction,
                    strength=ImpactStrength.HIGH,
                    relevance=DXY_RELEVANCE,
                    persistence=DXY_PERSISTENCE,
                    reason=reason,
                    transmission=event_type,
                    category=NewsCategory.USD_DXY,
                ))

    # --- IEF (Real yield proxy via 7-10Y Treasury) ---
    ief_change = _fetch_ief_change()
    if ief_change is not None and abs(ief_change) >= IEF_MIN_CHANGE_PCT:
        if ief_change < 0:
            # IEF falling = yields rising = real yields RISING = bearish gold/silver
            event_type = "REAL_YIELDS_RISING"
            direction = Direction.BEARISH
            reason = f"IEF (7-10Y Treasury ETF) fell {abs(ief_change):.2f}% -> yields rising (TradingView NASDAQ:IEF)"
        else:
            # IEF rising = yields falling = real yields FALLING = bullish gold/silver
            event_type = "REAL_YIELDS_FALLING"
            direction = Direction.BULLISH
            reason = f"IEF (7-10Y Treasury ETF) rose {ief_change:.2f}% -> yields falling (TradingView NASDAQ:IEF)"

        for asset in (Asset.GOLD, Asset.SILVER):
            if _should_emit(asset, event_type):
                signals.append(MarketDataSignal(
                    asset=asset,
                    event_type=event_type,
                    direction=direction,
                    strength=ImpactStrength.HIGH,
                    relevance=REAL_YIELD_RELEVANCE,
                    persistence=REAL_YIELD_PERSISTENCE,
                    reason=reason,
                    transmission=event_type,
                    category=NewsCategory.TREASURY_YIELDS,
                ))

    if signals:
        log.info("Market data signals generated: %s", [f"{s.asset.value}:{s.event_type}:{s.direction.value}" for s in signals])

    return signals


def market_data_signals_to_weighted(signals: list[MarketDataSignal]) -> list[WeightedSignal]:
    """Convert MarketDataSignal list to WeightedSignal list for pipeline injection."""
    from app.analysis.signal_factory import build_signal
    from app.models.enums import ReportKind

    weighted: list[WeightedSignal] = []
    for s in signals:
        weighted.append(build_signal(
            asset=s.asset,
            title=f"Market Data: {s.event_type}",
            direction=s.direction,
            strength=s.strength,
            importance=ImpactStrength.HIGH,
            published_at=datetime.now(timezone.utc),
            relevance=s.relevance,
            persistence=s.persistence,
            source_quality=s.source_quality,
            source_name="TradingView",
            category=s.category,
            session_multiplier=1.0,
            is_primary=True,
            event_key=f"market_data_{s.event_type}_{s.asset.value}",
            cluster_id=f"market_data_{s.event_type}_{s.asset.value}",
            reason=s.reason,
            transmission=s.transmission,
            report_kind=ReportKind.ACTUAL,
            mode="market_data",
        ))
    return weighted