"""
STEP 3 - the card must report how its verdict was classified.

``build_signal`` already had a ``mode`` parameter defaulting to "heuristic".
``_mode_for`` already aggregated it. But ``pipeline._recompute_sentiment``
never passed ``NewsArticle.analysis_mode`` through, so EVERY signal carried the
default and every card claimed a rule engine - even when the LLM had classified
every headline behind it. The only change needed was the one line of plumbing.

The classification-time contract is unchanged and is pinned here:
``heuristic_analyze`` yields ``ArticleAnalysis(mode="heuristic")`` and
``llm_analyze`` yields ``ArticleAnalysis(mode="llm")``, so those are the only
two values that ever reach ``_mode_for``.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

VALID_MODES = {"llm", "heuristic", "mixed"}


@pytest.fixture(autouse=True)
def _reset_breaker_after_each_test():
    """
    Never leave the LLM circuit breaker open.

    ``llm_client.circuit_breaker()`` is a module-level singleton that survives
    the test. Opening it here and not closing it leaked into
    ``test_pipeline_counter_invariant``, whose stub takes a different path and
    does not clear it - so the next real ``run_once`` in that file ran with the
    breaker still latched from this module.
    """
    from app.analysis.llm_client import circuit_breaker

    yield
    cb = circuit_breaker()
    try:
        asyncio.get_event_loop_policy().new_event_loop()  # noqa: F841
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(cb.reset())
            loop.close()
        except Exception:  # noqa: BLE001 - a broken loop must not fail teardown
            cb._consecutive_failures = 0
            cb._open = False


# ---------------------------------------------------------------------------
# The aggregation contract
# ---------------------------------------------------------------------------
def _signal(mode: str, title: str = "t"):
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (Asset, Direction, EconomicImportance,
                                  NewsCategory, ReportKind)

    return build_signal(
        asset=Asset.GOLD, title=title, direction=Direction.BULLISH,
        importance=EconomicImportance.HIGH, relevance=0.9, persistence=0.8,
        source_quality="TIER_2_MAJOR", source_name="Reuters",
        category=NewsCategory.CENTRAL_BANK, report_kind=ReportKind.GENERAL,
        mode=mode, session_multiplier=1.0)


@pytest.mark.parametrize("modes,expected", [
    (["llm", "llm"], "llm"),
    (["heuristic", "heuristic"], "heuristic"),
    (["llm", "heuristic"], "mixed"),
    (["heuristic", "llm"], "mixed"),
    (["llm", "llm", "heuristic"], "mixed"),
    ([], "heuristic"),
    (["", ""], "heuristic"),
])
def test_mode_for_aggregation(modes, expected):
    """All-llm -> llm, all-heuristic -> heuristic, any blend -> mixed."""
    from app.analysis.sentiment_engine import _mode_for

    assert _mode_for([_signal(m, f"h{i}") for i, m in enumerate(modes)]) == expected


def test_mode_for_defaults_to_heuristic_in_build_signal():
    """``mode`` keeps its default so a caller that omits it stays heuristic."""
    from app.analysis.signal_factory import build_signal
    from app.models.enums import (Asset, Direction, EconomicImportance,
                                  NewsCategory, ReportKind)

    sig = build_signal(
        asset=Asset.GOLD, title="t", direction=Direction.BULLISH,
        importance=EconomicImportance.HIGH, relevance=0.9, persistence=0.8,
        source_quality="TIER_2_MAJOR", category=NewsCategory.CENTRAL_BANK,
        report_kind=ReportKind.GENERAL, session_multiplier=1.0)
    assert sig.mode == "heuristic"


# ---------------------------------------------------------------------------
# The classification-time contract
# ---------------------------------------------------------------------------
def test_classifier_records_mode_on_both_paths():
    """heuristic -> 'heuristic', and the LLM path records 'llm'."""
    from app.analysis import news_classifier

    heuristic = news_classifier.heuristic_analyze(
        "Fed signals dovish pivot and rate cut")
    assert heuristic.mode == "heuristic"

    # The LLM path is checked by reading the source: a real provider call is
    # not available in a unit test, and the point is the label it stamps.
    src = _module_source(news_classifier)
    assert 'ArticleAnalysis(mode="llm")' in src, (
        "llm_analyze must stamp mode='llm' so the card can report it")
    assert 'ArticleAnalysis(mode="heuristic")' in src, (
        "heuristic_analyze must stamp mode='heuristic'")


def _module_source(module) -> str:
    from pathlib import Path

    return Path(module.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# End to end through the recompute
# ---------------------------------------------------------------------------
def _make_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models.db_models as models

    engine = create_engine(f"sqlite:///{(tmp_path / 'step3.db').as_posix()}")
    models.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, expire_on_commit=False), models


def _ingest(session, models, title, analysis_mode, guid,
            direction="BULLISH", minutes=20):
    """Write one classified article plus one impact per asset, the way the
    pipeline's own ``_ingest_article`` leaves them."""
    from app.models.enums import Asset, Direction, ImpactStrength

    art = models.NewsArticle(
        guid=guid, source="Reuters", source_quality="TIER_2_MAJOR",
        url=f"https://example.invalid/{guid}", title=title,
        published_at_utc=datetime.now(timezone.utc) - timedelta(minutes=minutes),
        is_today=True, category="Central Bank", importance="HIGH",
        report_kind="GENERAL", verification="CONFIRMED", relevance_score=0.9,
        analysis_mode=analysis_mode, llm_used=(analysis_mode == "llm"),
    )
    session.add(art)
    session.flush()
    for asset in Asset:
        session.add(models.AssetImpact(
            article_id=art.id, asset=asset.value,
            direction=Direction[direction].value,
            strength=ImpactStrength.VERY_HIGH.value,
            relevance=0.85, persistence=0.75))
    session.commit()
    return art


