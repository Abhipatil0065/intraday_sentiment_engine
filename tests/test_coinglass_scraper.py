"""
Unit tests for the CoinGlass scraper (no network, no browser).

The scrape path needs a live headless browser, so it is NOT exercised here.
What IS verified is every decision the scraper makes once a page has been
rendered:

  * heatmap extraction into the documented
    {"price_level", "intensity", "side"} shape
  * the nearest LARGE cluster below / above spot, and the 1% caution flags
  * the locally computed CVD divergence (price vs CVD trend)
  * footprint buy/sell imbalance
  * the cache TTL that enforces "max once every 5 minutes"
  * challenge detection + exponential backoff
  * conversion into WeightedSignals that actually move
    sentiment_engine.compute()'s bullish/bearish verdict
  * every failure mode returning None instead of raising

Each test feeds synthetic chart data shaped exactly like the in-page
extractor's output, so the parse logic is tested without CoinGlass.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.ingestion import coinglass_scraper as cs


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------
def _heatmap_chart(rows: list[tuple[float, float]], name="Liquidation Heatmap"):
    """(price, colour-value) rows -> one rendered heatmap chart."""
    return {
        "chart_index": 0,
        "series_name": name,
        "kind": "heatmap",
        "series_index": 0,
        "type": "heatmap",
        "points": [{"x": i, "y": price, "value": value, "color": ""}
                   for i, (price, value) in enumerate(rows)],
        "axis_labels": [],
    }


def _dense_heatmap(hot_rows, *, low=4100.0, high=4400.0, step=5.0,
                   name="Liquidation Heatmap"):
    """
    Build a REALISTIC heatmap page instead of a handful of rows.

    A live heatmap plots every price row on a fine grid and only colours the
    ones carrying leverage, so the extractor learns its bin size from the FULL
    grid and merges hot rows that are adjacent on it. Feeding it two rows
    250 apart would teach it a 250-wide bin and collapse them into one cluster
    - an artefact of the fixture, not the logic. So the grid is built first,
    then the hot rows are punched into it.
    """
    count = int(round((high - low) / step)) + 1
    rows = [(round(low + i * step, 4), 0.05) for i in range(count)]
    for price, value in hot_rows:
        index = int(round((price - low) / step))
        if 0 <= index < count:
            rows[index] = (round(low + index * step, 4), value)
    return _page(_heatmap_chart(rows, name))


def _snapshot_page(hot_rows, spot_value=None, *, low=4100.0, high=4400.0,
                   step=5.0):
    """Dense heatmap, optionally with a Close series supplying spot."""
    page_charts = []
    count = int(round((high - low) / step)) + 1
    rows = [(round(low + i * step, 4), 0.05) for i in range(count)]
    for price, value in hot_rows:
        index = int(round((price - low) / step))
        if 0 <= index < count:
            rows[index] = (round(low + index * step, 4), value)
    page_charts.append(_heatmap_chart(rows))
    if spot_value is not None:
        page_charts.append(_series_chart([(0, spot_value)], name="Close",
                                         kind="price"))
    return _page(*page_charts)


def _series_chart(points: list[tuple[float, float]], name="CVD", kind="cvd"):
    return {
        "chart_index": 0,
        "series_name": name,
        "kind": kind,
        "series_index": 0,
        "type": "line",
        "points": [{"x": x, "y": y, "value": None, "color": ""}
                   for x, y in points],
        "axis_labels": [],
    }


def _page(*charts):
    return {
        "title": "CoinGlass Chart",
        "heading": "Chart",
        "text": "some rendered chart page text",
        "charts": list(charts),
        "svgs": [],
    }


@pytest.fixture(autouse=True)
def _clean_scraper_state():
    """Never leak cache/backoff state between tests."""
    cs.reset_state_for_tests()
    yield
    cs.reset_state_for_tests()


# ---------------------------------------------------------------------------
# Liquidation heatmap
# ---------------------------------------------------------------------------
class TestHeatmapExtraction:
    def test_extracts_high_intensity_cluster_in_documented_shape(self):
        """The output shape must match the documented contract exactly."""
        page = _dense_heatmap([(4230.5, 1.00), (4250.0, 0.40)],
                              low=4200.0, high=4270.0, step=0.5)
        snapshot = cs._parse_extraction("XAUUSD", page,
                                        "https://example/chart")

        assert snapshot.heatmap_clusters, "the peak row must yield a cluster"
        peak = max(snapshot.heatmap_clusters,
                   key=lambda c: c.intensity_score)
        payload = peak.to_dict()
        assert set(payload) == {"price_level", "intensity", "side"}
        assert payload["price_level"] == 4230.50
        assert payload["intensity"] == "high"
        assert payload["side"] in ("long", "short")

    def test_low_value_rows_are_discarded(self):
        """A stray warm pixel is not a LARGE cluster."""
        page = _dense_heatmap([(4230.5, 1.00), (4240.0, 0.10), (4250.0, 0.20)],
                              low=4200.0, high=4270.0, step=0.5)
        snapshot = cs._parse_extraction("XAUUSD", page,
                                        "https://example/chart")
        assert len(snapshot.heatmap_clusters) == 1

    def test_adjacent_hot_rows_merge_into_one_cluster(self):
        """
        Rows adjacent on the grid are one band. The grid here is 0.5 wide, so
        the two hot rows 1.0 apart must merge into a single cluster rather
        than reporting two.
        """
        page = _dense_heatmap([(4230.0, 1.00), (4230.5, 0.60)],
                              low=4200.0, high=4270.0, step=0.5)
        snapshot = cs._parse_extraction("XAUUSD", page,
                                        "https://example/chart")
        assert len(snapshot.heatmap_clusters) == 1
        cluster = snapshot.heatmap_clusters[0]
        assert cluster.price_level == 4230.25
        # The band carries its STRONGEST row's intensity.
        assert cluster.intensity == "high"
        assert cluster.intensity_score == 1.0

    def test_intensity_buckets_map_from_normalised_value(self):
        assert cs._intensity_bucket(0.90) == "high"
        assert cs._intensity_bucket(0.50) == "medium"
        assert cs._intensity_bucket(0.20) == "low"


class TestNearestClusterAndCaution:
    """Spot comes from the Close series; clusters resolve to below/above."""

    def _snapshot(self, hot_rows, spot_value):
        return cs._parse_extraction(
            "XAUUSD", _snapshot_page(hot_rows, spot_value),
            "https://example/chart")

    def test_cluster_below_and_above_are_the_nearest_each_side(self):
        snapshot = self._snapshot(
            [(4180.0, 1.0), (4210.0, 0.9), (4240.0, 0.8), (4290.0, 1.0)],
            spot_value=4225.0)
        assert snapshot.spot_price == 4225.0
        assert snapshot.cluster_below.price_level < 4225.0
        assert snapshot.cluster_above.price_level > 4225.0
        # nearest, not just any
        assert snapshot.cluster_below.price_level == 4210.0
        assert snapshot.cluster_above.price_level == 4240.0

    def test_caution_when_cluster_is_within_one_percent(self):
        # spot 4225, cluster at 4220 -> 0.12% away -> caution
        snapshot = self._snapshot([(4220.0, 1.0)], spot_value=4225.0)
        assert snapshot.cluster_below is not None
        assert snapshot.caution_below is True
        assert "within 1.0% of spot" in snapshot.caution_note()

    def test_no_caution_when_cluster_is_far(self):
        # spot 4225, cluster at 4000 -> >5% away -> no caution
        snapshot = self._snapshot([(4100.0, 1.0)], spot_value=4225.0)
        assert snapshot.caution_below is False
        assert snapshot.caution_note() == ""

    def test_caution_above_side(self):
        snapshot = self._snapshot([(4230.0, 1.0)], spot_value=4225.0)
        assert snapshot.cluster_above is not None
        assert snapshot.caution_above is True

    def test_caution_boundary_is_inclusive_at_exactly_one_percent(self):
        """
        Exactly 1.00% away must raise the caution (the gate is ``<=``).

        The grid snaps 4182.75 up to 4185.0, so spot is derived FROM the
        snapped cluster to land on the boundary exactly rather than being
        eyeballed: spot = 4185.0 / (1 - 0.01).
        """
        cluster_price = 4185.0
        spot_value = cluster_price / (1.0 - cs.LIQ_CAUTION_PCT)
        snapshot = self._snapshot([(4182.75, 1.0)], spot_value=spot_value)
        assert snapshot.cluster_below.price_level == cluster_price
        gap_pct = (spot_value - cluster_price) / spot_value
        assert abs(gap_pct - cs.LIQ_CAUTION_PCT) < 1e-12
        assert snapshot.caution_below is True

    def test_caution_is_not_raised_just_outside_the_boundary(self):
        """The other side of the boundary: a hair further away is not a flag."""
        cluster_price = 4185.0
        # 1.1% gap -> outside the 1% window.
        spot_value = cluster_price / (1.0 - 0.011)
        snapshot = self._snapshot([(4182.75, 1.0)], spot_value=spot_value)
        assert snapshot.caution_below is False

    def test_side_is_long_below_spot_and_short_above(self):
        snapshot = self._snapshot([(4100.0, 1.0), (4350.0, 1.0)],
                                  spot_value=4225.0)
        assert snapshot.cluster_below.side == "long"
        assert snapshot.cluster_above.side == "short"


# ---------------------------------------------------------------------------
# CVD divergence
# ---------------------------------------------------------------------------
class TestCVDDivergence:
    def test_bullish_divergence_price_down_cvd_up(self):
        price = [(0, 100.0), (1, 95.0), (2, 90.0), (3, 85.0), (4, 80.0)]
        cvd = cs.CVDSeries(points=[(0, -50.0), (1, -40.0), (2, -30.0),
                                   (3, -20.0), (4, -10.0)])
        divergence, note, slope = cs.detect_divergence(cvd, price)
        assert divergence == "CVD_BULLISH"
        assert "bullish divergence" in note
        assert slope > 0

    def test_bearish_divergence_price_up_cvd_down(self):
        price = [(0, 80.0), (1, 85.0), (2, 90.0), (3, 95.0), (4, 100.0)]
        cvd = cs.CVDSeries(points=[(0, 50.0), (1, 40.0), (2, 30.0),
                                   (3, 20.0), (4, 10.0)])
        divergence, note, slope = cs.detect_divergence(cvd, price)
        assert divergence == "CVD_BEARISH"
        assert "bearish divergence" in note
        assert slope < 0

    def test_confirmed_when_both_move_together(self):
        price = [(0, 100.0), (1, 105.0), (2, 110.0), (3, 115.0), (4, 120.0)]
        cvd = cs.CVDSeries(points=[(0, 10.0), (1, 20.0), (2, 30.0),
                                   (3, 40.0), (4, 50.0)])
        divergence, _, _ = cs.detect_divergence(cvd, price)
        assert divergence == "CONFIRMED"

    def test_none_with_too_few_points(self):
        divergence, note, slope = cs.detect_divergence(
            cs.CVDSeries(points=[(0, 1.0), (1, 2.0)]), [(0, 1.0), (1, 2.0)])
        assert divergence == "NONE"
        assert "insufficient" in note

    def test_none_without_price_series(self):
        """
        No price series means no comparison, which the scorer must report as
        NONE rather than inventing a direction. The guard fires on the length
        check because an empty series cannot support any direction at all.
        """
        cvd = cs.CVDSeries(points=[(0, 1.0), (1, 2.0), (2, 3.0), (3, 4.0)])
        divergence, note, _ = cs.detect_divergence(cvd, [])
        assert divergence == "NONE"
        assert "insufficient" in note

    def test_divergence_survives_full_parse(self):
        """The parse path must populate divergence, not leave it at default."""
        price = _series_chart([(0, 100.0), (1, 95.0), (2, 90.0),
                               (3, 85.0), (4, 80.0)],
                              name="Close", kind="price")
        cvd = _series_chart([(0, -50.0), (1, -40.0), (2, -30.0),
                             (3, -20.0), (4, -10.0)], name="CVD", kind="cvd")
        snapshot = cs._parse_extraction("XAUUSD", _page(price, cvd),
                                        "https://example/chart")
        assert snapshot.cvd is not None
        assert snapshot.cvd.divergence == "CVD_BULLISH"
        assert snapshot.cvd.divergence_note


# ---------------------------------------------------------------------------
# Footprint
# ---------------------------------------------------------------------------
class TestFootprint:
    def _footprint(self, deltas):
        return {
            "chart_index": 0, "series_name": "Footprint", "kind": "footprint",
            "series_index": 0, "type": "column",
            "points": [{"x": i, "y": 4200.0 + i, "value": d, "color": ""}
                       for i, d in enumerate(deltas)],
            "axis_labels": [],
        }

    def test_buy_imbalance(self):
        snapshot = cs._parse_extraction(
            "XAUUSD", _page(self._footprint([10, 20, 30, 40])),
            "https://example/chart")
        assert snapshot.footprint is not None
        assert snapshot.footprint.imbalance == "BUY_IMBALANCE"
        assert snapshot.footprint.imbalance_pct == 100.0

    def test_sell_imbalance(self):
        snapshot = cs._parse_extraction(
            "XAUUSD", _page(self._footprint([-10, -20, -30, -40])),
            "https://example/chart")
        assert snapshot.footprint.imbalance == "SELL_IMBALANCE"

    def test_balanced_footprint_reports_none(self):
        snapshot = cs._parse_extraction(
            "XAUUSD", _page(self._footprint([10, -10, 5, -5])),
            "https://example/chart")
        assert snapshot.footprint.imbalance == "NONE"


# ---------------------------------------------------------------------------
# Degraded / failure modes
# ---------------------------------------------------------------------------
class TestDegradation:
    def test_empty_page_is_degraded_not_a_crash(self):
        snapshot = cs._parse_extraction("BTCUSD", _page(), "https://example")
        assert snapshot.degraded is True

    def test_degraded_snapshot_is_not_cached_or_signalled(self, fake_spot):
        """
        A page that renders but yields no chart data (no rows, no axis) must
        return a degraded snapshot and must not be cached or converted to
        signals. The parser should produce a degraded snapshot, not None.
        """
        # Feed empty canvas data - no rows, no axis
        empty_data = {"canvas": {"w": 800, "h": 600}, "texts": [], "rows": []}
        snapshot = cs._parse_canvas_extraction("XAUUSD", empty_data, "https://example")
        assert snapshot.degraded is True
        assert snapshot.heatmap_clusters == []

    def test_page_with_no_chart_at_all_is_degraded(self, fake_spot):
        snapshot = cs._parse_canvas_extraction("BTCUSD", {}, "https://example/chart")
        assert snapshot.degraded is True
        assert snapshot.heatmap_clusters == []

    def test_challenge_page_is_detected(self):
        assert cs._looks_like_challenge("Just a moment...")
        assert cs._looks_like_challenge("cf-browser-verification")
        assert cs._looks_like_challenge("Please verify you are human")
        assert not cs._looks_like_challenge("Bitcoin Liquidation Heatmap")

    def test_failure_records_backoff_and_logs_once(self):
        cs._record_failure("XAUUSD", "challenge/block page detected")
        cs._record_failure("XAUUSD", "still blocked")
        assert cs._failure_streak["XAUUSD"] == 2
        # Only one log entry per asset, so a long block is one warning.
        assert len(cs._failure_logged) == 1
        assert cs._backoff_active("XAUUSD") is True

    def test_success_clears_backoff(self):
        cs._record_failure("XAUUSD", "timeout")
        assert cs._backoff_active("XAUUSD")
        cs._record_success("XAUUSD")
        assert cs._backoff_active("XAUUSD") is False

    def test_backoff_grows_and_is_capped(self):
        cs._failure_streak["XAUUSD"] = 50
        cs._last_fetch_attempt["XAUUSD"] = cs.time.time()
        # 50 failures must park it, and the wait must never exceed the ceiling.
        assert cs._backoff_active("XAUUSD") is True
        assert cs.MAX_BACKOFF_SECONDS == 3600

    def test_backoff_is_inactive_on_a_clean_asset(self):
        """A never-failed asset must never be parked."""
        assert cs._backoff_active("XAUUSD") is False


class TestCache:
    def test_cache_round_trip_and_ttl(self, tmp_path):
        """A snapshot survives a serialise/rebuild cycle."""
        page = _dense_heatmap([(4200.0, 1.0), (4240.0, 0.8)])
        snapshot = cs._parse_extraction("XAUUSD", page,
                                        "https://example/chart")
        payload = cs._snapshot_to_dict(snapshot)
        assert payload["asset"] == "XAUUSD"
        # Spot falls back to the median plotted row, so both a below and an
        # above cluster must be present for a round trip to carry.
        assert payload["cluster_below"] is not None
        assert payload["cluster_above"] is not None
        assert payload["cluster_below"]["intensity"] in ("high", "medium", "low")

        rebuilt = cs._snapshot_from_dict(payload)
        assert rebuilt.asset == "XAUUSD"
        assert rebuilt.cluster_below is not None
        assert rebuilt.cluster_below.price_level == \
            snapshot.cluster_below.price_level
        assert rebuilt.cluster_below.side == snapshot.cluster_below.side
        assert rebuilt.cluster_below.intensity == \
            snapshot.cluster_below.intensity

    def test_cached_entry_expires_after_ttl(self, tmp_path):
        cache = cs._Cache(tmp_path / "c.json", ttl_seconds=1)
        cache.put("XAUUSD", {"asset": "XAUUSD"})
        assert cache.get("XAUUSD") is not None
        import time as _t
        # Simulate ageing past the TTL without sleeping 1s.
        cache._entries["XAUUSD"]["stored_at"] -= 2.0
        assert cache.get("XAUUSD") is None

    def test_corrupt_cache_file_is_not_fatal(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{ this is not json", encoding="utf-8")
        cache = cs._Cache(path, ttl_seconds=300)
        assert cache.get("XAUUSD") is None

    def test_cache_survives_a_new_instance(self, tmp_path):
        """The disk copy is what keeps a restart from costing a fetch."""
        first = cs._Cache(tmp_path / "c.json", ttl_seconds=300)
        first.put("BTCUSD", {"asset": "BTCUSD", "cvd_divergence": "CVD_BULLISH"})
        del first

        second = cs._Cache(tmp_path / "c.json", ttl_seconds=300)
        assert second.get("BTCUSD")["cvd_divergence"] == "CVD_BULLISH"
        assert second.age_seconds("BTCUSD") is not None

    def test_stale_format_entries_are_discarded_not_misparsed(self, tmp_path):
        """
        A cache written by an older build must be dropped whole.

        The payload shape has already changed once, and parsing an old entry
        under new assumptions is how a stale reading becomes WRONG data (an
        empty cluster list beside a populated price, say). Versioning makes
        that impossible: the reader refuses what it did not write.
        """
        path = tmp_path / "c.json"
        path.write_text(json.dumps({
            "BTCUSD": {"stored_at": 9999999999, "payload": {"asset": "BTCUSD"}}
        }), encoding="utf-8")   # shape from the previous build
        cache = cs._Cache(path, ttl_seconds=300)
        assert cache.get("BTCUSD") is None

    def test_current_format_entries_are_kept(self, tmp_path):
        path = tmp_path / "c.json"
        cache = cs._Cache(path, ttl_seconds=300)
        cache.put("BTCUSD", {"asset": "BTCUSD"})
        assert cs._Cache(path, ttl_seconds=300).get("BTCUSD") == {"asset": "BTCUSD"}


# ---------------------------------------------------------------------------
# Integration with the sentiment scorer
# ---------------------------------------------------------------------------
class TestSentimentIntegration:
    def test_cvd_bearish_drives_bearish_verdict(self):
        """A CVD_BEARISH read must move the badge to BEARISH."""
        from app.analysis.sentiment_engine import compute
        from app.models.enums import Asset

        snapshot = cs.CoinGlassSnapshot(
            asset="XAUUSD",
            cvd=cs.CVDSeries(points=[(0, 1.0), (1, 2.0), (2, 3.0), (3, 4.0)],
                             divergence="CVD_BEARISH",
                             divergence_note="bearish divergence"),
            degraded=False,
        )
        signals = cs.coinglass_signals_to_weighted({"XAUUSD": snapshot})
        assert len(signals) == 1

        result = compute(signals, Asset.GOLD)
        assert result.bias.value == "BEARISH"
        assert result.score < 0

    def test_cvd_bullish_drives_bullish_verdict(self):
        from app.analysis.sentiment_engine import compute

        snapshot = cs.CoinGlassSnapshot(
            asset="XAUUSD",
            cvd=cs.CVDSeries(points=[(0, 1.0), (1, 2.0), (2, 3.0), (3, 4.0)],
                             divergence="CVD_BULLISH",
                             divergence_note="bullish divergence"),
            degraded=False,
        )
        signals = cs.coinglass_signals_to_weighted({"XAUUSD": snapshot})
        from app.models.enums import Asset
        result = compute(signals, Asset.GOLD)
        assert result.bias.value == "BULLISH"
        assert result.score > 0

    def test_footprint_sell_imbalance_is_bearish(self):
        from app.analysis.sentiment_engine import compute

        snapshot = cs.CoinGlassSnapshot(
            asset="BTCUSD",
            footprint=cs.FootprintData(rows=[], imbalance="SELL_IMBALANCE",
                                       imbalance_pct=-40.0),
            degraded=False,
        )
        signals = cs.coinglass_signals_to_weighted({"BTCUSD": snapshot})
        from app.models.enums import Asset
        result = compute(signals, Asset.BITCOIN)
        assert result.bias.value == "BEARISH"

    def test_cvd_weight_is_at_least_the_documented_floor(self):
        """
        CVD carries weight 0.7 and can veto news bias. A HIGH-strength,
        HIGH-importance, 0.85-relevance signal through the normal weight path
        clears that floor.
        """
        snapshot = cs.CoinGlassSnapshot(
            asset="XAUUSD",
            cvd=cs.CVDSeries(points=[(0, 1.0), (1, 2.0), (2, 3.0), (3, 4.0)],
                             divergence="CVD_BULLISH",
                             divergence_note="x"),
            degraded=False,
        )
        signals = cs.coinglass_signals_to_weighted({"XAUUSD": snapshot})
        signal = signals[0]
        assert signal.relevance == 0.85
        assert signal.strength.value == "HIGH"
        assert signal.importance.value == "HIGH"
        assert signal.category.value == "CVD"

    def test_liquidation_cluster_is_not_a_directional_signal(self):
        """
        A cluster above spot is NOT "bearish" - it says a short squeeze is
        near. So a snapshot with ONLY clusters must produce no signal and no
        direction, only a caution flag.
        """
        snapshot = cs.CoinGlassSnapshot(
            asset="XAUUSD",
            spot_price=4225.0,
            cluster_above=cs.HeatmapCluster(4230.0, "high", "short", 0.9),
            caution_above=True,
            degraded=False,
        )
        assert cs.coinglass_signals_to_weighted({"XAUUSD": snapshot}) == []
        assert "within 1.0% of spot" in snapshot.caution_note()

    def test_caution_flags_helper_reports_near_clusters(self):
        snapshot = cs.CoinGlassSnapshot(
            asset="XAUUSD", spot_price=4225.0,
            cluster_below=cs.HeatmapCluster(4220.0, "high", "long", 0.9),
            caution_below=True, degraded=False)
        pair = cs.caution_flags({"XAUUSD": snapshot})
        assert len(pair) == 1
        assert pair[0][0] == "XAUUSD"
        assert "4220.00" in pair[0][1]


# ---------------------------------------------------------------------------
# Rate limiting guarantees
# ---------------------------------------------------------------------------
class TestRateLimiting:
    def test_disabled_feature_never_scrapes(self):
        async def go():
            return await cs.scrape_asset_async("XAUUSD")

        original = settings.coinglass_enabled
        settings.coinglass_enabled = False
        try:
            assert asyncio.run(go()) is None
        finally:
            settings.coinglass_enabled = original

    def test_fresh_cache_blocks_a_second_fetch(self):
        """
        The TTL cache is what implements "max once every 5 minutes": with a
        fresh entry the scrape returns the CACHED reading and never renders.
        """
        payload = {"asset": "XAUUSD", "degraded": False, "cvd_divergence":
                   "CVD_BULLISH", "cvd_note": "cached", "cvd_points": 4,
                   "source_url": "https://example/chart"}
        cache = cs._get_cache()
        cache.put("XAUUSD", payload)

        snapshot = asyncio.run(cs.scrape_asset_async("XAUUSD"))
        assert snapshot is not None
        assert snapshot.cvd is not None
        assert snapshot.cvd.divergence == "CVD_BULLISH"
        # Proves the cached payload was used verbatim.
        assert snapshot.source_url == "https://example/chart"

    def test_repeated_calls_inside_the_spacing_window_do_not_refetch(self):
        """
        Two callers racing in the same tick: the second must be stopped by the
        in-process spacing guard, not by a second browser.
        """
        original = settings.coinglass_enabled
        settings.coinglass_enabled = True
        try:
            # Prime the spacing stamp as if a fetch just happened.
            cs._last_fetch_attempt["XAUUSD"] = cs.time.time()
            # No cache entry -> the spacing guard is the only thing that can
            # stop a render here, and it must.
            assert asyncio.run(cs.scrape_asset_async("XAUUSD")) is None
        finally:
            settings.coinglass_enabled = original

    def test_backoff_parks_a_polling_asset(self):
        cs._failure_streak["XAUUSD"] = 3
        cs._last_fetch_attempt["XAUUSD"] = cs.time.time()
        # Backed off -> skipped, not fetched, and no exception.
        assert asyncio.run(cs.scrape_asset_async("XAUUSD")) is None


# ---------------------------------------------------------------------------
# URL / asset routing
# ---------------------------------------------------------------------------
class TestAssetRouting:
    def test_all_three_assets_map_to_their_configured_url(self):
        assert cs.asset_url("XAUUSD") == settings.coinglass_gold_url
        assert cs.asset_url("XAGUSD") == settings.coinglass_silver_url
        assert cs.asset_url("BTCUSD") == settings.coinglass_btc_url

    def test_unknown_asset_falls_back_to_btc(self):
        assert cs.asset_url("SPXUSD") == settings.coinglass_btc_url

    def test_asset_symbol_normalises_enum_and_string(self):
        from app.models.enums import Asset
        assert cs.asset_symbol(Asset.GOLD) == "XAUUSD"
        assert cs.asset_symbol("xauusd") == "XAUUSD"
        assert cs.asset_symbol(Asset.BITCOIN) == "BTCUSD"

    def test_btc_uses_the_public_keyless_page_by_default(self):
        """BTC's heatmap page is public, so it needs neither key nor login."""
        assert settings.coinglass_btc_url == \
            "https://www.coinglass.com/pro/futures/LiquidationHeatMap"

    def test_metals_may_be_pointed_at_the_legend_chart(self):
        """
        Gold and Silver come from the \"Legend\" chart, which is the only
        CoinGlass source that shows them. The default remains the public page
        (so an unconfigured install degrades safely), and .env overrides it
        with the Legend chart once a login session is saved.
        """
        # Either the safe default or an explicit Legend chart is acceptable;
        # what must never happen is a metals URL that silently renders BTC.
        for asset_url in (settings.coinglass_gold_url,
                          settings.coinglass_silver_url):
            assert asset_url  # never empty
            # A Legend chart needs a login, so it must never be paired with an
            # empty profile dir in a way that would look like a keyless read.
            if "legend.coinglass.com" in asset_url:
                assert settings.coinglass_btc_url != asset_url

    def test_per_asset_urls_are_overridable_independently(self):
        """Each asset keeps its own URL so a page can be swapped alone."""
        assert (settings.coinglass_gold_url != ""
                and settings.coinglass_silver_url != ""
                and settings.coinglass_btc_url != "")

    def test_default_poll_interval_is_five_minutes(self):
        assert settings.coinglass_poll_seconds == 300
        assert settings.coinglass_cache_ttl_seconds == 300


