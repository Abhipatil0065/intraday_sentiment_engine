"""Phase 5 (country multipliers), Phase 6 (awaiting release + composite cap)
and Phase 8 (conditional requests)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.economic_impact import (
    CalendarEvent,
    apply_awaiting_release,
    cap_composite_release,
    resolve_calendar_signals,
    signals_for_event,
)
from app.ingestion.economic_calendar import _parse_feed
from app.config import settings
from app.models.enums import Asset

T0 = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)


def ev(title, forecast=None, previous=None, actual=None,
       country="USD", impact="High", when=T0):
    return CalendarEvent(title=title, country=country, when_utc=when,
                         impact=impact, forecast=forecast, previous=previous,
                         actual=actual)


def gold_signals(*events):
    out = []
    for event in events:
        out.extend(s for s in signals_for_event(event) if s.asset is Asset.GOLD)
    return out


# --------------------------------------------------------------------------
# Phase 5: country multipliers
# --------------------------------------------------------------------------
def test_a_us_print_outweighs_a_japanese_one_for_gold():
    us = signals_for_event(ev("US CPI MoM", 0.3, 0.2, 0.4, country="USD"))
    jp = signals_for_event(ev("Japan CPI YoY", 2.4, 2.3, 2.6, country="JPY"))
    assert ([s for s in us if s.asset is Asset.GOLD][0].relevance
            > [s for s in jp if s.asset is Asset.GOLD][0].relevance)


def test_an_unlisted_country_gets_the_conservative_default():
    """A newly added country must not inherit the US weight by omission."""
    from app.analysis.economic_impact import _country_multiplier

    assert _country_multiplier(ev("X CPI", country="XYZ"),
                               Asset.GOLD) == pytest.approx(0.25)


def test_china_outweighs_gold_but_not_silver():
    """
    China's industrial cycle drives silver far more than gold or bitcoin, which
    is exactly what the per-asset override exists for.
    """
    from app.analysis.economic_impact import _country_multiplier

    chn = ev("China CPI YoY", country="CNY")
    assert (_country_multiplier(chn, Asset.SILVER)
            > _country_multiplier(chn, Asset.GOLD))
    assert (_country_multiplier(chn, Asset.SILVER)
            > _country_multiplier(chn, Asset.BITCOIN))
    assert (_country_multiplier(chn, Asset.BITCOIN)
            == pytest.approx(_country_multiplier(chn, Asset.GOLD)))


def test_country_multipliers_can_be_turned_off(monkeypatch):
    from app.config import settings
    from app.analysis.economic_impact import _country_multiplier

    monkeypatch.setattr(settings, "calendar_country_multipliers_enabled", False)
    assert _country_multiplier(ev("Japan CPI", country="JPY"),
                               Asset.GOLD) == 1.0


def test_relevance_never_exceeds_one_with_a_multiplier_and_a_bonus():
    for country in ("USD", "CNY", "JPY"):
        sig = [s for s in signals_for_event(
            ev("US Core CPI MoM", 0.3, 0.2, 0.4, country=country))
            if s.asset is Asset.GOLD][0]
        assert sig.relevance <= 1.0
# --------------------------------------------------------------------------
# Phase 6: awaiting release
# --------------------------------------------------------------------------
def test_an_overdue_expectation_is_damped_then_dropped():
    event = ev("US CPI MoM", 0.3, 0.2)
    base = [s for s in signals_for_event(event) if s.asset is Asset.GOLD][0]

    fresh = apply_awaiting_release([base], now=event.when_utc)[0]
    assert fresh.relevance == base.relevance

    overdue = apply_awaiting_release(
        [base], now=event.when_utc + timedelta(minutes=30))[0]
    assert overdue.relevance < base.relevance

    stale = apply_awaiting_release(
        [base], now=event.when_utc + timedelta(hours=6))
    assert stale == [], "a six-hour undelivered print is not evidence"


def test_a_released_print_is_never_damped():
    """The actual value is known, so the clock is irrelevant to it."""
    released = [s for s in signals_for_event(
        ev("US CPI MoM", 0.3, 0.2, 0.4)) if s.asset is Asset.GOLD][0]
    assert released.mode == "RELEASED"
    out = apply_awaiting_release(
        [released], now=released.event.when_utc + timedelta(hours=6))
    assert len(out) == 1
    assert out[0].relevance == released.relevance


def test_dampening_explains_itself():
    """A reduced weight must still say why it was reduced."""
    base = [s for s in signals_for_event(ev("US CPI MoM", 0.3, 0.2))
            if s.asset is Asset.GOLD][0]
    damped = apply_awaiting_release(
        [base], now=base.event.when_utc + timedelta(minutes=30))[0]
    assert "Awaiting release" in damped.reason


# --------------------------------------------------------------------------
# Phase 6: composite release cap
# --------------------------------------------------------------------------
def test_one_shared_timestamp_cannot_outweigh_the_whole_calendar():
    """
    NFP lands with the unemployment rate and wages. Three full-strength signals
    from a single market moment must not total more than the cap.
    """
    sigs = gold_signals(
        ev("US Nonfarm Payrolls", 180.0, 4.3, 220.0),
        ev("US Unemployment Rate", 180.0, 4.3, 220.0),
        ev("US Average Hourly Earnings", 180.0, 4.3, 220.0),
    )
    total = sum(s.relevance for s in sigs)
    capped = cap_composite_release(sigs, max_weight=1.30)
    assert total > 1.30, "the fixture must actually exceed the cap"
    assert sum(s.relevance for s in capped) <= 1.30 + 1e-9


def test_the_cap_scales_proportionally_and_drops_nothing():
    """Relative weighting inside a cluster must survive the cap."""
    sigs = gold_signals(
        ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0),
        ev("US Unemployment Rate", 4.4, 4.6, 4.2),
    )
    before = [s.relevance for s in sigs]
    capped = cap_composite_release(sigs, max_weight=0.5)
    assert len(capped) == len(sigs), "the cap reduces weight, it never hides"
    after = [s.relevance for s in capped]
    assert after[0] / after[1] == pytest.approx(before[0] / before[1], rel=1e-6)


def test_well_separated_releases_are_untouched():
    sigs = gold_signals(
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("Japan CPI YoY", 2.4, 2.3, 2.6, when=T0 + timedelta(hours=6)),
    )
    capped = cap_composite_release(sigs, max_weight=0.1)
    assert [s.relevance for s in capped] == [s.relevance for s in sigs]


def test_the_cap_never_reverses_a_direction():
    sigs = gold_signals(ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0))
    capped = cap_composite_release(sigs, max_weight=0.01)
    assert capped[0].direction is sigs[0].direction


# --------------------------------------------------------------------------
# The assembled pipeline
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Phase 8: conditional requests
# --------------------------------------------------------------------------
FEED = ('[{"title":"US CPI","country":"USD","date":"2026-10-08 12:30:00",'
        '"impact":"High","forecast":"0.3","previous":"0.2"}]')
URL = "http://provider/cal.json"


class _Resp:
    def __init__(self, code, body="", headers=None):
        self.status_code = code
        self.text = body
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class _Client:
    """Captures the headers passed to the CONSTRUCTOR, not just per-request."""

    def __init__(self, resp, seen, **client_kwargs):
        self._resp = resp
        self._seen = seen
        self._client_kwargs = client_kwargs
        seen.append({"headers": dict(client_kwargs.get("headers") or {})})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return self._resp


def _run_fetch(resp, seen):
    import app.ingestion.economic_calendar as ec

    real = ec.httpx.AsyncClient

    def factory(**client_kwargs):
        # Forward the constructor kwargs so the test can inspect the headers
        # the provider actually built.
        return _Client(resp, seen, **client_kwargs)

    ec.httpx.AsyncClient = factory
    try:
        return asyncio.run(ec._fetch_one(URL))
    finally:
        ec.httpx.AsyncClient = real


def test_a_changed_feed_sends_its_etag_on_the_next_poll():
    import app.ingestion.economic_calendar as ec

    ec.reset_http_state()
    seen = []
    _run_fetch(_Resp(200, FEED, {"ETag": '"abc"'}), seen)
    assert ec.get_etag(URL) == '"abc"'

    _run_fetch(_Resp(200, FEED, {"ETag": '"abc"'}), seen)
    assert seen[1].get("headers", {}).get("If-None-Match") == '"abc"'
    ec.reset_http_state()


# --------------------------------------------------------------------------
# Regression: the importance floor must be numeric
# --------------------------------------------------------------------------
def test_high_impact_events_survive_the_importance_floor():
    """
    EconomicImportance is a str Enum, so comparing members with < compares the
    VALUES alphabetically and "HIGH" < "MEDIUM" is True. The floor used to hold
    enum members, so every high-impact faireconomy event was silently dropped
    while the logs looked healthy.
    """
    feed = json.dumps([{
        "title": "US CPI", "country": "USD",
        "date": "2026-10-08 12:30:00", "impact": "High",
        "forecast": "0.3", "previous": "0.2",
    }])
    events = _parse_feed(feed)
    assert len(events) == 1, "a High-impact print must not be filtered out"


def test_the_floor_still_drops_low_impact_events():
    feed = json.dumps([{
        "title": "US Trade Balance", "country": "USD",
        "date": "2026-10-08 12:30:00", "impact": "Low",
        "forecast": "-10.0", "previous": "-5.0",
    }])
    assert _parse_feed(feed) == []

# --------------------------------------------------------------------------
# Phase 6: the composite cap covers the real multi-print releases
# --------------------------------------------------------------------------
def _weights(signals):
    """Total relevance per asset, plus the overall total."""
    per_asset: dict = {}
    for sig in signals:
        per_asset[sig.asset.value] = (per_asset.get(sig.asset.value, 0.0)
                                      + sig.relevance)
    return per_asset, sum(per_asset.values())


def _assert_within_cap(events):
    """
    Run the real pipeline and assert the PER-ASSET bound holds.

    The cap is per asset, so every asset must independently stay within it.
    Asserting only on the combined total would pass under the old global
    behaviour too, and so would not test what is actually being configured.
    """
    cap = float(settings.calendar_composite_max_weight)
    out = resolve_calendar_signals(events, horizon_hours=48, now=T0)
    per_asset, _total = _weights(out)

    # The fixture is only meaningful if some asset genuinely exceeds the cap
    # before it is applied.
    pre: dict = {}
    for e in events:
        for s in signals_for_event(e):
            pre[s.asset.value] = pre.get(s.asset.value, 0.0) + s.relevance
    assert max(pre.values()) > cap, "fixture must exceed the cap beforehand"

    for asset, value in per_asset.items():
        assert value <= cap + 1e-9, f"{asset} at {value:.4f} exceeds {cap}"
    return out, per_asset


def test_headline_and_core_cpi_respect_the_composite_cap():
    """
    A CPI release publishes FOUR rows - headline and core, each m/m and y/y.
    They are one market moment, so each asset's combined weight must stay
    within the cap.
    """
    events = [
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("US CPI YoY", 2.9, 2.8, 3.1),
        ev("US Core CPI MoM", 0.3, 0.2, 0.4),
        ev("US Core CPI YoY", 2.9, 2.8, 3.1),
    ]
    _out, per_asset = _assert_within_cap(events)
    # Per asset, the cap is reached - NOT split four ways across assets.
    assert per_asset[Asset.GOLD.value] == pytest.approx(
        float(settings.calendar_composite_max_weight), abs=1e-6)


def test_headline_and_core_pce_respect_the_composite_cap():
    """Same rule for PCE, which ships as headline and core in one release."""
    events = [
        ev("US PCE Price Index MoM", 0.3, 0.2, 0.4),
        ev("US Core PCE Price Index MoM", 0.3, 0.2, 0.4),
    ]
    _out, per_asset = _assert_within_cap(events)
    assert per_asset[Asset.GOLD.value] > 0


def test_nfp_unemployment_and_wages_respect_the_composite_cap():
    """The employment cluster: payrolls, the unemployment rate and wages."""
    events = [
        ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0),
        ev("US Unemployment Rate", 4.4, 4.3, 4.6),
        ev("US Average Hourly Earnings", 0.4, 0.3, 0.5),
    ]
    _out, per_asset = _assert_within_cap(events)
    assert per_asset[Asset.GOLD.value] > 0


def test_cap_holds_when_every_multi_print_release_lands_together():
    """
    The worst case: CPI (4 rows) and the employment cluster all at one
    timestamp. Still a single moment, so still one cap per asset.
    """
    events = [
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("US CPI YoY", 2.9, 2.8, 3.1),
        ev("US Core CPI MoM", 0.3, 0.2, 0.4),
        ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0),
        ev("US Unemployment Rate", 4.4, 4.3, 4.6),
    ]
    cap = float(settings.calendar_composite_max_weight)
    per_asset, _total = _weights(
        resolve_calendar_signals(events, horizon_hours=48, now=T0))
    for asset, value in per_asset.items():
        assert value <= cap + 1e-9, f"{asset} at {value:.4f} exceeds {cap}"


def test_scaling_one_asset_never_changes_another_assets_weight():
    """
    The central guarantee of the per-asset cap: assets are capped independently.

    Gold is pushed over the cap by direct per-asset signals while silver and
    bitcoin stay under it and must come through bit-for-bit unchanged.
    """
    cap = float(settings.calendar_composite_max_weight)
    from app.analysis.economic_impact import CalendarSignal
    from app.models.enums import Direction, EconomicImportance, ImpactStrength
    base = ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0)

    def sig(asset, relevance):
        return CalendarSignal(event=base, profile=None, asset=asset,
                              direction=Direction.BULLISH,
                              strength=ImpactStrength.HIGH, relevance=relevance,
                              importance=EconomicImportance.HIGH, persistence=0.5,
                              reason="fixture",
                              transmission="fixture", mode="EXPECTED")

    raw = [sig(Asset.GOLD, cap * 0.6), sig(Asset.GOLD, cap * 0.6),
           sig(Asset.SILVER, cap * 0.3), sig(Asset.BITCOIN, cap * 0.3)]
    pre: dict = {}
    for s in raw:
        pre.setdefault(s.asset.value, []).append(s.relevance)

    post: dict = {}
    for s in cap_composite_release(raw):
        post.setdefault(s.asset.value, []).append(s.relevance)

    # Gold genuinely breaches, so the fixture is meaningful.
    assert sum(pre[Asset.GOLD.value]) > cap
    # Every asset already under the cap must be untouched.
    under_cap = [a for a in (Asset.GOLD, Asset.SILVER, Asset.BITCOIN)
                 if a.value in pre and sum(pre[a.value]) <= cap]
    assert under_cap, "fixture must leave at least one asset under the cap"
    for asset in under_cap:
        assert post[asset.value] == pytest.approx(pre[asset.value],
                                                  rel=1e-9)
    # ...while gold is scaled to exactly the cap.
    assert sum(post[Asset.GOLD.value]) == pytest.approx(cap, abs=1e-6)


def test_a_narrow_release_and_a_multi_asset_release_share_the_rule():
    """
    A narrow industrial print (China copper imports -> silver-led narrow set) and a
    four-asset release (CPI) must obey the SAME per-asset bound. The cap must
    not depend on how many assets a release happens to touch.

    Note: no title currently classifies to a SINGLE asset - crude inventories
    would be the natural case, but they have no profile yet (Phase 4). Copper
    imports is the narrowest real fixture, and it exercises the same rule.
    """
    cap = float(settings.calendar_composite_max_weight)
    fixtures = (
        ("CPI (4 assets)",
         [ev("US CPI MoM", 0.3, 0.2, 0.4), ev("US Core CPI MoM", 0.3, 0.2, 0.4)]),
        ("China copper imports (2 assets)",
         [ev("China Copper Imports", 1.0, 0.5, 2.0, country="CNY")]),
    )
    for name, events in fixtures:
        capped = cap_composite_release(
            [s for e in events for s in signals_for_event(e)])
        per_asset, _total = _weights(capped)
        assert per_asset, f"{name} produced no signals"
        for asset, value in per_asset.items():
            assert value <= cap + 1e-9, f"{name}/{asset} at {value:.4f}"

    # The narrow release genuinely touches fewer assets than the wide one.
    narrow = len(signals_for_event(
        ev("China Copper Imports", 1.0, 0.5, 2.0, country="CNY")))
    wide = len(signals_for_event(ev("US CPI MoM", 0.3, 0.2, 0.4)))
    assert 0 < narrow < wide


def test_global_mode_is_still_available(monkeypatch):
    """
    The flag must genuinely restore the old shared-factor behaviour, so
    reverting is a config change and not a code change.
    """
    cap = float(settings.calendar_composite_max_weight)
    events = [
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("US Core CPI MoM", 0.3, 0.2, 0.4),
    ]
    raw = [s for e in events for s in signals_for_event(e)]

    monkeypatch.setattr(settings, "calendar_composite_cap_mode", "global")
    global_per_asset, global_total = _weights(cap_composite_release(raw))

    monkeypatch.setattr(settings, "calendar_composite_cap_mode", "per_asset")
    per_per_asset, per_total = _weights(cap_composite_release(raw))

    # Global shares the cap across assets; per-asset gives each the full cap.
    assert global_total < per_total
    assert global_per_asset[Asset.GOLD.value] < \
        per_per_asset[Asset.GOLD.value]


def test_the_cap_is_the_configured_one_not_a_hardcoded_constant():
    """Changing the setting must change the bound the tests rely on."""
    cap = float(settings.calendar_composite_max_weight)
    sigs = gold_signals(
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("US Core CPI MoM", 0.3, 0.2, 0.4),
    )
    loose = cap_composite_release(sigs, max_weight=10.0)
    tight = cap_composite_release(sigs, max_weight=cap)
    assert sum(s.relevance for s in loose) > sum(s.relevance for s in tight)


def test_a_304_reuses_the_previous_rows():
    """An empty 304 body must never be read as 'no events today'."""
    import app.ingestion.economic_calendar as ec

    ec.reset_http_state()
    seen = []
    first = _run_fetch(_Resp(200, FEED, {"ETag": '"abc"'}), seen)
    second = _run_fetch(_Resp(304, "", {}), seen)
    assert len(first) == 1
    assert len(second) == 1, "304 must return the remembered rows"
    ec.reset_http_state()


def test_a_dropped_etag_stops_the_conditional_request():
    """
    If the server stops sending a validator, a retained one would keep sending
    a stale If-None-Match and suppress real changes indefinitely.
    """
    import app.ingestion.economic_calendar as ec

    ec.reset_http_state()
    seen = []
    _run_fetch(_Resp(200, FEED, {"ETag": '"abc"'}), seen)
    _run_fetch(_Resp(200, FEED, {}), seen)
    assert ec.get_etag(URL) is None

    seen.clear()
    _run_fetch(_Resp(200, FEED, {}), seen)
    assert "If-None-Match" not in seen[0].get("headers", {})
    ec.reset_http_state()


def test_conditional_requests_can_be_turned_off(monkeypatch):
    from app.config import settings
    import app.ingestion.economic_calendar as ec

    ec.reset_http_state()
    monkeypatch.setattr(settings, "calendar_conditional_requests", False)
    seen = []
    _run_fetch(_Resp(200, FEED, {"ETag": '"abc"'}), seen)
    _run_fetch(_Resp(200, FEED, {"ETag": '"abc"'}), seen)
    assert "If-None-Match" not in seen[1].get("headers", {})
    ec.reset_http_state()
def test_the_pipeline_applies_every_stage_in_order():
    """The CPI pair collapses, then the shared timestamp is capped."""
    events = [
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("US CPI YoY", 2.9, 2.8, 3.1),
        ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0),
        ev("US Unemployment Rate", 4.4, 4.3, 4.6),
    ]
    out = resolve_calendar_signals(events, horizon_hours=48, now=T0)
    gold = [s for s in out if s.asset is Asset.GOLD]
    assert len([s for s in gold if "CPI" in s.event.title]) == 1
    assert sum(s.relevance for s in gold) <= 1.30 + 1e-9
