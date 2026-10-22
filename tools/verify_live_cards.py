"""Read the LIVE dashboard block and show what each driver box will render."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_db, session_scope
from app.main import _block


def main():
    init_db()
    with session_scope() as s:
        for asset in ("XAUUSD", "XAGUSD", "BTCUSD", "USOIL"):
            b = _block(s, asset, "MARKET_PREP")
            bull, bear = b["bull_drivers"], b["bear_drivers"]
            print("=" * 74)
            print("%s   bias=%s  score=%.2f" % (asset, b["bias"], b["score"]))
            print("  BULLISH DRIVERS (%d)" % len(bull))
            for d in bull[:3]:
                print("     + %-46s %+0.3f" % (d["title"][:46], d["contribution"]))
            if not bull:
                print("     None flagged.")
            print("  BEARISH DRIVERS (%d)" % len(bear))
            for d in bear[:3]:
                print("     - %-46s %+0.3f" % (d["title"][:46], d["contribution"]))
            if not bear:
                print("     None flagged.")
    print("=" * 74)


if __name__ == "__main__":
    main()