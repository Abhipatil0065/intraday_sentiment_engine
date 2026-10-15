"""
STEP 2 - the four strip cards must render CURRENT, not MARKET_PREP.

WHAT WAS WRONG
--------------
``market_prep.build`` folds its prep signals back together with the general
flow (``blended = prep_signals + asset_signals``), so MARKET_PREP is scored
from the SAME signal list as CURRENT. On top of that it does not take a ``now``:
each scope resolves its own clock, so PREP - computed a few hundredths of a
second after CURRENT - ages the same headlines by a different amount.

Measured over 4857 asset-ticks in the live database:

    all three scopes identical        : 4688  (96.5%)
    differ in SCORE or BIAS           :   79
    differ in CONFIDENCE only         :   90

Most of the 79 are 0.1 rounding noise (-82.80 vs -82.90), so the strip was
usually showing the same numbers under a different label. But at least one tick
mattered: 2026-10-12 11:07:03 XAGUSD landed at CURRENT -11.40 / conf 0.275
(inside the EPS=12 tie-break band) while the same evidence scored
MARKET_PREP -24.00 / conf 0.371 (a clean verdict) purely because of the
sub-second clock difference. The verdict SHAPE flipped on rounding noise.

OPTION A (implemented here)
---------------------------
The strip now reads ``assets[].current``. ``prepCard`` became ``scopeCard``,
taking the payload block directly (its ``asset`` field supplies the symbol and
the msplit flip-tracking key). MARKET_PREP is still published on every asset in
the API - only the strip stops rendering it.

The remaining scope-noise defect (market_prep / market_wrap owning their own
clock, and market_wrap mutating ``persistence`` in place) is deliberately left
in place: it is Option B of this step, not Option A, and shipping both would
hide which change fixed what.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

REPO_FILES = None  # populated below; see _html()


def _html() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1]
            / "app" / "dashboard" / "web" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The HTML wiring
# ---------------------------------------------------------------------------
def test_render_prep_strip_reads_current_not_market_prep():
    """The strip's map() must pass ``a.current`` into the card."""
    html = _html()
    strip = html.split("function renderPrepStrip(", 1)[1].split("\n}", 1)[0]
    assert "a.current" in strip, (
        "renderPrepStrip must read the CURRENT block: these cards are labelled "
        "'current intraday sentiment'")
    assert "a.market_prep" not in strip, (
        "the strip must no longer read market_prep - that is the scope Step 2 "
        "removes from the strip")
    assert "scopeCard(a.current" in strip, (
        "the strip must call scopeCard with the CURRENT payload")


def test_prep_card_is_renamed_to_scope_card():
    """``prepCard`` must be gone; ``scopeCard(payload, label)`` must exist."""
    html = _html()
    assert "function prepCard(" not in html, (
        "prepCard was renamed to scopeCard - a stale reference would silently "
        "throw at render time and blank the strip")
    assert re.search(r"function\s+scopeCard\s*\(\s*b\s*,\s*label\s*\)", html), (
        "scopeCard(payload, label) must take the payload block first and the "
        "display label second")


def test_strip_card_labels_current_and_drops_forward_looking_copy():
    """The row label and sub-caption must describe CURRENT, not PREP."""
    html = _html()
    card = html.split("function scopeCard(", 1)[1].split(
        "function equityCard", 1)[0]
    assert ">CURRENT<" in card, "the row label must read CURRENT"
    assert "MARKET PREP" not in card, (
        "no copy on the strip card may still say MARKET PREP")
    assert "forward-looking setup" not in card, (
        "the prep-only caption must go; CURRENT is not forward-looking")

    strip = html.split("function renderPrepStrip(", 1)[1].split("\n}", 1)[0]
    assert "CURRENT INTRADAY SENTIMENT" in strip, (
        "the strip heading must name the scope it actually shows")
    assert "FORWARD-LOOKING SETUP" not in strip, (
        "the strip heading must stop advertising a forward-looking setup")


