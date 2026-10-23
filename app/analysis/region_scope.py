"""
NEWS-PATH REGION SCOPE (mirror of the calendar whitelist).

The calendar path is already region-whitelisted: app/ingestion/economic_calendar.py
filters provider events through calendar_config.filter_events() against
app/config/calendar_bias.json, whose regions are exactly USA / EUR / CNY /
JPY / GBP - the only regions allowed to reach a card.

The NEWS path had no country dimension at all: a release arriving over the
wire ("Mexico ... Inflation came in at 0.41% ...") was routed by category
relevance only (INFLATION -> GOLD 0.82) and put on five cards at once.

This module closes that hole with two gates:

  G1 (region)  - a macro data print (CPI / NFP / PPI / GDP / retail sales / ...)
                 reaches a card only if its country maps to a whitelisted region.
  G2 (central bank) - a rate decision / policy stance / CB speech reaches a card
                 only if the bank is Fed / BoE / PBoC / BoJ / ECB.

Everything that is neither a macro print nor a central-bank event keeps its
current path (geopolitics, risk sentiment, oil, crypto flow, company news,
plain FX commentary). A headline naming NO country is allowed - only an
explicitly-named DISALLOWED country gets blocked.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from app.models.enums import ReportKind

CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "calendar_bias.json"


@lru_cache(maxsize=1)
def _news_scope() -> dict:
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw.get("news_scope") or {}


def _clear_cache():
    try:
        _news_scope.cache_clear()
    except Exception:
        pass


def enabled() -> bool:
    return bool(_news_scope().get("enabled", False))


def allowed_regions() -> tuple[str, ...]:
    return tuple(str(r).upper() for r in (_news_scope().get("allowed_regions") or ()))


def allowed_central_banks() -> tuple[str, ...]:
    return tuple(str(x).lower() for x in (_news_scope().get("allowed_central_banks") or ()))


def blocked_countries() -> tuple[str, ...]:
    return tuple(str(x).lower() for x in (_news_scope().get("blocked_countries") or ()))


# Canonical central-bank keys that are allowed to reach cards as CB events.
ALLOWED_CB_CANONICAL = {"fed", "boe", "pboc", "boj", "ecb"}


def normalize(text: str) -> str:
    """Lowercase and strip accents/diacritics so 'mexico' matches 'méxico'."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return stripped.lower()


_WORD_BOUNDARY = re.compile(r"(?<![a-z0-9]){needle}(?![a-z0-9])")


def _has_word(text: str, needle: str) -> bool:
    if not text or not needle:
        return False
    pat = _WORD_BOUNDARY.pattern.format(needle=re.escape(needle.lower()))
    return re.search(pat, normalize(text)) is not None


def _in(text: str, needle: str) -> bool:
    if not text or not needle:
        return False
    return needle.lower() in normalize(text)


def detect_region(text: str) -> str | None:
    """Return the allowed region code named in text, or None if none named."""
    scope = _news_scope()
    allowed = scope.get("allowed_countries") or {}
    hits: list[tuple[int, str]] = []
    for region, tokens in allowed.items():
        for tok in tokens or []:
            tok_s = str(tok).lower()
            if _has_word(text, tok_s):
                hits.append((len(tok_s), str(region).upper()))
    if not hits:
        return None
    hits.sort(key=lambda h: -h[0])
    return hits[0][1]