class TestAssetAllowlist:
    """
    Only BTC is readable, so the default must not schedule the metals.

    Listing an unreadable asset would cost a wasted browser render every poll
    against a login wall, which both wastes time and looks like bot traffic.
    """

    def test_default_allowlist_is_btc_only(self):
        assert cs.configured_assets() == ["BTCUSD"]

    def test_allowlist_parses_and_normalises(self, monkeypatch):
        monkeypatch.setattr(settings, "coinglass_assets",
                            "xauusd, BTCUSD ,XAGUSD")
        assert cs.configured_assets() == ["XAUUSD", "BTCUSD", "XAGUSD"]

    def test_empty_allowlist_falls_back_to_btc(self, monkeypatch):
        monkeypatch.setattr(settings, "coinglass_assets", "  ,  ")
        assert cs.configured_assets() == ["BTCUSD"]

    def test_scrape_all_only_fetches_configured_assets(self, monkeypatch):
        """The allowlist must decide what is fetched, not the Asset enum."""
        fetched: list[str] = []

        async def fake_scrape(asset):
            fetched.append(asset)
            return None

        monkeypatch.setattr(cs, "scrape_asset_async", fake_scrape)
        monkeypatch.setattr(cs, "configured_assets", lambda: ["BTCUSD"])
        result = asyncio.run(cs.scrape_all_async())
        assert fetched == ["BTCUSD"]        # no wasted metals render
        assert result == {}


