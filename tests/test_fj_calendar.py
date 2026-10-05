"""
PHASE 1 + 2: FJ CALENDAR PROVIDER AND CROSS-PROVIDER MERGE.

Pure unit tests - no browser, no network. The live render is verified
separately by ``python -m tests.probe_fj_calendar``.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.analysis.calendar_merge import (
    MergeStats,
    merge_events,
    normalise_title,
    same_release,
    title_similarity,
)
from app.analysis.economic_impact import CalendarEvent
from app.config import settings
from app.ingestion import fj_calendar as fj

REF = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def ev(title="US CPI m/m", country="USD", when=REF, impact="High",
       forecast=None, previous=None, actual=None, provider="x"):
    return CalendarEvent(title=title, country=country, when_utc=when,
                         impact=impact, forecast=forecast, previous=previous,
                         actual=actual, provider=provider)


@pytest.fixture(autouse=True)
def _clean_state():
    fj.reset_state_for_tests()
    yield
    fj.reset_state_for_tests()


def _row(**kw):
    base = {"dateText": "October 05", "time": "08:30",
            "title": "US Core CPI m/m", "impact": "3", "actual": "0.3%",
            "forecast": "0.2%", "previous": "0.3%",
            "date": date(2026, 10, 5)}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# Timezone: verified by EVIDENCE, not assumed
# --------------------------------------------------------------------------
def test_context_is_pinned_to_utc():
    """
    The context timezone is pinned so the page's clock cannot follow the host.

    Verified live: the same page rendered under TZ=UTC and TZ=Asia/Kolkata
    shifted EVERY clock by exactly +5:30, proving the page follows the
    browser. Pinning the context removes that dependency entirely.
    """
    assert fj.FJ_CONTEXT_TZ == "UTC"


def test_render_timezone_matches_the_pinned_context():
    """The two halves of one fact must not drift."""
    assert fj.render_tz_matches() is True


def test_timezone_mismatch_is_refused():
    """A drifted config is refused rather than silently skewing timestamps."""
    from app.config import settings

    monkey = settings.fj_calendar_render_timezone
    try:
        settings.fj_calendar_render_timezone = "UTC+05:30"
        assert fj.render_tz_matches() is False
    finally:
        settings.fj_calendar_render_timezone = monkey
    assert fj.render_tz_matches() is True


def test_conversion_is_independent_of_the_machine_timezone(monkeypatch):
    """
    The conversion must not depend on the host's TZ.

    Before pinning, the page rendered in the machine's zone; a host set to UTC
    would have had 05:30 subtracted twice, skewing every event by 5h30m. The
    render timezone is an explicit pinned constant, so the result is identical
    whatever the environment says.
    """
    import os
    import time as _time

    from app.ingestion.fj_calendar import FJ_CONTEXT_TZ

    def convert():
        return fj.resolve_when("13:15", date(2026, 10, 5),
                               settings.fj_render_tz, REF)

    results = {}
    original = os.environ.get("TZ")
    try:
        for tz in ("UTC", "Asia/Kolkata", "America/New_York"):
            os.environ["TZ"] = tz
            if hasattr(_time, "tzset"):
                _time.tzset()
            results[tz] = convert()
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        if hasattr(_time, "tzset"):
            _time.tzset()

    assert len(set(results.values())) == 1, results
    # Pinned to UTC, so the rendered clock passes through unchanged and the
    # answer is the same under every host timezone.
    assert list(results.values())[0].astimezone(
        timezone.utc).strftime("%H:%M") == "13:15"


def test_clock_converts_from_render_tz_to_utc():
    got = fj.resolve_when("13:15", date(2026, 10, 5), settings.fj_render_tz, REF)
    assert got is not None
    # Pinned to UTC, so the clock is already UTC and converts to itself.
    assert got.astimezone(timezone.utc).strftime("%H:%M") == "13:15"


def test_conversion_of_a_rendered_utc_clock_is_exact():
    """The live evidence: faireeconomy's Italian Services PMI was 07:45 UTC."""
    got = fj.resolve_when("07:45", date(2026, 10, 5), settings.fj_render_tz, REF)
    assert got.astimezone(timezone.utc).strftime("%H:%M") == "07:45"