def _modes(session, models, scope="CURRENT"):
    from sqlalchemy import select

    return {
        r.asset: r.mode
        for r in session.scalars(
            select(models.SentimentSnapshot).where(
                models.SentimentSnapshot.scope == scope)).all()
    }


def test_llm_classified_article_produces_an_llm_card(tmp_path):
    """The core Step 3 requirement: llm in, llm on the card."""
    Session, models = _make_db(tmp_path)
    with Session() as session:
        _ingest(session, models,
                "Fed signals dovish pivot and rate cut", "llm", "llm-1")
        from app.services.pipeline import _recompute_sentiment, IngestStats
        _recompute_sentiment(session, IngestStats())
        session.commit()

        modes = _modes(session, models)
        assert modes, "no CURRENT snapshots were written"
        assert modes == {a: "llm" for a in modes}, (
            "an article classified by the LLM must produce an llm card for "
            "every asset; got %r" % (modes,))
        for value in modes.values():
            assert value in VALID_MODES


def test_heuristic_classified_article_produces_a_heuristic_card(tmp_path):
    """The circuit-breaker / no-LLM path must report heuristic, not llm."""
    Session, models = _make_db(tmp_path)
    with Session() as session:
        _ingest(session, models,
                "Fed signals dovish pivot and rate cut", "heuristic", "heur-1")
        from app.services.pipeline import _recompute_sentiment, IngestStats
        _recompute_sentiment(session, IngestStats())
        session.commit()

        modes = _modes(session, models)
        assert modes == {a: "heuristic" for a in modes}


def test_a_blend_reports_mixed(tmp_path):
    """One llm and one heuristic headline on the same asset must say mixed."""
    Session, models = _make_db(tmp_path)
    with Session() as session:
        _ingest(session, models, "Fed signals dovish pivot and rate cut",
                "llm", "llm-mix")
        _ingest(session, models, "Dollar weakens sharply after soft jobs report",
                "heuristic", "heur-mix")
        from app.services.pipeline import _recompute_sentiment, IngestStats
        _recompute_sentiment(session, IngestStats())
        session.commit()

        modes = _modes(session, models)
        assert modes == {a: "mixed" for a in modes}, (
            "a blend of llm and heuristic news must be reported as mixed; "
            "got %r" % (modes,))