# ---------------------------------------------------------------------------
# Canvas-rendered pages (CoinGlass's public layout)
# ---------------------------------------------------------------------------
#: Captured from the live page: the price-axis labels with their canvas
#: coordinates, exactly as the instrumented fillText reported them.
AXIS_TEXTS = [
    {"s": "87572", "x": 1202, "y": 40, "fill": "#000000", "font": "12px"},
    {"s": "86000", "x": 1202, "y": 150, "fill": "#000000", "font": "12px"},
    {"s": "84000", "x": 1202, "y": 291, "fill": "#000000", "font": "12px"},
    {"s": "82000", "x": 1202, "y": 431, "fill": "#000000", "font": "12px"},
    {"s": "80000", "x": 1202, "y": 572, "fill": "#000000", "font": "12px"},
    {"s": "79172", "x": 1202, "y": 630, "fill": "#000000", "font": "12px"},
]


def _canvas_result(hot, *, spot=84_000.0):
    """One canvas analysis entry, in the shape _READ_CANVAS_JS returns."""
    dd = {
        "legend": [], "rows": [], "texts": list(AXIS_TEXTS),
        "canvas": {"w": 1255, "h": 730},
        "canvas_index": 0, "legend_x": 20,
    }
    for y, intensity in hot:
        dd["rows"].append({"y": y, "intensity": intensity,
                           "x": 700, "hex": "#46035c"})
    return dd