def test_midnight_rendered_time_rolls_back_a_day():
    """
    00:30 rendered in a POSITIVE-offset zone is the previous day in UTC; a
    rollover bug would date it a day late and break the event_key.

    With the context pinned to UTC there is no rollover, and that is the point:
    the answer is decided by the pinned constant, not by the host machine.
    """
    got = fj.resolve_when("00:30", date(2026, 10, 5), settings.fj_render_tz, REF)
    assert got is not None
    assert got.astimezone(timezone.utc).date() == date(2026, 10, 5)


def test_bad_clock_is_rejected():
    assert fj.resolve_when("nope", date(2026, 10, 5), settings.fj_render_tz,
                           REF) is None
    assert fj.resolve_when("25:99", date(2026, 10, 5), settings.fj_render_tz,
                           REF) is None


# --------------------------------------------------------------------------
# Country derivation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("title,code", [
    ("US Nonfarm Payrolls", "USD"),
    ("CPI m/m", "USD"),
    ("Eurozone CPI YoY", "EUR"),
    ("German Ifo Business Climate", "EUR"),
    ("UK Retail Sales m/m", "GBP"),
    ("Japanese Services PMI", "JPY"),
    ("China Caixin PMI", "CNY"),
    # Regression: the live review dropped 54 rows as "unknown country",
    # including these obvious US releases. "fed " (with a trailing space) did
    # not match the possessive in "Fed's Williams Speaks".
    ("Fed's Williams Speaks", "USD"),
    ("Fed's Bowman Speaks", "USD"),
    ("AIG Manufacturing Index", "USD"),
    ("AIG Construction Index", "USD"),
    ("US Trade Balance", "USD"),
    ("FOMC Meeting Minutes", "USD"),
    ("US S&P Services PMI Final", "USD"),
    ("NZIER Confidence", "NZD"),
    ("S. Korean Current Account Bal NSA", "KRW"),
])
def test_country_is_derived_from_the_title(title, code):
    assert fj.guess_country(title) == code


@pytest.mark.parametrize("title", [
    # Not a country-specific macro release: no country multiplier can honestly
    # be applied, so these are dropped rather than guessed.
    "IMF's MD Georgieva Speaks",
    "Pepsi Q3 2026 Earnings",
])
def test_non_macro_titles_stay_unresolved(title):
    assert fj.guess_country(title) == ""


def test_unknown_title_yields_no_country_and_is_dropped():
    assert fj.guess_country("Zebra Widget Releases") == ""
    events, skipped = fj.parse_rows(
        [{"title": "Zebra Widget", "time": "09:00", "impact": "3",
          "date": date(2026, 10, 5)}], reference=REF)
    assert events == [] and skipped == 1


# --------------------------------------------------------------------------
# Impact decoding
# --------------------------------------------------------------------------
@pytest.mark.parametrize("code,expected", [
    ("3", "High"), ("2", "Medium"), ("1", "Low"),
    ("9", "Medium"), (None, "Medium"), ("", "Medium"),
])
def test_impact_class_decodes(code, expected):
    assert fj.impact_from_class(code) == expected


def test_missing_impact_is_never_promoted_to_high():
    assert fj.impact_from_class(None) == "Medium"

# --------------------------------------------------------------------------
# Row parsing
# --------------------------------------------------------------------------
def test_values_are_parsed_to_floats():
    events, _ = fj.parse_rows([_row()], reference=REF)
    assert len(events) == 1
    assert events[0].actual == pytest.approx(0.3)
    assert events[0].forecast == pytest.approx(0.2)


def test_unparseable_values_become_none_not_guesses():
    events, _ = fj.parse_rows(
        [_row(actual="-", forecast="<0.1%", previous="1.5|2.1")],
        reference=REF)
    e = events[0]
    assert e.actual is None and e.forecast is None and e.previous is None


def test_duplicate_rows_collapse_to_one_event():
    events, skipped = fj.parse_rows([_row(), _row()], reference=REF)
    assert len(events) == 1


def test_low_impact_rows_are_filtered():
    events, skipped = fj.parse_rows(
        [_row(title="US Housing Starts", impact="1")], reference=REF,
        min_importance="MEDIUM")
    assert events == [] and skipped == 1


