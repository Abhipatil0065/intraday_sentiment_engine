"""
GOLDEN CORPUS LOADER + MISMATCH RUNNER.

Purpose
-------
A labelled corpus of 50 headlines with human-expected per-asset bias. The
runner compares the engine's actual verdict against the label and prints a
MISMATCH TABLE with, for each miss:

  * the headline and its group
  * expected bias vs what the engine produced
  * the event_type(s) the transmission map detected
  * each asset's signed contribution and magnitude

It deliberately DOES NOT auto-fix anything. A golden corpus exists to surface
disagreements for a human to adjudicate; silently "correcting" the engine to
match a label would destroy the very signal the corpus is meant to provide.

Status of the labels
--------------------
Every ``expected`` field is currently the placeholder ``"TODO"`` because the
labels are being supplied by the engine's owner. The runner therefore reports
those rows as UNLABELLED rather than as passes or failures, so the suite stays
green while the corpus is being filled in. Once labels exist the same runner
becomes the regression gate automatically.

CLI
---
    python -m tests.golden_runner              # summary + mismatch table
    python -m tests.golden_runner --verbose    # full per-row table
    python -m tests.golden_runner --group btc_only
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CORPUS_PATH = Path(__file__).resolve().parent / "golden" / "headlines.jsonl"

ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD")
VALID_BIAS = {"BULLISH", "BEARISH", "NO_BIAS", "TODO"}
PLACEHOLDER = "TODO"


@dataclass
class GoldenCase:
    """One labelled headline."""

    id: str
    group: str
    text: str
    source: str
    timestamp: str
    expected: dict
    notes: str = ""
    cluster_hint: Optional[str] = None

    @property
    def is_labelled(self) -> bool:
        return all(self.expected.get(a) != PLACEHOLDER for a in ASSETS)


@dataclass
class AssetOutcome:
    """What the engine actually produced for one asset."""

    asset: str
    bias: str
    direction: str
    magnitude: float
    relevance: float
    contribution: float
    transmission: str


@dataclass
class Mismatch:
    """One expected-vs-actual disagreement, for human adjudication."""

    case_id: str
    group: str
    text: str
    asset: str
    expected: str
    got: str
    event_types: list
    outcomes: list = field(default_factory=list)

    def why(self) -> str:
        o = next((x for x in self.outcomes if x.asset == self.asset), None)
        if o is None:
            return "asset not routed at all"
        return (f"dir={o.direction} mag={o.magnitude:.2f} "
                f"rel={o.relevance:.2f} contrib={o.contribution:+.3f}")


def load_corpus(path: Path = CORPUS_PATH) -> list:
    """Read the JSONL corpus. Malformed lines raise rather than being skipped."""
    cases: list[GoldenCase] = []
    for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"{path.name} line {lineno} is not valid JSON: {exc}") from exc
        for asset in ASSETS:
            value = row.get("expected", {}).get(asset)
            if value not in VALID_BIAS:
                raise AssertionError(
                    f"{path.name} line {lineno} ({row.get('id')}): expected "
                    f"{asset} must be one of {sorted(VALID_BIAS)}, got {value!r}")
        cases.append(GoldenCase(
            id=row["id"], group=row["group"], text=row["text"],
            source=row.get("source", ""), timestamp=row["timestamp"],
            expected=row["expected"], notes=row.get("notes", ""),
            cluster_hint=row.get("cluster_hint"),
        ))
    return cases


def _bias_of(direction, strength_value: str) -> str:
    """Map an item-level direction onto the three published states.

    An item that takes no side, or one too weak to matter, is NO_BIAS rather
    than a direction - mirroring what the aggregator does at asset level.
    """
    from app.models.enums import Direction, ImpactStrength

    if direction in (Direction.BULLISH, Direction.BEARISH):
        if strength_value == ImpactStrength.LOW.value:
            return "NO_BIAS"
        return direction.value
    return "NO_BIAS"


def classify(text: str) -> dict:
    """Run the engine and return per-asset outcomes plus detected event types."""
    from app.analysis import transmission_map as tm
    from app.analysis.news_classifier import heuristic_analyze

    analysis = heuristic_analyze(text)
    detected = [e.value for e in tm.detect_event_types(text, analysis.category)]
    resolved = tm.resolve(text, analysis.category)

    outcomes = {}
    for entry in analysis.assets:
        impact = resolved[entry.asset]
        outcomes[entry.asset.value] = AssetOutcome(
            asset=entry.asset.value,
            bias=_bias_of(entry.direction, entry.strength.value),
            direction=entry.direction.value,
            magnitude=round(impact.magnitude, 3),
            relevance=round(entry.relevance, 3),
            contribution=round(impact.sign * impact.magnitude, 3),
            transmission=(impact.dominant.value if impact.dominant else "-"),
        )
    # Assets the article never routed to are UNROUTED, not NEUTRAL: "the
    # engine never considered this asset" and "the engine considered it and
    # found nothing" are different claims.
    for asset in ASSETS:
        outcomes.setdefault(asset, AssetOutcome(
            asset=asset, bias="UNROUTED", direction="UNROUTED", magnitude=0.0,
            relevance=0.0, contribution=0.0, transmission="-"))
    return {"detected": detected, "outcomes": outcomes, "analysis": analysis}


def compare(cases: list) -> tuple:
    """Compare every labelled case. Returns (mismatches, unlabelled, checked)."""
    mismatches: list[Mismatch] = []
    unlabelled: list[GoldenCase] = []
    checked = 0

    for case in cases:
        if not case.is_labelled:
            unlabelled.append(case)
            continue
        checked += 1
        result = classify(case.text)
        for asset in ASSETS:
            expected = case.expected[asset]
            got = result["outcomes"][asset].bias
            if expected != got:
                mismatches.append(Mismatch(
                    case_id=case.id, group=case.group, text=case.text,
                    asset=asset, expected=expected, got=got,
                    event_types=result["detected"],
                    outcomes=[result["outcomes"][a] for a in ASSETS],
                ))
    return mismatches, unlabelled, checked


def format_mismatch_table(mismatches: list) -> str:
    """The human-readable mismatch report."""
    if not mismatches:
        return "No mismatches."
    lines = [
        "=" * 108,
        f"GOLDEN CORPUS MISMATCHES  ({len(mismatches)} total)",
        "=" * 108,
    ]
    current = None
    for m in mismatches:
        if m.case_id != current:
            current = m.case_id
            lines.append("")
            lines.append(f"[{m.case_id}] ({m.group})  {m.text}")
            lines.append(f"    event_types: {', '.join(m.event_types) or '(none)'}")
            lines.append(f"    {'ASSET':<8}{'EXPECTED':<12}{'GOT':<12}DETAIL")
        lines.append(f"    {m.asset:<8}{m.expected:<12}{m.got:<12}{m.why()}")
    lines.append("")
    lines.append("-" * 108)
    lines.append("These are DISAGREEMENTS, not auto-fixes. Adjudicate each one:")
    lines.append("  - label wrong  -> correct tests/golden/headlines.jsonl")
    lines.append("  - engine wrong -> change the transmission map / text quality")
    lines.append("                     rules, and add a focused unit test for it")
    lines.append("-" * 108)
    return "\n".join(lines)
    text: str
def format_detail_table(cases: list, verbose: bool) -> str:
    """Optional full per-row view, for eyeballing the whole corpus."""
    lines = [
        "=" * 108,
        f"{'ID':<6}{'GROUP':<15}{'ASSET':<8}{'EXPECT':<11}{'GOT':<11}"
        f"{'DIR':<10}{'MAG':<6}{'TRANS'}"
        + ("  TEXT" if verbose else ""),
        "=" * 108,
    ]
    for case in cases:
        result = classify(case.text)
        for asset in ASSETS:
            o = result["outcomes"][asset]
            row = (f"{case.id:<6}{case.group:<15}{asset:<8}"
                   f"{case.expected[asset]:<11}{o.bias:<11}{o.direction:<10}"
                   f"{o.magnitude:<6}{o.transmission:<28}")
            if verbose:
                row += case.text[:46]
            lines.append(row)
        if verbose:
            lines.append("")
    return "\n".join(lines)


def summary(cases: list, mismatches: list, unlabelled: list, checked: int) -> str:
    groups = sorted({c.group for c in cases})
    lines = [
        "=" * 108,
        "GOLDEN CORPUS SUMMARY",
        "=" * 108,
        f"corpus size        : {len(cases)} headlines across {len(groups)} groups",
        f"groups             : {', '.join(groups)}",
        f"labels present     : {checked}",
        f"labels TODO (awaiting owner) : {len(unlabelled)}",
        f"mismatches         : {len(mismatches)}",
    ]
    if checked == 0:
        lines += [
            "",
            "NO LABELS PRESENT YET - every expected.* field is the TODO placeholder.",
            "Mismatches cannot be evaluated until the labels are supplied.",
            "Run `python -m tests.golden_runner --verbose` to inspect what the",
            "engine currently produces for each headline, as a starting point.",
        ]
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Golden corpus mismatch runner")
    parser.add_argument("--verbose", action="store_true",
                        help="print the full per-row table")
    parser.add_argument("--group", default=None,
                        help="restrict the run to one corpus group")
    args = parser.parse_args(argv)

    cases = load_corpus()
    if args.group:
        cases = [c for c in cases if c.group == args.group]
        if not cases:
            print(f"No cases in group {args.group!r}")
            return 1

    mismatches, unlabelled, checked = compare(cases)
    print(summary(cases, mismatches, unlabelled, checked))
    if args.verbose:
        print()
        print(format_detail_table(cases, verbose=True))
    else:
        print()
        print(format_mismatch_table(mismatches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    asset: str
    expected: str
    got: str
    event_types: list
    outcomes: list = field(default_factory=list)

    def why(self) -> str:
        o = next((x for x in self.outcomes if x.asset == self.asset), None)
        if o is None:
            return "asset not routed at all"
        return (f"dir={o.direction} mag={o.magnitude:.2f} "
                f"rel={o.relevance:.2f} contrib={o.contribution:+.3f}")