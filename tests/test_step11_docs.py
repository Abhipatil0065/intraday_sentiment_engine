"""
STEP 11 - docs generation and the cleanup it documents.

The docs are generated from the map rather than hand-written, so they cannot
drift. These tests pin the generator's output against the real map, and pin
the four documented claims that a reader would otherwise have to trust.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run_generator():
    r = subprocess.run(
        [sys.executable, str(REPO / "tools" / "generate_transmission_docs.py")],
        capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, f"generator failed: {r.stderr[-2000:]}"
    return r.stdout


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------
def test_generator_runs_and_writes_the_doc():
    _run_generator()
    doc = (REPO / "docs" / "TRANSMISSION_MAP.md").read_text(encoding="utf-8")
    assert "## Event types" in doc
    assert "## Driver ranks" in doc
    assert "## Known model choices" in doc


def test_generated_doc_covers_every_event_type():
    """Every detected event type must appear in the table, or the doc is stale."""
    from app.analysis.transmission_map import EVENT_SIGNALS

    doc = (REPO / "docs" / "TRANSMISSION_MAP.md").read_text(encoding="utf-8")
    # EVENT_SIGNALS is a tuple of (EventType, any_of, qualifiers) triples.
    missing = [etype.value for etype, _any, _qual in EVENT_SIGNALS
               if f"`{etype.value}`" not in doc]
    assert not missing, f"event types absent from the doc: {missing[:5]}"


def test_generated_doc_uses_the_real_map_values():
    """The cells must be the JSON's actual sign and magnitude."""
    from app.analysis.transmission_map import EVENT_SIGNALS

    raw = json.loads((REPO / "app" / "config" / "transmission_map.json")
                     .read_text(encoding="utf-8"))
    doc = (REPO / "docs" / "TRANSMISSION_MAP.md").read_text(encoding="utf-8")

    # Spot-check the three most consequential mappings, by exact rendered text.
    checks = [
        ("GEOPOLITICAL_RISK", "XAUUSD", "▲ bullish `1.2`"),
        ("GEOPOLITICAL_RISK", "BTCUSD", "▼ bearish `0.35`"),
        ("CRYPTO_REGULATION", "BTCUSD", "▼ bearish `0.8`"),
        ("GENERAL_MACRO", "EQUITY", "○ no side `0.1`"),
    ]
    for etype, asset, want in checks:
        sign, magnitude = raw[etype][asset]
        arrow = {1: "▲", -1: "▼", 0: "○"}[int(sign)]
        word = {1: "bullish", -1: "bearish", 0: "no side"}[int(sign)]
        rendered = f"{arrow} {word} `{float(magnitude):g}`"
        assert rendered == want, (
            f"{etype}/{asset}: the doc now renders {rendered!r}, the test "
            f"expected {want!r} - the map changed, update this check")
        assert rendered in doc, (
            f"{etype}/{asset} rendered as {rendered!r} but that text is not "
            f"in the generated doc")


def test_generated_doc_documents_the_deliberate_decisions():
    doc = (REPO / "docs" / "TRANSMISSION_MAP.md").read_text(encoding="utf-8")
    for phrase in ("CRYPTO_REGULATION", "GOLD_ETF_FLOW", "INDEX_FLOW",
                   "Step 4a", "Step 4c", "Step 4d", "Step 4e"):
        assert phrase in doc, f"the doc must explain {phrase}"


def test_generator_is_deterministic():
    """Two runs must produce identical tables (only the timestamp differs)."""
    _run_generator()
    first = (REPO / "docs" / "TRANSMISSION_MAP.md").read_text(encoding="utf-8")
    _run_generator()
    second = (REPO / "docs" / "TRANSMISSION_MAP.md").read_text(encoding="utf-8")
    strip = lambda t: "\n".join(
        l for l in t.splitlines() if not l.startswith("_Generated"))
    assert strip(first) == strip(second)


# ---------------------------------------------------------------------------
# SCORING.md
# ---------------------------------------------------------------------------
def test_scoring_doc_exists_and_documents_the_formulas():
    doc = (REPO / "docs" / "SCORING.md").read_text(encoding="utf-8")
    for fragment in (
            "0.5 ** (age_hours / half_life)",
            "100 × (bull − bear) / (bull + bear)",
            "min(bull,bear) / max(bull,bear)",
            "DECAY_FLOOR",
            "EQUITY:<region>",
    ):
        assert fragment in doc, f"SCORING.md must document: {fragment}"


