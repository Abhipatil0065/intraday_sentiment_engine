"""Item 5 verification: confidence flip gate + LLM batch priority.

Read-only. Asserts:
  1. A computed flip is REFUSED when confidence < FLIP_MIN_CONFIDENCE: the
     previous bias is held and the note says "Low confidence hold".
  2. A flip with confidence >= FLIP_MIN_CONFIDENCE is allowed.
  3. A first-run verdict (no previous bias) is never refused by the gate.
  4. Binary output preserved: the held label is still BULLISH or BEARISH.
  5. LLM_BATCH_LIMIT is 40.
  6. _priority_sort_articles ranks a Tier 1 release above Tier 4 filler.

Usage: python scripts/scratch/e2e_item5_gates.py
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from app.analysis.sentiment_engine import (
    Bias, compute, _flip_min_confidence, remember_bias, recall_bias,
    clear_bias_history,
)
from app.models.enums import (
    Asset, Direction, EconomicImportance, ImpactStrength, NewsCategory,
)
from app.analysis.signal_factory import build_signal


def _sig(title, importance, source_quality="TIER_2_MAJOR",
         direction=Direction.BULLISH, strength=ImpactStrength.HIGH,
         relevance=0.9, persistence=0.9):
    return build_signal(
        asset=Asset.GOLD, title=title, direction=direction, strength=strength,
        importance=importance, relevance=relevance, persistence=persistence,
        category=NewsCategory.CENTRAL_BANK, source_quality=source_quality,
    )


def main() -> None:
    clear_bias_history()
    now = datetime.now(timezone.utc)

    # 1. Low-confidence flip is refused: seed a BEARISH bias, then feed a torn
    #    market where the bull side barely edges the bear side. The score
    #    crosses the margin but the agreement is poor, so confidence stays
    #    below the gate and the previous bias is held with the reason noted.
    remember_bias("GOLD", Bias.BEARISH)
    torn = [
        _sig("Gold plunges", EconomicImportance.HIGH, "TIER_2_MAJOR",
             Direction.BEARISH, ImpactStrength.HIGH),
        _sig("Gold surges", EconomicImportance.HIGH, "TIER_2_MAJOR",
             Direction.BULLISH, ImpactStrength.HIGH),
        _sig("Gold consolidates", EconomicImportance.MEDIUM, "TIER_3_CRYPTO",
             Direction.BULLISH, ImpactStrength.MEDIUM),
    ]
    r = compute(torn, Asset.GOLD, now=now, history_key="GOLD")
    assert r.bias is Bias.BEARISH, r.bias
    assert "Low confidence hold" in r.note, r.note
    assert r.confidence < _flip_min_confidence()
    print(f"  OK low-confidence flip refused (conf={r.confidence:.2f} < "
          f"{_flip_min_confidence():.2f}), {r.bias.value} held")

    # 2. High-confidence flip is allowed: a one-sided, high-conviction move
    # crosses both the margin and the confidence gate.
    strong = [_sig("Gold surges on Fed pivot", EconomicImportance.VERY_HIGH,
                   "TIER_1_OFFICIAL")]
    r2 = compute(strong, Asset.GOLD, now=now, history_key="GOLD")
    assert r2.bias is Bias.BULLISH, r2.bias
    assert r2.confidence >= _flip_min_confidence()
    print(f"  OK high-confidence flip allowed -> {r2.bias.value} "
          f"(conf={r2.confidence:.2f})")

    # 3. First-run verdict is never refused by the gate.
    clear_bias_history()
    first = compute(strong, Asset.GOLD, now=now, history_key="GOLD")
    assert first.bias is Bias.BULLISH, first.bias
    assert "Low confidence hold" not in first.note, first.note
    print("  OK first-run verdict never refused by the confidence gate")

    # 4. Binary output preserved on a held call.
    assert r.bias in (Bias.BULLISH, Bias.BEARISH)
    print("  OK binary output preserved (no third state)")

    # 5. LLM batch limit.
    from app.services.pipeline import LLM_BATCH_LIMIT, _priority_sort_articles
    from app.ingestion.base import RawArticle
    assert LLM_BATCH_LIMIT == 40, LLM_BATCH_LIMIT
    print(f"  OK LLM_BATCH_LIMIT = {LLM_BATCH_LIMIT}")

    # 6. Sort: Tier 1 release beats Tier 4 filler.
    tier1 = RawArticle(guid="t1", source="Fed", source_quality="TIER_1_OFFICIAL",
                       url="http://t/1", title="Fed announces surprise rate cut",
                       body="Actual 0.2 Forecast 0.4 Previous 0.3",
                       published_raw="2026-10-10T10:00:00+00:00")
    tier4 = RawArticle(guid="t4", source="Social", source_quality="TIER_4_OTHER",
                       url="http://t/4", title="random crypto tweet about nothing",
                       body="", published_raw="2026-10-10T12:00:00+00:00")
    ordered = _priority_sort_articles([tier4, tier1])
    assert ordered[0].guid == "t1", [a.guid for a in ordered]
    print("  OK Tier 1 release sorts before Tier 4 filler")

    print("\nALL ITEM 5 CHECKS PASSED")


if __name__ == "__main__":
    main()