"""
FINANCIALJUICE ECONOMIC CALENDAR PROVIDER (phase 1).

A SECOND, INDEPENDENT source of scheduled macro releases, alongside
faireconomy. It exists because faireconomy only publishes the current week -
``ff_calendar_nextweek.json`` is a genuine 404 - so a longer forward horizon
was never available. FinancialJuice renders several days at once.

WHAT THIS IS NOT
----------------
It is NOT the headline scraper. ``app/ingestion/financialjuice.py`` and its
launch flags are left completely untouched. This module launches PLAIN
Chromium:

    * no --disable-blink-features=AutomationControlled
    * no stealth plugin
    * no user-agent or canvas fingerprint spoofing
    * no challenge solving, ever

Live verification showed the calendar hydrates and reads identically with and
without such flags, so none is needed or used. If Cloudflare serves a
challenge or block page, this poll FAILS, is logged ONCE, and is backed off
for ``fj_calendar_backoff_minutes``. It is never retried rapidly and the
challenge is never attempted to be solved.

FAILURE IS ALWAYS FAILURE, NEVER "NO EVENTS"
---------------------------------------------
The page is a JavaScript app. Reading it too early yields a small, nearly
empty shell that looks exactly like a quiet calendar. So:

  * the render WAITS on a condition (row count), never a fixed sleep;
  * fewer than ``fj_calendar_min_rows`` rows is a FAILED poll, and is never
    merged into the calendar as "there is nothing today".

NEWS AND NUMBERS ONLY. No price is read, fetched, derived or stored here.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.analysis.economic_impact import CalendarEvent, classify_event, to_float
from app.config import settings
from app.ingestion.chromium_lock import chromium_slot


class BrowserBusy(RuntimeError):
    """
    Raised when the shared Chromium slot could not be taken in time.

    This is a DEFERRED poll, not a failure: the caller records it and returns,
    and the next poll renders normally.
    """
from app.logging_conf import get_logger

log = get_logger(__name__)

PROVIDER_NAME = "financialjuice"

#: Root holding the rendered calendar. Verified live: 386 ``div-table-row``
#: elements, of which ~192 are event header/value pairs.
CAL_CONTAINER = "#my-cal-data"

#: Text markers that mean we were served a challenge/block page rather than
#: the app. Any of these is a FAILED poll - never a solve attempt.
CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
    "attention required",
    "access denied",
    "cf-challenge",
)

#: FJ encodes impact as a CSS modifier ``imp-N`` on the values row.
#: Distribution observed live: imp-3=94, imp-2=49, imp-1=30.
IMPACT_BY_CODE = {"3": "High", "2": "Medium", "1": "Low"}

#: Title -> ISO country. FJ renders no country column, so the country is
#: derived from the event name itself. Kept explicit rather than guessed: an
#: unrecognised title yields "" and the event is dropped downstream.
#:
#: ORDER MATTERS. Explicit country and institution markers are tested FIRST,
#: because generic indicators ("cpi", "pmi", "gdp", "retail sales") appear in
#: every country's calendar. Testing USD's generic list first would classify
#: "Eurozone CPI YoY" and "Japanese Services PMI" as US releases.
COUNTRY_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    # --- explicit country / institution markers, checked first ---
    (("eurozone", "euro area", "ecb", "german", "germany", "french", "france",
      "italian", "italy", "spanish", "spain", "dutch", "portuguese"), "EUR"),
    (("uk ", "u.k.", "british", "boe", "bank of england", "gilts"), "GBP"),
    (("japan", "japanese", "boj", "nikkei"), "JPY"),
    (("china", "chinese", "caixin", "pboc"), "CNY"),
    (("swiss", "switzerland", "snb"), "CHF"),
    (("canada", "canadian", "boc ", "boerse"), "CAD"),
    (("australia", "australian", "rba"), "AUD"),
    (("new zealand", "rbnz", "nzier"), "NZD"),
    (("korea", "korean", "bok"), "KRW"),
    (("turkey", "turkish", "tcmb"), "TRY"),
    (("brazil", "brazilian", "bcb"), "BRL"),
    (("india", "indian", "rbi"), "INR"),
    (("us ", "u.s.", "united states", "american", "federal reserve", "fomc",
      "fed", "powell", "nonfarm", "non-farm", "nfp", "ism", "pce", "dxy",
      "dollar index", "crude oil", "eia", "api crude", "jobless",
      "initial claim", "s&p", "aig", "philly fed", "kansas city", "durable"),
     "USD"),
    # --- generic indicators: only reached when no country was named ---
    (("cpi", "ppi", "pce", "pmi", "gdp", "retail sales", "durable goods",
      "consumer confidence", "housing", "jobless claim", "initial jobless",
      "payroll", "inflation"), "USD"),
)

#: Speaker-style rows ("ECB's Lane Speaks") carry no numbers at all. They are
#: recognised so they can still be PROFILED (phase 4), but must never produce
#: a surprise because there is no forecast to compare against.
_SPEAKER = re.compile(
    r"\b(speaks?|speech|testif\w+|press conf\w+|keynote|remarks|presser)\b",
    re.IGNORECASE)

#: Month headings on the page, e.g. "October 05".
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_DATE_HEADING = re.compile(r"^([A-Za-z]{3,9})\s+(\d{1,2})$")

#: The timezone the browser context is pinned to. FJ renders in the BROWSER's
#: timezone (verified live by rendering the same page under two timezones and
#: watching every clock shift), so pinning this to UTC makes the rendered clock
#: UTC and the conversion to UTC the identity - independent of the host machine.
#: ``fj_calendar_render_timezone`` must agree with it; see render_tz_matches().
FJ_CONTEXT_TZ = "UTC"


def render_tz_matches() -> bool:
    """
    True when the configured render timezone matches the pinned context.

    They are two halves of one fact and must not drift: the context decides
    what the page renders, the config decides how it is read back. A mismatch
    would skew EVERY timestamp by the difference, so it is refused rather than
    corrected silently.
    """
    configured = settings.fj_render_tz
    return configured.utcoffset(None) == timezone.utc.utcoffset(None)


def guess_country(title: str) -> str:
    """
    Derive the ISO country from an event title.

    FJ renders no country column, so this is the only signal available. It is
    a deliberate lookup against a fixed table rather than a heuristic: an
    unrecognised title returns "" and the caller drops the event, because a
    wrong country would mis-weight the signal through the country multiplier.

    NEEDLES are tried in TWO passes: first as whole words (``\\b``-anchored), so
    "fed" cannot match inside "federal"; then as plain substrings, so
    distinctive multi-word needles and possessives like "Fed's Williams Speaks"
    still resolve. A hit in either pass wins.
    """
    lowered = (title or "").lower()
    for pass_fn in (lambda n: re.search(rf"\b{re.escape(n)}\b", lowered),
                    lambda n: n in lowered):
        for needles, code in COUNTRY_HINTS:
            for needle in needles:
                if pass_fn(needle):
                    return code
    return ""


def parse_page_date(heading: str, reference: datetime) -> Optional[date]:
    """'October 05' -> a real date, resolved against ``reference``'s year."""
    match = _DATE_HEADING.match((heading or "").strip())
    if not match:
        return None
    month = _MONTHS.get(match.group(1)[:3].lower())
    if month is None:
        return None
    try:
        day = int(match.group(2))
    except ValueError:
        return None
    year = reference.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    # A heading months BEHIND the reference is next year's tail; months ahead
    # is next year's head. Pick whichever is closer, so a page spanning a year
    # boundary never produces a date 11 months in the past.
    candidates = []
    for delta in (-1, 0, 1):
        try:
            candidates.append(candidate.replace(year=year + delta))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates,
               key=lambda d: abs((d - reference.date()).days))