def test_scoring_doc_does_not_overclaim():
    """
    The doc must state the three things that are NOT true of this engine,
    rather than letting a reader assume they are.
    """
    doc = (REPO / "docs" / "SCORING.md").read_text(encoding="utf-8")
    assert "never fired in production" in doc, (
        "SCORING.md must say that contradiction_detected has never fired")
    assert "Step 9.4 is not done" in doc, (
        "SCORING.md must flag the un-fixed FJ date branch")
    assert "unfitted by design" in doc, (
        "SCORING.md must say the calibration table is unfitted")


# ---------------------------------------------------------------------------
# The retired constants
# ---------------------------------------------------------------------------
def test_retired_constants_are_gone():
    """Step 11 retired the NO_BIAS-era settings; nothing may read them."""
    import app.config as cfg

    for name in ("bias_min_abs_score", "bias_min_confidence",
                 "bias_score_scale"):
        assert not hasattr(cfg, name), (
            f"{name} was retired in Step 11 and must not come back")


def test_retired_constants_live_equivalents_are_the_module_ones():
    """The real thresholds are the engine's, which is why the settings died."""
    from app.analysis.sentiment_engine import EPS, TIEBREAK_MAX_CONFIDENCE

    assert EPS == 12.0, "EPS is the old bias_min_abs_score"
    assert TIEBREAK_MAX_CONFIDENCE == 0.30, (
        "TIEBREAK_MAX_CONFIDENCE is the old bias_min_confidence")


# ---------------------------------------------------------------------------
# final_weight
# ---------------------------------------------------------------------------
def test_final_weight_source_mentions_signal_weight():
    """
    Step 11 made the live path populate final_weight with _signal_weight, so
    the published weight matches the score.

    TASK 1 moved that write from the pre-cluster row loop to AFTER
    cluster_signals(), so the persisted weight is the one the aggregation
    summed. This guards the post-cluster location.
    """
    text = (REPO / "app" / "services" / "pipeline.py").read_text(encoding="utf-8")
    assert "_weight_targets" in text, (
        "the recompute must remember which impact row each signal came from")
    assert "_signal_weight(surviving, recompute_now)" in text, (
        "the post-cluster write must persist the SURVIVING signal's weight")

    # The write must sit after clustering, not in the per-row loop.
    i_write = text.index("_signal_weight(surviving, recompute_now)")
    i_cluster = text.index(
        "signals = cluster_signals(signals, window_hours=0.5)")
    assert i_write > i_cluster, (
        "final_weight must be written AFTER cluster_signals(), or it is the "
        "pre-cluster weight and will not match the score")


def test_final_weight_is_populated_on_the_live_path(tmp_path):
    """End-to-end: the recompute must no longer leave final_weight at 0.0."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.database as dbmod
    import app.models.db_models as models
    from app.models.enums import (Asset, Direction, ImpactStrength)
    from app.services.pipeline import IngestStats, _recompute_sentiment

    engine = create_engine(f"sqlite:///{(tmp_path / 'fw.db').as_posix()}")
    models.Base.metadata.create_all(bind=engine)
    dbmod.engine = engine
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    with Session() as s:
        art = models.NewsArticle(
            guid="fw-1", source="Reuters", source_quality="TIER_2_MAJOR",
            url="https://example.invalid/fw",
            title="Fed signals patience as inflation cools",
            body="", published_at_utc=(now - timedelta(minutes=20))
            .replace(tzinfo=None),
            is_today=True, category="Central Bank", importance="HIGH",
            report_kind="GENERAL", verification="CONFIRMED",
            relevance_score=0.9, analysis_mode="llm", llm_used=True)
        s.add(art)
        s.flush()
        for a in Asset:
            s.add(models.AssetImpact(
                article_id=art.id, asset=a.value,
                direction=Direction.BULLISH.value,
                strength=ImpactStrength.HIGH.value,
                relevance=0.85, persistence=0.75))
        s.commit()

    with Session() as s:
        _recompute_sentiment(s, IngestStats())
        s.commit()
        rows = s.query(models.AssetImpact).all()

    assert rows
    for r in rows:
        assert r.final_weight and r.final_weight > 0, (
            f"{r.asset} final_weight is {r.final_weight}: the live path must "
            f"publish the weight the score actually uses")
        assert r.recency_weight == r.final_weight