def test_speaker_rows_parse_with_no_values():
    events, _ = fj.parse_rows(
        [_row(title="ECB Lane Speaks", impact="2", actual=None,
              forecast=None, previous=None)], reference=REF)
    assert len(events) == 1
    assert events[0].actual is None


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------
def test_qualifier_words_are_ignored_for_matching_only():
    assert normalise_title("US Core CPI m/m") == normalise_title("US Core CPI y/y")


def test_similar_titles_match():
    assert title_similarity("US CPI m/m", "US CPI m/m") == pytest.approx(1.0)
    assert title_similarity("US CPI m/m", "US CPI y/y") > 0.85


def test_different_titles_do_not_match():
    assert not same_release(ev(title="US CPI m/m"), ev(title="Japan GDP q/q"))


def test_different_country_never_matches():
    assert not same_release(ev(title="CPI m/m", country="USD"),
                            ev(title="CPI m/m", country="EUR"))


def test_times_beyond_tolerance_do_not_match():
    assert not same_release(ev(title="US CPI m/m", when=REF),
                            ev(title="US CPI m/m",
                                when=REF + timedelta(minutes=45)))


def test_times_inside_tolerance_do_match():
    assert same_release(ev(title="US CPI m/m", when=REF),
                        ev(title="US CPI m/m",
                           when=REF + timedelta(minutes=3)))


def test_mom_and_yoy_of_the_same_release_do_not_merge():
    """
    Regression: live merge collapsed 'Turkish CPI YoY' into 'Turkish CPI MoM'.

    Both are real, distinct prints published at the same hour. Merging them
    attributed a monthly actual to a yearly forecast, so the frequency
    qualifier must veto the match.
    """
    yoy = ev(title="Turkish CPI YoY", country="TRY", when=REF,
             actual=29.73, forecast=30.26)
    mom = ev(title="Turkish CPI MoM", country="TRY", when=REF,
             actual=1.84, forecast=2.28)
    assert not same_release(yoy, mom)
    out = merge_events([("financialjuice", [yoy]), ("faireconomy", [mom])])
    assert len(out) == 2
    assert out[0].forecast == pytest.approx(30.26)
    assert out[1].forecast == pytest.approx(2.28)


def test_same_frequency_padded_wording_still_merges():
    a = ev(title="US CPI m/m")
    b = ev(title="US CPI m/m preliminary")
    assert same_release(a, b)


def test_qualifier_words_are_ignored_for_matching_only():
    assert normalise_title("US Core CPI m/m") == normalise_title("US Core CPI y/y")


def test_similar_titles_match():
    assert title_similarity("US CPI m/m", "US CPI m/m") == pytest.approx(1.0)
    assert title_similarity("US CPI m/m", "US CPI y/y") > 0.85


def test_same_cpi_from_both_providers_becomes_one_event():
    """The headline merge case: one release, missing actual filled."""
    fj_row = ev(title="US CPI m/m", when=REF, forecast=0.2, previous=0.3,
                actual=None, provider="financialjuice")
    fe_row = ev(title="US CPI m/m", when=REF + timedelta(minutes=2),
                forecast=0.2, previous=0.3, actual=0.4, provider="faireconomy")
    stats = MergeStats()
    out = merge_events([("financialjuice", [fj_row]),
                        ("faireconomy", [fe_row])], stats)
    assert len(out) == 1
    assert stats.merged == 1
    assert out[0].actual == pytest.approx(0.4)


def test_higher_priority_wins_and_fills_gaps():
    high = ev(title="US CPI m/m", forecast=0.2, previous=0.3, actual=None,
              provider="financialjuice")
    low = ev(title="US CPI m/m", forecast=9.9, previous=9.9, actual=0.4,
             provider="faireconomy")
    out = merge_events([("financialjuice", [high]), ("faireconomy", [low])])
    assert len(out) == 1
    assert out[0].forecast == pytest.approx(0.2)   # priority stands
    assert out[0].actual == pytest.approx(0.4)     # gap filled


def test_actual_disagreement_prefers_priority_and_is_counted_once():
    high = ev(title="US CPI m/m", actual=0.4, provider="financialjuice")
    low = ev(title="US CPI m/m", actual=0.5, provider="faireconomy")
    stats = MergeStats()
    out = merge_events([("financialjuice", [high]), ("faireconomy", [low])],
                       stats)
    assert len(out) == 1
    assert out[0].actual == pytest.approx(0.4)
    assert stats.disagreements == 1


