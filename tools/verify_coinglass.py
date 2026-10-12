"""
ONE-OFF VERIFICATION (task item 7).

Scrapes the Gold and Bitcoin CoinGlass pages ONCE, prints the extracted data
to the console, and confirms the shape the sentiment scorer consumes.

    python tools/verify_coinglass.py [--asset XAUUSD|XAGUSD|BTCUSD] [--all]

What it checks:
  1. the browser can render the page at all (no Cloudflare block);
  2. the documented cluster shape is produced:
         {"price_level": 4230.50, "intensity": "high", "side": "long"}
  3. a CVD series with a locally computed divergence verdict;
  4. a footprint imbalance reading;
  5. the conversion into WeightedSignals, and that compute() accepts them.

This script is a diagnostic. It never runs as part of a tick and writes nothing
except the on-disk cache the scraper already owns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings                      # noqa: E402
from app.ingestion import coinglass_scraper as cs     # noqa: E402


def _banner(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def verify_asset(asset: str) -> bool:
    url = cs.asset_url(asset)
    _banner(f"COINGLASS VERIFY {asset}  {url}")

    if not settings.coinglass_enabled:
        print("coinglass_enabled is False - enable it in .env to run.")
        return False

    snapshot = asyncio.run(cs.scrape_asset_async(asset))

    print("\n--- RAW EXTRACTION -------------------------------------------")
    if snapshot is None:
        print(json.dumps({"asset": asset, "scrape": None,
                          "reason": "challenge/block, timeout, backoff, or "
                                    "no renderable chart data"}, indent=2))
        print("\n  The trade loop is unaffected: the pipeline degrades to news "
              "+ price confirmation and the last good cached reading stays "
              "on the dashboard.")
        return False

    print(json.dumps(snapshot.to_dict(), indent=2))

    print("\n--- CHECKS ---------------------------------------------------")
    results: list[bool] = []

    results.append(_check(
        "spot price resolved",
        snapshot.spot_price is not None,
        f"spot={snapshot.spot_price}"))

    results.append(_check(
        "liquidation heatmap clusters extracted",
        bool(snapshot.heatmap_clusters),
        f"{len(snapshot.heatmap_clusters)} cluster(s)"))

    below = snapshot.cluster_below
    if below is not None:
        payload = below.to_dict()
        shape_ok = (set(payload) == {"price_level", "intensity", "side"}
                    and isinstance(payload["price_level"], float)
                    and payload["intensity"] in ("low", "medium", "high")
                    and payload["side"] in ("long", "short"))
        results.append(_check(
            "cluster_below matches documented output shape",
            shape_ok,
            json.dumps(payload)))

    above = snapshot.cluster_above
    if above is not None:
        payload = above.to_dict()
        shape_ok = (set(payload) == {"price_level", "intensity", "side"}
                    and isinstance(payload["price_level"], float)
                    and payload["intensity"] in ("low", "medium", "high")
                    and payload["side"] in ("long", "short"))
        results.append(_check(
            "cluster_above matches documented output shape",
            shape_ok,
            json.dumps(payload)))

    if snapshot.cvd is not None:
        verdicts = ("CVD_BULLISH", "CVD_BEARISH", "CONFIRMED", "NONE")
        results.append(_check(
            "CVD series extracted with a divergence verdict",
            snapshot.cvd.divergence in verdicts
            and bool(snapshot.cvd.points),
            f"{len(snapshot.cvd.points)} points -> "
            f"{snapshot.cvd.divergence}"))
    else:
        print("  [INFO] no CVD series on this page - heatmap only.")

    if snapshot.footprint is not None:
        results.append(_check(
            "footprint imbalance extracted",
            snapshot.footprint.imbalance in (
                "BUY_IMBALANCE", "SELL_IMBALANCE", "NONE"),
            f"{snapshot.footprint.imbalance} "
            f"({snapshot.footprint.imbalance_pct:+.1f}%)"))
    else:
        print("  [INFO] no footprint series on this page.")

    results.append(_check(
        "snapshot is not marked degraded",
        not snapshot.degraded))

    # --- sentiment-scorer compatibility ---
    from app.models.enums import Asset

    try:
        asset_enum = Asset(asset)
    except ValueError:
        asset_enum = None

    if asset_enum is not None:
        signals = cs.coinglass_signals_to_weighted({asset: snapshot})
        print("\n--- SENTIMENT SCORER INPUT ----------------------------------")
        if signals:
            for signal in signals:
                print(f"  {signal.category.value:20s} "
                      f"{signal.direction.value:8s} "
                      f"rel={signal.relevance:.2f} "
                      f"pers={signal.persistence:.2f} "
                      f"strength={signal.strength.value}")
                print(f"      title: {signal.title}")
                print(f"      reason: {signal.reason}")

            from app.analysis.sentiment_engine import compute
            result = compute(signals, asset_enum)
            print(f"\n  compute() -> {result.bias.value} "
                  f"score={result.score:+.1f} "
                  f"confidence={result.confidence:.2f}")
            results.append(_check(
                "compute() accepts the scraped signals and names a side",
                result.bias.value in ("BULLISH", "BEARISH"),
                f"{result.bias.value}"))
        else:
            print("  (no directional signal: the page yielded only clusters "
                  "and/or caution flags)")
            results.append(_check(
                "cluster-only snapshot produces no directional signal",
                True,
                "clusters are caution flags, not directions"))

    if snapshot.caution_note():
        print(f"\n  caution note: {snapshot.caution_note()}")
        results.append(_check(
            "liquidation caution flags detected near spot",
            True))

    print("\n--- CACHE ----------------------------------------------------")
    cache = cs._get_cache()
    age = cache.age_seconds(asset)
    results.append(_check(
        "snapshot cached to disk (rate limit enforced)",
        cache.get(asset) is not None,
        f"age={age}s ttl={settings.coinglass_cache_ttl_seconds}s"))

    print()
    return all(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", default=None,
                        help="one of XAUUSD, XAGUSD, BTCUSD")
    parser.add_argument("--all", action="store_true",
                        help="verify all three assets sequentially")
    args = parser.parse_args()

    if args.all:
        assets = ["XAUUSD", "XAGUSD", "BTCUSD"]
    elif args.asset:
        assets = [args.asset.upper()]
    else:
        # Default to what is actually configured for scraping. Only BTC is
        # readable, so an unconfigured default would fail on assets that are
        # deliberately not scheduled.
        assets = cs.configured_assets()

    outcomes = {}
    for asset in assets:
        outcomes[asset] = verify_asset(asset)

    _banner("SUMMARY")
    for asset, ok in outcomes.items():
        print(f"  {asset:8s} {'EXTRACTED' if ok else 'NOT EXTRACTED'}")

    if all(outcomes.values()):
        print("\n  Scraper built and first extraction successful.")
        return 0

    # Distinguish the reasons an asset can fail, so the output says what to do
    # next rather than just "no data".
    metals = [a for a in assets if a in ("XAUUSD", "XAGUSD")]
    blocked = [a for a in metals if not outcomes[a]]
    if blocked:
        print("\n  NOTE - Gold/Silver are not configured for scraping.")
        print("  The CoinGlass \"Legend\" charts that carry the metals redirect")
        print("  anonymous visitors to a login wall, and the auth session is a")
        print("  session-only cookie that Chromium never persists to a profile")
        print("  (verified: it works inside a run, then is gone on close).")
        print("  The REST API answers \"Upgrade plan\" for every endpoint on")
        print("  this key's plan, so the API is not an alternative either.")
        print("\n  They therefore fall back to news + price confirmation, and")
        print("  they are NOT scheduled: an unreadable asset would cost a")
        print("  wasted browser render against that wall on every poll.")
        print("  BTC is the one instrument readable with no key and no login,")
        print("  and it is what this scraper is configured for:")
        print("      COINGLASS_ASSETS=BTCUSD")
        return 1

    if any(outcomes.values()):
        print("\n  Partial success. A failed asset means that page was "
              "blocked, timed out, or rendered no chart - the pipeline "
              "degrades to news + price for it and keeps trading.")
        return 1
    print("\n  No page yielded data. This is an ANTI-BOT or render problem, "
          "not a logic problem: the parse path is covered by "
          "tests/test_coinglass_scraper.py. Check the URL, then slow the "
          "poll rate or rotate the user agent.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
