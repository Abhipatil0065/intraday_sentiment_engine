"""
CHART SCOPE - "news from the X chart only moves the X card".

WHY THIS EXISTS
===============
TradingView serves a per-symbol news feed: ``filter=symbol:OANDA:XAUUSD``
returns the stories listed on the GOLD chart, ``symbol:OANDA:XAGUSD`` the
stories on the SILVER chart, and so on. That is a much sharper statement than
"this headline mentions gold": the chart itself decides which stories belong
to it. This module turns that per-chart feed into per-card isolation.

THE ROUTING KEY: THE CHART THAT LISTED THE STORY
------------------------------------------------
The key is the ORIGIN - which chart query returned the story - and NOT the
story's own ``relatedSymbols`` tags.

The tags look like the better key and are not, because TradingView tags
cross-instrument stories freely. Measured against the live API on 2026-10-22
for the gold query (30 items):

    OANDA:XAUUSD 30 | BIST:XAGUSD1! 16 | TVC:GOLD 12 | NYMEX:PL1! 12
    OANDA:XAGUSD  4 | COMEX:SI1! 4    | MCX:SILVER1! 4

so 16 of the 30 stories on the GOLD chart also carry a silver-futures tag.
Routing by tag would push half the gold feed onto the silver card. Routing by
origin keeps the gold feed on the gold card, which is what the operator asked
for: "gold chart news only goes to the gold card".

The same measurement for the SILVER query (30 items) shows the reverse - 22 of
them carry ``OANDA:XAUUSD`` - because the silver chart lists a lot of gold
macro stories. Under origin routing those stories are silver-chart news (they
are on the silver chart) and, when the same story also appears in the gold
query, they are on BOTH charts.

MULTI-OWNER (TRADINGVIEW'S OWN SEMANTICS)
-----------------------------------------
A story that two chart queries both return IS listed on two charts, so it
feeds both cards. "Gold Futures Inch Higher" carries OANDA:XAUUSD and
OANDA:XAGUSD and is on both charts, so it feeds both the gold and the silver
card. That is the default here.

``settings.chart_scope_single_owner`` turns this into a strict one-card rule:
the first match in GOLD -> SILVER -> BITCOIN -> EQUITY order wins and the
other cards get nothing. Off by default - the multi-owner rule is TradingView's
own semantics.

WHAT IS NOT IN SCOPE
--------------------
Only the TradingView feed carries a chart. The other ~19 sources (Reuters
RSS, CNBC, Kitco, FinancialJuice, FXStreet ...) have no chart, so their rows
keep the classifier's routing exactly as before and may reach several cards.
A chart-scoped row NEVER reaches a card outside its scope.

Measured reference for the symbol table below (live, 2026-10-22):
items returned per query / how many carry that exact symbol tag -
OANDA:XAUUSD 30/30, OANDA:XAGUSD 7/30, TVC:SILVER 0/30, BINANCE:BTCUSD 0/30,
BITSTAMP:BTCUSD 29/30, BINANCE:BTCUSDT 3/30, NASDAQ:MSFT 30/30.
"""

from __future__ import annotations

from app.config import settings
from app.models.enums import Asset

