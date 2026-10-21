"""
ECONOMIC CALENDAR: standardised surprise + EVENT_RISK (improvement item 8).

Two changes over the previous flat-threshold behaviour:

1. STANDARDISED SURPRISE. The old code called any move under 2% "no surprise",
   which is meaningless across indicators: a 0.2% CPI miss is enormous, and a
   120k payroll miss is routine. Surprise is now measured in standard
   deviations of the indicator itself::

       z = (actual - forecast) / std(indicator)

   The std table is config (``SURPRISE_STD_TABLE``), so it can be retuned per
   indicator without touching code, and an unknown indicator falls back to
   ``surprise_std_default`` rather than being force-fitted.

2. EVENT_RISK. When a high-impact release is imminent, any standing bias is
   about to be invalidated by a print nobody can predict. The engine says so
   explicitly instead of presenting a confident-looking score.

The engine still NEVER invents a print: RELEASED mode uses the realised
surprise, EXPECTED mode uses published consensus and is labelled as such.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings


def matched_std(title: str) -> Optional[float]:
    """
    The historical std of the indicator a title names, or None.

    Matched longest-key-first against the config table, so "core pce" wins
    over "pce". None means the indicator is NOT in the table - which is
    different from "the table says 1.0": a caller that needs to know whether
    a real std exists (the regional bias engine falls back to a percent
    deviation when it does not) can ask here, while :func:`std_for` keeps
    returning the default for everyone else.
    """
    lowered = (title or "").lower()
    stds = settings.surprise_stds
    best_key, best_len = None, -1
    for key in stds:
        if key and key in lowered and len(key) > best_len:
            best_key, best_len = key, len(key)
    if best_key is None:
        return None
    value = float(stds[best_key])
    return value if value > 0 else None


def std_for(title: str) -> float:
    """
    Historical standard deviation for one calendar indicator.

    Matched longest-key-first against the config table, so "core pce" wins over
    "pce". Returns ``surprise_std_default`` when the indicator is unknown.
    """
    value = matched_std(title)
    if value is None:
        return float(settings.surprise_std_default)
    return value


def standardised_surprise(actual: float, forecast: Optional[float],
                          title: str = "") -> Optional[float]:
    """
    (actual - forecast) / std, in standard deviations.

    None when there is no forecast to compare against - the engine does not
    invent the consensus it was not given.
    """
    if forecast is None or actual is None:
        return None
    denominator = std_for(title)
    if denominator <= 0:
        return None
    return (float(actual) - float(forecast)) / denominator


def surprise_band(z: Optional[float]) -> str:
    """
    NEUTRAL / MODERATE / STRONG / EXTREME from a standardised surprise.

    Bands are configurable so they can be tuned against the hit-rate report
    (item 11) rather than guessed at.
    """
    if z is None:
        return "NEUTRAL"
    magnitude = abs(float(z))
    neutral = float(settings.surprise_neutral_band)
    strong = float(settings.surprise_strong_band)
    if magnitude < neutral:
        return "NEUTRAL"
    if magnitude < strong:
        return "MODERATE"
    if magnitude < strong * 2.0:
        return "STRONG"
    return "EXTREME"


def event_risk_check(events, *, now: Optional[datetime] = None,
                     minutes: Optional[int] = None,
                     min_importance: Optional[str] = None,
                     other_events=None) -> tuple[bool, str]:
    """
    Is a market-moving release imminent? Returns ``(at_risk, note)``.

    A scheduled high-impact print within the lookahead means the current bias
    is unreliable by construction, and the trader must be told that rather than
    shown a confident score that is about to be overwritten.

    ``other_events`` are releases from regions OUTSIDE the whitelist. They
    cannot score - they are not evidence for a dollar - but they still move
    the market, so an imminent one raises the same warning, labelled
    "other region".
    """
    now = now or datetime.now(timezone.utc)
    lookahead = (minutes if minutes is not None
                 else int(settings.event_risk_lookahead_minutes))
    threshold = (min_importance or settings.event_risk_min_importance).upper()
    horizon = now + timedelta(minutes=lookahead)

    soonest = None
    for event in events or []:
        when = getattr(event, "when_utc", None)
        if when is None:
            continue
        impact = str(getattr(event, "impact", "") or "").strip().lower()
        if threshold == "HIGH" and impact != "high":
            continue
        if now <= when <= horizon:
            if soonest is None or when < soonest.when_utc:
                soonest = event

    soonest_other = None
    for event in other_events or []:
        when = getattr(event, "when_utc", None)
        if when is None:
            continue
        impact = str(getattr(event, "impact", "") or "").strip().lower()
        if threshold == "HIGH" and impact != "high":
            continue
        if now <= when <= horizon:
            if soonest_other is None or when < soonest_other.when_utc:
                soonest_other = event

    # A whitelisted release that is sooner wins the wording; an imminent
    # release from another region still raises the warning, labelled as such.
    if soonest is None and soonest_other is None:
        return False, ""
    if soonest is None or (soonest_other is not None
                           and soonest_other.when_utc < soonest.when_utc):
        target = soonest_other
        label = f"{target.title} ({target.country})".strip()
        return True, (f"EVENT RISK (other region): {label} releases in "
                      f"~{max(0, int((target.when_utc - now).total_seconds() // 60))} min. "
                      f"This bias may be invalidated by the print - size "
                      f"positions accordingly.")
    minutes_out = max(0, int((soonest.when_utc - now).total_seconds() // 60))
    label = f"{soonest.title} ({soonest.country})".strip()
    return True, (f"EVENT RISK: {label} releases in ~{minutes_out} min. "
                  f"This bias may be invalidated by the print - size "
                  f"positions accordingly.")