"""Step 7: measure whether two Chromium renders can overlap, and at what cost.

Runs the real news provider and the real calendar provider concurrently and
samples OS-level Chromium process count and RSS while they are in flight.
"""
import asyncio
import subprocess
import sys
import time


def chrome_procs():
    """
    Count live headless-renderer processes and total RSS in MB.

    Counts BOTH ``chrome`` and ``headless_shell``: Playwright's default
    headless Chromium runs as ``headless_shell``, so a probe that watches only
    ``chrome`` silently measures the user's normal browser instead and reports
    a reassuring flat line while two renderers are actually running.
    """
    script = (
        "$p = Get-Process chrome,headless_shell -ErrorAction SilentlyContinue; "
        "if ($p) { '{0}|{1}' -f $p.Count, "
        "[math]::Round(($p | Measure-Object WorkingSet64 -Sum).Sum/1MB,0) } "
        "else { '0|0' }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20).stdout.strip()
        n, _, mb = out.partition("|")
        return int(n or 0), float(mb or 0)
    except Exception:
        return 0, 0.0


async def sample(label, stop, peaks):
    while not stop.is_set():
        n, mb = chrome_procs()
        peaks["procs"] = max(peaks["procs"], n)
        peaks["rss_mb"] = max(peaks["rss_mb"], mb)
        await asyncio.sleep(0.5)


async def main():
    from app.ingestion.fj_calendar import fetch_fj_calendar
    from app.ingestion.financialjuice import FinancialJuiceSource

    base_p, base_mb = chrome_procs()
    print("baseline chrome procs=%d rss=%.0f MB" % (base_p, base_mb))

    peaks = {"procs": base_p, "rss_mb": base_mb}
    stop = asyncio.Event()
    watcher = asyncio.create_task(sample("both", stop, peaks))

    t0 = time.monotonic()
    news, cal = await asyncio.gather(
        FinancialJuiceSource().fetch(),
        fetch_fj_calendar(),
        return_exceptions=True,
    )
    elapsed = time.monotonic() - t0
    stop.set()
    await watcher

    for label, val in (("news", news), ("calendar", cal)):
        if isinstance(val, BaseException):
            print("%-9s ERROR %s: %s" % (label, type(val).__name__, val))
            continue
        # News returns a ProviderResult; the calendar returns a plain list.
        items = getattr(val, "articles", None)
        if items is None:
            items = getattr(val, "events", None)
        if items is None and isinstance(val, (list, tuple)):
            items = val
        print("%-9s %d items" % (label, len(items or [])))

    print()
    print("concurrent wall time   : %.1fs" % elapsed)
    print("peak chrome procs      : %d (baseline %d, delta +%d)"
          % (peaks["procs"], base_p, peaks["procs"] - base_p))
    print("peak chrome RSS        : %.0f MB (baseline %.0f MB, delta +%.0f)"
          % (peaks["rss_mb"], base_mb, peaks["rss_mb"] - base_mb))
    print()
    print("VERDICT: %s" % (
        "overlap is REAL - serialise with a shared lock"
        if peaks["rss_mb"] - base_mb > 400 else
        "no meaningful overlap - no lock needed"))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))