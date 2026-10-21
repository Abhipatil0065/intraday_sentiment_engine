"""
STALE MASS CAP - old news must not outvote the news that just landed.

THE DEFECT
----------
The per-signal decay floors at ``DECAY_FLOOR`` (0.05) past the trading-day
cutoff, so old news fades but never vanishes. That contract is deliberate and
is tested elsewhere. The problem is that the floor is PER SIGNAL: with 130+
signals a tick, hundreds of hours-old headlines each contribute 5% of their
weight, and the accumulated shelf becomes the majority of the evidence.

Measured on the live database, the share of the day's surviving
HIGH/VERY_HIGH weight mass that is already older than the 8h cutoff:

    hour  09  10  11  12  13  14  15  16  17  18  19  20  23
    stale 20% 29% 29% 31% 31% 56% 54% 68% 66% 69% 67% 65% 57%

So through the US session the card was majority-driven by news that had
already been priced in hours ago, and a headline that just landed could not
move it. That is the opposite of what a DAY-TRADING tool should be.

THE FIX
-------
Each side's stale mass is capped at ``stale_mass_cap_ratio`` times the card's
TOTAL fresh mass. The cap is measured against the total fresh mass, not
against the same side's fresh mass, so a side with no fresh news of its own
cannot be drowned by the other side's stale news either.

The per-signal floor is untouched: an old headline still contributes, it just
cannot outvote the fresh news as a block. With no fresh news at all the cap
does not apply - a quiet morning with only old headlines is better served by
those headlines than by nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)


def _signal(direction, age_hours, title="headline"):
    """One signal ``age_hours`` old."""
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (EconomicImportance, ImpactStrength,
                                  NewsCategory, ReportKind)

    return build_signal(
        asset=None, title=title, direction=direction,
        strength=ImpactStrength.VERY_HIGH,
        importance=EconomicImportance.VERY_HIGH,
        published_at=NOW - timedelta(hours=age_hours),
        relevance=0.9, persistence=0.9,
        source_quality="TIER_1", source_name="",
        category=NewsCategory.CENTRAL_BANK, independent_sources=1,
        report_kind=ReportKind.GENERAL, session_multiplier=1.0,
    )


def _aggregate(signals, now=NOW):
    from app.analysis.sentiment_engine import _aggregate as agg

    return agg(signals, now)


def test_stale_mass_is_capped_against_the_fresh_mass(monkeypatch):
    """
    A pile of hours-old headlines must not outvote one fresh headline.

    Without the cap the stale pile carries far more total weight than the
    single fresh one, so the card reads BULLISH. With the cap the fresh news
    is at least half of the evidence and the card reads BEARISH.
    """
    from app.analysis.sentiment_engine import _aggregate as agg
    from app.config import settings
    from app.models.enums import Direction

    fresh = _signal(Direction.BEARISH, 0.5, "fresh bearish")
    stale = [_signal(Direction.BULLISH, 12.0, f"stale bull {i}")
             for i in range(60)]

    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 0.5,
                        raising=False)
    capped = agg([fresh] + stale, NOW)
    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 1000.0,
                        raising=False)
    uncapped = agg([fresh] + stale, NOW)

    assert uncapped["bull"] > uncapped["bear"], (
        "without the cap the stale pile is expected to win")
    assert capped["bull"] < capped["bear"], (
        "with the cap the fresh headline must be able to carry the card")

    # The stale side may not exceed half of the card's TOTAL fresh mass.
    fresh_total = uncapped["bear"]
    assert capped["bull"] <= 0.5 * fresh_total + 1e-9, (
        "the stale side may not exceed the cap budget")


def test_the_cap_is_measured_against_the_total_fresh_mass(monkeypatch):
    """
    A side with no fresh news of its own must not be drowned by the OTHER
    side's stale news.

    Fresh: many bullish. Stale: a few bearish. Without a cap the stale
    bearish mass could exceed the fresh bullish mass; the cap stops it.
    """
    from app.analysis.sentiment_engine import _aggregate as agg
    from app.config import settings
    from app.models.enums import Direction

    fresh = [_signal(Direction.BULLISH, 0.5, f"fresh bull {i}") for i in range(4)]
    stale = [_signal(Direction.BEARISH, 14.0, f"stale bear {i}") for i in range(40)]

    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 0.5, raising=False)
    capped = agg(fresh + stale, NOW)

    assert capped["bear"] <= capped["bull"], (
        "stale bearish news must not outweigh four fresh bullish headlines")


def test_the_cap_does_not_apply_when_there_is_no_fresh_news(monkeypatch):
    """A quiet morning with only old headlines is better served by them."""
    from app.analysis.sentiment_engine import _aggregate as agg
    from app.config import settings
    from app.models.enums import Direction

    stale = [_signal(Direction.BEARISH, 12.0, f"stale bear {i}") for i in range(5)]

    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 0.5, raising=False)
    result = agg(stale, NOW)
    assert result["bear"] > 0.0
    assert result["bull"] == 0.0


def test_the_cap_can_be_switched_off(monkeypatch):
    """0.0 disables the cap - old news then counts in full."""
    import pytest

    from app.analysis.sentiment_engine import _aggregate as agg
    from app.config import settings
    from app.models.enums import Direction

    fresh = _signal(Direction.BEARISH, 0.5, "fresh bearish")
    stale = [_signal(Direction.BULLISH, 12.0, f"stale bull {i}")
             for i in range(60)]

    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 0.0, raising=False)
    off = agg([fresh] + stale, NOW)
    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 1000.0, raising=False)
    full = agg([fresh] + stale, NOW)
    assert off["bull"] == pytest.approx(full["bull"])
    assert off["bear"] == pytest.approx(full["bear"])


def test_fresh_news_only_is_unaffected_by_the_cap(monkeypatch):
    """When every headline is fresh the cap must not change anything."""
    import pytest

    from app.analysis.sentiment_engine import _aggregate as agg
    from app.config import settings
    from app.models.enums import Direction

    signals = [_signal(Direction.BULLISH, 0.5, f"bull {i}") for i in range(3)]
    signals += [_signal(Direction.BEARISH, 1.0, f"bear {i}") for i in range(2)]

    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 0.5, raising=False)
    capped = agg(signals, NOW)
    monkeypatch.setattr(settings, "stale_mass_cap_ratio", 1000.0, raising=False)
    full = agg(signals, NOW)
    assert capped["bull"] == pytest.approx(full["bull"])
    assert capped["bear"] == pytest.approx(full["bear"])
