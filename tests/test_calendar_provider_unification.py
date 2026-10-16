"""Regression guard for calendar provider unification on FinancialJuice."""
import pathlib
import re

from app.config import settings
from app.ingestion.economic_calendar import fetch_calendar
from app.services import pipeline


def test_fetch_calendar_only_financialjuice(monkeypatch):
    # FinancialJuice ships OFF by default; enable it for the assertion and keep
    # the stale-cache path out of the way so we inspect freshly-fetched events.
    monkeypatch.setattr(settings, "financialjuice_calendar_enabled", True)
    monkeypatch.setattr(settings, "calendar_cache_enabled", False)

    async def main():
        events, errors = await fetch_calendar()
        for ev in events:
            assert getattr(ev, "provider", "financialjuice") == "financialjuice"

    import asyncio
    asyncio.run(main())


def test_config_has_no_faireconomy_urls():
    assert not hasattr(settings, "economic_calendar_url")
    assert not hasattr(settings, "economic_calendar_nextweek_url")


def test_economic_calendar_does_not_import_faireconomy():
    src = pathlib.Path("app/ingestion/economic_calendar.py").read_text()
    assert "_fetch_faireconomy" not in src


def test_dashboard_payload_only_fj():
    payload = pipeline.build_payload("XAUUSD") if hasattr(pipeline, "build_payload") else None
    if payload is None:
        return
    rows = payload.get("economic_calendar", []) if isinstance(payload, dict) else []
    for r in rows:
        if isinstance(r, dict):
            assert r.get("provider", "financialjuice") == "financialjuice"


def test_no_faireconomy_string_under_app():
    root = pathlib.Path("app")
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".html", ".js", ".css"}:
            text = path.read_text(errors="ignore")
            assert "faireconomy" not in text.lower(), f"faireconomy found in {path}"
