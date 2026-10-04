"""Render the real split markup, mirroring index.html msplit() exactly.

msplit() no longer branches on the label, so the same markup is emitted for
every bias - that is the point of the change.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis.display import to_display
from app.database import init_db, session_scope
from app.main import _block

BOX = {"bull": "\033[42;30m %s \033[0m", "bear": "\033[41;37m %s \033[0m"}


def msplit(score, bias):
    """Mirror of index.html msplit() - no label branch, by design."""
    d = to_display(score, bias)
    bull = BOX["bull"] % "BULLISH %d%%" % round(d["bull_pct"])
    bear = BOX["bear"] % "BEARISH %d%%" % round(d["bear_pct"])
    return ("  |  +-------------+  +-------------+\n"
            "  |  | %s |  | %s |\n"
            "  |  +-------------+  +-------------+" % (bull, bear))


def main():
    print("LIVE SNAPSHOTS")
    init_db()
    with session_scope() as s:
        for asset in ("XAUUSD", "XAGUSD", "BTCUSD"):
            b = _block(s, asset, "MARKET_PREP")
            d = to_display(b["score"], b["bias"])
            print("%-7s bias=%-9s score=%7.2f -> chip \"%s %d%%\""
                  % (asset, b["bias"], b["score"], d["label"], d["pct"]))
            print(msplit(b["score"], b["bias"]))
            print()

    print("SYNTHETIC NO_BIAS (the case that used to grey out)")
    for score in (3, -3, 0, -10.5):
        d = to_display(score, "NO_BIAS")
        print("%-7s bias=NO_BIAS   score=%7.2f -> chip \"%s %d%%\""
              % ("BTCUSD", score, d["label"], d["pct"]))
        print(msplit(score, "NO_BIAS"))
        print()
    print("Green box is ALWAYS the bull figure, red ALWAYS the bear figure,")
    print("for every bias. Only the verdict chip greys on NEUTRAL.")


if __name__ == "__main__":
    main()