def _canvas_page(hot: list[tuple[float, float]], *, spot=84_000.0):
    """
    Build the canvas profile the extractor sees.

    ``hot`` is (pixel_y, legend_intensity) pairs; each becomes one sampled row.
    Rows below ``MIN_CLUSTER_INTENSITY`` are dropped by the caller's filter, so
    a low-intensity row models a cold pixel that should not form a cluster.
    """
    return {"texts": list(AXIS_TEXTS),
            "canvas_list": [{"index": 0, "w": 1255, "h": 730}],
            "results": [_canvas_result(hot, spot=spot)]}


_SPOTS = {"BTCUSD": 84_000.0, "XAUUSD": 4_167.5, "XAGUSD": 60.205}


@pytest.fixture()
def fake_spot(monkeypatch):
    """Pin the spot price per asset so no price feed is contacted."""
    monkeypatch.setattr(cs, "_fetch_spot_price",
                        lambda asset: _SPOTS.get(asset))


class TestCanvasPriceAxis:
    def test_axis_labels_are_calibrated_into_pixel_price_pairs(self):
        axis = cs._calibrate_price_axis(AXIS_TEXTS)
        assert len(axis) == 6
        # sorted by pixel y ascending
        assert axis[0][0] == 40 and axis[-1][0] == 630
        # price descends as y grows (a price chart's y axis)
        assert axis[0][1] > axis[-1][1]

    def test_round_numbers_and_range_edges_are_both_kept(self):
        """The non-round min/max labels are the data range, not noise."""
        axis = cs._calibrate_price_axis(AXIS_TEXTS)
        prices = {p for _, p in axis}
        assert 79_172 in prices and 87_572 in prices
        assert 80_000 in prices and 86_000 in prices

    def test_interpolation_hits_a_known_gridline(self):
        axis = cs._calibrate_price_axis(AXIS_TEXTS)
        # The midpoints between two gridlines must interpolate exactly.
        assert cs._price_at(axis, 150.0) == pytest.approx(86_000.0)
        assert cs._price_at(axis, 572.0) == pytest.approx(80_000.0)

    def test_interpolated_price_between_gridlines(self):
        axis = cs._calibrate_price_axis(AXIS_TEXTS)
        # Halfway between y=572 (80000) and y=431 (82000) is 81000.
        assert cs._price_at(axis, 501.5) == pytest.approx(81_000.0)

    def test_axis_column_is_identified_from_a_shared_x(self):
        """A stray numeric label elsewhere must not join the calibration."""
        noisy = list(AXIS_TEXTS) + [
            {"s": "48.88M", "x": 24, "y": 51, "fill": "#333", "font": "10px"},
            {"s": "0", "x": 24, "y": 643, "fill": "#333", "font": "10px"},
        ]
        axis = cs._calibrate_price_axis(noisy)
        # Still exactly the six price-axis points.
        assert len(axis) == 6

    def test_no_axis_means_no_calibration(self):
        assert cs._calibrate_price_axis([]) == []
        assert cs._calibrate_price_axis(
            [{"s": "abc", "x": 5, "y": 5, "fill": "", "font": ""}]) == []