def test_disjoint_events_are_all_kept():
    out = merge_events([("financialjuice", [ev(title="US CPI m/m")]),
                        ("faireconomy", [ev(title="Japan GDP q/q")])])
    assert len(out) == 2


def test_merge_preserves_event_key_format():
    """Repeated polls must UPDATE, not duplicate."""
    row = ev(title="US CPI m/m", country="USD", when=REF)
    out = merge_events([("financialjuice", [row])])
    assert out[0].event_key == row.event_key


def test_merge_does_not_mutate_inputs():
    row = ev(title="US CPI m/m", actual=None)
    merge_events([("financialjuice", [row]), ("faireconomy", [row])])
    assert row.actual is None


def test_empty_batches_are_safe():
    assert merge_events([]) == []
    assert merge_events([("a", []), ("b", [])]) == []


# --------------------------------------------------------------------------
# Failure isolation
# --------------------------------------------------------------------------
def test_one_provider_failing_leaves_the_other_working():
    out = merge_events([("financialjuice", []),
                        ("faireconomy", [ev(title="US CPI m/m")])])
    assert len(out) == 1


def test_challenge_is_a_failure_with_backoff(monkeypatch):
    monkeypatch.setattr(settings, "fj_calendar_backoff_minutes", 30,
                        raising=False)
    fj.note_challenge("challenge/block page detected")
    assert fj.in_backoff() is True


def test_success_clears_backoff():
    fj.note_challenge("test")
    assert fj.in_backoff() is True
    fj.clear_backoff()
    assert fj.in_backoff() is False


def test_health_records_consecutive_failures():
    fj._record_health("financialjuice", False, 0, "boom")
    fj._record_health("financialjuice", False, 0, "boom")
    assert fj.provider_health()["financialjuice"]["consecutive_failures"] == 2
    fj._record_health("financialjuice", True, 190)
    health = fj.provider_health()["financialjuice"]
    assert health["consecutive_failures"] == 0
    assert health["last_row_count"] == 190
    assert health["last_success_utc"]


def test_disabled_provider_reports_disabled(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "financialjuice_calendar_enabled", False,
                        raising=False)
    result = asyncio.run(fj.fetch_fj_calendar())
    assert result.ok is False
    assert result.error == "disabled"


def test_hydration_shell_below_floor_is_a_failure(monkeypatch):
    """Fewer rows than the floor means the SPA never hydrated - a FAILED poll,
    never 'there are no events today'."""
    import asyncio

    monkeypatch.setattr(settings, "financialjuice_calendar_enabled", True,
                        raising=False)
    monkeypatch.setattr(settings, "fj_calendar_min_rows", 10, raising=False)

    async def fake_render():
        return {"rows": [{"title": "US CPI m/m", "time": "08:30",
                          "impact": "3", "dateText": "October 05"}],
                "total": 2, "challenge": False, "text": "Actual Forecast"}

    monkeypatch.setattr(fj, "_render_once", fake_render)
    result = asyncio.run(fj.fetch_fj_calendar())
    assert result.ok is False
    assert "hydration" in result.error
    assert result.events == []


def test_challenge_page_is_a_failure_not_a_solve(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "financialjuice_calendar_enabled", True,
                        raising=False)

    async def fake_render():
        return {"rows": [], "total": 0, "challenge": True,
                "text": "Just a moment... Checking your browser"}

    monkeypatch.setattr(fj, "_render_once", fake_render)
    result = asyncio.run(fj.fetch_fj_calendar())
    assert result.ok is False
    assert result.challenge is True
    assert fj.in_backoff() is True


def test_provider_uses_no_evasion_flags():
    """
    The launch must be PLAIN Chromium.

    Parsed as AST rather than grepped, because this module's comments
    legitimately NAME the banned flags in order to state they are absent. Only
    the arguments actually passed to ``chromium.launch`` are inspected.
    """
    import ast
    import inspect

    from app.ingestion.fj_calendar import _render_once

    tree = ast.parse(inspect.getsource(_render_once))
    launches = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "launch"):
            launches.append(node)
    assert len(launches) == 1, "expected exactly one browser launch"
    args = launches[0].args
    assert args == [], "chromium.launch must be called with NO positional args"

    banned = {"AutomationControlled", "--no-sandbox", "--disable-blink-features"}
    for node in ast.walk(launches[0]):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for flag in banned:
                assert flag not in node.value, flag