# ---------------------------------------------------------------------------
# Which chart a query symbol belongs to.
#
# Only the symbols this project actually QUERIES are listed as chart keys.
# The aliases (TVC:GOLD, MCX:SILVER1!, CRYPTO:BTCUSD, ...) are the same
# instrument on a different venue, so they map to the same card; they matter
# for the audit field and for any future tag-based mode, not for the origin
# routing, which only ever sees a queried symbol.
# ---------------------------------------------------------------------------
CHART_OF_SYMBOL: dict[str, Asset] = {
    # --- gold ---
    "OANDA:XAUUSD": Asset.GOLD,
    "TVC:GOLD": Asset.GOLD,
    "NYMEX:GC1!": Asset.GOLD,
    "COMEX:GC1!": Asset.GOLD,
    "BIST:XAUUSD1!": Asset.GOLD,
    # --- silver ---
    "OANDA:XAGUSD": Asset.SILVER,
    "TVC:SILVER": Asset.SILVER,
    "COMEX:SI1!": Asset.SILVER,
    "MCX:SILVER1!": Asset.SILVER,
    "BIST:XAGUSD1!": Asset.SILVER,
    # --- bitcoin ---
    # BITSTAMP:BTCUSD is the symbol TradingView actually tags BTC stories
    # with (29/30 items). BINANCE:BTCUSD matched NOTHING (0/30) - the
    # project's old map queried it, so its bitcoin feed was never
    # bitcoin-tagged at all.
    "BITSTAMP:BTCUSD": Asset.BITCOIN,
    "BINANCE:BTCUSDT": Asset.BITCOIN,
    "BINANCE:BTCUSD": Asset.BITCOIN,
    "CRYPTO:BTCUSD": Asset.BITCOIN,
    "COINBASE:BTCEUR": Asset.BITCOIN,
    "COINBASE:BTCGBP": Asset.BITCOIN,
    # --- crude oil ---
    # TVC:USOIL is the symbol the project QUERIES for the oil chart (see
    # app/ingestion/tradingview_api.py DEFAULT_SYMBOL_MAP). The others are
    # the aliases TradingView tags oil stories with, measured live on
    # 2026-10-22: NYMEX:CL1! (WTI futures), MCX:CRUDEOIL1! and TVC:UKOIL /
    # ICEUS:CC1! (Brent).
    "TVC:USOIL": Asset.OIL,
    "NYMEX:CL1!": Asset.OIL,
    "MCX:CRUDEOIL1!": Asset.OIL,
    "TVC:UKOIL": Asset.OIL,
    "ICEUS:CC1!": Asset.OIL,
}

# The order a strict single-owner assignment walks. Gold first because it is
# the deepest market in this engine, then silver (its closest pair), then
# bitcoin, then the equity proxy. Oil comes last: it shares the most stories
# with the metals, so it must not take one away from them.
SINGLE_OWNER_ORDER: tuple[Asset, ...] = (
    Asset.GOLD,
    Asset.SILVER,
    Asset.BITCOIN,
    Asset.EQUITY,
    Asset.OIL,
)


def chart_of_symbol(symbol: str | None) -> Asset | None:
    """The card a chart symbol belongs to, or None when it is unknown."""
    if not symbol:
        return None
    return CHART_OF_SYMBOL.get(str(symbol).strip().upper())


def chart_assets_for_symbols(
    symbols: list[str] | None,
    *,
    equity: bool = False,
) -> list[Asset]:
    """
    The cards whose charts listed a story.

    ``symbols`` is the list of chart queries that returned the story, so a
    story found by both the gold and the silver query belongs to BOTH cards
    (TradingView's own semantics). ``equity=True`` is the equity feed, whose
    per-session tickers all belong to the one equity card.

    Honours ``settings.chart_scope_single_owner``: when that is on, only the
    first match in ``SINGLE_OWNER_ORDER`` survives.
    """
    if not symbols:
        return []

    found: list[Asset] = []
    if equity:
        found.append(Asset.EQUITY)
    else:
        for symbol in symbols:
            asset = chart_of_symbol(symbol)
            if asset is not None and asset not in found:
                found.append(asset)

    if not found:
        return []

    if settings.chart_scope_single_owner:
        for asset in SINGLE_OWNER_ORDER:
            if asset in found:
                return [asset]
    return found


def scope_label(assets: list[Asset] | None) -> str:
    """The scope as a compact, sortable string: "XAUUSD" / "XAUUSD,XAGUSD"."""
    if not assets:
        return ""
    return ",".join(a.value for a in assets)


def parse_scope(label: str | None) -> list[Asset]:
    """Read a stored scope label back into assets (unknown names ignored)."""
    if not label:
        return []
    out: list[Asset] = []
    for part in str(label).split(","):
        name = part.strip().upper()
        if not name:
            continue
        try:
            asset = Asset(name)
        except ValueError:
            continue
        if asset not in out:
            out.append(asset)
    return out


def scope_for_article(article) -> list[Asset]:
    """The scope of a stored article row (empty for a non-chart article)."""
    return parse_scope(getattr(article, "chart_scope", "") or "")