def resolve_when(clock: str, day: Optional[date],
                 render_tz: timezone,
                 reference: datetime) -> Optional[datetime]:
    """
    '13:15' + a page date -> an aware UTC datetime.

    The page renders times in the BROWSER's timezone, so the wall clock must be
    interpreted in ``render_tz`` before conversion. That offset is NOT guessed:
    it was verified against faireeconomy's copy of the same event (Italian
    Services PMI rendered 13:15 while faireconomy published 07:45 UTC, i.e.
    UTC+05:30) and is configuration.
    """
    match = re.match(r"^(\d{1,2}):(\d{2})$", (clock or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    base_day = day or reference.astimezone(render_tz).date()
    try:
        local = datetime(base_day.year, base_day.month, base_day.day,
                         hour, minute, tzinfo=render_tz)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


#: Impact rank, keyed by the UPPERCASE string CalendarEvent/impact use.
IMPACT_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "HOLIDAY": 0}


def impact_from_class(code: Optional[str]) -> str:
    """'3' -> 'High'. Unknown or absent falls back to Medium, never 'High'."""
    if not code:
        return "Medium"
    return IMPACT_BY_CODE.get(str(code).strip(), "Medium")


def parse_rows(raw_rows: list[dict], reference: Optional[datetime] = None,
               render_tz: Optional[timezone] = None,
               min_importance: str = "MEDIUM") -> tuple[list[CalendarEvent], int]:
    """
    Turn the page's raw row dicts into ``CalendarEvent`` records.

    Returns ``(events, skipped)``. FJ splits each event across TWO
    ``div-table-row`` elements - a header row (time/title/impact) and a values
    row (actual/forecast/previous) - so the caller pairs them before calling
    this.

    ``reference`` fixes "today" for resolving the page's month/day headings, so
    the result does not depend on the hour the poll happened to run.
    """
    reference = reference or datetime.now(timezone.utc)
    render_tz = render_tz or settings.fj_render_tz
    # Default 0, NOT 2: an unrecognised floor must not silently promote a Low
    # row to Medium and let it through a MEDIUM filter.
    floor = IMPACT_RANK.get((min_importance or "MEDIUM").upper(), 0)

    events: list[CalendarEvent] = []
    skipped = 0
    seen: set[str] = set()

    for row in raw_rows:
        title = (row.get("title") or "").strip()
        clock = (row.get("time") or "").strip()
        if not title or not clock:
            skipped += 1
            continue

        when = resolve_when(clock, row.get("date"), render_tz, reference)
        if when is None:
            skipped += 1
            continue

        country = (row.get("country") or "").strip().upper() or guess_country(title)
        if not country:
            # No country means no country multiplier can be applied, and the
            # engine's USD-only gate would silently drop it anyway.
            skipped += 1
            continue

        impact = impact_from_class(row.get("impact"))
        # Uppercase both sides: impact_from_class returns "High", the rank
        # table is keyed "HIGH", and a case mismatch silently ranked every
        # row as 0 and dropped the whole calendar.
        if IMPACT_RANK.get(impact.upper(), 0) < floor:
            skipped += 1
            continue

        event = CalendarEvent(
            title=title,
            country=country,
            when_utc=when,
            impact=impact,
            # Values go through the shared phase-3 parser. Unparseable text
            # ('-', '<0.1%', '1.5|2.1') becomes None, which the impact engine
            # reads as "no surprise" rather than guessing.
            forecast=to_float(row.get("forecast")),
            previous=to_float(row.get("previous")),
            actual=to_float(row.get("actual")),
            provider=PROVIDER_NAME,
        )
        if event.event_key in seen:
            skipped += 1
            continue
        seen.add(event.event_key)
        events.append(event)

    return events, skipped


# --------------------------------------------------------------------------
# Provider result + health
# --------------------------------------------------------------------------
class ChallengeDetected(Exception):
    """A Cloudflare challenge or block page was served. Never solved."""


class HydrationFailure(Exception):
    """The SPA had not hydrated: too few rows to be a real calendar."""


@dataclass
class ProviderResult:
    """Outcome of one provider poll. Never raises to its caller."""

    provider: str
    events: list[CalendarEvent] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    row_count: int = 0
    skipped: int = 0
    seconds: float = 0.0
    challenge: bool = False
    memory_mb: Optional[float] = None
    # True when the poll was skipped because another render held the shared
    # Chromium slot. Distinct from a failure: the provider is healthy, the
    # poll simply did not run and will run on the next cycle.
    deferred: bool = False

    @property
    def event_count(self) -> int:
        return len(self.events)


# --------------------------------------------------------------------------
# In-page extraction
# --------------------------------------------------------------------------
#: Pairs each event header row with the values row beneath it, and carries the
#: current "October 05" heading forward. Written as one DOM snapshot so the
#: page is read exactly once and cannot shift mid-parse.
_EXTRACT_JS = """
(opts) => {
  const box = document.querySelector(opts.container);
  if (!box) return {rows: [], total: 0, challenge: false, text: ''};
  const rowsAll = Array.from(box.querySelectorAll('div.div-table-row'));
  const out = [];
  let currentDate = null;
  let currentDateText = null;
  for (let i = 0; i < rowsAll.length; i++) {
    const cols = {};
    for (const c of rowsAll[i].querySelectorAll(':scope > div')) {
      const raw = (c.className || '');
      const key = raw.replace('div-table-col', '').trim().split(/\\s+/)[0];
      cols[key] = (c.innerText || '').trim();
    }
    if (cols['cal-event-date'] !== undefined) {
      currentDateText = cols['cal-event-date'];
      continue;
    }
    if (cols['event-time'] && cols['event-title']) {
      const nxt = rowsAll[i + 1];
      let vals = {}, imp = null;
      if (nxt) {
        for (const c of nxt.querySelectorAll(':scope > div')) {
          const cls = (c.className || '');
          const key = cls.replace('div-table-col', '').trim().split(/\\s+/)[0];
          vals[key] = (c.innerText || '').trim();
          const m = cls.match(/imp-(\\d)/);
          if (m) imp = m[1];
        }
      }
      out.push({
        dateText: currentDateText,
        time: cols['event-time'],
        title: cols['event-title'],
        impact: imp,
        actual: vals['event-actual'] || null,
        forecast: vals['event-forcast'] || null,
        previous: vals['event-previous'] || null,
        hasValues: !!(vals['event-actual'] || vals['event-forcast']
                      || vals['event-previous'])
      });
    }
  }
  const text = (document.body.innerText || '').slice(0, 4000);
  return {rows: out, total: rowsAll.length, challenge: false, text: text};
}
"""


def _rows_to_raw(rows: list[dict], reference: datetime) -> list[dict]:
    """Attach a resolved ``date`` object to each row for the parser."""
    out = []
    for row in rows:
        record = dict(row)
        record["date"] = parse_page_date(row.get("dateText") or "", reference)
        out.append(record)
    return out


#: Why a rendered row did not become an event. Used by the review probe so a
#: drop is always explainable rather than a silent shrink.
SKIP_REASONS = (
    "no title or time",
    "unresolvable date/time",
    "unknown country",
    "below min importance",
    "duplicate row",
)


async def summarise_skips(reason_counts: Optional[Counter] = None
                          ) -> tuple:
    """
    Render the page and tally WHY rows were dropped, with sample titles.

    Kept out of ``parse_rows`` so the hot path stays allocation-light; the
    counts come from a second diagnostic pass over the same live page.
    """
    from collections import Counter as _Counter

    counts = reason_counts if reason_counts is not None else _Counter()
    data = await _render_once()
    reference = datetime.now(timezone.utc)
    rows = _rows_to_raw(data.get("rows") or [], reference)

    samples = {name: [] for name in SKIP_REASONS}
    seen_titles: set = set()
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title or not (row.get("time") or "").strip():
            _tally(counts, samples, "no title or time", title)
            continue
        if resolve_when(row["time"], row.get("date"),
                        settings.fj_render_tz, reference) is None:
            _tally(counts, samples, "unresolvable date/time", title)
            continue
        if not guess_country(title):
            _tally(counts, samples, "unknown country", title)
            continue
        impact = impact_from_class(row.get("impact"))
        if IMPACT_RANK.get(impact.upper(), 0) < IMPACT_RANK.get(
                (settings.economic_calendar_min_importance or "MEDIUM").upper(), 0):
            _tally(counts, samples, "below min importance", title)
            continue
        if title in seen_titles:
            _tally(counts, samples, "duplicate row", title)
        seen_titles.add(title)
    return counts, samples


def _tally(counts: "Counter", samples: dict, reason: str, title: str) -> None:
    counts[reason] += 1
    bucket = samples.setdefault(reason, [])
    if title and len(bucket) < 10:
        bucket.append(title)


# --------------------------------------------------------------------------
# Backoff state (module-level, per process)
# --------------------------------------------------------------------------
_backoff_until: Optional[datetime] = None
_failure_logged = False
_health: dict = {}


def in_backoff(now: Optional[datetime] = None) -> bool:
    """True while a detected challenge/block is still cooling off."""
    if _backoff_until is None:
        return False
    return (now or datetime.now(timezone.utc)) < _backoff_until


def note_challenge(reason: str = "challenge") -> None:
    """
    Record a challenge/block and start the backoff. Logs ONCE on the first
    failure and once on recovery - not on every poll.

    The challenge is never attempted to be solved, and we never retry inside
    the backoff window; faireconomy remains the provider.
    """
    global _backoff_until, _failure_logged
    minutes = int(settings.fj_calendar_backoff_minutes)
    _backoff_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    if not _failure_logged:
        log.warning(
            "FinancialJuice calendar: %s - marking this poll FAILED and "
            "backing off %d min. Not retrying, not solving. "
            "faireeconomy's schedule is unaffected.", reason, minutes)
        _failure_logged = True


def clear_backoff() -> None:
    """Called on a successful poll; logs the recovery once."""
    global _backoff_until, _failure_logged
    if _failure_logged:
        log.info("FinancialJuice calendar: recovered - challenge backoff cleared.")
    _backoff_until = None
    _failure_logged = False


def provider_health() -> dict:
    """Per-provider health for the optional API field."""
    return dict(_health)


def _record_health(provider: str, ok: bool, rows: int,
                  error: str = "") -> None:
    entry = _health.setdefault(provider, {
        "last_success_utc": None,
        "consecutive_failures": 0,
        "last_row_count": 0,
        "last_error": "",
    })
    entry["last_row_count"] = rows
    if ok:
        entry["last_success_utc"] = datetime.now(timezone.utc).isoformat()
        entry["consecutive_failures"] = 0
        entry["last_error"] = ""
    else:
        entry["consecutive_failures"] = int(
            entry.get("consecutive_failures", 0)) + 1
        entry["last_error"] = error[:200]


# --------------------------------------------------------------------------
# The render
# --------------------------------------------------------------------------
async def _render_once() -> dict:
    """
    One PLAIN-Chromium render. Returns the raw extraction dict.

    Launch flags are deliberately EMPTY of any detection-evasion option: no
    ``--disable-blink-features=AutomationControlled``, no stealth plugin, no
    fingerprint spoofing. Verified live that the calendar renders identically
    without them.
    """
    from playwright.async_api import async_playwright

    # Serialised against the news provider's render. Measured overlap was
    # +12 processes / ~1.0 GB RSS; the calendar waits its turn and defers to
    # the next poll rather than competing for memory.
    async with chromium_slot(wait_seconds=settings.chromium_lock_wait_seconds) as ok:
        if not ok:
            raise BrowserBusy("chromium busy; calendar render deferred")

        async with async_playwright() as pw:
            # Plain Chromium. No evasion args.
            browser = await pw.chromium.launch(headless=True)
            ctx = None
            try:
                ctx = await browser.new_context(
                    user_agent=settings.http_user_agent,
                    viewport={"width": 1440, "height": 2400},
                    locale="en-US",
                    # Pinned, so the page's rendered clocks are UTC regardless
                    # of the host machine's timezone. A plain context option.
                    timezone_id=FJ_CONTEXT_TZ,
                )
                page = await ctx.new_page()
                # Navigation can be slow on a cold cache; the hard ceiling is
                # enforced by the caller's wait_for, this is just generous
                # enough that a slow FIRST load is not mistaken for failure.
                await page.goto(
                    "https://www.financialjuice.com/", timeout=35000,
                    wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # Wait on a CONDITION, not a fixed sleep: the calendar only
                # exists once the SPA has hydrated.
                await page.wait_for_selector(
                    CAL_CONTAINER, timeout=15000, state="attached")

                # ...then until enough rows exist to be a real calendar.
                floor = int(settings.fj_calendar_min_rows)
                await page.wait_for_function(
                    """(args) => {
                        const box = document.querySelector(args.container);
                        if (!box) return false;
                        return box.querySelectorAll('div.div-table-row').length
                               >= args.floor;
                    }""",
                    arg={"container": CAL_CONTAINER, "floor": floor},
                    timeout=15000)

                return await page.evaluate(
                    _EXTRACT_JS, {"container": CAL_CONTAINER})
            finally:
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                try:
                    await browser.close()
                except Exception:
                    pass


def _looks_like_challenge(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


async def fetch_fj_calendar() -> ProviderResult:
    """
    Poll the FinancialJuice calendar ONCE. Never raises.

    Returns a ``ProviderResult`` the caller can merge or ignore. A failure
    here must never affect faireconomy or the news pipeline, so every failure
    mode - disabled, backed off, challenge, hydration timeout, network error -
    returns ``ok=False`` with an explanation rather than propagating.
    """
    if not settings.financialjuice_calendar_enabled:
        return ProviderResult(provider=PROVIDER_NAME, ok=False,
                              error="disabled")
    if in_backoff():
        return ProviderResult(provider=PROVIDER_NAME, ok=False,
                              error="backoff")
    if not render_tz_matches():
        # Refuse rather than silently skew every timestamp by the difference
        # between what the page renders and what we read it back as.
        message = (f"config timezone {settings.fj_calendar_render_timezone!r} "
                   f"does not match the pinned context {FJ_CONTEXT_TZ!r}")
        log.error("FinancialJuice calendar: %s - poll FAILED", message)
        _record_health(PROVIDER_NAME, False, 0, "timezone mismatch")
        return ProviderResult(provider=PROVIDER_NAME, ok=False,
                              error=message)

    reference = datetime.now(timezone.utc)
    started = reference
    try:
        data = await asyncio.wait_for(
            _render_once(), timeout=int(settings.fj_calendar_timeout_seconds))
    except asyncio.TimeoutError:
        _record_health(PROVIDER_NAME, False, 0, "timeout")
        return ProviderResult(provider=PROVIDER_NAME, ok=False,
                              error=f"timeout after "
                                    f"{settings.fj_calendar_timeout_seconds}s",
                              seconds=_elapsed(started))
    except BrowserBusy as exc:
        # Deferred, not failed: the news render holds the slot. Not counted as
        # a health failure, because nothing is wrong with this provider.
        log.info("FinancialJuice calendar: %s", exc)
        return ProviderResult(provider=PROVIDER_NAME, ok=False,
                              error="deferred (chromium busy)",
                              deferred=True,
                              seconds=_elapsed(started))
    except Exception as exc:  # noqa: BLE001 - a provider must never break a poll
        _record_health(PROVIDER_NAME, False, 0,
                       f"{type(exc).__name__}: {exc}")
        return ProviderResult(provider=PROVIDER_NAME, ok=False,
                              error=f"{type(exc).__name__}: {exc}",
                              seconds=_elapsed(started))

    if _looks_like_challenge(data.get("text", "")):
        note_challenge("challenge/block page detected")
        _record_health(PROVIDER_NAME, False, data.get("total", 0),
                       "challenge")
        return ProviderResult(provider=PROVIDER_NAME, ok=False,
                              error="challenge", challenge=True,
                              row_count=data.get("total", 0),
                              seconds=_elapsed(started))

    rows = data.get("rows") or []
    floor = int(settings.fj_calendar_min_rows)
    # The selector wait should already have enforced this, but a mid-read
    # re-render could still leave a short page. Below the floor it is a
    # FAILED poll, never "no events today".
    if len(rows) < floor:
        _record_health(PROVIDER_NAME, False, len(rows), "below row floor")
        return ProviderResult(
            provider=PROVIDER_NAME, ok=False,
            error=f"hydration: {len(rows)} rows < floor {floor}",
            row_count=len(rows), seconds=_elapsed(started))

    events, skipped = parse_rows(
        _rows_to_raw(rows, reference), reference=reference,
        min_importance=settings.economic_calendar_min_importance)

    clear_backoff()
    _record_health(PROVIDER_NAME, True, len(rows))
    log.info("FinancialJuice calendar: %d rows -> %d events (%d skipped) "
             "in %.1fs", len(rows), len(events), skipped, _elapsed(started))
    return ProviderResult(
        provider=PROVIDER_NAME, events=events, ok=True,
        row_count=len(rows), skipped=skipped, seconds=_elapsed(started))


def _elapsed(started: datetime) -> float:
    return round((datetime.now(timezone.utc) - started).total_seconds(), 2)


def reset_state_for_tests() -> None:
    """Clear backoff/health so tests do not leak into one another."""
    global _backoff_until, _failure_logged, _health
    _backoff_until = None
    _failure_logged = False
    _health = {}
