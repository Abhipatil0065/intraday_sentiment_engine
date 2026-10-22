"""
Generate docs/TRANSMISSION_MAP.md from the transmission map itself.

The map is data (app/config/transmission_map.json) plus keyword rules
(app/analysis/transmission_map.py). Prose docs drift from both within days, so
this regenerates the reference straight from the source of truth on every run.
Never hand-edit the output - edit the generator or the map.

    python tools/generate_transmission_docs.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

OUT = REPO / "docs" / "TRANSMISSION_MAP.md"
ASSETS = ("XAUUSD", "XAGUSD", "BTCUSD", "EQUITY", "USOIL")
ASSET_LABELS = {
    "XAUUSD": "Gold",
    "XAGUSD": "Silver",
    "BTCUSD": "Bitcoin",
    "EQUITY": "Equity (S&P 500 proxy)",
    "USOIL": "Crude oil (WTI)",
}


def _fmt_cell(value) -> str:
    """Render one `[sign, magnitude]` cell.

    Accepts either the raw JSON pair or the Transmission object the loader
    produces, so the table is correct whichever source it is fed from.
    """
    if value is None:
        return "_absent → NEUTRAL_"
    if isinstance(value, (list, tuple)):
        sign, magnitude = value
    else:
        sign, magnitude = value.sign, value.magnitude
    arrow = {1: "▲", -1: "▼", 0: "○"}[int(sign)]
    word = {1: "bullish", -1: "bearish", 0: "no side"}[int(sign)]
    return f"{arrow} {word} `{float(magnitude):g}`"


def main() -> int:
    from app.analysis.transmission_map import EVENT_SIGNALS
    from app.config import DRIVER_RANKINGS
    from app.models.enums import Asset

    # The RAW json is the literal source of truth for sign/magnitude;
    # load_transmission_map() converts it to Transmission objects, which are
    # equivalent but no longer show an absent key as absent.
    raw = json.loads(
        (REPO / "app" / "config" / "transmission_map.json")
        .read_text(encoding="utf-8"))
    rankings = DRIVER_RANKINGS

    lines: list[str] = []
    w = lines.append

    w("# Transmission map reference")
    w("")
    w(f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
      f"by `tools/generate_transmission_docs.py`. Do not edit by hand._")
    w("")
    w("**Note:** The JSON map contains **75 event types**. This table shows the "
      "**73** that participate in detection via `EVENT_SIGNALS`. The two "
      "excluded are `GENERAL_MACRO` (used only as a category fallback for "
      "unclassified headlines) and `GOLD_ETF_FLOW` (removed in Step 4c in "
      "favor of its `_IN`/`_OUT` variants, which carry their own sign).")
    w("")
    w("## How to read this")
    w("")
    w("Each event type has a **detection rule** (keywords that fire it) and a "
      "per-asset mapping of `[sign, magnitude]`:")
    w("")
    w("- **sign** — `1` bullish, `-1` bearish, `0` no side. A `0` contributes "
      "to the `directionless` total, which lowers certainty in "
      "`_confidence_from` without adding to either bull or bear.")
    w("- **magnitude** — per-asset sensitivity in `0.0–1.5`. It scales the "
      "cue magnitude inside `resolve()`, and it is also the "
      "`asset_sensitivity` used by `_signal_weight`.")
    w("")
    w(f"`resolve()` combines every matching event as "
      f"`sign × magnitude × cue_scale × specificity × driver_rank`, where "
      f"`specificity = 1 / (1 + 0.5 × index)` — so an event listed **earlier** "
      f"in `EVENT_SIGNALS` counts for more. One headline can fire several "
      f"events; they are summed.")
    w("")
    w("An **absent** asset key is NOT the same as `[0, x]`: absent coerces to "
      "`NEUTRAL (0, 0.0)` and contributes nothing at all, where `[0, x]` "
      "still registers as real evidence and dents confidence. Step 4e filled "
      "the five absent `EQUITY` keys for exactly this reason.")
    w("")
    w("## Event types")
    w("")
    w("| # | Event type | Detection rule | " + " | ".join(ASSETS) + " |")
    w("|---|---|---|---|---|---|")

    for idx, (etype, any_of, qualifiers) in enumerate(EVENT_SIGNALS, start=1):
        name = etype.value
        per = raw.get(name, {})
        cells = " | ".join(_fmt_cell(per.get(a)) for a in ASSETS)

        rule_parts = []
        shown = list(any_of)[:6]
        if shown:
            rule_parts.append("any of: " + ", ".join(f"`{k}`" for k in shown))
            if len(any_of) > 6:
                rule_parts.append(f"…+{len(any_of) - 6} more")
        else:
            rule_parts.append("_category fallback_")
        if qualifiers:
            q = list(qualifiers)[:6]
            rule_parts.append("**and** one of: "
                              + ", ".join(f"`{k}`" for k in q)
                              + (f"…+{len(qualifiers) - 6} more"
                                 if len(qualifiers) > 6 else ""))
        rule = "<br>".join(rule_parts)

        w(f"| {idx} | `{name}` | {rule} | {cells} |")

    w("")
    w("## Driver ranks")
    w("")
    w("`DRIVER_RANKINGS` in `app/config.py`, used as the final multiplier in "
      "`resolve()`. A rank of `None` means the event is not ranked for that "
      "asset and `0.3` is used instead.")
    w("")
    w("| Event type | " + " | ".join(ASSETS) + " |")
    w("|---|---|---|---|---|")
    for etype, _k, _q in EVENT_SIGNALS:
        row = rankings.get(etype.value, {})
        cells = []
        for a in ASSETS:
            key = Asset(a)
            v = row.get(key)
            cells.append("—" if v is None else f"{v:g}")
        w(f"| `{etype.value}` | " + " | ".join(cells) + " |")

    w("")
    w("## Deliberate design decisions")
    w("")
    w("- **`GEOPOLITICAL_RISK` is bullish for gold (+1.20), bullish for crude "
      "oil (+1.00), bearish for bitcoin (-0.35) and bearish for equity "
      "(-0.30).** The per-asset split is intentional: a conflict is a "
      "safe-haven bid for the metals, a supply-risk-premium bid for crude, "
      "and a risk-off shock for the other two.")
    w("- **`CRYPTO_REGULATION` is signed bearish for BTCUSD** `[-1, 0.80]` "
      "(Step 4a). It used to be `[0, 1.00]` — directionless *and* the largest "
      "magnitude in the map, which is the worst combination: every "
      "crypto-regulation headline reached maximum weight with no side. It is "
      "also ordered **after** `REGULATION_CLAMP` and `REGULATION_CLARITY` so a "
      "signed rule wins whenever one applies.")
    w("- **`GOLD_ETF_FLOW` is never detected** (Step 4c). It duplicated "
      "`GOLD_ETF_FLOW_IN` / `GOLD_ETF_FLOW_OUT`, so one gold-ETF headline "
      "fired two events and was summed twice. Only the `_IN` / `_OUT` "
      "variants, which carry the sign themselves, remain.")
    w("- **`INDEX_FLOW` requires an equity-index qualifier** (Step 4c). The "
      "bare `etf inflow` phrases made every gold- and bitcoin-ETF headline "
      "also count as an equity index flow.")
    w("- **`RISK_ON` no longer fires on `stocks rally`** (Step 4d). That "
      "phrase belongs to `STOCK_RALLY`, which is the equity-specific rule; "
      "having it in both double-counted every equity rally.")
    w("- **`RATE_REPRICING` carries only repricing language** (Step 4d). It "
      "shared nine phrases with `HAWKISH_POLICY`, so every hawkish headline "
      "double-counted.")
    w("")
    w("## Known model choices (not defects)")
    w("")
    w("These look wrong against conventional market expectations but are "
      "deliberate and documented:")
    w("")
    w("- `STRONG_JOBS` is bullish for equity (+0.85) and bearish for the "
      "metals (-0.90) — a strong print tightens the rate path.")
    w("- `RECESSION_CUT` is bearish for bitcoin and no-side for silver: a "
      "recession-driven cut does not transmit to silver through that channel.")
    w("- `SECTOR_ROTATION` and `CORP_EARNINGS_SEASON` are directionless by "
      "construction — they describe money moving, not a direction.")
    w("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)} "
          f"({len(lines)} lines, {len(EVENT_SIGNALS)} event types)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