class TestCanvasExtraction:
    def test_hot_rows_become_clusters_at_the_calibrated_price(self, fake_spot):
        # y=501.5 interpolates to 81000 on the captured axis.
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", _canvas_result([(501.5, 1.0)]),
            "https://www.coinglass.com/pro/futures/LiquidationHeatMap")
        assert not snapshot.degraded
        assert len(snapshot.heatmap_clusters) == 1
        assert snapshot.heatmap_clusters[0].price_level == pytest.approx(
            81_000.0, abs=1.0)

    def test_spot_is_resolved_from_the_price_feed(self, fake_spot):
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", _canvas_result([(501.5, 1.0)]), "https://example/chart")
        assert snapshot.spot_price == 84_000.0

    def test_nearest_cluster_below_and_above_resolve_against_spot(self, fake_spot):
        # spot 84000 sits at pixel y=291 on the captured axis.
        # Below: y=310 (~83700). Above: y=270 (~84300). They are 40px apart,
        # far beyond the 3px band-merge window, so they stay two clusters.
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", _canvas_result([(310.0, 1.0), (270.0, 0.8)]),
            "https://example/chart")
        assert snapshot.cluster_below is not None
        assert snapshot.cluster_above is not None
        assert snapshot.cluster_below.price_level < snapshot.spot_price
        assert snapshot.cluster_above.price_level > snapshot.spot_price
        assert snapshot.cluster_below.side == "long"
        assert snapshot.cluster_above.side == "short"

    def test_adjacent_rows_merge_into_one_cluster(self, fake_spot):
        """Rows inside the 3px band window are a single cluster."""
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", _canvas_result([(300.0, 1.0), (301.0, 0.9), (302.0, 0.8)]),
            "https://example/chart")
        assert len(snapshot.heatmap_clusters) == 1

    def test_caution_flag_when_cluster_is_within_one_percent_of_spot(self, fake_spot):
        # spot 84000; 1% is 83640. y=305 is ~83700, inside 1%.
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", _canvas_result([(305.0, 1.0)]),
            "https://example/chart")
        assert snapshot.caution_below is True
        assert "within 1.0% of spot" in snapshot.caution_note()

    def test_cold_rows_do_not_form_a_cluster(self, fake_spot):
        """A row the legend rates below the floor is not a LARGE cluster."""
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", _canvas_result([(501.5, 0.10)]), "https://example/chart")
        assert snapshot.heatmap_clusters == []
        assert snapshot.degraded is True

    def test_intensity_is_normalised_against_the_hottest_row(self, fake_spot):
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", _canvas_result([(300.0, 0.5), (320.0, 1.0)]),
            "https://example/chart")
        hot = max(c.intensity_score for c in snapshot.heatmap_clusters)
        assert hot == pytest.approx(1.0)
        # The cooler row normalises to 0.5 of the peak.
        cool = min(c.intensity_score for c in snapshot.heatmap_clusters)
        assert cool == pytest.approx(0.5, abs=0.01)

    def test_missing_axis_degrades_instead_of_guessing(self, fake_spot):
        """Rows but no axis means no defensible price: degraded, not invented."""
        snapshot = cs._parse_canvas_extraction(
            "BTCUSD", {"canvas": {"w": 800, "h": 600},
                       "texts": [], "rows": [{"y": 100, "intensity": 1.0}]},
            "https://example/chart")
        assert snapshot.degraded is True
        assert snapshot.heatmap_clusters == []

    def test_page_with_no_chart_at_all_is_degraded(self, fake_spot):
        snapshot = cs._parse_canvas_extraction("BTCUSD", {}, "https://example")
        assert snapshot.degraded is True

    def test_dispatch_sends_canvas_data_to_the_canvas_parser(self, fake_spot):
        data = _canvas_page([(501.5, 1.0)])
        snapshot = cs._parse_extraction("BTCUSD", data, "https://example/chart")
        assert snapshot.spot_price == 84_000.0
        assert snapshot.heatmap_clusters

    def test_multi_panel_page_matches_the_panel_for_the_asset(self, fake_spot):
        """
        A Legend chart stacks one panel per instrument. Each asset must be
        matched to the panel whose price axis belongs to it - gold's ~4.2k axis
        must not be handed to the silver request.
        """
        gold_panel = _canvas_result([(501.5, 1.0)])
        gold_panel["texts"] = [
            {"s": "4,300", "x": 1202, "y": 40, "fill": "#000", "font": "12px"},
            {"s": "4,200", "x": 1202, "y": 572, "fill": "#000", "font": "12px"},
            {"s": "4,100", "x": 1202, "y": 630, "fill": "#000", "font": "12px"},
        ]
        silver_panel = _canvas_result([(501.5, 1.0)])
        silver_panel["texts"] = [
            {"s": "75", "x": 1202, "y": 40, "fill": "#000", "font": "12px"},
            {"s": "60", "x": 1202, "y": 572, "fill": "#000", "font": "12px"},
            {"s": "45", "x": 1202, "y": 630, "fill": "#000", "font": "12px"},
        ]
        page = {
            "texts": gold_panel["texts"] + silver_panel["texts"],
            "canvas_list": [{"index": 0}, {"index": 1}],
            "results": [gold_panel, silver_panel],
        }

        silver = cs._parse_extraction("XAGUSD", page, "https://example/chart")
        assert silver.degraded is False
        assert silver.spot_price == 60.2      # silver's spot, not gold's or BTC's
        assert silver.cluster_above is not None
        assert silver.cluster_above.price_level < 100   # silver's own scale

        gold = cs._parse_extraction("XAUUSD", page, "https://example/chart")
        assert gold.spot_price == 4_167.5     # the gold spot
        assert gold.cluster_above.price_level > 1000    # gold's own scale

    def test_multi_panel_page_fails_when_no_panel_matches(self, fake_spot):
        """No panel in range -> fail the poll, do not read the wrong one."""
        btc_panel = _canvas_result([(501.5, 1.0)])   # AXIS_TEXTS: ~79k..88k
        page = {"texts": list(AXIS_TEXTS),
                "canvas_list": [{"index": 0}],
                "results": [btc_panel]}
        silver = cs._parse_extraction("XAGUSD", page, "https://example/chart")
        assert silver.degraded is True
        assert silver.heatmap_clusters == []

    def test_same_instrument_two_panes_prefers_the_heatmap(self, fake_spot):
        """
        A pane's price axis cannot tell a silver HEATMAP apart from a silver CVD
        chart - both use silver's scale. The one actually drawing a heat field
        must win, or the request would be answered with no clusters at all.
        """
        # A silver line/CVD pane: plausible axis, but almost no palette rows.
        silver_line = _canvas_result([])
        silver_line["texts"] = [
            {"s": "75", "x": 1202, "y": 40, "fill": "#000", "font": "12px"},
            {"s": "60", "x": 1202, "y": 572, "fill": "#000", "font": "12px"},
            {"s": "45", "x": 1202, "y": 630, "fill": "#000", "font": "12px"},
        ]
        # The silver HEATMAP pane: same axis, but a real heat field.
        silver_heatmap = _canvas_result(
            [(300.0, 1.0), (305.0, 0.9), (310.0, 0.8), (320.0, 1.0),
             (400.0, 0.7), (450.0, 1.0), (480.0, 0.6)])
        silver_heatmap["texts"] = list(silver_line["texts"])

        page = {"texts": silver_line["texts"],
                "canvas_list": [{"index": 0}, {"index": 1}],
                "results": [silver_line, silver_heatmap]}

        snap = cs._parse_extraction("XAGUSD", page, "https://example/chart")
        assert snap.degraded is False
        assert snap.spot_price == 60.2
        # The heatmap pane's clusters, not the empty line pane's.
        assert snap.heatmap_clusters
        prices = [c.price_level for c in snap.heatmap_clusters]
        assert all(p < 100 for p in prices)   # still silver's own scale

    def test_single_panel_page_resolves_without_matching(self, fake_spot):
        """One pane needs no matching - it is the only candidate."""
        page = {"texts": list(AXIS_TEXTS),
                "canvas_list": [{"index": 0}],
                "results": [_canvas_result([(501.5, 1.0)])]}
        snap = cs._parse_extraction("BTCUSD", page, "https://example/chart")
        assert snap.spot_price == 84_000.0
        assert snap.heatmap_clusters

    def test_dispatch_sends_highcharts_data_to_the_dom_parser(self):
        data = _page(_heatmap_chart([(4200.0, 1.0), (4240.0, 0.8)]),
                     _series_chart([(0, 4225.0)], name="Close", kind="price"))
        snapshot = cs._parse_extraction("XAUUSD", data, "https://example/chart")
        assert snapshot.spot_price == 4225.0
        assert snapshot.heatmap_clusters


