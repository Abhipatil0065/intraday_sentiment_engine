"""
MACRO NEWS ANALYST - runner + evaluation scheduler
==================================================

One-off analysis of a news event::

    python scripts/run_macro_analyst.py --headline "CPI prints at 3.2%, above forecast"
        --source "Bureau of Labor Statistics" --actual 3.2 --forecast 3.0 --previous 3.1
    python scripts/run_macro_analyst.py --headline "..." --event-file event.json

Persistent evaluation scheduler (Ctrl-C to stop)::

    python scripts/run_macro_analyst.py --run-scheduler

What it does
------------
* Builds the event dict from user-supplied fields plus a best-effort market
  snapshot (GOLD, SILVER, BITCOIN, EQUITIES price + pct change since news,
  DXY, US10Y, US real yield, VIX, oil, key levels, upcoming events).  Fields
  with no feed are sent as null - prices are never invented.
* Calls app.analysis.macro_analyst.analyze(event).
* Validates the model output before logging: required keys, bias in
  {BULLISH, BEARISH, NEUTRAL, NO-TRADE}, confidence an integer 0-100,
  reaction_type in {CONTINUATION, FADE, CHOP}.  One retry on invalid output;
  if still invalid, the raw output is logged with "valid": false and the
  process exits non-zero.
* Appends one JSONL record per ANALYZE to data/macro_analyst_log.jsonl and
  creates pending evaluation jobs due at +15, +60 and +240 minutes, persisted
  in data/macro_pending.jsonl so a restart never loses them.
* --run-scheduler loops every 30s, finds due jobs, fetches prices (null when
  the feed cannot give them), calls analyze.evaluate(...), appends an
  EVALUATE record and marks the job done.  Feed unavailable at due time ->
  up to 3 retries -> job marked MISSED (no guessed prices).  Bitcoin trades
  24/7; for GOLD, SILVER and EQUITIES a job due while the market is closed is
  deferred to the next open and the deferral is noted in the record.

This tool only analyses. It never places, sizes or triggers trades.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `python scripts/run_macro_analyst.py` from the repo root: make the
# `app` package importable without installing anything.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis import macro_analyst  # noqa: E402
from app.config import settings  # noqa: E402
from app.logging_conf import get_logger  # noqa: E402
from app.market import price_data  # noqa: E402
from app.models.enums import Asset  # noqa: E402

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_PATH = DATA_DIR / "macro_analyst_log.jsonl"
PENDING_PATH = DATA_DIR / "macro_pending.jsonl"

ASSETS = ("GOLD", "SILVER", "BITCOIN", "EQUITIES")
VALID_BIAS = {"BULLISH", "BEARISH", "NEUTRAL", "NO-TRADE"}
VALID_REACTION = {"CONTINUATION", "FADE", "CHOP"}
REQUIRED_TOP_KEYS = ("mode", "assets")
REQUIRED_ASSET_KEYS = ("bias", "confidence", "reaction_type")

SCHEDULER_TICK_SECONDS = 30
WINDOWS = (15, 60, 240)
FEED_RETRIES = 3          # price feed retries when a reachable feed fails
EVAL_MAX_ATTEMPTS = 5     # evaluate-call retries before giving up on a job
MARKET_SCAN_DAYS = 7      # how far ahead to look for the next market open


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into aware UTC (``Z`` and naive both OK)."""
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# price feed - existing modules are imported read-only; missing feed => null
# ---------------------------------------------------------------------------

def _fetch_price(asset: str) -> float | None:
    """Best-effort spot price for one asset.

    BITCOIN is covered by the existing Coinbase feed (app.market.price_data).
    GOLD, SILVER and EQUITIES have no reachable spot feed in this codebase, so
    they are null - never guessed.
    """
    if asset != "BITCOIN":
        return None
    try:
        price, _source = asyncio.run(price_data.get_spot_price(Asset.BITCOIN))
    except Exception as exc:  # noqa: BLE001 - any failure means unavailable
        log.debug("Coinbase feed failed for BITCOIN: %s", exc)
        return None
    if price is None or price <= 0:
        return None
    return price


def fetch_market_snapshot() -> dict:
    """Market context for the ANALYZE event. Missing fields are null."""
    prices: dict[str, dict] = {}
    for asset in ASSETS:
        prices[asset] = {"price": _fetch_price(asset), "pct_change_since_news": None}
    return {
        "current_prices": prices,
        "dxy": None,
        "us10y": None,
        "us_real_yield": None,
        "vix": None,
        "oil": None,
        "key_levels": None,
        "upcoming_events": None,
    }