def test_provider_does_not_use_a_stealth_plugin():
    """
    Playwright's ``stealth``/plugin API must not appear in executable code.

    Only the launch CALL subtree is inspected; the module docstring names the
    technique in order to state it is not used.
    """
    import ast
    import inspect

    from app.ingestion.fj_calendar import _render_once

    tree = ast.parse(inspect.getsource(_render_once))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    for banned in ("use_stealth", "add_init_script", "route", "expose_function"):
        assert banned not in called, banned


def test_provider_never_reads_price():
    """News and scheduled numbers only. Scoped to executable code paths."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fj))
    # Collect every string literal that is NOT a comment or docstring.
    banned = ("price", "last_trade", "ticker", "bid", "ask")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if isinstance(node.value, str) and len(node.value) > 200:
            continue  # docstring / long JS payload
        low = node.value.lower()
        for word in banned:
            assert word not in low, f"{word!r} in {node.value[:60]!r}"


# --------------------------------------------------------------------------
# Step 2: faireconomy 429 handling
# --------------------------------------------------------------------------
def test_429_sets_a_backoff(monkeypatch):
    import app.ingestion.economic_calendar as ec

    ec.reset_provider_state()
    monkeypatch.setattr(ec.settings, "calendar_fe_429_backoff_minutes", 15,
                        raising=False)
    assert ec.faireconomy_in_backoff() is False
    assert ec.note_rate_limit() == 15
    assert ec.faireconomy_in_backoff() is True


def test_retry_after_header_wins_over_the_default(monkeypatch):
    """The server knows when it will accept requests again."""
    import app.ingestion.economic_calendar as ec

    ec.reset_provider_state()
    monkeypatch.setattr(ec.settings, "calendar_fe_429_backoff_minutes", 15,
                        raising=False)
    assert ec.note_rate_limit("300") == 5      # 300 seconds == 5 minutes


def test_retry_after_is_capped(monkeypatch):
    """A hostile or absurd header must not park the calendar indefinitely."""
    import app.ingestion.economic_calendar as ec

    ec.reset_provider_state()
    monkeypatch.setattr(ec.settings,
                        "calendar_fe_429_max_retry_after_minutes", 60,
                        raising=False)
    assert ec.note_rate_limit("86400") == 60   # 24h requested, capped to 60m


def test_unparseable_retry_after_falls_back_to_the_default(monkeypatch):
    import app.ingestion.economic_calendar as ec

    monkeypatch.setattr(ec.settings, "calendar_fe_429_backoff_minutes", 15,
                        raising=False)
    for junk in ("Wed, 21 Oct 2026 07:28:00 GMT", "", "soon"):
        ec.reset_provider_state()
        assert ec.note_rate_limit(junk) == 15


def test_success_clears_the_rate_limit():
    import app.ingestion.economic_calendar as ec

    ec.reset_provider_state()
    ec.note_rate_limit()
    assert ec.faireconomy_in_backoff() is True
    ec.clear_rate_limit()
    assert ec.faireconomy_in_backoff() is False


def test_backoff_expiry_lets_polling_resume():
    import app.ingestion.economic_calendar as ec

    ec.reset_provider_state()
    ec.note_rate_limit()
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert ec.faireconomy_in_backoff(now=later) is False


def test_a_429_degrades_instead_of_raising():
    """The provider must never propagate into the pipeline."""
    import asyncio

    import httpx

    import app.ingestion.economic_calendar as ec

    ec.reset_provider_state()

    class FakeResponse:
        status_code = 429
        headers = {"Retry-After": "120"}

        def raise_for_status(self):
            raise httpx.HTTPStatusError("429", request=None, response=self)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return FakeResponse()

    real = ec.httpx.AsyncClient
    ec.httpx.AsyncClient = lambda **kw: FakeClient()
    try:
        events, errors = asyncio.run(ec._fetch_faireconomy())
    finally:
        ec.httpx.AsyncClient = real

    assert events == []
    assert any("429" in e for e in errors)
    assert ec.faireconomy_in_backoff() is True


def test_during_backoff_faireconomy_is_not_fetched_at_all():
    """No request is issued, so the rate limit is not extended."""
    import asyncio

    import app.ingestion.economic_calendar as ec

    ec.reset_provider_state()
    ec.note_rate_limit()

    called = {"n": 0}
    real = ec._fetch_one

    async def spy(url):
        called["n"] += 1
        return []

    ec._fetch_one = spy
    try:
        events, errors = asyncio.run(ec._fetch_faireconomy())
    finally:
        ec._fetch_one = real

    assert called["n"] == 0
    assert events == [] and errors == []


# --------------------------------------------------------------------------
# Step 7: shared Chromium render slot
# --------------------------------------------------------------------------
def test_only_one_render_runs_at_a_time():
    """
    Measured motivation: news + calendar rendered concurrently cost +12
    processes and ~1.0 GB RSS. The slot must make overlap impossible.
    """
    import asyncio

    from app.ingestion.chromium_lock import chromium_slot, reset_lock_state

    async def scenario():
        reset_lock_state()
        active, peak = 0, 0

        async def render():
            nonlocal active, peak
            async with chromium_slot(wait_seconds=10) as ok:
                assert ok
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

        await asyncio.gather(render(), render(), render())
        return peak

    assert asyncio.run(scenario()) == 1


def test_a_waiter_that_cannot_get_the_slot_defers():
    """A calendar refresh is worth less than the news render it queues behind."""
    import asyncio

    from app.ingestion.chromium_lock import chromium_slot, reset_lock_state

    async def scenario():
        reset_lock_state()
        release = asyncio.Event()

        async def holder():
            async with chromium_slot(wait_seconds=10):
                await release.wait()

        async def impatient():
            # 0.1s budget while the holder is still working.
            async with chromium_slot(wait_seconds=0.1) as ok:
                return ok

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.02)
        got = await impatient()
        release.set()
        await task
        return got

    assert asyncio.run(scenario()) is False


def test_a_queued_render_still_gets_its_turn():
    """Deferral is a last resort, not the normal path."""
    import asyncio

    from app.ingestion.chromium_lock import chromium_slot, reset_lock_state

    async def scenario():
        reset_lock_state()
        done = []

        async def render(name):
            async with chromium_slot(wait_seconds=10) as ok:
                if ok:
                    done.append(name)

        await asyncio.gather(render("news"), render("calendar"))
        return done

    assert sorted(asyncio.run(scenario())) == ["calendar", "news"]


def test_the_slot_is_released_after_a_failure():
    """A crashed render must not wedge the provider forever."""
    import asyncio

    from app.ingestion.chromium_lock import chromium_slot, reset_lock_state

    async def scenario():
        reset_lock_state()

        async def boom():
            async with chromium_slot(wait_seconds=10):
                raise RuntimeError("render exploded")

        try:
            await boom()
        except RuntimeError:
            pass

        async with chromium_slot(wait_seconds=1) as ok:
            return ok

    assert asyncio.run(scenario()) is True


def test_a_deferred_calendar_poll_is_not_a_health_failure():
    """
    Deferral means "not now", not "broken". Reporting it as a provider failure
    would trigger needless backoff and an alert.
    """
    import asyncio

    from app.ingestion import fj_calendar as fjmod
    from app.ingestion.chromium_lock import reset_lock_state

    reset_lock_state()
    fjmod.reset_state_for_tests()

    async def busy(*a, **kw):
        raise fjmod.BrowserBusy("chromium busy; calendar render deferred")

    real = fjmod._render_once
    fjmod._render_once = busy
    try:
        result = asyncio.run(fjmod.fetch_fj_calendar())
    finally:
        fjmod._render_once = real

    assert result.ok is False
    assert result.deferred is True
    assert "chromium busy" in result.error
    # Not counted as a failure, so no backoff is armed.
    assert fjmod.in_backoff() is False
    assert not same_release(ev(title="CPI m/m", country="USD"),
                            ev(title="CPI m/m", country="EUR"))


def test_times_beyond_tolerance_do_not_match():
    assert not same_release(ev(title="US CPI m/m", when=REF),
                            ev(title="US CPI m/m", when=REF + timedelta(minutes=45)))


def test_times_inside_tolerance_do_match():
    assert same_release(ev(title="US CPI m/m", when=REF),
                        ev(title="US CPI m/m", when=REF + timedelta(minutes=3)))


def test_missing_impact_is_never_promoted_to_high():
    assert fj.impact_from_class(None) == "Medium"