"""
MARKET WRAP ENGINE (Section A).

Market WRAP describes what ACTUALLY happened during the completed or current
session: what moved, which headlines mattered, and whether momentum
strengthened or weakened. Strictly separate from MARKET_PREP.

Momentum is measured by comparing weighted flow in the recent window against
the earlier part of the session - never from price data, which is unavailable
unless explicitly enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.analysis.recency_engine import recency_weight
from app.analysis.sentiment_engine import (
    SentimentResult,
    WeightedSignal,
    compute as compute_sentiment,
)
from app.logging_conf import get_logger
from app.models.enums import Asset, Direction, ReportKind

log = get_logger(__name__)

# Signals newer than this are treated as "recent session flow" when judging
# whether momentum strengthened or weakened.
RECENT_WINDOW_HOURS = 3.0


@dataclass
class WrapResult:
    asset: Asset
    sentiment: SentimentResult
    what_changed: list[str] = field(default_factory=list)
    key_news: list[str] = field(default_factory=list)
    momentum: str = "Data unavailable."
    momentum_delta: float = 0.0
    source_headlines: list[str] = field(default_factory=list)
    note: str = ""


def _net_of(signals: Sequence[WeightedSignal]) -> float:
    """Weighted signed net (positive = bullish) for a slice of signals."""
    total = 0.0
    for s in signals:
        sign = 1.0 if s.direction is Direction.BULLISH else (
            -1.0 if s.direction is Direction.BEARISH else 0.0)
        total += sign * s.final_weight
    return total


def build(signals: Sequence[WeightedSignal], asset: Asset) -> WrapResult:
    """Build the wrap view for one asset."""
    asset_signals = [s for s in signals if s.asset is asset]
    wrap_signals = [s for s in asset_signals if s.report_kind is ReportKind.WRAP]

    explicit = bool(wrap_signals)
    # A wrap reflects the whole session's realised flow, with explicit wrap
    # reports weighted slightly above ordinary intraday headlines.
    if explicit:
        for s in wrap_signals:
            s.persistence = min(1.0, s.persistence + 0.15)
        sentiment = compute_sentiment(asset_signals, asset)
        note = "Includes today's MARKET_WRAP reports."
    else:
        sentiment = compute_sentiment(asset_signals, asset)
        note = ("No MARKET_WRAP article found today - session flow reconstructed "
                "from intraday news.")

    # ---- momentum: recent vs earlier flow ----
    recent = [s for s in asset_signals
              if recency_weight(s.published_at) >= 0.80]
    earlier = [s for s in asset_signals
               if recency_weight(s.published_at) < 0.80]

    recent_net = _net_of(recent)
    earlier_net = _net_of(earlier)
    delta = round(recent_net - earlier_net, 4)

    if not asset_signals:
        momentum = "Data unavailable."
    elif abs(delta) < 0.02:
        momentum = "Momentum broadly unchanged through the session."
    elif delta > 0:
        momentum = "Momentum strengthened on more recent flow (bullish skew)."
    else:
        momentum = "Momentum weakened on more recent flow (bearish skew)."

    key_news = [s.title for _, s in sorted(
        ((s.final_weight, s) for s in asset_signals), key=lambda x: -x[0])[:3]]

    what_changed = []
    if sentiment.net_bias > 8:
        what_changed.append(f"Net bullish flow built to +{sentiment.net_bias:.1f}")
    elif sentiment.net_bias < -8:
        what_changed.append(f"Net bearish flow built to {sentiment.net_bias:.1f}")
    else:
        what_changed.append("Flow stayed largely two-sided during the session")
    if recent:
        what_changed.append(f"{len(recent)} high-recency item(s) drove the latest move")
    if sentiment.conflicts:
        what_changed.append("Opposing signals remain unresolved")

    return WrapResult(
        asset=asset,
        sentiment=sentiment,
        what_changed=what_changed,
        key_news=key_news,
        momentum=momentum,
        momentum_delta=delta,
        source_headlines=[s.title for s in (wrap_signals or asset_signals)[:3]],
        note=note,
    )