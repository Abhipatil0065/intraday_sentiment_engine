"""
GOLDEN CORPUS RUNNER (Step 0 baseline harness).

For every labelled headline in ``tests/golden/headlines.jsonl`` this module
executes the REAL engine path, in order, exactly as production does for a
freshly-classified article:

    detect_event_types(title, category)      app/analysis/transmission_map.py:927
    -> resolve(title, category)              app/analysis/transmission_map.py:981
    -> build_signal(...)                     app/analysis/signal_factory.py:49
    -> compute([signal], asset)              app/analysis/sentiment_engine.py:1019
    -> to_display(score, bias)               app/analysis/display.py:98

and asserts four things:

  1. ``expected_event_types``  - the set of event types that fired
  2. ``expected_direction``    - the per-asset direction ``resolve`` produced
  3. ``compute`` bias          - the label the aggregator published
  4. ``display`` split         - the bull/bear percentage a card would render

WHY THE SUITE IS GREEN ON CURRENT BEHAVIOUR
-------------------------------------------
Every row carries two independent label sets:

  * ``expected_*``      - the HUMAN ground truth, captured by a developer
                          reading the headline and deciding what should happen.
  * ``baseline_*``      - what the engine ACTUALLY produced when the corpus was
                          frozen (tools/build_golden_corpus.py).

``known_defect`` is True exactly when the two disagree. A known-defect row
asserts the FROZEN BASELINE, not the human label. That is deliberate:

  * today the row passes, so the suite is a green "before" snapshot;
  * the moment somebody fixes the engine, that row goes RED and names the
    asset and the event types, which is the signal to re-adjudicate it;
  * a row with no ``known_defect`` asserts the human label directly and goes
    red on the first regression.

Nothing here changes engine behaviour. There is no auto-fix, no tolerance
window and no "close enough" direction match. ``MIXED`` is not ``BULLISH``.

USAGE
-----
    python -m tests.golden.golden_runner                # summary + mismatch table
    python -m tests.golden.golden_runner --verbose      # full per-row table
    python -m tests.golden.golden_runner --group btc_regulation
    python -m tests.golden.golden_runner --asset XAGUSD
    pytest -k golden                                     # same checks as a gate
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORPUS_PATH = Path(__file__).resolve().parent / "headlines.jsonl"

ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD", "EQUITY")
VALID_DIRECTIONS = {"BULLISH", "BEARISH", "MIXED", "NEUTRAL"}
REQUIRED_FIELDS = (
    "id", "group", "title", "published_at", "source",
    "expected_asset", "expected_direction", "expected_event_types",
    "baseline_direction", "baseline_event_types", "gate", "known_defect",
)

# A fixed clock so the run is deterministic regardless of wall time.
NOW = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)
# Every headline is pinned to "25 minutes old" so the decay factor is
# identical across rows; Step 0 measures the mapping, not the clock.
AGE = timedelta(minutes=25)


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------
@dataclass
class GoldenCase:
    """One labelled headline."""

    id: str
    group: str
    title: str
    body: str
    published_at: str
    source: str
    source_quality: str
    expected_asset: str
    expected_direction: str
    expected_event_types: list
    notes: str
    known_defect: bool
    baseline_direction: str
    baseline_event_types: list
    baseline_category: str = ""
    #: ``settings.only_high_impact_counts`` when the baseline was frozen.
    #: The corpus is only reproducible under the setting it was recorded
    #: under, so the runner forces it for the duration of the row.
    gate: bool = True

    @property
    def is_known_defect(self) -> bool:
        return self.known_defect


@dataclass
class RowResult:
    """What the engine produced for one headline."""

    case: GoldenCase
    category: str
    event_types: list
    direction: str
    magnitude: float
    cue_magnitude: float
    relevance: float
    bias: str
    score: float
    confidence: float
    display: dict
    contributing: int

    @property
    def asset(self) -> str:
        return self.case.expected_asset


def load_corpus(path: Path = CORPUS_PATH) -> list:
    """Read the JSONL corpus. Malformed rows raise rather than being skipped.

    A crash here is the point: a silently dropped row is a silently unguarded
    headline, and the whole value of the corpus is that every row is checked.
    """
    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()

    for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"{path.name} line {lineno} is not valid JSON: {exc}") from exc

        for key in REQUIRED_FIELDS:
            if key not in row:
                raise AssertionError(
                    f"{path.name} line {lineno} ({row.get('id', '?')}): "
                    f"missing required field {key!r}")

        cid = row["id"]
        if cid in seen_ids:
            raise AssertionError(f"{path.name}: duplicate id {cid!r}")
        seen_ids.add(cid)

        asset = row["expected_asset"]
        if asset not in ASSETS:
            raise AssertionError(
                f"{path.name} line {lineno} ({cid}): expected_asset must be "
                f"one of {sorted(ASSETS)}, got {asset!r}")

        for key in ("expected_direction", "baseline_direction"):
            if row[key] not in VALID_DIRECTIONS:
                raise AssertionError(
                    f"{path.name} line {lineno} ({cid}): {key} must be one of "
                    f"{sorted(VALID_DIRECTIONS)}, got {row[key]!r}")

        for key in ("expected_event_types", "baseline_event_types"):
            if not isinstance(row[key], list) or any(
                    not isinstance(v, str) for v in row[key]):
                raise AssertionError(
                    f"{path.name} line {lineno} ({cid}): {key} must be a "
                    f"list of strings, got {row[key]!r}")

        if not isinstance(row["known_defect"], bool):
            raise AssertionError(
                f"{path.name} line {lineno} ({cid}): known_defect must be a "
                f"bool, got {row['known_defect']!r}")

        cases.append(GoldenCase(
            id=cid,
            group=row["group"],
            title=row["title"],
            body=row.get("body", ""),
            published_at=row.get("published_at", ""),
            source=row.get("source", ""),
            source_quality=row.get("source_quality", "TIER_2_MAJOR"),
            expected_asset=asset,
            expected_direction=row["expected_direction"],
            expected_event_types=list(row["expected_event_types"]),
            notes=row.get("notes", ""),
            known_defect=row["known_defect"],
            baseline_direction=row["baseline_direction"],
            baseline_event_types=list(row["baseline_event_types"]),
            baseline_category=row.get("baseline_category", ""),
            gate=bool(row["gate"]),
        ))
    return cases


# ---------------------------------------------------------------------------
# The engine path under test
# ---------------------------------------------------------------------------
def run_case(case: GoldenCase, now: datetime = NOW) -> RowResult:
    """Execute detect_event_types -> resolve -> build_signal -> compute."""
    from app.analysis import sentiment_engine as se
    from app.analysis.cue_rules import detect_category
    from app.analysis.display import to_display
    from app.analysis.signal_factory import build_signal
    from app.analysis.transmission_map import detect_event_types, measure_cue_magnitude, resolve
    from app.config import settings
    from app.models.enums import (Asset, Direction, EconomicImportance,
                                  ImpactStrength, NewsCategory, ReportKind)

    text = f"{case.title} {case.body}".strip()

    # The impact gate decides whether MEDIUM/LOW evidence contributes at
    # all. tests/conftest.py turns it OFF for the legacy suite, so the corpus
    # pins the value it was frozen under and restores it afterwards - a golden
    # row must not silently change meaning because of a sibling fixture.
    gate_was = settings.only_high_impact_counts
    settings.only_high_impact_counts = case.gate
    try:
        result = _run_case_locked(case, text, now)
    finally:
        settings.only_high_impact_counts = gate_was
    return result


def _run_case_locked(case: GoldenCase, text: str, now: datetime) -> RowResult:
    from app.analysis import sentiment_engine as se
    from app.analysis.cue_rules import detect_category
    from app.analysis.display import to_display
    from app.analysis.signal_factory import build_signal
    from app.analysis.transmission_map import detect_event_types, measure_cue_magnitude, resolve
    from app.models.enums import (Asset, Direction, EconomicImportance,
                                  ImpactStrength, NewsCategory, ReportKind)

    # A clean carry-forward slate: one headline alone must not inherit a
    # verdict from the previous row in the corpus.
    se.clear_bias_history()

    category = detect_category(text)
    events = detect_event_types(text, category)
    event_names = [e.value for e in events]
    resolved = resolve(text, category)

    asset = Asset(case.expected_asset)
    outcome = resolved[asset]

    strength = _strength_for(outcome)
    published = now - AGE

    signal = build_signal(
        asset=asset,
        title=case.title,
        direction=Direction(outcome.direction.value),
        strength=strength,
        importance=EconomicImportance.MEDIUM,
        published_at=published,
        relevance=_relevance_for(case, asset, category),
        persistence=0.60,
        source_quality=case.source_quality or "TIER_2_MAJOR",
        source_name=case.source,
        category=category,
        independent_sources=1,
        report_kind=ReportKind.GENERAL,
        session_multiplier=1.0,
    )

    result = se.compute([signal], asset, now=now)

    return RowResult(
        case=case,
        category=category.value,
        event_types=event_names,
        direction=outcome.direction.value,
        magnitude=round(outcome.magnitude, 3),
        cue_magnitude=round(measure_cue_magnitude(text), 3),
        relevance=round(signal.relevance, 3),
        bias=result.bias.value,
        score=result.raw_score,
        confidence=result.confidence,
        display=to_display(result.raw_score, result.bias),
        contributing=result.contributing_articles,
    )


def _strength_for(outcome):
    """A strong reading only for a directional, high-magnitude outcome."""
    from app.models.enums import Direction, ImpactStrength

    if outcome.direction is Direction.NEUTRAL:
        return ImpactStrength.LOW
    if outcome.magnitude >= 0.90:
        return ImpactStrength.VERY_HIGH
    if outcome.magnitude >= 0.60:
        return ImpactStrength.HIGH
    if outcome.magnitude >= 0.35:
        return ImpactStrength.MEDIUM
    return ImpactStrength.LOW


def _relevance_for(case: GoldenCase, asset, category) -> float:
    """The production relevance for this asset/category pair.

    ``relevance_for`` raises KeyError for categories absent from its matrix
    (``NewsCategory.OTHER`` is the one this corpus hits); the pipeline
    tolerates that by treating the asset as unrouted, so the harness must
    mirror it rather than crash.
    """
    from app.analysis.asset_relevance import relevance_for as _rel

    try:
        return _rel(asset, category, case.title)
    except (KeyError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
@dataclass
class Failure:
    """One unmet expectation, rendered for a human."""

    case_id: str
    asset: str
    what: str
    expected: object
    got: object
    known_defect: bool
    detail: str = ""
    event_types: list = field(default_factory=list)


def check_case(result: RowResult) -> list:
    """Return the list of unmet assertions for one row (empty == pass)."""
    case = result.case
    failures: list[Failure] = []

    # A known-defect row freezes the engine's CURRENT behaviour; a clean row
    # asserts the human label.
    want_events = (case.baseline_event_types if case.known_defect
                   else case.expected_event_types)
    want_direction = (case.baseline_direction if case.known_defect
                      else case.expected_direction)

    # 1. event-type set, compared as a SET so ordering is irrelevant but
    #    membership is exact: no missing types, no extras.
    if result.event_types != want_events:
        failures.append(Failure(
            case_id=case.id, asset=case.expected_asset,
            what="event types",
            expected=", ".join(want_events) or "(none)",
            got=", ".join(result.event_types) or "(none)",
            known_defect=case.known_defect,
            event_types=result.event_types,
        ))

    # 2. per-asset direction from resolve()
    if result.direction != want_direction:
        failures.append(Failure(
            case_id=case.id, asset=case.expected_asset,
            what="direction",
            expected=want_direction, got=result.direction,
            known_defect=case.known_defect,
            event_types=result.event_types,
            detail=(f"cue={result.cue_magnitude} mag={result.magnitude} "
                    f"cat={result.category}"),
        ))

    # 3. compute() must publish a label consistent with the resolved
    #    direction. A directionless signal cannot move a single-item score,
    #    so it must land on the carry-forward default with score 0.
    if result.direction in ("BULLISH", "BEARISH"):
        if result.bias != result.direction:
            failures.append(Failure(
                case_id=case.id, asset=case.expected_asset,
                what="compute bias",
                expected=result.direction, got=result.bias,
                known_defect=case.known_defect,
                event_types=result.event_types,
                detail=(f"score={result.score:+.1f} "
                        f"conf={result.confidence:.3f}"),
            ))
    else:
        if result.score != 0.0 or result.contributing != 0:
            failures.append(Failure(
                case_id=case.id, asset=case.expected_asset,
                what="compute bias (directionless)",
                expected="score 0.0 / 0 contributing",
                got=f"score {result.score:+.1f} / {result.contributing} contributing",
                known_defect=case.known_defect,
                event_types=result.event_types,
            ))

    # 4. The rendered split must name the same side the label did.
    label = result.display.get("label")
    if label not in ("BULLISH", "BEARISH", "NEUTRAL"):
        failures.append(Failure(
            case_id=case.id, asset=case.expected_asset,
            what="display label",
            expected="BULLISH | BEARISH | NEUTRAL", got=label,
            known_defect=case.known_defect,
            event_types=result.event_types,
        ))
    if result.display.get("bull_pct", -1) + result.display.get("bear_pct", -1) != 100:
        failures.append(Failure(
            case_id=case.id, asset=case.expected_asset,
            what="display split sums to 100",
            expected=100,
            got=result.display.get("bull_pct", 0) + result.display.get("bear_pct", 0),
            known_defect=case.known_defect,
            event_types=result.event_types,
        ))

    return failures


def run(cases: list, now: datetime = NOW) -> tuple:
    """Run the corpus. Returns (results, failures, defect_failures)."""
    results: list[RowResult] = []
    failures: list[Failure] = []
    defect_failures: list[Failure] = []

    for case in cases:
        result = run_case(case, now=now)
        results.append(result)
        for f in check_case(result):
            if f.known_defect:
                defect_failures.append(f)
            else:
                failures.append(f)
    return results, failures, defect_failures


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_summary(cases: list, results: list,
                   failures: list, defect_failures: list) -> str:
    clean = [c for c in cases if not c.known_defect]
    dirty = [c for c in cases if c.known_defect]
    groups = sorted({c.group for c in cases})
    assets = {a: sum(1 for c in cases if c.expected_asset == a) for a in ASSETS}

    lines = [
        "=" * 104,
        "GOLDEN CORPUS  (Step 0 baseline harness)",
        "=" * 104,
        f"rows              : {len(cases)} headlines, {len(groups)} groups",
        "per-asset         : " + ", ".join(f"{a} {assets[a]}" for a in ASSETS),
        f"                  : {len({c.source for c in cases})} distinct sources",
        f"human-clean rows  : {len(clean)}  (assert the human label)",
        f"known-defect rows : {len(dirty)}  (assert the frozen baseline)",
        f"failures          : {len(failures)} unexpected, "
        f"{len(defect_failures)} known-defect drift",
        "-" * 104,
    ]
    if not failures and not defect_failures:
        lines.append("STATUS: GREEN - the engine reproduces the frozen "
                     "baseline on every row.")
    else:
        if failures:
            lines.append(f"STATUS: RED - {len(failures)} unexpected "
                         f"failure(s) on human-clean rows.")
        if defect_failures:
            lines.append(f"NOTE: {len(defect_failures)} known-defect row(s) "
                         f"drifted - a fix landed; re-adjudicate them.")
    return "\n".join(lines)


def format_failures(failures: list, defect_failures: list,
                    cases_by_id: dict) -> str:
    if not failures and not defect_failures:
        return ""

    def block(title: str, items: list) -> list:
        if not items:
            return []
        out = ["", "=" * 104, title, "=" * 104]
        for f in items:
            case = cases_by_id[f.case_id]
            out.append("")
            out.append(f"[{f.case_id}] ({case.group})  {case.title}")
            out.append(f"    asset : {f.asset}   source: {case.source}")
            out.append(f"    events: {', '.join(f.event_types) or '(none)'}")
            out.append(f"    {f.what}")
            out.append(f"      expected : {f.expected}")
            out.append(f"      got      : {f.got}")
            if f.detail:
                out.append(f"      detail   : {f.detail}")
            out.append(f"    notes  : {case.notes}")
        return out

    lines = block("UNEXPECTED FAILURES  (human-clean rows - these are "
                  "regressions)", failures)
    lines += block("KNOWN-DEFECT DRIFT  (a fix changed the engine; move the "
                   "row from baseline to expected)", defect_failures)
    return "\n".join(lines)


def format_detail(results: list) -> str:
    lines = [
        "=" * 104,
        "PER-ROW DETAIL",
        "=" * 104,
        f"{'ID':<6}{'ASSET':<8}{'GROUP':<26}{'DIRECTION':<10}"
        f"{'BIAS':<10}{'SCORE':>7}{'CONF':>6}  EVENTS",
        "-" * 104,
    ]
    for r in results:
        lines.append(
            f"{r.case.id:<6}{r.asset:<8}{r.case.group[:25]:<26}"
            f"{r.direction:<10}{r.bias:<10}{r.score:>+7.1f}"
            f"{r.confidence:>6.2f}  {', '.join(r.event_types) or '(none)'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Golden corpus runner")
    parser.add_argument("--verbose", action="store_true",
                        help="print the full per-row table")
    parser.add_argument("--group", default=None,
                        help="restrict the run to one corpus group")
    parser.add_argument("--asset", default=None, choices=ASSETS,
                        help="restrict the run to one asset")
    args = parser.parse_args(argv)

    cases = load_corpus()
    if args.group:
        cases = [c for c in cases if c.group == args.group]
    if args.asset:
        cases = [c for c in cases if c.expected_asset == args.asset]
    if not cases:
        print("No cases match the given filter.")
        return 1

    results, failures, defect_failures = run(cases)
    print(format_summary(cases, results, failures, defect_failures))
    print(format_failures(failures, defect_failures,
                          {c.id: c for c in cases}))
    if args.verbose:
        print()
        print(format_detail(results))

    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