class TestCanvasInstrumentationContract:
    """
    The JS is what makes a canvas chart readable, so its contract is pinned.

    These assert on the source strings rather than executing them, which is
    enough to catch a regression that silently drops a patch: the page draws
    the axis labels through fillText, so if that method is not wrapped there is
    no calibration and every canvas page degrades.
    """

    def test_filltext_is_patched(self):
        assert "proto.fillText" in cs._INSTRUMENT_JS
        assert "origFillText" in cs._INSTRUMENT_JS

    def test_transform_is_applied_to_local_coordinates(self):
        """Without getTransform the coordinates are chart-local, not canvas."""
        assert "getTransform()" in cs._INSTRUMENT_JS

    def test_read_back_returns_one_analysis_per_canvas(self):
        """
        A Legend chart holds several instruments as separate canvases, so the
        extractor must analyse every one rather than only the first.
        """
        assert "canvases.forEach" in cs._READ_CANVAS_JS
        assert "canvas_list" in cs._READ_CANVAS_JS
        assert "out.results.push" in cs._READ_CANVAS_JS

    def test_legend_is_sampled_for_colour_intensity(self):
        """No hard-coded palette: intensity comes from the page's own legend."""
        assert "legend" in cs._READ_CANVAS_JS
        assert "intensity" in cs._READ_CANVAS_JS
