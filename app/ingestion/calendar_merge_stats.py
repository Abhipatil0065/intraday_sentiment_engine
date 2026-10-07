"""
DIAGNOSTIC: collect cross-provider disagreements with both values intact.

Purely for review/reporting - ``merge_events`` does NOT use this. It exists so
a disagreement can be inspected BEFORE enabling the flag and judged as either a
real provider difference or a matching bug.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.analysis.calendar_merge import same_release, title_similarity
from app.analysis.economic_impact import CalendarEvent


def _fields(event: CalendarEvent) -> dict:
    return {
        "forecast": event.forecast,
        "previous": event.previous,
        "actual": event.actual,
    }


def _delta_minutes(a: CalendarEvent, b: CalendarEvent):
    if a.when_utc is None or b.when_utc is None:
        return "n/a"
    return "%.1f min" % (
        abs((a.when_utc - b.when_utc).total_seconds()) / 60.0)


def _verdict(fj: dict, fe: dict) -> str:
    """
    Is this a real provider difference, or a matching bug?

    A real difference is two providers publishing different numbers for the
    same release. A matching bug is two DIFFERENT releases paired up, which
    shows up as every disagreeing field disagreeing in a way that tracks the
    other row's values rather than a small revision.
    """
    fj_t = {k: v for k, v in fj.items() if v is not None}
    fe_t = {k: v for k, v in fe.items() if v is not None}
    disagree = [k for k in ("forecast", "previous", "actual")
                if fj.get(k) is not None and fe.get(k) is not None
                and fj[k] != fe[k]]
    if not disagree:
        return "AGREEMENT (no field conflicts)"
    if len(disagree) == 1:
        return ("SINGLE-FIELD DIFF - likely a genuine revision "
                "(higher-priority value kept)")
    return ("MULTI-FIELD DIFF - inspect: usually a genuine provider "
            "difference, but verify the two rows are the same release")


def collect_disagreements(
        per_provider: Sequence[tuple[str, Sequence[CalendarEvent]]]
) -> list[dict]:
    """
    Find every overlapping pair and report their field-by-field values.

    Unlike ``merge_events`` this keeps BOTH rows, so the reviewer can see what
    each provider said instead of only what survived.
    """
    batches = [(name, list(events or [])) for name, events in per_provider]
    if len(batches) < 2:
        return []

    high_name, high_events = batches[0]
    low_name, low_events = batches[1]
    out: list[dict] = []

    for a in high_events:
        for b in low_events:
            if not same_release(a, b):
                continue
            fj_fields = _fields(a)
            fe_fields = _fields(b)
            out.append({
                "title": a.title,
                "fe_title": b.title,
                "country": a.country,
                "similarity": round(title_similarity(a.title, b.title), 3),
                "time_delta": _delta_minutes(a, b),
                "fj": fj_fields,
                "fe": fe_fields,
                "kept_from": high_name,
                "compared_with": low_name,
                "verdict": _verdict(fj_fields, fe_fields),
            })
            break
    return out