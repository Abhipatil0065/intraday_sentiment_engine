"""
BIAS SNAPSHOT + COMPARISON TOOL.

    python tools/baseline_snapshot.py save  baseline_before.json
    python tools/baseline_snapshot.py show  baseline_before.json
    python tools/baseline_snapshot.py compare baseline_before.json

``save``/``show``/``compare`` all run the deterministic harness in
``tools/bias_harness.py``, so the numbers depend only on engine code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.bias_harness import snapshot  # noqa: E402

FIELDS = ("bias", "label_shown", "bull_pct", "bear_pct", "score",
          "confidence", "contributing_articles", "contributing_clusters")


def _row(values: list) -> str:
    return " | ".join(str(v) for v in values)


def _fmt(snap: dict) -> str:
    out = [
        f"wiring={snap['wiring']}  corpus={snap['corpus_size']} "
        f"headlines  now={snap['generated_for_now_utc']}",
        "",
    ]
    header = ["asset"] + list(FIELDS)
    out.append(_row(header))
    out.append("-" * len(_row(header)))
    for asset, data in snap["assets"].items():
        out.append(_row([asset] + [data.get(f, "") for f in FIELDS]))
    return "\n".join(out)


def _fmt_compare(before: dict, after: dict) -> str:
    out = ["", "PER-ASSET CHANGE", "=" * 78, ""]
    header = ["asset", "field", "before", "after", "delta"]
    out.append(_row(header))
    out.append("-" * len(_row(header)))
    for asset, data in before["assets"].items():
        now_data = after["assets"].get(asset, {})
        for field in FIELDS:
            b, a = data.get(field), now_data.get(field)
            if b == a:
                continue
            if isinstance(b, (int, float)) and isinstance(a, (int, float)):
                delta = f"{a - b:+.2f}"
            else:
                delta = "CHANGED"
            out.append(_row([asset, field, b, a, delta]))
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    command = argv[1]

    if command == "save":
        # ``legacy=True`` captures the PRE-fix wiring, which is what phase 0
        # must record before anything is touched.
        path = Path(argv[2] if len(argv) > 2 else "baseline_before.json")
        data = snapshot(legacy=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"wrote {path}")
        print(_fmt(data))
        return 0

    if command == "show":
        path = Path(argv[2])
        print(_fmt(json.loads(path.read_text(encoding="utf-8"))))
        return 0

    if command == "compare":
        path = Path(argv[2])
        before = json.loads(path.read_text(encoding="utf-8"))
        after = snapshot(legacy=False)
        print(_fmt(before))
        print(_fmt(after))
        print(_fmt_compare(before, after))
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))