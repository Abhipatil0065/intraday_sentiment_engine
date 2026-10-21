"""
THE EQUITY CARD'S EVIDENCE LINE - the payload must publish what the card reads.

The dashboard's ``equityCard()`` draws the evidence line

    score -60.9 - bull 0.5 - bear 2.3

ONLY when the payload carries ``raw_score``:

    (e.raw_score !== null && e.raw_score !== undefined) ? line : ''

The asset cards get that key from ``main._block()`` (``"raw_score": _display_raw``).
The equity card is built by ``equity_card.to_payload()`` instead, and
``to_payload()`` did not publish it - so the gate failed, the line was never
drawn, and the bull/bear totals went unprinted with it. Every other card showed
the line; the equity card silently showed nothing.

THE TWO HALVES OF THE CONTRACT WERE ASSERTED APART
---------------------------------------------------
``tests/test_task2_bull_bear.py`` asserted that ``to_payload`` publishes
``bull`` and ``bear``; ``tests/test_dashboard_rendering.py`` asserted that the
JavaScript READS ``e.raw_score``. Neither test made the two halves MEET, so
the gap was invisible to the suite. The first test below is that missing test:
it reads the dashboard's own ``equityCard()`` source, extracts every
``e.<field>`` the function consumes, and requires the payload to publish each
one.

NO EVIDENCE IS NOT A ZERO LEAN
------------------------------
A card with nothing behind it - no regional news at all, or every matched
signal carrying zero relevance - has no captured lean. Publishing ``0.0``
would print "score 0.0 - bull 0.0 - bear 0.0" and read as a MEASURED even
split rather than the absence of one, so the payload publishes ``None`` for
those cards and the dashboard's own null gate skips the line. A genuine even
split with evidence behind it (bull == bear > 0) still publishes ``0.0``,
because that one IS measured.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.analysis.equity_card import (
    EquityCardResult,
    build_equity_card,
    neutral_card,
    to_payload,
)
from app.analysis.sentiment_engine import clear_bias_history, payload_score
from app.analysis.signal_factory import build_signal
from app.market.session_detector import SessionName
from app.models.enums import (
    Asset,
    Direction,
    EconomicImportance,
    ImpactStrength,
    NewsCategory,
)
from tests.fixed_clock import FIXED_NOW as NOW

HTML_PATH = (Path(__file__).resolve().parents[1]
             / "app" / "dashboard" / "web" / "index.html")


@pytest.fixture(autouse=True)
def _isolated():
    """The engine's bias history is process-global; keep each test independent."""
    clear_bias_history()
    yield
    clear_bias_history()


def _us_signal(title: str, direction: Direction = Direction.BULLISH):
    """A headline that reaches the US region (matches a US session keyword)."""
    return build_signal(
        asset=Asset.BITCOIN, title=title, direction=direction,
        strength=ImpactStrength.HIGH, importance=EconomicImportance.HIGH,
        published_at=NOW, relevance=0.7, persistence=0.6,
        category=NewsCategory.EQUITIES)


def _live_payload():
    """A real equity card built from real signals, as the payload sees it."""
    card = build_equity_card(
        [_us_signal("Apple beats earnings estimates as revenue surges")],
        now=NOW, session=SessionName.US)
    return card, to_payload(card)


# ---------------------------------------------------------------------------
# The payload must carry the key the dashboard's gate looks for
# ---------------------------------------------------------------------------
def test_equity_card_payload_publishes_raw_score():
    """to_payload must publish raw_score, or the evidence line never renders."""
    card, payload = _live_payload()

    assert "raw_score" in payload, (
        "to_payload must publish raw_score - the dashboard's equityCard() "
        "draws its evidence line only when that key is present, so a payload "
        "without it renders no line and no bull/bear totals either")
    assert isinstance(payload["raw_score"], float), (
        f"raw_score must be a number, got {payload['raw_score']!r}")
    assert payload["raw_score"] == pytest.approx(card.raw_score), (
        "the published raw lean must be the card's own raw lean")
    assert payload["bull"] > 0.0, "the fixture must reach the card with evidence"
    assert payload["bear"] >= 0.0


def test_published_score_is_the_label_consistent_twin_of_raw_score():
    """The payload publishes the SAME pair the asset cards publish.

    ``score`` is the label-consistent twin (payload_score flips the sign so the
    number cannot contradict the badge); ``raw_score`` is the un-flipped net
    lean. The asset cards publish both, and the dashboard prints the raw one on
    the evidence line - so the equity card publishes the same pair.
    """
    card, payload = _live_payload()

    assert payload["score"] == pytest.approx(
        payload_score(card.raw_score, card.bias)), (
        "the published score must be the label-consistent twin of the raw lean")
    assert payload["score"] == pytest.approx(card.score)


