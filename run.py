"""
Unified entry point.

    python run.py --api           start the FastAPI service + background pipeline
    python run.py --streamlit     start the Streamlit dashboard
    python run.py --once          run one pipeline pass and print the console view
    python run.py --demo          offline demo (synthetic news, no API key needed)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.logging_conf import get_logger, setup_logging

log = get_logger(__name__)


def run_api(host: str, port: int, reload: bool) -> None:
    import uvicorn

    log.info("Starting API on http://%s:%d (dashboard at /)", host, port)
    uvicorn.run("app.main:app", host=host, port=port, reload=reload,
                log_level="info")


def run_streamlit(port: int) -> None:
    script = BASE_DIR / "app" / "dashboard" / "dashboard.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(script),
        "--server.port", str(port),
        "--server.headless", "true",
    ]
    log.info("Starting Streamlit on http://127.0.0.1:%d", port)
    raise SystemExit(subprocess.call(cmd))


def run_once_demo() -> None:
    """One pipeline pass against the synthetic feed, then print the console."""
    import asyncio

    from app.config import settings

    # Settings are cached at import time, so flip the flag on the live object
    # rather than relying on os.environ (which would arrive too late).
    settings.use_mock_source = True
    log.info("Demo mode: using synthetic offline source (USE_MOCK_SOURCE=true)")

    from app.database import init_db, session_scope
    from app.main import build_payload
    from app.services.pipeline import run_once

    setup_logging()
    init_db()

    stats = asyncio.run(run_once())
    log.info(
        "Demo run: fetched=%d new=%d rejected=%d duplicates=%d",
        stats.fetched, stats.new_today, stats.rejected, stats.duplicates,
    )

    with session_scope() as session:
        data = build_payload(session)

    print()
    print("=" * 74)
    print("TODAY'S INTRADAY MARKET SENTIMENT")
    print("=" * 74)
    print(f"DATE:            {data['date']}")
    print(f"CURRENT TIME:    {data['current_time']} ({data['display_timezone']})")
    print(f"CURRENT SESSION: {data['current_session']}")
    print(f"MODE:            {data['status']['analysis_mode']}")

    for asset in data["assets"]:
        print()
        print("-" * 74)
        print(f"{asset['label'].upper()} - {asset['symbol']}")
        print("-" * 74)
        for key, title in (("market_prep", "MARKET PREP"),
                           ("market_wrap", "MARKET WRAP"),
                           ("current", "CURRENT DAY-TRADING SENTIMENT")):
            b = asset[key]
            print(f"  {title}")
            print(f"    Bias:          {b['bias']}")
            print(f"    Score:         {b['score']:+.1f}  (-100..+100)")
            print(f"    Confidence:    {b['confidence']:.2f} "
                  f"(events: {b['contributing_articles']}, mode: {b['mode']})")
            if b["delta_60m"]:
                print(f"    Changed 60min: {b['delta_60m']:+.1f}")
            for d in b["drivers"][:3]:
                print(f"    + {d['contribution']:+.3f}  {d['title'][:70]}")
            if b["conflict"]:
                print(f"    ! CONFLICT: {b['conflict']['title'][:70]}")
            if b["event_risk"]:
                print(f"    !! {b['event_risk_note']}")
            print(f"    Price check:   {b['price_note']}")

    print()
    print("=" * 74)
    print("TODAY'S NEWS IMPACT FEED")
    print("=" * 74)
    for item in data["news_feed"][:10]:
        hit = ", ".join(sorted({i["asset"] for i in item["impacts"]})) or "None"
        top = max(item["impacts"], key=lambda i: i["relevance"], default=None)
        print(f"[{item['display_time']}] {item['title'][:90]}")
        print(f"    {item['source']} | {item['category']} | {item['importance']} "
              f"| Asset: {hit}")
        if top:
            print(f"    Impact: {top['direction']} ({top['strength']})")
            print(f"    Transmission: {top['transmission'][:100]}")
        print()


def check_llm() -> None:
    """
    Verify the configured LLM provider end-to-end.

    Confirms the key authenticates, the model exists and is not retired, and
    that the endpoint returns parseable JSON. Exits non-zero on failure so it
    can be used in CI or a pre-start script.
    """
    import asyncio

    from app.analysis import llm_client
    from app.config import settings

    setup_logging()

    print("=" * 66)
    print("LLM PROVIDER CHECK")
    print("=" * 66)
    print(f"Base URL : {settings.llm_base_url}")
    print(f"Model    : {settings.llm_model}")
    print(f"Key      : {'set (' + str(len(settings.llm_api_key)) + ' chars)'
                  if settings.llm_api_key.strip() else 'NOT SET -> heuristic mode'}")
    print(f"JSON mode: {settings.llm_json_mode}")
    print(f"Rate limit: {settings.llm_requests_per_minute} requests/min "
          f"({'disabled' if settings.llm_requests_per_minute <= 0 else 'rolling 60s window'})")
    print("-" * 66)

    if not settings.has_llm:
        print("RESULT: heuristic mode (no key). Engine works, LLM analysis off.")
        raise SystemExit(0)

    # Reset circuit breaker so a previous failure doesn't block the check
    asyncio.run(llm_client.reset_circuit_breaker())

    try:
        result = asyncio.run(llm_client.health_check())
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"RESULT: FAILED - {type(exc).__name__}: {exc}")
        raise SystemExit(1)

    if result["ok"]:
        print(f"RESULT: OK - {result['detail']}")
        print("LLM classification is active.")
        raise SystemExit(0)

    print(f"RESULT: FAILED - {result['detail']}")
    print()
    print("Common causes:")
    print("  401/403 -> key is wrong, expired, or for a different provider")
    print("  404-> model id does not exist at this endpoint")
    print("  410     -> model retired by the provider; pick another model id")
    print("  timeout -> endpoint unreachable / wrong base URL")
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", action="store_true", help="run the FastAPI service")
    parser.add_argument("--streamlit", action="store_true", help="run the dashboard")
    parser.add_argument("--once", action="store_true",
                        help="run one pipeline pass against live sources")
    parser.add_argument("--demo", action="store_true",
                        help="offline demo with synthetic news")
    parser.add_argument("--check-llm", action="store_true",
                        help="verify the configured LLM key/provider and exit")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="uvicorn autoreload")
    args = parser.parse_args()

    from app.config import settings

    host = args.host or settings.api_host
    port = args.port or settings.api_port

    if args.check_llm:
        check_llm()
    elif args.streamlit:
        run_streamlit(settings.dashboard_port)
    elif args.demo:
        run_once_demo()
    elif args.once:
        import asyncio

        from app.database import init_db
        from app.services.pipeline import run_once

        setup_logging()
        init_db()
        stats = asyncio.run(run_once())
        print(f"fetched={stats.fetched} new={stats.new_today} "
              f"rejected={stats.rejected} duplicates={stats.duplicates}")
        for e in stats.errors[:10]:
            print("  !", e)
    elif args.api or True:  # default to the API server
        run_api(host, port, args.reload)


if __name__ == "__main__":
    main()