# ---------------------------------------------------------------------------
# previous bias (last logged ANALYZE output)
# ---------------------------------------------------------------------------

def last_analyze_output() -> dict | None:
    """Return the most recent logged ANALYZE output, or None when there is none."""
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "ANALYZE":
            output = record.get("output")
            return output if isinstance(output, dict) else None
    return None


# ---------------------------------------------------------------------------
# event building
# ---------------------------------------------------------------------------

def _coerce_number(value: str) -> float | str:
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def build_event(args: argparse.Namespace) -> dict:
    """User-supplied fields + market snapshot; user fields win, missing => null."""
    event: dict = {}
    if args.event_file:
        try:
            with open(args.event_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read event file {args.event_file}: {exc}", file=sys.stderr)
            sys.exit(2)
        if isinstance(data, dict):
            event.update(data)
        else:
            print(f"ERROR: event file {args.event_file} must contain a JSON object", file=sys.stderr)
            sys.exit(2)

    if args.source:
        event["source"] = args.source
    for name in ("actual", "forecast", "previous"):
        value = getattr(args, name, None)
        if value is not None:
            event[name] = _coerce_number(value)

    event["headline"] = args.headline
    event.setdefault("timestamp", _iso(_now_utc()))

    snapshot = fetch_market_snapshot()
    for key, value in snapshot.items():
        event.setdefault(key, value)

    event.setdefault("previous_bias", last_analyze_output())
    return event


# ---------------------------------------------------------------------------
# output validation
# ---------------------------------------------------------------------------

def _as_confidence(value) -> int | None:
    """Coerce an integral number into a confidence int; bool and non-integral rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def validate_analyze_output(output: object) -> tuple[bool, dict | None, str]:
    """Validate a model ANALYZE output per the acceptance rules.

    Returns (valid, normalised_output, reason).  On success the normalised
    output has integer confidences.
    """
    if not isinstance(output, dict):
        return False, None, "model output is not a JSON object"
    for key in REQUIRED_TOP_KEYS:
        if key not in output:
            return False, None, f"missing required key: {key}"
    assets = output.get("assets")
    if not isinstance(assets, dict):
        return False, None, "assets must be an object"
    for asset in ASSETS:
        entry = assets.get(asset)
        if not isinstance(entry, dict):
            return False, None, f"missing asset block: {asset}"
        for key in REQUIRED_ASSET_KEYS:
            if key not in entry:
                return False, None, f"{asset} missing required key: {key}"
        bias = entry.get("bias")
        if bias not in VALID_BIAS:
            return False, None, f"{asset} invalid bias: {bias!r}"
        confidence = _as_confidence(entry.get("confidence"))
        if confidence is None or not (0 <= confidence <= 100):
            return False, None, (
                f"{asset} invalid confidence (must be integer 0-100): {entry.get('confidence')!r}"
            )
        if entry.get("reaction_type") not in VALID_REACTION:
            return False, None, (
                f"{asset} invalid reaction_type: {entry.get('reaction_type')!r}"
            )
        entry["confidence"] = confidence
    return True, output, ""


# ---------------------------------------------------------------------------
# analyze call
# ---------------------------------------------------------------------------

def run_analyze(event: dict) -> tuple[dict | None, dict | None]:
    """Call analyze(event), retrying once on invalid output.

    Returns (output, raw_output).  output is None when the call errored or
    stayed invalid; raw_output holds the last model response (or None).
    """
    raw_output: dict | None = None
    for attempt in (1, 2):
        try:
            output = asyncio.run(macro_analyst.analyze(event))
        except RuntimeError as exc:
            print(f"ERROR: macro_analyst.analyze call failed: {exc}", file=sys.stderr)
            return None, raw_output
        except Exception as exc:  # noqa: BLE001 - surface, do not crash silently
            print(f"ERROR: macro_analyst.analyze call failed: {exc}", file=sys.stderr)
            return None, raw_output
        raw_output = output
        valid, normalised, reason = validate_analyze_output(output)
        if valid:
            return normalised, output
        print(
            f"WARNING: analyze output invalid (attempt {attempt}/2): {reason}",
            file=sys.stderr,
        )
    return None, raw_output


# ---------------------------------------------------------------------------
# JSONL log
# ---------------------------------------------------------------------------

def append_record(record: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_analyze(event: dict, output: dict | None, valid: bool,
                raw_output: dict | None) -> str:
    record = {
        "id": str(uuid.uuid4()),
        "type": "ANALYZE",
        "logged_at": _iso(_now_utc()),
        "event": event,
        "output": output if valid else (raw_output if raw_output is not None else None),
        "valid": bool(valid),
        "model": settings.llm_model,
        "eval_status": "PENDING" if valid else "SKIPPED",
    }
    append_record(record)
    return record["id"]


# ---------------------------------------------------------------------------
# evaluation jobs (persisted so restarts never lose them)
# ---------------------------------------------------------------------------

def load_pending_jobs() -> list[dict]:
    if not PENDING_PATH.exists():
        return []
    jobs: list[dict] = []
    with PENDING_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                jobs.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("skipping unparseable pending-job line: %r", line[:120])
    return jobs


def save_pending_jobs(jobs: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = PENDING_PATH.with_suffix(".jsonl.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        for job in jobs:
            fh.write(json.dumps(job, ensure_ascii=False) + "\n")
    os.replace(tmp_path, PENDING_PATH)


def find_analyze_record(analyze_id: str) -> dict | None:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("id") == analyze_id and record.get("type") == "ANALYZE":
            return record
    return None


def evaluate_exists(analyze_id: str, window_min: int) -> bool:
    """True when an EVALUATE record for this analyze/window is already logged."""
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (record.get("type") == "EVALUATE"
                and record.get("analyze_id") == analyze_id
                and record.get("window_min") == window_min):
            return True
    return False


def create_eval_jobs(analyze_id: str) -> list[str]:
    """Create the +15 / +60 / +240 pending evaluation jobs for one analysis."""
    now = _now_utc()
    jobs = load_pending_jobs()
    job_ids: list[str] = []
    for window in WINDOWS:
        job = {
            "job_id": str(uuid.uuid4()),
            "analyze_id": analyze_id,
            "window_min": window,
            "due_at": _iso(now + timedelta(minutes=window)),
            "status": "PENDING",
            "attempts": 0,
            "note": None,
            "created_at": _iso(now),
        }
        jobs.append(job)
        job_ids.append(job["job_id"])
    save_pending_jobs(jobs)
    return job_ids


# ---------------------------------------------------------------------------
# market-hours handling (gold, silver, equities)
# ---------------------------------------------------------------------------

def _market_open(dt: datetime) -> bool:
    """Market open for GOLD/SILVER/EQUITIES at the given UTC instant.

    Bitcoin trades 24/7 and is handled separately.  The metals/equities
    sessions follow the project's session detector in the market timezone;
    weekends are closed.
    """
    from app.market.session_detector import SessionName, detect_session

    if dt.weekday() >= 5:  # Saturday / Sunday
        return False
    return detect_session(dt) is not SessionName.OFF_HOURS


def _next_open(after: datetime) -> datetime | None:
    """Next instant at or after `after` when the market is open, or None."""
    cursor = after
    limit = after + timedelta(days=MARKET_SCAN_DAYS)
    step = timedelta(minutes=5)
    while cursor <= limit:
        if _market_open(cursor):
            return cursor
        cursor += step
    return None


# ---------------------------------------------------------------------------
# price bundle for one evaluation job
# ---------------------------------------------------------------------------

def _window_value(price: float | None) -> dict | None:
    if price is None:
        return None
    return {"price": price, "high": None, "low": None, "source": "Coinbase" if price is not None else None}


def fetch_evaluation_prices(job: dict, price_at_bias: dict) -> tuple[dict | None, bool]:
    """Fetch prices for one due evaluation job.

    Returns (prices, feed_ok).  prices maps "+15min"/"+60min"/"+240min" to
    per-asset bundles (or None per asset when no feed exists).  feed_ok is
    False when a reachable feed (Coinbase/BITCOIN) failed all its retries.
    """
    window = job["window_min"]
    prices: dict[str, dict | None] = {"+15min": None, "+60min": None, "+240min": None}
    bundle: dict[str, dict | None] = {}
    for asset in ASSETS:
        if asset != "BITCOIN":
            bundle[asset] = None  # no feed exists -> null
            continue
        price: float | None = None
        for _attempt in range(FEED_RETRIES):
            price = _fetch_price("BITCOIN")
            if price is not None:
                break
            time.sleep(1.0)
        if price is None:
            return None, False
        bundle[asset] = _window_value(price)
    prices[f"+{window}min"] = bundle
    return {"price_at_bias": price_at_bias, "prices": prices, "history": None}, True


def run_evaluate(previous_analysis: dict, prices: dict) -> dict:
    return asyncio.run(macro_analyst.evaluate(previous_analysis, prices))


def log_evaluate(analyze_id: str, window_min: int, output: dict) -> None:
    append_record({
        "id": str(uuid.uuid4()),
        "type": "EVALUATE",
        "logged_at": _iso(_now_utc()),
        "analyze_id": analyze_id,
        "window_min": window_min,
        "output": output,
    })


def process_job(job: dict, jobs: list[dict]) -> str:
    """Process one due PENDING job.  Mutates + persists the job list.

    Returns one of: DONE, MISSED, DEFERRED, STILL_PENDING.
    """
    analyze_id = job["analyze_id"]
    window_min = job["window_min"]

    # Already evaluated (crash between log and mark-done): dedup and close out.
    if evaluate_exists(analyze_id, window_min):
        job["status"] = "DONE"
        job["note"] = (job.get("note") or "") + " | already evaluated; job closed"
        save_pending_jobs(jobs)
        return "DONE"

    due = _parse_iso(job.get("due_at"))

    # Market closed at due time for GOLD/SILVER/EQUITIES -> defer to next open.
    if due is not None and not _market_open(due):
        nxt = _next_open(due) if due is not None else None
        if nxt is None:
            job["status"] = "MISSED"
            job["note"] = f"market did not reopen within {MARKET_SCAN_DAYS} days after {_iso(due)}"
            save_pending_jobs(jobs)
            return "MISSED"
        job["due_at"] = _iso(nxt)
        job["status"] = "DEFERRED"
        job["note"] = (
            f"market closed at {_iso(due)} for GOLD/SILVER/EQUITIES; "
            f"deferred to next open {_iso(nxt)} (Bitcoin trades 24/7)"
        )
        save_pending_jobs(jobs)
        return "DEFERRED"

    # Fetch prices at due time; reachable-feed failure after retries => MISSED.
    record = find_analyze_record(analyze_id)
    if record is None:
        job["status"] = "MISSED"
        job["note"] = f"source ANALYZE record {analyze_id} not found in log"
        save_pending_jobs(jobs)
        return "MISSED"
    event = record.get("event") or {}
    current_prices = event.get("current_prices") or {}
    price_at_bias = {asset: current_prices.get(asset) for asset in ASSETS}

    prices, feed_ok = fetch_evaluation_prices(job, price_at_bias)
    if not feed_ok:
        job["attempts"] = int(job.get("attempts", 0)) + 1
        if int(job.get("attempts", 0)) >= FEED_RETRIES:
            job["status"] = "MISSED"
            job["note"] = f"price feed unavailable after {FEED_RETRIES} retries at {_iso(due or _now_utc())}"
            save_pending_jobs(jobs)
            return "MISSED"
        save_pending_jobs(jobs)
        return "STILL_PENDING"

    # Evaluate via the macro analyst (LLM).  Failures are retried on later ticks.
    try:
        previous_analysis = record.get("output")
        output = run_evaluate(previous_analysis, prices)
    except RuntimeError as exc:
        job["attempts"] = int(job.get("attempts", 0)) + 1
        log.warning("evaluate call failed for job %s: %s", job["job_id"], exc)
        if int(job.get("attempts", 0)) >= EVAL_MAX_ATTEMPTS:
            job["status"] = "MISSED"
            job["note"] = f"evaluate call failed after {EVAL_MAX_ATTEMPTS} attempts: {exc}"
        save_pending_jobs(jobs)
        return "MISSED" if job["status"] == "MISSED" else "STILL_PENDING"
    except Exception as exc:  # noqa: BLE001
        job["attempts"] = int(job.get("attempts", 0)) + 1
        log.warning("evaluate call failed for job %s: %s", job["job_id"], exc)
        if int(job.get("attempts", 0)) >= EVAL_MAX_ATTEMPTS:
            job["status"] = "MISSED"
            job["note"] = f"evaluate call failed after {EVAL_MAX_ATTEMPTS} attempts: {exc}"
        save_pending_jobs(jobs)
        return "MISSED" if job["status"] == "MISSED" else "STILL_PENDING"

    log_evaluate(analyze_id, window_min, output)
    job["status"] = "DONE"
    job["note"] = (job.get("note") or "") + f" | evaluated {_iso(_now_utc())}"
    save_pending_jobs(jobs)
    return "DONE"


def tick_scheduler() -> int:
    """One scheduler pass: process every due PENDING job. Returns jobs processed."""
    jobs = load_pending_jobs()
    now = _now_utc()
    processed = 0
    for job in jobs:
        if job.get("status") != "PENDING":
            continue
        due = _parse_iso(job.get("due_at"))
        if due is None or due > now:
            continue
        status = process_job(job, jobs)
        processed += 1
        if status in ("DONE", "MISSED"):
            continue  # terminal; subsequent ticks skip it
    return processed


def run_scheduler() -> None:
    """Long-running scheduler loop (one tick every 30 seconds)."""
    print(f"macro analyst scheduler started - tick {SCHEDULER_TICK_SECONDS}s")
    print(f"log: {LOG_PATH}")
    print(f"pending jobs: {PENDING_PATH}")
    print("Press Ctrl-C to stop.")
    try:
        while True:
            try:
                n = tick_scheduler()
                if n:
                    print(f"scheduler tick: {n} job(s) processed at {_iso(_now_utc())}")
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.exception("scheduler tick failed: %s", exc)
            time.sleep(SCHEDULER_TICK_SECONDS)
    except KeyboardInterrupt:
        print("\nscheduler stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_summary(event: dict, output: dict | None, valid: bool) -> None:
    headline = event.get("headline")
    print("-" * 66)
    print("MACRO ANALYST - ANALYZE summary")
    print("-" * 66)
    print(f"headline: {headline}")
    print(f"source: {event.get('source')}")
    print(f"validated: {valid}")
    if valid and output:
        assets = output.get("assets", {})
        print(f"news_type: {output.get('news_type')}  regime: {output.get('market_regime')}")
        print(f"cross_asset_check: {output.get('cross_asset_check')}")
        print(f"{'asset':<10}{'bias':<12}{'confidence':<12}{'reaction_type':<16}invalidation")
        print("-" * 66)
        for asset in ASSETS:
            entry = assets.get(asset, {}) or {}
            print(
                f"{asset:<10}{str(entry.get('bias')):<12}"
                f"{str(entry.get('confidence')):<12}"
                f"{str(entry.get('reaction_type')):<16}"
                f"{entry.get('invalidation') or '-'}"
            )
        if output.get("risk_flags"):
            print("risk_flags: " + "; ".join(str(f) for f in output["risk_flags"]))
    print("-" * 66)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the macro news analyst on a news event, or run the "
                    "evaluation scheduler (--run-scheduler).")
    parser.add_argument("--headline", required=False, default=None,
                        help="news headline (required unless --run-scheduler)")
    parser.add_argument("--source", default=None, help="news source")
    parser.add_argument("--actual", default=None, help="actual release value")
    parser.add_argument("--forecast", default=None, help="forecast release value")
    parser.add_argument("--previous", default=None, help="previous release value")
    parser.add_argument("--event-file", default=None,
                        help="JSON file with extra event fields")
    parser.add_argument("--run-scheduler", action="store_true",
                        help="run the persistent evaluation scheduler")
    args = parser.parse_args(argv)

    if args.run_scheduler:
        run_scheduler()
        return 0

    if not args.headline:
        parser.error("--headline is required (omit only with --run-scheduler)")

    event = build_event(args)
    output, raw_output = run_analyze(event)
    valid = output is not None
    analyze_id = log_analyze(event, output, valid, None if valid else raw_output)

    if valid:
        create_eval_jobs(analyze_id)
        print_summary(event, output, valid)
        print(f"logged id={analyze_id} type=ANALYZE valid=true eval_status=PENDING")
        print(f"evaluation jobs due +15 / +60 / +240 min created in {PENDING_PATH.name}")
        return 0

    print_summary(event, None, False)
    print(
        f"logged id={analyze_id} type=ANALYZE valid=false eval_status=SKIPPED "
        f"(raw model output logged; see {LOG_PATH.name})",
        file=sys.stderr,
    )
    print(f"evaluation jobs were NOT created for an invalid analysis", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
