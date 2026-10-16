"""
STEP 1 REVIEW, before enabling the flag.

  a) the skipped-row count broken down BY REASON, with samples.

Runs the REAL FinancialJuice provider and the REAL merge. Read-only.

Run:  python -m tests.probe_calendar_review
"""

from __future__ import annotations

import asyncio
from collections import Counter

from app.analysis.calendar_merge import (
    normalise_title,
    same_release,
    title_similarity,
)
from app.config import settings
from app.ingestion import fj_calendar as fjmod
from app.ingestion.calendar_merge_stats import collect_disagreements
from app.ingestion.economic_calendar import _fetch_fj


async def main() -> None:
    settings.financialjuice_calendar_enabled = True
    print("=" * 78)
    print("STEP 1 REVIEW (FinancialJuice only)")
    print("=" * 78)

    fj_events, fj_err = await _fetch_fj()
    print("financialjuice : %d events %s" % (len(fj_events), fj_err or ""))
    print()

    # ---------- (a) self-disagreements within FJ ----------
    print("-" * 78)
    print("(a) MERGE DISAGREEMENTS (FJ vs itself - should be none)")
    print("-" * 78)
    pairs = collect_disagreements([("financialjuice", fj_events)])
    if not pairs:
        print("  (none - no overlapping pair disagreed on any field)")
    for i, p in enumerate(pairs, 1):
        print()
        print("  %d. %s  [%s]" % (i, p["title"][:60], p["country"]))
        print("     similarity : %.3f  (threshold %.2f)"
              % (p["similarity"], settings.calendar_merge_similarity))
        print("     time delta : %s" % p["time_delta"])
        print("     verdict    : %s" % p["verdict"])
    print()

    # ---------- (b) skipped rows ----------
    print("-" * 78)
    print("(b) SKIPPED ROWS BY REASON")
    print("-" * 78)
    reasons, samples = await fjmod.summarise_skips()
    total = sum(reasons.values())
    print("  total skipped : %d" % total)
    for reason, count in reasons.most_common():
        print("    %-28s %d" % (reason, count))
    print()
    print("  samples:")
    for reason, titles in samples.items():
        if not titles:
            continue
        print("    [%s]" % reason)
        for t in titles:
            print("       - %s" % t[:66])
    print()


if __name__ == "__main__":
    asyncio.run(main())
