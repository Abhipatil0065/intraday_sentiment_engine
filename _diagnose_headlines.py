"""Diagnose detect_direction for 16 equity headlines."""
from app.analysis.cue_rules import detect_direction
from app.models.enums import Direction

HEADLINES = [
    # (headline, expected_metal, expected_equity)
    ("Missile strike widens as sanctions bite amid escalating conflict", "BEARISH", "BEARISH"),
    ("Diplomatic breakthrough eases Middle East tensions", "BULLISH", "BULLISH"),
    ("Nonfarm payrolls beat expectations strong job growth", "BEARISH", "BULLISH"),
    ("Nonfarm payrolls miss expectations unemployment rises", "BULLISH", "BEARISH"),
    ("Unemployment rate rises to 4.3% vs 4.1% forecast", "BULLISH", "BEARISH"),
    ("Unemployment rate falls to 3.8% vs 4.0% forecast", "BEARISH", "BULLISH"),
    ("Fed signals rate hike as Powell says higher for longer", "BEARISH", "BEARISH"),
    ("Fed pivots dovish signals rate cuts coming", "BULLISH", "BULLISH"),
    ("CPI comes in hot at 3.5% vs 3.1% forecast", "BEARISH", "BEARISH"),
    ("CPI cools to 2.1% vs 2.5% forecast, below expectations", "BULLISH", "BULLISH"),
    ("Nasdaq surges on tech earnings surge fed", "BULLISH", "BULLISH"),
    ("S&P 500 slides as yields jump on hot CPI fed", "BEARISH", "BEARISH"),
    ("Fed: analyst preview: companies may guide lower", "NEUTRAL", "BEARISH"),
    ("Goldman Sachs earnings beat drives rally across Wall Street fed", "BULLISH", "BULLISH"),
    ("Market rally continues as risk sentiment improves fed", "BULLISH", "BULLISH"),
    ("Risk-off sentiment grips stocks as war fears rise fed", "BEARISH", "BEARISH"),
]

print(f"{'#':>3}  {'Headline':<65} {'Metal':>8} {'Eq':>8} {'MetalOK':>8} {'EqOK':>8}  Status")
print("-" * 120)
issues = []
for i, (headline, exp_metal, exp_eq) in enumerate(HEADLINES, 1):
    metal = detect_direction(headline, equity=False)
    eq = detect_direction(headline, equity=True)
    metal_ok = metal.value in ("BULLISH", "BEARISH") if exp_metal == "NEUTRAL" else metal.value == exp_metal
    eq_ok = eq.value in ("BULLISH", "BEARISH") if exp_eq == "NEUTRAL" else eq.value == exp_eq
    status = "OK" if (metal_ok and eq_ok) else "ISSUE"
    if status == "ISSUE":
        issues.append((i, headline, metal.value, eq.value, exp_metal, exp_equity := exp_eq))
    print(f"{i:>3}  {headline:<65} {metal.value:>8} {eq.value:>8} {'YES' if metal_ok else 'NO':>8} {'YES' if eq_ok else 'NO':>8}  {status}")

print(f"\nIssues found: {len(issues)}")
for i, hl, metal, eq, exp_m, exp_e in issues:
    print(f"  #{i}: '{hl}' -> metal={metal} (exp {exp_m}), equity={eq} (exp {exp_e})")
