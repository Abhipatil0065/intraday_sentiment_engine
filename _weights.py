"""BEFORE/AFTER measurement for the per-asset composite cap.

Run before and after the change; the output is the before/after table.
"""
from datetime import datetime, timezone

from app.analysis.economic_impact import (
    CalendarEvent, cap_composite_release, classify_event,
    resolve_calendar_signals, signals_for_event,
)
from app.config import settings

T0 = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
CAP = float(settings.calendar_composite_max_weight)
MODE = getattr(settings, "calendar_composite_cap_mode", "per_asset")
print("cap = %.2f   window = %d min   mode = %s"
      % (CAP, settings.calendar_composite_window_minutes, MODE))


def ev(title, f=0.3, p=0.2, a=0.4, when=T0, country="USD"):
    return CalendarEvent(title=title, country=country, when_utc=when,
                         impact="High", forecast=f, previous=p, actual=a)


def per_asset(signals):
    out: dict = {}
    for s in signals:
        out[s.asset.value] = out.get(s.asset.value, 0.0) + s.relevance
    return out


FIXTURES = [
    ("CPI headline+core (m/m + y/y)", [
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("US CPI YoY", 2.9, 2.8, 3.1),
        ev("US Core CPI MoM", 0.3, 0.2, 0.4),
        ev("US Core CPI YoY", 2.9, 2.8, 3.1),
    ]),
    ("PCE headline+core", [
        ev("US PCE Price Index MoM", 0.3, 0.2, 0.4),
        ev("US Core PCE Price Index MoM", 0.3, 0.2, 0.4),
    ]),
    ("NFP + unemployment (+wages)", [
        ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0),
        ev("US Unemployment Rate", 4.4, 4.3, 4.6),
        ev("US Average Hourly Earnings", 0.4, 0.3, 0.5),
    ]),
    ("CPI + employment together", [
        ev("US CPI MoM", 0.3, 0.2, 0.4),
        ev("US CPI YoY", 2.9, 2.8, 3.1),
        ev("US Core CPI MoM", 0.3, 0.2, 0.4),
        ev("US Nonfarm Payrolls", 180.0, 150.0, 220.0),
        ev("US Unemployment Rate", 4.4, 4.3, 4.6),
    ]),
]

ASSETS = ["XAUUSD", "XAGUSD", "BTCUSD", "USOIL"]
print()
print("%-32s %-26s %s" % ("fixture", "asset", "BEFORE -> AFTER"))
print("-" * 78)

summary = []
for name, events in FIXTURES:
    raw = [s for e in events for s in signals_for_event(e)]
    pre = per_asset(raw)
    post = per_asset(cap_composite_release(raw))
    for a in ASSETS:
        b, aft = pre.get(a, 0.0), post.get(a, 0.0)
        summary.append((name, a, b, aft))
        print("%-32s %-26s %.4f -> %.4f%s"
              % (name if a == ASSETS[0] else "", a, b, aft,
                 "  (capped)" if aft <= CAP + 1e-9 < b else ""))

print()
print("per-asset totals through the FULL pipeline (resolve_calendar_signals):")
for name, events in FIXTURES:
    out = resolve_calendar_signals(events, horizon_hours=48, now=T0)
    d = per_asset(out)
    print("  %-32s %s" % (name, "  ".join(
        "%s=%.4f" % (k, v) for k, v in sorted(d.items()))))
    bad = [k for k, v in d.items() if v > CAP + 1e-9]
    print("  %-32s breach: %s" % ("", bad or "none"))