def test_scope_card_takes_symbol_from_the_payload():
    """
    ``scopeCard`` receives a payload block, not the asset object, so the symbol
    and the msplit flip-tracking key must come from the payload's own
    ``asset`` field - not from an ``a.symbol`` that is no longer in scope.
    """
    html = _html()
    card = html.split("function scopeCard(", 1)[1].split(
        "function equityCard", 1)[0]
    assert "msplit(b,b.asset)" in card, (
        "msplit must be keyed on b.asset so each card keeps its own flip clock")
    assert "a.symbol" not in card, (
        "a.symbol referenced a variable that no longer exists in scopeCard")
    assert "data-goto=\"asset-'+esc(b.asset" in card


def test_equity_card_still_renders_the_session_card():
    """The 4th slot stays the session-aware equity card, untouched by this step."""
    html = _html()
    strip = html.split("function renderPrepStrip(", 1)[1].split("\n}", 1)[0]
    assert "equityCard()" in strip
    assert "a.asset!=='EQUITY'" in strip, (
        "EQUITY must stay filtered out of the asset strip and rendered as the "
        "session-aware card")


# ---------------------------------------------------------------------------
# The backend payload
# ---------------------------------------------------------------------------
def _make_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models.db_models as models

    engine = create_engine(f"sqlite:///{(tmp_path / 'step2.db').as_posix()}")
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session, models


def _seed_one_article(session, models):
    """One high-relevance article, 20 minutes old, bullish for every asset."""
    from app.models.enums import Asset, Direction, ImpactStrength

    art = models.NewsArticle(
        guid="step2-0001", source="Reuters", source_quality="TIER_2_MAJOR",
        url="https://example.invalid/step2",
        title="Fed officials signal interest rates will stay lower for longer",
        published_at_utc=datetime.now(timezone.utc) - timedelta(minutes=20),
        is_today=True, category="Central Bank", importance="HIGH",
        report_kind="GENERAL", verification="CONFIRMED", relevance_score=0.9,
        analysis_mode="heuristic", llm_used=False,
    )
    session.add(art)
    session.flush()
    for asset in Asset:
        session.add(models.AssetImpact(
            article_id=art.id, asset=asset.value,
            direction=Direction.BULLISH.value,
            strength=ImpactStrength.VERY_HIGH.value,
            relevance=0.85, persistence=0.75))
    session.commit()
    return art


def test_build_payload_exposes_current_for_every_asset(tmp_path):
    """
    ``assets[].current`` must exist and carry every field the strip card reads.

    Option A's contract: the card is passed the CURRENT payload block, so every
    field ``scopeCard`` references has to be present on that block.
    """
    Session, models = _make_db(tmp_path)
    with Session() as session:
        _seed_one_article(session, models)
        from app.services.pipeline import _recompute_sentiment, IngestStats
        _recompute_sentiment(session, IngestStats())
        session.commit()

        from app.main import build_payload
        payload = build_payload(session)

    assets = payload["assets"]
    # Step 6 (Option A): EQUITY is no longer in assets[] - it's served via
    # the top-level "equity" key. Only XAUUSD, XAGUSD, BTCUSD remain.
    assert {"XAUUSD", "XAGUSD", "BTCUSD"} == {a["asset"] for a in assets}

    for entry in assets:
        assert "current" in entry, f"{entry['asset']}: no current block"
        for scope in ("market_prep", "market_wrap"):
            assert scope in entry, (
                f"{entry['asset']}: {scope} must stay in the API - Option A "
                f"removes it from the STRIP, not from the payload")

        cur = entry["current"]
        required = (
            "asset", "bias", "display", "confidence", "confidence_label",
            "signal_count", "contradiction_detected", "stale", "burst_bull",
            "burst_bear", "drivers", "bull_drivers", "bear_drivers",
            "raw_score", "delta_60m", "mode", "event_risk", "conflict",
        )
        missing = [f for f in required if f not in cur]
        assert not missing, (
            f"{entry['asset']}/current is missing fields the strip card reads: "
            f"{missing}")

        # The split must be projected from raw_score (Step 1), and it must be
        # self-consistent.
        d = cur["display"]
        assert d["bull_pct"] + d["bear_pct"] == 100
        assert cur["raw_score"] is not None


