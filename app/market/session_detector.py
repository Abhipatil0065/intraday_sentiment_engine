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


# ---------------------------------------------------------------------------
# Per-asset session profiles (Item 3).
# ---------------------------------------------------------------------------
# A session profile decides how much the ACTIVE SESSION's clock should move
# an asset's signal weight. It is a property of the INSTRUMENT, not the news:
# gold is thinly traded outside London/New York, so an Asia-session headline
# is discounted; bitcoin trades 24 hours a day, so its session clock is flat.
#
# Profiles:
#   "metal"   - the standard curve in CATEGORY_SESSION_WEIGHT, plus a weekend
#               discount (Friday evening -> Sunday close are low-liquidity for
#               physical metals). This is what XAUUSD / XAGUSD inherit.
#   "flat"    - multiplier 1.0 in every session and on every weekday. Used for
#               BTCUSD: a 24/7 market with deep overnight and weekend liquidity,
#               so discounting an Asia or weekend headline would be wrong.
#   "weekend_tolerant" - the metal curve for sessions, but weekends lift to
#               1.0 instead of being discounted. Kept as a documented option
#               rather than the default: BTC weekend volume is real but thinner
#               than weekday, so whether it deserves full weight is a tuning
#               call, not a fact.
ASSET_SESSION_PROFILES: dict[str, str] = {
    "XAUUSD": "metal",
    "XAGUSD": "metal",
    "BTCUSD": "flat",
    "EQUITY": "metal",
}

WEEKEND_MULTIPLIER: dict[str, float] = {
    "metal": 0.70,
    "flat": 1.00,
    "weekend_tolerant": 1.00,
}

# Override per asset via env, e.g. BTC_SESSION_PROFILE=weekend_tolerant.
# The override key may be the short symbol ("BTC", "XAG") or the full asset
# string ("BTCUSD", "XAGUSD"); both are accepted so the .env line reads
# naturally either way. Exact match wins; otherwise the longest key that the
# asset string starts with is used, so "BTC" matches "BTCUSD".
def _profile_for(asset: str) -> str:
    from app.config import settings

    override = settings.asset_session_profile_map
    if asset in override:
        return override[asset]
    best_key = None
    for key in override:
        if asset.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is not None:
        return override[best_key]
    return ASSET_SESSION_PROFILES.get(asset, "metal")


def _clamp_profile(profile: str) -> str:
    """Never let a bad config value leave the session detector."""
    return profile if profile in WEEKEND_MULTIPLIER else "metal"


def asset_session_multiplier(asset: str, session: SessionName,
                             category: str, now: datetime | None = None) -> float:
    """
    Session multiplier for one asset, honouring that asset's session profile.

    A flat-profile asset (BTCUSD) returns 1.0 regardless of session or weekday.
    A metal-profile asset returns the CATEGORY_SESSION_WEIGHT curve, with the
    weekend discount applied. This is the ONLY place a weekend discount exists:
    it is a property of the instrument, applied after the session curve, and
    never a property of the news category.
    """
    profile = _clamp_profile(_profile_for(asset))
    if profile == "flat":
        return 1.0
    base = session_relevance(session, category)
    if profile == "weekend_tolerant":
        return base
    # metal: apply the weekend discount on top of the session curve.
    if now is None:
        now = T.now_utc()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        base *= WEEKEND_MULTIPLIER["metal"]
    return base