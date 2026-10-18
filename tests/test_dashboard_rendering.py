"""
Dashboard rendering tests (Change 8).

These tests assert on the dashboard's JavaScript source (app/dashboard/web/
index.html) because the card rendering functions (scopeCard, equityCard,
renderFinancialJuice, renderFeed) run in the browser, not on the server. A
pure Python test cannot execute client-side JS, so we verify the source
contains the expected rendering logic instead of scraping live DOM output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "app" / "dashboard" / "web" / "index.html").read_text(encoding="utf-8")


def _has(pattern: str) -> None:
    assert pattern in HTML, f"expected pattern not found in index.html: {pattern!r}"


def _fn_slice(name: str) -> str:
    """Return the source slice from ``function name(`` up to the next top-level
    ``function `` declaration (or end of file)."""
    start = HTML.index(f"function {name}(")
    # Find the next "function " at the start of a line after this point.
    rest = HTML[start:]
    nxt = rest.find("\nfunction ")
    end = start + nxt if nxt != -1 else len(HTML)
    return HTML[start:end]


def _fn_has(name: str, pattern: str) -> None:
    assert pattern in _fn_slice(name), (
        f"expected pattern {pattern!r} not found in function {name}")


# ---------------------------------------------------------------------------
# Change 2: raw_score + bull/bear on bias cards
# ---------------------------------------------------------------------------
def test_scope_card_shows_raw_score():
    """scopeCard renders scoreLine with raw_score, bull, bear after .mfoot."""
    _fn_has("scopeCard", "var scoreLine")
    _fn_has("scopeCard", "n1(b.raw_score)")
    _fn_has("scopeCard", "n1(b.bull || 0)")
    _fn_has("scopeCard", "n1(b.bear || 0)")
    _fn_has("scopeCard", 'class="mscore"')
    # scoreLine must render after the .mfoot div within scopeCard's return.
    body = _fn_slice("scopeCard")
    mfoot_open = body.index('<div class="mfoot"')
    score_usage = body.index("+scoreLine")
    assert score_usage > mfoot_open, "scoreLine must render after .mfoot in scopeCard"

    # equityCard also renders scoreLine.
    _fn_has("equityCard", "var scoreLine")
    _fn_has("equityCard", "n1(e.raw_score)")
    _fn_has("equityCard", "n1(e.bull || 0)")
    _fn_has("equityCard", "n1(e.bear || 0)")


def test_equity_card_uses_bull_bear_drivers():
    """equityCard renders both BULLISH DRIVERS and BEARISH DRIVERS sections."""
    _fn_has("equityCard", "mnote('bull','BULLISH DRIVERS',e.bull_drivers,3)")
    _fn_has("equityCard", "mnote('bear','BEARISH DRIVERS',e.bear_drivers,3)")


# ---------------------------------------------------------------------------
# Change 4: event_risk_note text
# ---------------------------------------------------------------------------
def test_scope_card_shows_event_risk_note():
    """scopeCard renders event_risk_note below the score line when event_risk."""
    _fn_has("scopeCard", "var riskNote")
    _fn_has("scopeCard", "b.event_risk_note")
    _fn_has("scopeCard", 'class="msub risk-note"')
    # riskNote must render after scoreLine within scopeCard.
    body = _fn_slice("scopeCard")
    score_idx = body.index("var scoreLine")
    risk_idx = body.index("var riskNote")
    assert risk_idx > score_idx, "riskNote must be defined after scoreLine"


# ---------------------------------------------------------------------------
# Change 5: burst chip
# ---------------------------------------------------------------------------
def test_burst_chip_when_burst_gte_3():
    """scopeCard renders a BURST chip when burst_bull or burst_bear >= 3."""
    _fn_has("scopeCard", "var burstChip")
    _fn_has("scopeCard", "b.burst_bull")
    _fn_has("scopeCard", "b.burst_bear")
    _fn_has("scopeCard", ">= 3")
    _fn_has("scopeCard", "BURST")
    _fn_has("scopeCard", 'class="burst"')


# ---------------------------------------------------------------------------
# Change 6: FJ section hidden when empty
# ---------------------------------------------------------------------------
def test_fj_section_hidden_when_no_reports():
    """renderFinancialJuice hides the header when no box is found."""
    _has('id="fjHeader"')
    _fn_has("renderFinancialJuice", "var anyFound")
    _fn_has("renderFinancialJuice", "d.boxes.some(function(b){ return b.found; })")
    _fn_has("renderFinancialJuice", "header.style.display = 'none'")
    _fn_has("renderFinancialJuice", "box.innerHTML = ''")
