"""
pytest wiring for the Step 0 golden baseline harness.

    pytest -k golden            # run the corpus
    pytest -k golden -v         # one line per headline
    pytest -k golden --collect-only

The corpus itself lives in ``tests/golden/headlines.jsonl`` and the runner
logic in ``tests/golden/golden_runner.py``. This module only turns each
corpus row into a parametrised pytest case so a failure names the exact
headline, asset and expectation.

What "green on current behaviour" means here
--------------------------------------------
A row flagged ``known_defect`` asserts the engine's CURRENT (wrong) output.
It passes today and turns red the moment somebody fixes the underlying
mapping, which is the intended tripwire. Rows without the flag assert the
human ground truth directly.

No engine code is imported differently from production: the same functions
run, in the same order, through ``golden_runner.run_case``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_RUNNER = _HERE / "golden_runner.py"

# ``tests/`` has no __init__.py, so the runner is imported by file path
# rather than as ``tests.golden.golden_runner``.
_spec = importlib.util.spec_from_file_location("golden_runner", _RUNNER)
golden_runner = importlib.util.module_from_spec(_spec)
sys.modules["golden_runner"] = golden_runner
_spec.loader.exec_module(golden_runner)

CASES = golden_runner.load_corpus()


def _ids():
    """Readable pytest ids: ``<id> <asset> <group>``."""
    return [
        pytest.param(
            c,
            id=f"{c.id}-{c.expected_asset}-{c.group}",
        )
        for c in CASES
    ]


@pytest.mark.golden
@pytest.mark.parametrize("case", _ids())
def test_golden_headline(case):
    """One headline must reproduce its frozen baseline (or its label)."""
    result = golden_runner.run_case(case)
    failures = golden_runner.check_case(result)

    if not failures:
        return

    lines = []
    for f in failures:
        lines.append(f"  [{f.what}]")
        lines.append(f"    expected : {f.expected}")
        lines.append(f"    got      : {f.got}")
        if f.detail:
            lines.append(f"    detail   : {f.detail}")
        lines.append(f"    events   : {', '.join(f.event_types) or '(none)'}")
        lines.append(f"    category : {result.category}")
        if f.known_defect:
            lines.append("    NOTE: this row is flagged known_defect, so the "
                         "engine CHANGED. Re-adjudicate it: promote "
                         "`baseline_*` into `expected_*` in "
                         "tests/golden/headlines.jsonl and clear "
                         "`known_defect`, or revert the engine change.")
        else:
            lines.append("    NOTE: this row asserts the human label "
                         "directly, so this is a regression.")

    pytest.fail(
        "\n".join([
            "",
            f"golden {case.id} [{case.expected_asset}] {case.group}",
            f"  headline : {case.title}",
            f"  source   : {case.source}",
            f"  notes    : {case.notes}",
            *lines,
        ])
    )


def test_golden_corpus_is_coherent():
    """The corpus itself must satisfy the invariants the runner relies on."""
    problems = []

    if len(CASES) < 60:
        problems.append(
            f"corpus has only {len(CASES)} rows; a usable baseline needs 60+")

    assets_seen = {c.expected_asset for c in CASES}
    missing = set(golden_runner.ASSETS) - assets_seen
    if missing:
        problems.append(f"no rows for asset(s): {sorted(missing)}")

    duplicate_titles = [
        t for t in {c.title for c in CASES}
        if sum(1 for c in CASES if c.title == t) > 1
    ]
    if duplicate_titles:
        # The same headline may legitimately assert two different assets.
        # What must NOT happen is the same headline asserted twice for the
        # SAME asset - that is a duplicated row, not a second opinion.
        seen = {}
        for c in CASES:
            seen.setdefault(c.title, set()).add(c.expected_asset)
        bad = [t for t in duplicate_titles if len(seen[t]) == 1]
        if bad:
            problems.append(
                "duplicate title with the SAME expected_asset (a real "
                f"duplicate, not two asset views): {bad[:5]}")

    if not any(c.known_defect for c in CASES):
        problems.append(
            "no row is flagged known_defect - either the engine is perfect "
            "or the labels were copied from its output")

    clean = [c for c in CASES if not c.known_defect]
    if not clean:
        problems.append("every row is a known defect; the corpus asserts nothing")

    if problems:
        pytest.fail("\n".join(f"  - {p}" for p in problems))


def test_golden_corpus_reproduces_the_engine():
    """End-to-end: every row, no filters, zero unexpected failures."""
    _results, failures, defect_failures = golden_runner.run(CASES)

    if failures or defect_failures:
        pytest.fail(golden_runner.format_summary(
            CASES, _results, failures, defect_failures))
