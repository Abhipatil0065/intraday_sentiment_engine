"""Ad-hoc end-to-end verification of the VAL-4 / VAL-5 / VAL-7 fixes.

Prints the SIGNED per-asset contribution for each headline and its mirror so the
opposition is visible at a glance. Not part of the test suite.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.golden_runner import classify

PAIRS = (
    ("The dollar index rose broadly",
     "The dollar index fell broadly"),
    ("risk appetite improved sharply",
     "risk appetite faded completely"),
    ("Treasury yields rose to a four-week high",
     "Treasury yields fell to a four-month low"),
    ("Bitcoin rose as risk appetite improved",
     "Bitcoin fell as risk appetite faded"),
    ("Fed announces a rate cut",
     "Fed announces a rate hike"),
    ("CPI accelerates sharply",
     "CPI cools sharply"),
    ("Nonfarm payrolls beat forecasts",
     "Nonfarm payrolls miss forecasts"),
)

SOLO = (
    "Risk aversion lifts gold",
    "Safe haven demand lifts gold",
    "Gold rose sharply on haven demand",
    "Gold fell sharply on haven demand",
)


def row(text):
    outcomes = classify(text)["outcomes"]
    return (outcomes["XAUUSD"].contribution,
            outcomes["XAGUSD"].contribution,
            outcomes["BTCUSD"].contribution,
            str(outcomes["XAUUSD"].transmission))


def main():
    header = ("HEADLINE", "GOLD", "SILVER", "BTC", "GOLD-TRANSMISSION")
    print("%-46s%9s%9s%9s   %s" % header)
    print("-" * 100)
    for up_text, down_text in PAIRS:
        for text in (up_text, down_text):
            gold, silver, btc, mech = row(text)
            print("%-46s%+9.3f%+9.3f%+9.3f   %s"
                  % (text[:44], gold, silver, btc, mech))
        gold_up, _, _, _ = row(up_text)
        gold_dn, _, _, _ = row(down_text)
        verdict = "OPPOSITE" if gold_up * gold_dn < 0 else "!! SAME SIGN !!"
        print("  -> gold %s\n" % verdict)
    print("Single-headline checks (VAL-7 and direct asset moves):")
    for text in SOLO:
        gold, silver, btc, mech = row(text)
        print("%-46s%+9.3f%+9.3f%+9.3f   %s"
              % (text[:44], gold, silver, btc, mech))


if __name__ == "__main__":
    main()