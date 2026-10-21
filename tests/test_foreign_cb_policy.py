"""
POLICY SCOPE - a foreign central bank is not a Tier-1 event for US assets.

THE INCIDENT
------------
On 2026-10-21 the gold card (XAUUSD, 16:33 UTC) carried this headline as
its single largest driver:

    "Australian Dollar hits one-mounth high as hawkish RBA gives boost"
    +0.695

+0.695 of a 2.180 bear pool: 32% of the card's entire bearish evidence,
and the fresh-Tier-1 override flipped the badge from BULLISH to BEARISH on
its own ("Bias flipped: BULLISH -> BEARISH on a fresh Tier-1 release").

Every factor in that 0.695 sat at 1.0:

    decay 0.739  x  relevance 0.88  x  persistence 0.70  x  quality 0.97
    x  session 1.50  x  tier 1.00 x CONFIRMED 1.05  x  asset_sensitivity 1.00

and every one of them was wrong for this headline:

  * relevance 0.88 came from the CATEGORY table (USD/DXY -> gold 0.88). The
    story was filed under USD/DXY because the word "Dollar" is in it - the
    RBA was never read.
  * tier 1.00 came from VERY_HIGH importance, which the keyword heuristic
    handed out for the single adjective "hawkish".
  * asset_sensitivity 1.00 came from HAWKISH_POLICY -> XAUUSD (sign -1,
    magnitude 1.0, gold's #1 ranked driver). That chain - hawkish Fed ->
    higher US real yields -> dearer gold - is a US chain. An RBA that turns
    hawkish does not raise US real yields, and a stronger AUD is not a
    stronger dollar.

THE FIX
-------
1. POLICY SCOPE (app/analysis/policy_scope.py): a central-bank stance that
   belongs to a FOREIGN central bank travels a weak channel, not the US
   rate channel the map is written for.
2. TIER: a foreign central bank's stance is capped at TIER_2, so it can
   never be a release-grade event and can never fire the fresh-Tier-1 flip
   override.
3. THE CLASSIFIER: "hawkish"/"dovish" are stance adjectives, not events.
   They no longer make a headline VERY_HIGH on their own.

The US central bank keeps the full channel: a hawkish Fed is still Tier 1
with the complete weight.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 10, 21, 16, 33, 8, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 10, 21, 15, 53, 47, tzinfo=timezone.utc)

# The exact headline from the live incident, with the body the classifier saw.
RBA_TITLE = "Australian Dollar hits one-mounth high as hawkish RBA gives boost"
RBA_BODY = (
    "AUD/USD extends its gains on Wednesday, rising 0.42% on the day to trade "
    "around 0.7025 at the time of writing. The pair records its fourth "
    "consecutive day of gains, reaching a fresh one-month high after "
    "rebounding from its lowest level since July, below 0.6900, last week."
)

# The contribution the live card showed for that headline, as a driver line.
LIVE_CONTRIBUTION = 0.6953

# The control: a US central bank doing the same thing.
FED_TITLE = "Fed signals rate hike as Powell says higher for longer"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _classified(title: str, body: str = ""):
    """The production heuristic classification of one article."""
    from app.analysis.news_classifier import heuristic_analyze

    return heuristic_analyze(title, body)


def _gold_signal(title: str, body: str = "", *, published=PUBLISHED,
                 importance=None, strength=None, relevance=None,
                 persistence=None, category=None, source_quality="TIER_2_MAJOR",
                 session_multiplier=1.62):
    """The signal the pipeline would build for the gold card."""
    from app.analysis.signal_factory import build_signal
    from app.models.enums import Asset

    analysis = _classified(title, body)
    impact = [a for a in analysis.assets if a.asset is Asset.GOLD][0]
    return build_signal(
        asset=Asset.GOLD,
        title=title,
        direction=impact.direction,
        strength=strength or impact.strength,
        importance=importance or analysis.importance,
        published_at=published,
        relevance=relevance if relevance is not None else impact.relevance,
        persistence=(persistence if persistence is not None
                     else impact.persistence),
        source_quality=source_quality,
        source_name="FXStreet",
        category=category or analysis.category,
        independent_sources=1,
        report_kind=analysis.report_kind,
        session_multiplier=session_multiplier,
        mode="heuristic",
        surprise_weight=1.0,
        speaker_tier=1.0,
    )


def _magnitude(signal, now=NOW) -> float:
    """The driver-line number: weight x strength multiplier."""
    from app.analysis.sentiment_engine import STRENGTH_MULT, _signal_weight

    return _signal_weight(signal, now) * STRENGTH_MULT.get(signal.strength, 0.45)


# ---------------------------------------------------------------------------
# 1. the incident itself
# ---------------------------------------------------------------------------
def test_rba_headline_is_not_a_tier_1_event():
    """A foreign central bank's stance is not release-grade for gold."""
    from app.analysis.sentiment_engine import event_tier

    signal = _gold_signal(RBA_TITLE, RBA_BODY)
    assert event_tier(signal) == "TIER_2", (
        "a foreign central bank's stance must not be Tier 1: Tier 1 is what "
        "carries the full multiplier, the long half-life and the flip "
        "override")