def detect_central_bank(text: str) -> str | None:
    """Return canonical name of a central bank named in text, or None."""
    scope = _news_scope()
    allowed = scope.get("allowed_central_banks") or []
    hits: list[tuple[int, str]] = []
    for tok in allowed:
        tok_s = str(tok).lower()
        if _has_word(text, tok_s):
            if "fed" in tok_s or "federal reserve" in tok_s or "fomc" in tok_s or "powell" in tok_s:
                hits.append((len(tok_s), "Fed"))
            elif "boe" in tok_s or "bank of england" in tok_s or "bailey" in tok_s:
                hits.append((len(tok_s), "BoE"))
            elif "pboc" in tok_s or ("people" in tok_s and "china" in tok_s):
                hits.append((len(tok_s), "PBoC"))
            elif "boj" in tok_s or "bank of japan" in tok_s or "ueda" in tok_s:
                hits.append((len(tok_s), "BoJ"))
            elif "ecb" in tok_s or "european central bank" in tok_s or "lagarde" in tok_s:
                hits.append((len(tok_s), "ECB"))
            else:
                hits.append((len(tok_s), tok_s))
    blocked_cbs = [
        "rba", "reserve bank of australia", "rbnz", "reserve bank of new zealand",
        "boc", "bank of canada", "snb", "swiss national bank",
        "banco de mexico", "banxico", "bank of mexico",
        "russia", "riksbank", "norges bank", "sarb", "bok", "bank of korea",
        "mnb", "magyar nemzeti bank", "nbp", "narodowy bank polski",
        "cnb", "czech national bank", "bcr", "national bank of romania",
    ]
    for tok in blocked_cbs:
        if _has_word(text, tok):
            if "australia" in tok or tok == "rba":
                hits.append((len(tok), "RBA"))
            elif "zealand" in tok or tok == "rbnz":
                hits.append((len(tok), "RBNZ"))
            elif "canada" in tok or tok == "boc":
                hits.append((len(tok), "BoC"))
            elif "swiss" in tok or tok == "snb":
                hits.append((len(tok), "SNB"))
            elif "mexico" in tok or "banxico" in tok:
                hits.append((len(tok), "Banxico"))
            elif "russia" in tok:
                hits.append((len(tok), "BoR"))
            elif "sweden" in tok or tok == "riksbank":
                hits.append((len(tok), "Riksbank"))
            elif "norway" in tok:
                hits.append((len(tok), "Norges"))
            elif "south africa" in tok or tok == "sarb":
                hits.append((len(tok), "SARB"))
            elif "korea" in tok or tok == "bok":
                hits.append((len(tok), "BoK"))
            elif "hungary" in tok or tok == "mnb":
                hits.append((len(tok), "MNB"))
            elif "poland" in tok or tok == "nbp":
                hits.append((len(tok), "NBP"))
            elif "czech" in tok or tok == "cnb":
                hits.append((len(tok), "CNB"))
            elif "romania" in tok or tok == "bcr":
                hits.append((len(tok), "NBR"))
            else:
                hits.append((len(tok), tok))
    if not hits:
        return None
    hits.sort(key=lambda h: -h[0])
    return hits[0][1]


RATE_VERBS = [
    "rate cut", "rate hike", "cuts rates", "hikes rates",
    "hold rates", "holds rates", "holds interest rate", "interest rate decision",
    "interest rate", "policy rate", "monetary policy", "rate decision",
]


def is_central_bank_event(text: str) -> bool:
    if detect_central_bank(text) is not None:
        return True
    lowered = normalize(text)
    for rv in RATE_VERBS:
        if rv in lowered:
            return True
    return False


def is_macro_print(report_kind) -> bool:
    """True when the classifier tagged this as an ACTUAL scheduled-data print."""
    try:
        value = getattr(report_kind, "value", report_kind)
        if str(value).upper() == "ACTUAL_DATA":
            return True
    except Exception:
        pass
    return False


def in_scope(text: str, report_kind, category=None) -> tuple[bool, str]:
    """
    Returns (ok, reason). G1: macro print from non-whitelisted region -> blocked.
    G2: central bank event from non-allowed bank -> blocked.
    Everything else (geopolitics, risk, oil, crypto, etc.) -> allowed.
    """
    if not enabled():
        return True, ""

    t = normalize(text)
    print_ = is_macro_print(report_kind)
    cb = is_central_bank_event(text)

    if not print_ and not cb:
        return True, ""

    if print_:
        region = detect_region(text)
        if region is not None:
            if region in allowed_regions():
                return True, ""
            return False, f"macro print names '{region}' which is not in the news_scope whitelist"
        # check blocked countries even if not in allowed list
        for bc in blocked_countries():
            if _in(t, bc):
                return False, f"macro print names a country '{bc}' which is not in the news_scope whitelist"
        # No country named - allow (US/global headlines)
        return True, ""

    # central bank event
    bank = detect_central_bank(text)
    if bank is not None:
        if bank.lower() in ALLOWED_CB_CANONICAL:
            return True, ""
        return False, f"central bank '{bank}' is not in the news_scope allowed list"
    # no specific bank named but it's a rate/CB event - check country
    for bc in blocked_countries():
        if _in(t, bc):
            return False, f"central bank event names a country '{bc}' which is not in the news_scope whitelist"
    return True, ""
