"""
CROSS-PROVIDER CALENDAR MERGE (phase 2).

Two providers reporting the same release is the NORMAL case, not an error:
FinancialJuice renders a multi-day calendar while faireconomy publishes the
current week, so they overlap heavily. Counting one release twice would double
its weight in the aggregate, so events are matched and merged.

MATCHING RULE
-------------
Two rows are the same release when ALL of these hold:

  * the country matches;
  * normalised titles have token-set similarity >= ``calendar_merge_similarity``
    (default 0.85), ignoring case, punctuation, and ONLY the qualifier words
    m/m, y/y, q/q, prelim and final - those decorate a release rather than
    distinguishing it;
  * the scheduled times are within ``calendar_merge_time_tolerance_minutes``
    (default 5).

MERGE RULE
----------
Field-level, and priority-ordered: the FIRST provider in
``calendar_providers`` that holds a value wins, and later providers only fill
gaps. A disagreement on ``actual`` is resolved in favour of the higher-priority
provider and logged ONCE per event per poll, never silently averaged - an
average of two published numbers is a number nobody published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from app.analysis.economic_impact import CalendarEvent
from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

#: Decorations that do not distinguish one release from another. Dropped for
#: MATCHING ONLY - the stored title keeps them, so the dashboard still shows
#: "Core CPI m/m" rather than a flattened "Core CPI".
_MATCH_NOISE = frozenset({
    "prelim", "preliminary", "final", "revised", "revision", "flash",
    "index", "rate", "rates", "the", "of", "and", "for", "in", "on",
})

#: Frequency qualifiers are dropped ONLY for the containment half of the
#: similarity. Jaccard still sees them, which is what keeps "Turkish CPI YoY"
#: and "Turkish CPI MoM" from collapsing into one release: dropping m/m and
#: y/y entirely made them identical and merged two distinct prints, losing one
#: macro datapoint and mixing a YoY actual into a MoM forecast.
_FREQ_QUALIFIERS = frozenset({
    "m/m", "mm", "mom", "month", "monthly",
    "y/y", "yy", "yoy", "year", "yearly",
    "q/q", "qq", "qoq", "quarter", "quarterly",
})

_WORD = re.compile(r"[a-z0-9]+")


def _words(title: str) -> set[str]:
    return {w for w in _WORD.findall((title or "").lower())
            if w not in _MATCH_NOISE and len(w) > 1}


def normalise_title(title: str) -> set[str]:
    """
    Lowercase, strip punctuation, drop qualifier words - for MATCHING only.

    "US Core CPI m/m" and "US Core CPI y/y" both reduce to {us, core, cpi},
    which is the intent: they are the same release family at two frequencies.
    """
    return _words(title)


def _has_freq_conflict(a: str, b: str) -> bool:
    """
    True when two titles name DIFFERENT measurement frequencies.

    "CPI m/m" vs "CPI y/y" are two separate prints at the same hour. Merging
    them would attribute a monthly actual to a yearly forecast, so they are
    kept separate regardless of how similar the rest of the title is.
    """
    def freqs(text: str) -> set[str]:
        return {w for w in _WORD.findall((text or "").lower())
                if w in _FREQ_QUALIFIERS}

    fa, fb = freqs(a), freqs(b)
    if not fa or not fb:
        return False          # one side unqualified: not a conflict
    return fa != fb


def title_similarity(a: str, b: str) -> float:
    """Blend of Jaccard and containment, 0..1.

    Containment rescues padded headlines ("US CPI m/m" vs "US Core CPI m/m
    preliminary"); Jaccard stops one shared word merging two unrelated prints.
    """
    ta, tb = normalise_title(a), normalise_title(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    jaccard = inter / len(ta | tb)
    containment = inter / min(len(ta), len(tb))
    return 0.5 * jaccard + 0.5 * containment


def same_release(a: CalendarEvent, b: CalendarEvent,
                 similarity_threshold: Optional[float] = None,
                 tolerance_minutes: Optional[int] = None) -> bool:
    """True when two rows describe the same scheduled release."""
    threshold = (similarity_threshold if similarity_threshold is not None
                 else float(settings.calendar_merge_similarity))
    tolerance = (tolerance_minutes if tolerance_minutes is not None
                 else int(settings.calendar_merge_time_tolerance_minutes))

    if (a.country or "").strip().upper() != (b.country or "").strip().upper():
        return False
    # Different measurement frequencies are different releases, however similar
    # the rest of the title is. Caught before similarity so a lenient threshold
    # can never merge a MoM print into its YoY twin.
    if _has_freq_conflict(a.title, b.title):
        return False
    if title_similarity(a.title, b.title) < threshold:
        return False
    if a.when_utc is None or b.when_utc is None:
        return False
    delta = abs((a.when_utc - b.when_utc).total_seconds()) / 60.0
    return delta <= tolerance


@dataclass
class MergeStats:
    """What the merge did, for logging and the report."""

    input_events: int = 0
    merged: int = 0
    output_events: int = 0
    filled_actual: int = 0
    filled_forecast: int = 0
    filled_previous: int = 0
    disagreements: int = 0
    pairs: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "input_events": self.input_events,
            "merged": self.merged,
            "output_events": self.output_events,
            "filled_actual": self.filled_actual,
            "filled_forecast": self.filled_forecast,
            "filled_previous": self.filled_previous,
            "disagreements": self.disagreements,
        }


def _clone(event: CalendarEvent, **overrides) -> CalendarEvent:
    """Copy an event, optionally overriding fields. Never mutates the input."""
    base = {
        "title": event.title,
        "country": event.country,
        "when_utc": event.when_utc,
        "impact": event.impact,
        "forecast": event.forecast,
        "previous": event.previous,
        "actual": event.actual,
        "provider": event.provider,
    }
    base.update(overrides)
    return CalendarEvent(**base)


def _fill(target: CalendarEvent, other: CalendarEvent, stats: MergeStats,
          title: str) -> None:
    """
    Copy across any field the higher-priority row is missing.

    A DISAGREEMENT (both present, both different) is never averaged. The
    higher-priority provider's value stands and the clash is counted once, so
    it is visible in the stats rather than buried.
    """
    for field, counter in (("actual", "filled_actual"),
                           ("forecast", "filled_forecast"),
                           ("previous", "filled_previous")):
        mine = getattr(target, field)
        theirs = getattr(other, field)
        if mine is None and theirs is not None:
            setattr(target, field, theirs)
            setattr(stats, counter, getattr(stats, counter) + 1)
        elif (mine is not None and theirs is not None
              and mine != theirs):
            stats.disagreements += 1
            log.info(
                "Calendar merge: %s %s disagreement for %r - keeping %s=%r, "
                "discarding %s=%r", target.country, field, title,
                target.provider, mine, other.provider, theirs)


def merge_events(per_provider: Sequence[tuple[str, Iterable[CalendarEvent]]],
                 stats: Optional[MergeStats] = None) -> list[CalendarEvent]:
    """
    Merge provider batches into one deduplicated list.

    ``per_provider`` is ordered by PRIORITY: ``[(name, events), ...]``, and the
    first entry wins every conflict. Output order follows first appearance,
    and each output keeps the ``event_key`` of its winning row so repeated
    polls UPDATE rather than duplicate - the same key format is preserved
    because the winning row is a real ``CalendarEvent``, never a synthesis.
    """
    stats = stats if stats is not None else MergeStats()

    batches = [(name, list(events or [])) for name, events in per_provider]
    stats.input_events = sum(len(items) for _, items in batches)

    winners: list[CalendarEvent] = []

    for provider_name, events in batches:
        for event in events:
            match_index = None
            for index, existing in enumerate(winners):
                if same_release(existing, event):
                    match_index = index
                    break
            if match_index is None:
                # New release: keep it, preserving the row's own event_key.
                winners.append(_clone(event))
                continue

            stats.merged += 1
            target = winners[match_index]
            _fill(target, event, stats, target.title)
            stats.pairs.append((provider_name, target.title,
                                title_similarity(target.title, event.title)))

    stats.output_events = len(winners)
    return winners