def test_rba_headline_contribution_collapses():
    """
    The live card showed +0.695 for this headline. It must now be a small
    fraction of that - it is a weak, indirect channel at best.
    """
    magnitude = _magnitude(_gold_signal(RBA_TITLE, RBA_BODY))
    assert magnitude < 0.15 * LIVE_CONTRIBUTION, (
        f"contribution {magnitude:.4f} is still a large share of the old "
        f"{LIVE_CONTRIBUTION}")


def test_rba_headline_cannot_fire_the_fresh_tier1_flip_override():
    """
    On the live card this headline flipped gold BULLISH -> BEARISH on the
    fresh-Tier-1 override, with the note "Bias flipped: BULLISH -> BEARISH
    on a fresh Tier-1 release". That override must not fire for it - not
    even if a classifier insists the story is VERY_HIGH.
    """
    from app.analysis.sentiment_engine import Bias, _has_fresh_tier1_opposite
    from app.models.enums import EconomicImportance

    signal = _gold_signal(RBA_TITLE, RBA_BODY)
    # Even if some classifier still calls this VERY_HIGH, the scope rules
    # must keep it from being a Tier-1 flip trigger.
    signal.importance = EconomicImportance.VERY_HIGH
    assert not _has_fresh_tier1_opposite([signal], Bias.BULLISH, NOW), (
        "a foreign central bank must not fire the fresh-Tier-1 flip override")

    # The control: a US central bank still may.
    fed = _gold_signal(FED_TITLE)
    assert _has_fresh_tier1_opposite([fed], Bias.BULLISH, NOW), (
        "a US central bank must keep the fresh-Tier-1 flip authority")


def test_rba_headline_never_reports_a_tier1_flip():
    """The end-to-end symptom: no Tier-1 flip note for this headline."""
    from app.analysis.sentiment_engine import clear_bias_history, compute
    from app.models.enums import Asset, EconomicImportance

    clear_bias_history()
    signal = _gold_signal(RBA_TITLE, RBA_BODY)
    signal.importance = EconomicImportance.VERY_HIGH

    result = compute([signal], Asset.GOLD, now=NOW)
    assert "fresh Tier-1" not in result.note, (
        f"a foreign central bank must not trigger the Tier-1 flip: {result.note}")
    assert "flipped" not in result.note, result.note


def test_rba_headline_is_not_the_dominant_driver():
    """
    The card-level symptom: the RBA headline was the TOP bear driver, ahead
    of a Kitco market report by 4x. It must not be.
    """
    from app.analysis.sentiment_engine import _aggregate

    rba = _gold_signal(RBA_TITLE, RBA_BODY)
    kitco = _gold_signal(
        "Gold gains, silver slips as oil prices rise, Fed rate hike "
        "expectations persist - Kitco AM Report",
        published=PUBLISHED - timedelta(minutes=5),
    )
    agg = _aggregate([rba, kitco], NOW)
    bear = agg["bear_items"]
    assert len(bear) == 2
    top_magnitude, top_signal = max(bear, key=lambda item: item[0])
    assert top_signal.title != RBA_TITLE, (
        "the RBA headline must not outrank a report that names the Fed")
    assert top_magnitude > 0, "sanity: the card must still have bear evidence"


# ---------------------------------------------------------------------------
# 2. the US central bank keeps the full channel
# ---------------------------------------------------------------------------
def test_fed_release_keeps_the_full_channel():
    """The control: a hawkish Fed is still Tier 1 and still weighs full."""
    from app.analysis.sentiment_engine import event_tier

    signal = _gold_signal(FED_TITLE)
    assert event_tier(signal) == "TIER_1", (
        "a US central bank headline must keep the Tier-1 treatment")
    assert _magnitude(signal) > 0.5, (
        "a US central bank headline must keep the full weight")


