"""Scratch: locate to_display callers and display test expectations."""
import os
import re

ROOT = "."
SKIP = (".git", "__pycache__", ".pytest_cache", "node_modules", "extensions")

# 1) callers of to_display
print("=== to_display callers ===")
for r, d, fs in os.walk(ROOT):
    if any(s in r for s in SKIP):
        continue
    for f in fs:
        if not f.endswith(".py"):
            continue
        p = os.path.join(r, f)
        for i, l in enumerate(open(p, encoding="utf-8", errors="ignore")):
            if "to_display(" in l:
                print(f"{p}:{i+1}: {l.strip()}")

# 2) pct expectations in tests
print("=== pct assertions in tests ===")
for r, d, fs in os.walk(os.path.join(ROOT, "tests")):
    if any(s in r for s in SKIP):
        continue
    for f in fs:
        if not f.endswith(".py"):
            continue
        p = os.path.join(r, f)
        for i, l in enumerate(open(p, encoding="utf-8", errors="ignore")):
            if re.search(r"(bull_pct|bear_pct|\bpct\b)", l) and (
                "assert" in l or "==" in l or "to_display" in l
            ):
                print(f"{p}:{i+1}: {l.rstrip()}")