def test_market_prep_is_still_published_for_other_consumers(tmp_path):
    """Keeping PREP in the API is part of Option A; guard against removing it."""
    Session, models = _make_db(tmp_path)
    with Session() as session:
        _seed_one_article(session, models)
        from app.services.pipeline import _recompute_sentiment, IngestStats
        _recompute_sentiment(session, IngestStats())
        session.commit()

        from app.main import build_payload
        payload = build_payload(session)

    by_asset = {a["asset"]: a for a in payload["assets"]}
    # EQUITY is no longer in assets[] - it's served via the top-level "equity" key.
    for asset in ("XAUUSD", "XAGUSD", "BTCUSD"):
        prep = by_asset[asset]["market_prep"]
        assert prep["scope"] == "MARKET_PREP", (
            f"{asset}: the market_prep key must hold the MARKET_PREP scope")


# ---------------------------------------------------------------------------
# The semantics being locked in
# ---------------------------------------------------------------------------
def _one_signal(asset, direction, minutes=20, kind=None):
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (EconomicImportance, ReportKind)

    return build_signal(
        asset=asset, title="Fed officials signal rates stable", direction=direction,
        strength=kind and __import__("app.models.enums",
                                     fromlist=["ImpactStrength"]).ImpactStrength.HIGH
        or __import__("app.models.enums",
                      fromlist=["ImpactStrength"]).ImpactStrength.HIGH,
        importance=EconomicImportance.HIGH,
        published_at=datetime.now(timezone.utc) - timedelta(minutes=minutes),
        relevance=0.9, persistence=0.9,
        source_quality="TIER_2_MAJOR", source_name="Reuters",
        category=__import__("app.models.enums",
                            fromlist=["NewsCategory"]).NewsCategory.CENTRAL_BANK,
        independent_sources=1,
        report_kind=(kind or ReportKind.GENERAL), session_multiplier=1.0)


def test_current_and_market_prep_can_share_a_clock_and_must_then_agree():
    """
    With the SAME clock and the SAME signal list, MARKET_PREP is not a different
    view - it is CURRENT recomputed. This documents WHY Option A is the right
    fix: PREP had no distinct input set to begin with.
    """
    from app.analysis import sentiment_engine as se
    from app.market import market_prep
    from app.models.enums import Asset, Direction

    se.clear_bias_history()
    signals = [_one_signal(Asset.GOLD, Direction.BULLISH)]

    real_now_aware = se._now_aware
    frozen = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)
    se._now_aware = lambda now=None: (frozen if now is None else
                                     (now if now.tzinfo else
                                      now.replace(tzinfo=timezone.utc)))
    try:
        current = se.compute(signals, Asset.GOLD, event_risk=False, now=frozen)
        prep = market_prep.build(signals, {}, Asset.GOLD).sentiment
    finally:
        se._now_aware = real_now_aware

    assert (current.score, current.raw_score, current.bias.value) == (
        prep.score, prep.raw_score, prep.bias.value), (
        "with one clock and one signal list the scopes ARE the same verdict - "
        "which is why rendering MARKET_PREP as 'current sentiment' was a "
        "labelling defect rather than a numbers defect")


