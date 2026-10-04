"""Render one MARKET PREP card per asset, mirroring prepCard() exactly."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis.display import to_display

CASES = (
    ("XAUUSD", "GOLD", {"score": 42.0, "bias": "BULLISH", "mode": "heuristic",
                        "contributing_articles": 14}),
    ("XAGUSD", "SILVER", {"score": -58.0, "bias": "BEARISH", "mode": "llm",
                          "contributing_articles": 9}),
    ("BTCUSD", "BITCOIN", {"score": 3.0, "bias": "NO_BIAS", "mode": "heuristic",
                           "contributing_articles": 2}),
)

# Mirrors the .bpct CSS: green box for the bull figure, red for the bear one.
BOX = {"bull": "\033[42;30m %s \033[0m", "bear": "\033[41;37m %s \033[0m",
       "neu": "\033[100;37m %s \033[0m"}


def render(label, symbol, b):
    d = to_display(b["score"], b["bias"])
    cls = {"BULLISH": "BULLISH", "BEARISH": "BEARISH"}.get(d["label"], "NO_BIAS")
    neu = d["label"] == "NEUTRAL"
    box = BOX["neu"] if neu else None
    bull = (box or BOX["bull"]) % "BULLISH %d%%" % d["bull_pct"]
    bear = (box or BOX["bear"]) % "BEARISH %d%%" % d["bear_pct"]
    return (
        "+-- %s  %s\n"
        "  |  MARKET PREP          [ %s %d%% ]\n"
        "  |  expected / forward-looking setup\n"
        "  |  [bar:%s]\n"
        "  |    bu width=%d%%   be width=%d%%\n"
        "  |  +-------------+  +-------------+\n"
        "  |  | %s |  | %s |\n"
        "  |  +-------------+  +-------------+\n"
        "  |  TOP DRIVERS ...      mode=%s  n=%d\n"
        % (label, symbol, d["label"], d["pct"], "-" * 39,
           d["bull_pct"], d["bear_pct"], bull, bear,
           b.get("mode", "heuristic"), b.get("contributing_articles", 0))
    )


def main():
    for _symbol, label, raw in CASES:
        print(render(label, _symbol, raw))
    print("Green box is ALWAYS the bullish figure and red ALWAYS the bearish one,")
    print("independent of which side won. NEUTRAL greys both. Figures come from")
    print("the same display block as the bar, so the two can never disagree.")


if __name__ == "__main__":
    main()