def test_the_card_payload_carries_the_mode(tmp_path):
    """``mode`` must reach the dashboard through _block / build_payload."""
    Session, models = _make_db(tmp_path)
    with Session() as session:
        _ingest(session, models, "Fed signals dovish pivot and rate cut",
                "llm", "llm-payload")
        from app.services.pipeline import _recompute_sentiment, IngestStats
        _recompute_sentiment(session, IngestStats())
        session.commit()

        from app.main import _block, build_payload

        block = _block(session, "XAUUSD", "CURRENT")
        assert block["mode"] == "llm"

        payload = build_payload(session)
        gold = next(a for a in payload["assets"] if a["asset"] == "XAUUSD")
        assert gold["current"]["mode"] == "llm"
        assert gold["current"]["mode"] in VALID_MODES


def test_the_html_footer_renders_the_mode_value():
    """The footer must print the mode it is given, for all three values."""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1]
            / "app" / "dashboard" / "web" / "index.html").read_text(encoding="utf-8")
    assert "mode <b>" in html
    # Both the asset card and the equity card footer render the payload's mode.
    assert html.count("mode <b>'+esc(") >= 2, (
        "the asset card and the equity card footers must both render the "
        "payload's mode field")
    assert "||'heuristic'" in html, (
        "an absent mode must degrade to 'heuristic', not to a blank chip")


# ---------------------------------------------------------------------------
# The classify -> ingest path, with the LLM mocked
# ---------------------------------------------------------------------------
def test_analyze_article_reports_llm_mode_with_a_stubbed_provider(monkeypatch):
    """
    With the provider available, ``analyze_article`` reports mode='llm'.
    Uses a stubbed low-level call so no network is involved.
    """
    from app.analysis import news_classifier
    from app.analysis.llm_client import circuit_breaker
    from app.config import settings

    monkeypatch.setattr(type(settings), "has_llm", True, raising=False)
    monkeypatch.setattr(settings, "llm_enabled", True, raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)

    # Stub llm_analyze itself: this test is about the label analyze_article
    # returns for the LLM path, not about parsing a provider response.
    async def fake_llm(title, body=""):
        return news_classifier.ArticleAnalysis(
            mode="llm", note="stubbed", relevance_score=0.9)

    monkeypatch.setattr(news_classifier, "llm_analyze", fake_llm, raising=False)

    async def scenario():
        await circuit_breaker().reset()
        return await news_classifier.analyze_article(
            "Fed signals dovish pivot and rate cut")

    analysis, llm_ok, skip_reason = asyncio.run(scenario())
    assert llm_ok is True
    assert skip_reason == "ok"
    assert analysis.mode == "llm"


def test_circuit_breaker_open_reports_heuristic(monkeypatch):
    """Breaker open -> heuristic, and the note says so."""
    from app.analysis import news_classifier
    from app.analysis.llm_client import circuit_breaker
    from app.config import settings

    monkeypatch.setattr(type(settings), "has_llm", True, raising=False)
    monkeypatch.setattr(settings, "llm_enabled", True, raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "test-key", raising=False)

    async def scenario():
        cb = circuit_breaker()
        await cb.reset()
        # Drive the breaker open through its own API rather than a private
        # field, so the test tracks the real threshold.
        for _ in range(cb.threshold + 1):
            await cb.record_failure()
        assert cb.is_open, "precondition: the breaker must be open"
        return await news_classifier.analyze_article(
            "Fed signals dovish pivot and rate cut")

    analysis, llm_ok, skip_reason = asyncio.run(scenario())
    assert llm_ok is False
    assert skip_reason == "circuit_breaker"
    assert analysis.mode == "heuristic"
    assert "circuit breaker" in analysis.note.lower()
