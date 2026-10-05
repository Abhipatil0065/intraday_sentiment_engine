"""Which titles produce a signal for exactly ONE asset?"""
from datetime import datetime, timezone

from app.analysis.economic_impact import (
    CalendarEvent, classify_event, signals_for_event,
)

T0 = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)

CASES = [
    ("US Manufacturing PMI", "USD"),
    ("China Manufacturing PMI", "CNY"),
    ("German Manufacturing PMI", "EUR"),
    ("China Copper Imports", "CNY"),
    ("Japan Industrial Production", "JPY"),
    ("China Retail Sales", "CNY"),
    ("China Industrial Production", "CNY"),
    ("China GDP", "CNY"),
    ("China Iron Ore Imports", "CNY"),
    ("Eurozone Industrial Production", "EUR"),
    ("China Caixin Manufacturing PMI", "CNY"),
    ("China Fixed Asset Investment", "CNY"),
]


def ev(title, country):
    return CalendarEvent(title=title, country=country, when_utc=T0,
                         impact="High", forecast=1.0, previous=0.5,
                         actual=2.0)


print("%-34s %-6s %-18s %s" % ("title", "ccy", "category", "assets"))
print("-" * 84)
for title, country in CASES:
    prof = classify_event(ev(title, country))
    sigs = signals_for_event(ev(title, country))
    assets = sorted(s.asset.value for s in sigs)
    flag = "  <== SINGLE" if len(assets) == 1 else ""
    print("%-34s %-6s %-18s %s%s"
          % (title, country, prof.category.value if prof else "NONE",
             ", ".join(assets) or "(none)", flag))