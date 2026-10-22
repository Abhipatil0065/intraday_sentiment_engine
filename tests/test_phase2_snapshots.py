"""
PHASE 2: BIAS SNAPSHOT LOGGING (news-only).

The log exists so a published bias can be reviewed by hand. Two properties
matter most: it must never break a poll, and it must record exactly what the
dashboard displayed.

Deliberately NEWS-ONLY - no price column, no price fetch, no price-derived
number anywhere. Price comparison is manual.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.enums import Asset
from app.services.bias_snapshots import (
    DRIVER_LIMIT,
    AssetVerdict,
    build_rows,
    ist_stamp,
    write_bias_snapshots,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class FakeDriver:
    def __init__(self, title, contribution=0.5, weight=0.4,
                 direction="BULLISH"):
        self.title = title
        self.contribution = contribution
        self.weight = weight
        self.direction = direction


def verdict(asset=Asset.GOLD, score=-30.0, confidence=0.4, bias="BEARISH",
            **kw):
    return AssetVerdict(asset=asset, score=score, confidence=confidence,
                        bias=bias, article_count=5, **kw)


# --------------------------------------------------------------------------
# Row shape
# --------------------------------------------------------------------------
def test_one_row_per_asset():
    rows = build_rows([verdict(a) for a in Asset], now=NOW)
    assert [r["asset"] for r in rows] == Asset.values()
    assert len(rows) == len(Asset.values())


def test_row_carries_every_required_column():
    row = build_rows([verdict()], now=NOW)[0]
    for field in ("timestamp_utc", "timestamp_ist", "asset", "bias", "score",
                  "confidence", "bullish_pct", "bearish_pct", "article_count",
                  "last_signal_age_min", "bull_drivers", "bear_drivers"):
        assert field in row, field


def test_percentages_match_the_display_projection():
    """The log records what the reader saw, not a re-derived number."""
    from app.analysis.display import to_display

    shown = to_display(-30.0, "BEARISH")
    row = build_rows([verdict(score=-30.0)], now=NOW)[0]
    assert row["bullish_pct"] == shown["bull_pct"]
    assert row["bearish_pct"] == shown["bear_pct"]


def test_percentages_always_sum_to_100():
    for score in (-100.0, -12.0, 0.0, 45.0, 100.0):
        row = build_rows([verdict(score=score)], now=NOW)[0]
        assert round(row["bullish_pct"] + row["bearish_pct"], 6) == 100.0


def test_signal_age_is_recorded():
    row = build_rows([verdict(last_signal_age_min=12.5)], now=NOW)[0]
    assert row["last_signal_age_min"] == 12.5
# --------------------------------------------------------------------------
# IST timestamp
# --------------------------------------------------------------------------
def test_ist_stamp_is_12_hour():
    stamp = ist_stamp(NOW)
    assert stamp.endswith("IST")
    assert "AM" in stamp or "PM" in stamp


def test_ist_stamp_converts_utc_to_ist():
    # 12:00 UTC == 17:30 IST (+05:30), i.e. 5:30 PM on a 12-hour clock.
    assert ist_stamp(NOW) == "05:30:00 PM IST"


def test_ist_stamp_midnight_is_12_am():
    midnight = datetime(2026, 10, 5, 18, 30, tzinfo=timezone.utc)
    assert ist_stamp(midnight) == "12:00:00 AM IST"


def test_row_carries_both_timestamps():
    row = build_rows([verdict()], now=NOW)[0]
    assert row["timestamp_utc"] == NOW
    assert row["timestamp_ist"].endswith("IST")


# --------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------
def test_top_three_drivers_are_stored_per_side():
    drivers = [FakeDriver(f"story {i}") for i in range(6)]
    row = build_rows([verdict(bull_drivers=drivers,
                              bear_drivers=drivers)], now=NOW)[0]
    assert len(row["bull_drivers"]) == DRIVER_LIMIT
    assert len(row["bear_drivers"]) == DRIVER_LIMIT


def test_drivers_carry_their_weight():
    row = build_rows([verdict(bull_drivers=[FakeDriver("Fed dovish",
                                                       weight=0.8123)])],
                     now=NOW)[0]
    assert row["bull_drivers"][0]["weight"] == 0.8123
    assert row["bull_drivers"][0]["title"] == "Fed dovish"


def test_drivers_default_to_empty_lists():
    row = build_rows([verdict()], now=NOW)[0]
    assert row["bull_drivers"] == []
    assert row["bear_drivers"] == []


def test_more_than_three_drivers_is_capped_not_truncated_silently():
    drivers = [FakeDriver(f"s{i}") for i in range(10)]
    row = build_rows([verdict(bull_drivers=drivers)], now=NOW)[0]
    assert len(row["bull_drivers"]) == 3


# --------------------------------------------------------------------------
# NEWS-ONLY: no price anywhere in the log
# --------------------------------------------------------------------------
def test_no_price_key_exists_on_any_row():
    for row in build_rows([verdict(a) for a in Asset], now=NOW):
        assert not any("price" in k.lower() for k in row)


def test_model_table_has_no_price_column():
    from app.models.db_models import BiasSnapshot

    cols = {c.name.lower() for c in BiasSnapshot.__table__.columns}
    assert not any("price" in c for c in cols)


def test_writer_module_has_no_price_import():
    """The writer must not reach for a price source at all."""
    import inspect

    from app.services import bias_snapshots

    src = inspect.getsource(bias_snapshots)
    assert "price_data" not in src
    assert "get_price_reading" not in src


def test_writer_is_pure_and_deterministic():
    """Same inputs, same output - no clock or network read inside build_rows."""
    a = build_rows([verdict()], now=NOW)
    b = build_rows([verdict()], now=NOW)
    assert a == b


# --------------------------------------------------------------------------
# Never breaks a poll
# --------------------------------------------------------------------------
def test_write_failure_is_swallowed_and_returns_zero():
    """A broken session must never propagate into the pipeline."""

    class Exploding:
        def add(self, _row):
            raise RuntimeError("db down")

    assert write_bias_snapshots(Exploding(), [verdict()]) == 0


def test_write_is_a_noop_when_disabled(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "bias_snapshot_enabled", False,
                        raising=False)
    assert write_bias_snapshots(object(), [verdict()]) == 0


def test_empty_verdict_list_writes_nothing():
    assert write_bias_snapshots(object(), []) == 0


# --------------------------------------------------------------------------
# CSV export
# --------------------------------------------------------------------------
class FakeSnapshot(dict):
    """Stand-in for a loaded ``bias_snapshots`` row (a plain dict)."""

    def __init__(self, asset, bias, ts, **kw):
        super().__init__(
            timestamp_utc=ts,
            timestamp_ist="05:30:00 PM IST",
            asset=asset,
            bias=bias,
            score=kw.get("score", -30.0),
            confidence=kw.get("confidence", 0.4),
            bullish_pct=kw.get("bullish_pct", 35.0),
            bearish_pct=kw.get("bearish_pct", 65.0),
            article_count=kw.get("article_count", 5),
            last_signal_age_min=kw.get("last_signal_age_min"),
            bull_drivers=kw.get("bull_drivers", []),
            bear_drivers=kw.get("bear_drivers", []),
        )


def _gold_series():
    from scripts.export_snapshots import to_csv_rows

    return to_csv_rows([
        FakeSnapshot("XAUUSD", "BEARISH", NOW),
        FakeSnapshot("XAUUSD", "BEARISH", NOW + timedelta(minutes=3)),
        FakeSnapshot("XAUUSD", "BULLISH", NOW + timedelta(minutes=6)),
        FakeSnapshot("XAUUSD", "BULLISH", NOW + timedelta(minutes=9)),
    ])


def test_export_marks_only_real_flips():
    assert [r["bias_changed"] for r in _gold_series()] == [
        "FALSE", "FALSE", "TRUE", "FALSE"]


def test_first_row_of_an_asset_is_not_a_change():
    assert _gold_series()[0]["bias_changed"] == "FALSE"


def test_flips_are_tracked_per_asset_independently():
    from scripts.export_snapshots import to_csv_rows

    rows = [
        FakeSnapshot("XAUUSD", "BEARISH", NOW),
        FakeSnapshot("BTCUSD", "BULLISH", NOW),
        FakeSnapshot("XAUUSD", "BULLISH", NOW + timedelta(minutes=3)),
        FakeSnapshot("BTCUSD", "BULLISH", NOW + timedelta(minutes=3)),
    ]
    out = to_csv_rows(rows)
    assert [r["bias_changed"] for r in out if r["asset"] == "XAUUSD"] == [
        "FALSE", "TRUE"]
    assert [r["bias_changed"] for r in out if r["asset"] == "BTCUSD"] == [
        "FALSE", "FALSE"]


def test_manual_price_note_is_present_and_empty():
    row = _gold_series()[0]
    assert "manual_price_note" in row
    assert row["manual_price_note"] == ""


def test_export_columns_match_the_contract():
    from scripts.export_snapshots import FIELDNAMES, to_csv_rows

    rows = to_csv_rows([FakeSnapshot("XAUUSD", "BEARISH", NOW)])
    assert set(rows[0]) == set(FIELDNAMES)
    assert "bias_changed" in FIELDNAMES


def test_drivers_are_flattened_into_one_cell():
    from scripts.export_snapshots import to_csv_rows

    row = to_csv_rows([FakeSnapshot(
        "XAUUSD", "BULLISH", NOW,
        bull_drivers=[{"title": "Fed dovish", "weight": 0.81},
                      {"title": "Yields fall", "weight": 0.55}])])[0]
    assert "Fed dovish" in row["bull_drivers"]
    assert "0.81" in row["bull_drivers"]
    assert "|" in row["bull_drivers"]


def test_date_bound_parsing_is_inclusive_of_the_end_day():
    from scripts.export_snapshots import parse_bound

    start = parse_bound("2026-10-05", end_of_day=False)
    end = parse_bound("2026-10-05", end_of_day=True)
    assert start.hour == 0 and start.minute == 0
    assert end.hour == 23 and end.minute == 59


def test_bad_date_is_ignored_rather_than_crashing():
    from scripts.export_snapshots import parse_bound

    assert parse_bound("not-a-date", end_of_day=False) is None
    assert parse_bound("", end_of_day=False) is None


def test_unknown_asset_is_rejected():
    from scripts.export_snapshots import main

    assert main(["export_snapshots.py", "--asset", "GBPUSD"]) == 2


def test_absent_signal_age_is_null_not_zero():
    """Null means 'no signal at all', which is not the same as 'just now'."""
    row = build_rows([verdict(last_signal_age_min=None)], now=NOW)[0]
    assert row["last_signal_age_min"] is None