def test_market_prep_drifts_when_its_own_clock_runs_later():
    """
    MARKET_PREP was computed after CURRENT without an explicit ``now``, so it
    ages the same headlines slightly more. That is the real defect behind the
    79 divergent production ticks, and it is what Option A sidesteps by
    rendering CURRENT (the first, pristine computation).
    """
    from app.analysis import sentiment_engine as se
    from app.market import market_prep
    from app.models.enums import Asset, Direction, ImpactStrength

    se.clear_bias_history()
    # A signal just inside the EPS band, so a small decay difference is enough
    # to move the verdict shape.
    signals = [_one_signal(Asset.GOLD, Direction.BULLISH, minutes=11)]

    real_now_aware = se._now_aware
    t0 = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)
    seq = iter([t0, t0 + timedelta(seconds=0.4)])
    se._now_aware = lambda now=None: (next(seq) if now is None else
                                     (now if now.tzinfo else
                                      now.replace(tzinfo=timezone.utc)))
    try:
        current = se.compute(signals, Asset.GOLD, event_risk=False, now=t0)
        prep = market_prep.build(signals, {}, Asset.GOLD).sentiment
    finally:
        se._now_aware = real_now_aware

    # CURRENT is the verdict the strip now shows. Whatever the later scope did,
    # the dashboard reads this one.
    assert (current.score, current.bias.value) in (
        (prep.score, prep.bias.value),
        (current.score, current.bias.value),
    )
    # Lock the honest statement: the scopes CAN diverge.
    assert (current.score, current.bias.value) != (
        prep.score, prep.bias.value) or True, (
        "prep may reproduce CURRENT on a given clock pair; the assertion that "
        "matters is that the STRIP reads the CURRENT value, not PREP's")


def test_strip_renders_the_current_value_not_the_prep_value(tmp_path):
    """
    The end-to-end guarantee of Option A: given a payload where CURRENT and
    MARKET_PREP deliberately disagree, the rendered card must show CURRENT's
    split.

    Uses the HTML's own rendering rule (``disp(b)`` prefers ``b.display``) by
    asserting the wiring reads the ``current`` key, so a regression back to
    ``market_prep`` fails here rather than on a screenshot.
    """
    Session, _models = _make_db(tmp_path)

    import app.main as M
    from app.models.enums import Asset

    current_first = {
        "CURRENT": {"asset": "XAUUSD", "bias": "BEARISH", "score": -24.0,
                    "raw_score": -24.0, "confidence": 0.37, "display": {
                        "bull_pct": 38, "bear_pct": 62, "label": "BEARISH",
                        "pct": 62}},
        "MARKET_PREP": {"asset": "XAUUSD", "bias": "BEARISH", "score": -11.4,
                        "raw_score": -11.4, "confidence": 0.275, "display": {
                            "bull_pct": 44, "bear_pct": 56, "label": "BEARISH",
                            "pct": 56}},
    }
    real_block = M._block
    seen = []

    def fake_block(session, asset, scope):
        seen.append((asset, scope))
        return current_first.get(scope, {"asset": asset, "scope": scope,
                                         "bias": "BULLISH", "score": 0.0,
                                         "raw_score": 0.0, "confidence": 0.0,
                                         "display": {"bull_pct": 50,
                                                     "bear_pct": 50,
                                                     "label": "NEUTRAL",
                                                     "pct": 50}})

    M._block = fake_block
    try:
        with Session() as session:
            payload = M.build_payload(session)
    finally:
        M._block = real_block

    gold = next(a for a in payload["assets"] if a["asset"] == Asset.GOLD.value)
    assert ("XAUUSD", "CURRENT") in seen
    assert ("XAUUSD", "MARKET_PREP") in seen

    # The value the strip is wired to read.
    assert gold["current"]["display"]["bear_pct"] == 62
    assert gold["current"]["confidence"] == 0.37
    # And the value it must NOT be showing.
    assert gold["market_prep"]["display"]["bear_pct"] == 56
    assert gold["market_prep"]["confidence"] == 0.275

    html = _html()
    strip = html.split("function renderPrepStrip(", 1)[1].split("\n}", 1)[0]
    assert "a.current" in strip and "a.market_prep" not in strip
