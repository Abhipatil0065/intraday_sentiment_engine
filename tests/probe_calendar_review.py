"""
STEP 1 REVIEW, before enabling the flag.

  a) the merge disagreements, both providers' values side by side;
  b) the skipped-row count broken down BY REASON, with samples.

Runs the REAL providers and the REAL merge. Read-only.

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
from app.ingestion.economic_calendar import _fetch_faireconomy, _fetch_fj


async def main() -> None:
    settings.financialjuice_calendar_enabled = True
    print("=" * 78)
    print("STEP 1 REVIEW")
    print("=" * 78)

    fj_events, fj_err = await _fetch_fj()
    fe_events, fe_err = await _fetch_faireconomy()
    print("financialjuice : %d events %s" % (len(fj_events), fj_err or ""))
    print("faireconomy    : %d events %s"
          % (len(fe_events), (fe_err[:1] if fe_err else "")))
    print()

    # ---------- (a) disagreements ----------
    print("-" * 78)
    print("(a) MERGE DISAGREEMENTS - both providers, side by side")
    print("-" * 78)
    pairs = collect_disagreements(
        [("financialjuice", fj_events), ("faireconomy", fe_events)])
    if not pairs:
        print("  (none - either faireconomy was unavailable, or every")
        print("   overlapping pair agreed on every field)")
    for i, p in enumerate(pairs, 1):
        print()
        print("  %d. %s  [%s]" % (i, p["title"][:60], p["country"]))
        print("     similarity : %.3f  (threshold %.2f)"
              % (p["similarity"], settings.calendar_merge_similarity))
        print("     time delta : %s" % p["time_delta"])
        print("     %-9s %-12s %-12s" % ("field", "financialjuice", "faireconomy"))
        for field in ("forecast", "previous", "actual"):
            a, b = p["fj"][field], p["fe"][field]
            flag = "  <-- DISAGREE" if (a is not None and b is not None
                                        and a != b) else ""
            print("     %-9s %-12s %-12s%s" % (field, a, b, flag))
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