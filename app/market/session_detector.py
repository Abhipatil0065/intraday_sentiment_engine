"""Session awareness (section 14) + market regime detection (section 15)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.logging_conf import get_logger
from app.models.enums import SessionName
from app.utils import timeutils as T

log = get_logger(__name__)

# Session windows in the market timezone (New York by default).
# Forex/CFD convention: sessions overlap at the edges, so we use explicit
# start/stop hours with the higher-liquidity session winning the overlap.
SESSION_WINDOWS: list[tuple[SessionName, int, int]] = [
    (SessionName.ASIA, 20, 4),      # 20:00 previous day -> 04:00
    (SessionName.EUROPE, 3, 12),    # 03:00 -> 12:00
    (SessionName.US, 8, 17),        # 08:00 -> 17:00
]

SESSION_ORDER: list[SessionName] = [
    SessionName.ASIA,
    SessionName.EUROPE,
    SessionName.US,
]


def detect_session(now: datetime | None = None) -> SessionName:
    """
    Current trading session in the market timezone.

    Overnight gaps are handled by checking the same hour against the previous
    calendar day for the Asia window.
    """
    ref = (now or T.now_utc()).astimezone(T.market_tz())
    hour = ref.hour

    # Order matters: US is checked first because it overlaps Europe.
    for name, start, end in SESSION_WINDOWS:
        if name is SessionName.ASIA:
            # Asia wraps midnight.
            if hour >= start or hour < end:
                return SessionName.ASIA
            continue
        if start <= hour < end:
            return name

    return SessionName.OFF_HOURS


def _session_start(ref: datetime, session: SessionName) -> datetime:
    """Approximate start datetime of the given session on/around `ref`."""
    for name, start, end in SESSION_WINDOWS:
        if name is not session:
            continue
        base = ref.replace(hour=start, minute=0, second=0, microsecond=0)
        if session is SessionName.ASIA and ref.hour < end:
            base -= timedelta(days=1)
        return base
    return ref


def previous_session(now: datetime | None = None) -> SessionName:
    ref = (now or T.now_utc()).astimezone(T.market_tz())
    current = detect_session(ref)
    idx = SESSION_ORDER.index(current) if current in SESSION_ORDER else -1
    if idx <= 0:
        return SessionName.US  # after Asia comes the prior NY session
    return SESSION_ORDER[idx - 1]


def next_session(now: datetime | None = None) -> SessionName:
    ref = (now or T.now_utc()).astimezone(T.market_tz())
    current = detect_session(ref)
    idx = SESSION_ORDER.index(current) if current in SESSION_ORDER else len(SESSION_ORDER)
    return SESSION_ORDER[idx % len(SESSION_ORDER)]


def session_progress(now: datetime | None = None) -> float:
    """0.0-1.0 through the current session - used to fade stale prep signals."""
    ref = (now or T.now_utc()).astimezone(T.market_tz())
    session = detect_session(ref)
    for name, start, end in SESSION_WINDOWS:
        if name is not session:
            continue
        begin = _session_start(ref, name)
        span = (end + 24 - start) % 24 or 8
        elapsed = (ref - begin).total_seconds() / 3600.0
        return max(0.0, min(1.0, elapsed / span))
    return 0.0


# Session relevance multipliers - how much a news category matters right now.
CATEGORY_SESSION_WEIGHT: dict[SessionName, dict[str, float]] = {
    SessionName.ASIA: {
        "China": 1.35, "Industrial Demand": 1.25, "Commodity Supply/Demand": 1.20,
        "Equities": 1.10, "USD/DXY": 1.05,
        "Employment": 0.85, "GDP": 0.90, "Central Bank": 0.95,
        "Bitcoin ETF Flows": 0.95, "Crypto Market Flows": 0.95,
    },
    SessionName.EUROPE: {
        "GDP": 1.20, "Central Bank": 1.15, "Oil/Energy": 1.10, "Equities": 1.10,
        "Crypto Market Flows": 1.05,
    },
    SessionName.US: {
        "Central Bank": 1.40, "Interest Rates": 1.40, "Inflation": 1.40,
        "Employment": 1.35, "GDP": 1.30, "Treasury Yields": 1.35, "USD/DXY": 1.35,
        "Bitcoin ETF Flows": 1.30, "Crypto Regulation": 1.20, "Equities": 1.15,
        "Banking/Financial Stress": 1.20, "ETF Flows": 1.20,
        "China": 0.85, "Industrial Demand": 0.90,
    },
    SessionName.OFF_HOURS: {"Geopolitics": 1.15, "Risk Sentiment": 1.10},
}


# Session-aware global equity card (one card, title follows the session).
# Forex/CFD convention windows above are NY-tz; this map only names the card.
EQUITY_SESSION_MAP: dict[SessionName, dict[str, object]] = {
    SessionName.ASIA: {
        "title": "ASIA EQUITY",
        "symbol": "NIKKEI / HSI",
        "keywords": [
            "nikkei", "topix", "japan", "boj", "bank of japan",
            "hang seng", "hsi", "shanghai", "sse", "csi 300", "china",
            "kospi", "korea", "asian shares", "asia-pacific", "nifty",
            "sensex", "india", "asx", "australia", "rba",
        ],
    },
    SessionName.EUROPE: {
        "title": "EUROPE EQUITY",
        "symbol": "DAX / STOXX50",
        "keywords": [
            "dax", "stoxx", "ftse", "cac 40", "cac40", "euro stoxx",
            "european shares", "europe", "ecb", "european central bank",
            "bank of england", "boe", "bundesbank",
        ],
    },
    SessionName.US: {
        "title": "US EQUITY",
        "symbol": "SPX / NDX",
        "keywords": [
            "s&p", "s&p 500", "spx", "nasdaq", "ndx", "dow jones", "dow",
            "nyse", "wall street", "us stocks", "fed", "federal reserve",
            "fomc", "treasury", "russell 2000",
            "earnings", "apple", "microsoft", "amazon", "nvidia", "google",
            "meta", "tesla", "profit", "revenue", "eps", "guidance",
        ],
    },
}


def equity_session(now: datetime | None = None) -> SessionName:
    """Active equity region: current session, or previous one in OFF_HOURS."""
    current = detect_session(now)
    if current is SessionName.OFF_HOURS:
        return previous_session(now)
    return current


def equity_title(session: SessionName) -> str:
    """Card title for a session, e.g. SessionName.ASIA -> 'ASIA EQUITY'."""
    info = EQUITY_SESSION_MAP.get(equity_session()
                                  if session is SessionName.OFF_HOURS
                                  else session)
    if info is not None:
        title = info.get("title")
        if isinstance(title, str) and title:
            return title
    return "US EQUITY"


def equity_keywords(session: SessionName) -> tuple[str, ...]:
    """Lowercase keyword set used to filter EQUITIES news to one region."""
    info = EQUITY_SESSION_MAP.get(session)
    if not info:
        return ()
    words = info.get("keywords") or []
    return tuple(str(w).lower() for w in words if str(w).strip())


def session_relevance(session: SessionName, category: str) -> float:
    """Multiplier applied to asset relevance for the active session."""
    return CATEGORY_SESSION_WEIGHT.get(session, {}).get(category, 1.0)