def test_a_headline_naming_both_central_banks_keeps_the_full_channel():
    """Naming the Fed AND the RBA is a US-policy headline for these cards."""
    from app.analysis.sentiment_engine import event_tier

    signal = _gold_signal(
        "FOMC raises rates by 50bp while the RBA holds rates steady")
    assert event_tier(signal) == "TIER_1"


# ---------------------------------------------------------------------------
# 3. the classifier
# ---------------------------------------------------------------------------
def test_hawkish_alone_is_not_very_high():
    """
    "hawkish" is a stance adjective, not an event. On its own it must not
    make a headline VERY_HIGH.
    """
    analysis = _classified("Australian Dollar extends gains as mood turns hawkish")
    assert analysis.importance.value != "VERY_HIGH", (
        "a stance adjective must not be release-grade importance")


def test_a_rate_decision_is_still_very_high():
    """The control: a decision is an event and keeps VERY_HIGH."""
    analysis = _classified("FOMC raises rates by 50bp, highest in two decades")
    assert analysis.importance.value == "VERY_HIGH"


def test_rba_only_headline_is_a_central_bank_story():
    """The classifier must recognise the RBA as a central bank."""
    from app.models.enums import NewsCategory

    analysis = _classified("RBA holds rates steady, signals hawkish shift")
    assert analysis.category is NewsCategory.CENTRAL_BANK, (
        f"got {analysis.category.value}")


# ---------------------------------------------------------------------------
# 4. the scope module
# ---------------------------------------------------------------------------
def test_central_bank_detection():
    from app.analysis.policy_scope import (foreign_policy_scope,
                                           names_foreign_central_bank,
                                           names_us_central_bank)

    assert names_us_central_bank("Fed holds rates steady")
    assert names_us_central_bank("Powell speaks on policy")
    assert not names_us_central_bank("RBA holds rates steady")

    assert names_foreign_central_bank("RBA holds rates steady")
    assert names_foreign_central_bank("Bank of Japan signals a cut")
    assert not names_foreign_central_bank("Fed holds rates steady")

    assert foreign_policy_scope("hawkish RBA gives the AUD a boost")
    assert not foreign_policy_scope("hawkish Fed gives the dollar a boost")
    assert not foreign_policy_scope("hawkish Fed and RBA")


def test_policy_scale_only_touches_policy_events():
    from app.analysis.policy_scope import (FOREIGN_CB_POLICY_SCALE,
                                           policy_event_scale)

    assert policy_event_scale("HAWKISH_POLICY", RBA_TITLE) == FOREIGN_CB_POLICY_SCALE
    assert policy_event_scale("DOVISH_POLICY", RBA_TITLE) == FOREIGN_CB_POLICY_SCALE
    assert policy_event_scale("HAWKISH_POLICY", FED_TITLE) == 1.0
    # A non-policy mechanism is read on its own terms.
    assert policy_event_scale("STRONG_USD", RBA_TITLE) == 1.0
    assert policy_event_scale("GOLD_ETF_FLOW_IN", RBA_TITLE) == 1.0


def test_the_weight_path_applies_the_scale():
    """
    The transmission magnitude that reaches the card must be the scaled one:
    a foreign central bank's stance may not read as gold's #1 driver.
    """
    from app.analysis.policy_scope import FOREIGN_CB_POLICY_SCALE
    from app.analysis.sentiment_engine import _signal_weight
    from app.models.enums import Asset, Direction, EconomicImportance, ImpactStrength

    def _weight_for(title: str) -> float:
        from app.analysis.signal_factory import build_signal
        from app.models.enums import NewsCategory, ReportKind

        return _signal_weight(
            build_signal(
                asset=Asset.GOLD, title=title, direction=Direction.BEARISH,
                strength=ImpactStrength.VERY_HIGH,
                importance=EconomicImportance.VERY_HIGH,
                published_at=PUBLISHED, relevance=0.88, persistence=0.70,
                source_quality="TIER_2_MAJOR", source_name="",
                category=NewsCategory.USD_DXY, independent_sources=1,
                report_kind=ReportKind.GENERAL, session_multiplier=1.0,
            ),
            NOW,
        )

    foreign = _weight_for(RBA_TITLE)
    domestic = _weight_for(FED_TITLE)
    assert domestic > foreign, (
        "a Fed headline must outrank a foreign central bank's headline")
    assert foreign <= domestic * FOREIGN_CB_POLICY_SCALE + 1e-9, (
        "the foreign headline must carry the scaled channel")