# ---------------------------------------------------------------------------
# The contract test that was missing: the two halves must MEET
# ---------------------------------------------------------------------------
def test_every_field_the_equity_card_reads_is_published():
    """Every ``e.<field>`` the dashboard's equityCard() consumes must exist.

    This is the test that would have caught the missing ``raw_score``: the
    JavaScript and the Python were asserted separately, so the key could be
    absent from the payload for as long as both halves kept their own test.
    """
    html = HTML_PATH.read_text(encoding="utf-8")
    start = html.index("function equityCard(")
    rest = html[start:]
    nxt = rest.find("\nfunction ")
    body = rest[:nxt] if nxt != -1 else rest

    fields = set(re.findall(r"\be\.([A-Za-z_][A-Za-z0-9_]*)", body))
    assert fields, "the equityCard() source slice failed - no fields found"

    payload = to_payload(neutral_card(SessionName.US))
    missing = sorted(f for f in fields if f not in payload)
    assert not missing, (
        f"the equity card reads {missing} but the payload does not publish "
        "it/them - the card renders those as empty or 0. A key may be null, "
        "but it must be there.")


def test_dashboard_gate_is_the_null_check_the_payload_satisfies():
    """The card's line is gated on a null check, and the payload honours it.

    The gate in the dashboard is
    ``(e.raw_score !== null && e.raw_score !== undefined) ? line : ''``, so a
    null value is exactly what suppresses the line for a card with no
    evidence.
    """
    html = HTML_PATH.read_text(encoding="utf-8")
    assert "e.raw_score !== null && e.raw_score !== undefined" in html, (
        "the dashboard's evidence-line gate is expected to skip a null "
        "raw_score - if it changed, the no-evidence rule must be revisited")

    payload = to_payload(neutral_card(SessionName.US))
    assert payload["raw_score"] is None, (
        "the no-evidence card must publish null so the gate skips the line")


# ---------------------------------------------------------------------------
# No evidence is not a zero lean
# ---------------------------------------------------------------------------
def test_no_evidence_card_publishes_null_raw_score():
    """A card with nothing behind it publishes null, not 0.0."""
    payload = to_payload(neutral_card(SessionName.US))

    assert payload["no_data"] is True
    assert payload["raw_score"] is None, (
        "a card with no evidence has no captured lean - 0.0 would render as a "
        "MEASURED even split")
    assert payload["bull"] == 0.0 and payload["bear"] == 0.0


def test_even_split_with_evidence_publishes_zero_raw_score():
    """A measured even split is a real number and must still be published."""
    card = EquityCardResult(
        region="US", title="US EQUITY", symbol="SPX / NDX",
        score=0.0, raw_score=0.0, bull=1.5, bear=1.5,
        confidence=0.5, bias="BULLISH", no_data=False, stale=False)
    payload = to_payload(card)

    assert payload["raw_score"] == 0.0, (
        "bull == bear with evidence behind it is a MEASURED even split, so it "
        "must publish 0.0 rather than null")
    assert payload["bull"] == 1.5 and payload["bear"] == 1.5


# ---------------------------------------------------------------------------
# A raw lean that disagrees with the badge must explain itself
# ---------------------------------------------------------------------------
def test_engine_note_is_carried_to_the_card():
    """The card publishes the engine's note, so a disagreement is explained.

    A tie-break reading (|raw| < EPS) names one side while the aggregate
    leans the other. The card then shows a badge and a raw lean that point
    different ways, so the engine's note has to travel with them - the asset
    cards carry the same note, and the equity card used to drop it.
    """
    card = build_equity_card(
        [
            _us_signal("Apple beats earnings estimates as revenue surges"),
            _us_signal("Nasdaq falls as Fed signals rate hike higher for longer",
                       Direction.BEARISH),
            _us_signal("Wall Street stocks rally on strong payrolls"),
        ],
        now=NOW, session=SessionName.US)
    payload = to_payload(card)

    assert payload["raw_score"] > 0.0, (
        "the fixture must produce a reading whose aggregate leans bullish")
    assert payload["bias"] == "BEARISH", (
        "the fixture must produce a verdict that disagrees with the aggregate")
    assert payload["note"], (
        "a card whose badge and raw lean disagree must carry the engine's "
        "note - otherwise the reader sees BEARISH above a positive score "
        "with no reason why")
    assert "tie-break" in payload["note"], (
        f"expected the engine's tie-break note, got {payload['note']!r}")
