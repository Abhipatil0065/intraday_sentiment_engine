"""
Streamlit dashboard (section 27).

Native Streamlit rendering of the engine's /api/dashboard payload. Replaces the
pure HTML-embed approach so the dashboard can render per-asset sentiment cards,
a session heatmap, a convergence banner, event-risk badges, a price overlay and
a trade blotter widget using Streamlit's own components.

Run both:
    python run.py --api          (terminal 1)
    python run.py --streamlit    (terminal 2)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from app.config import settings

st.set_page_config(
    page_title="Intraday News Sentiment & Market Impact Engine",
    page_icon="📈",
    layout="wide",
)

API_BASE = f"http://{settings.api_host}:{settings.api_port}"

# ---------------------------------------------------------------------------
# Colour tokens (mirror the HTML dashboard so both UIs read identically)
# ---------------------------------------------------------------------------
BULL = "#2ea043"
BEAR = "#f85149"
NEU = "#6e7681"
GOLD = "#f0b429"
SILVER = "#c0c8d1"
BTC = "#f7931a"
PANEL = "#141922"
LINE = "#232b3a"


def _asset_colour(symbol: str) -> str:
    s = (symbol or "").upper()
    if s == "XAUUSD":
        return GOLD
    if s == "XAGUSD":
        return SILVER
    if s == "BTCUSD":
        return BTC
    return NEU


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def _fetch_dashboard() -> dict | None:
    import urllib.request
    import json

    try:
        with urllib.request.urlopen(f"{API_BASE}/api/dashboard", timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        st.session_state["_last_fetch_error"] = str(exc)
        return None


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def bar_figure(bull_pct: float, bear_pct: float, height: int = 26) -> go.Figure:
    """A 100% stacked bull/bear bar as a Plotly figure.

    Mirrors the HTML dashboard's split bar: green (bull) on the left, red
    (bear) on the right, widths sum to 100%.
    """
    bull_pct = max(0.0, min(100.0, float(bull_pct or 0)))
    bear_pct = max(0.0, min(100.0, float(bear_pct or 0)))
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=[bull_pct],
            y=[""],
            orientation="h",
            marker_color=BULL,
            showlegend=False,
            hoverinfo="skip",
            width=[0.6],
        )
    )
    fig.add_trace(
        go.Bar(
            x=[bear_pct],
            y=[""],
            orientation="h",
            marker_color=BEAR,
            showlegend=False,
            hoverinfo="skip",
            width=[0.6],
        )
    )
    fig.update_layout(
        barmode="stack",
        height=height,
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(range=[0, 100], showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def conviction_label(score: float, confidence: float | None,
                     contradiction: bool = False) -> str:
    """Map a verdict to a conviction tier (WEAK / MODERATE / STRONG)."""
    from app.analysis.display import to_conviction
    return to_conviction(score, confidence, contradiction_detected=contradiction)


def display_of(block: dict) -> dict:
    """Return the display block, computing it server-side shape if missing."""
    d = block.get("display") or {}
    if d:
        return d
    # Fallback: reproduce server-side display.py arithmetic from score/bias.
    score = float(block.get("score", 0) or 0)
    bias = block.get("bias") or "NO_BIAS"
    conf = block.get("confidence")
    from app.analysis.display import to_display
    return to_display(score, bias, conf,
                      tie_break=abs(float(block.get("raw_score") or 0)) < 12.0,
                      contradiction_detected=bool(block.get("contradiction_detected")),
                      burst_total=int(block.get("burst_bull", 0) or 0)
                      + int(block.get("burst_bear", 0) or 0))


def render_block(block: dict, label: str, accent: str,
                 show_price: bool = False) -> None:
    """Render one sentiment card block (prep / wrap / current scope).

    Renders: name + symbol, verdict chip with confidence badge, bull/bear bar,
    bull/bear percentage boxes, drivers, event-risk badge, conflict note, foot.
    """
    if not block:
        st.info(f"No data for {label}.")
        return

    disp = display_of(block)
    label_txt = disp.get("label", "NO_BIAS")
    pct = disp.get("pct", 50)
    bull_pct = disp.get("bull_pct", 50)
    bear_pct = disp.get("bear_pct", 50)
    confidence = float(block.get("confidence", 0) or 0)
    conf_label = block.get("confidence_label", "LOW")
    bias = block.get("bias", "NO_BIAS")
    stale = bool(block.get("stale"))
    event_risk = bool(block.get("event_risk"))
    event_risk_note = block.get("event_risk_note") or ""
    delta = block.get("delta_60m", 0.0)
    score = float(block.get("score", 0) or 0)
    drivers = block.get("drivers") or []
    bull_drivers = block.get("bull_drivers") or []
    bear_drivers = block.get("bear_drivers") or []
    conflict = block.get("conflict")
    note = block.get("note") or ""
    signal_count = int(block.get("signal_count", 0) or 0)
    contributing = int(block.get("contributing_articles", 0) or 0)
    mode = block.get("mode", "heuristic")
    contradiction = bool(block.get("contradiction_detected"))

    is_weak = conf_label == "LOW" or stale
    verdict_color = BULL if label_txt == "BULLISH" else BEAR if label_txt == "BEARISH" else NEU

    with st.container(border=True):
        # Header row: name, symbol, verdict chip, confidence, event-risk badge
        c1, c2, c3 = st.columns([3, 1, 2])
        with c1:
            st.markdown(f"**{label}**")
            st.caption(accent)
        with c2:
            st.markdown(f"**{label_txt} {pct}%**", unsafe_allow_html=False)
        with c3:
            conv = conviction_label(score, confidence, contradiction)
            conv_color = {"STRONG": BULL, "MODERATE": GOLD, "WEAK": NEU}.get(conv, NEU)
            st.markdown(
                f"<span style='color:{conv_color};font-weight:700'>{conv}</span>"
                f" &middot; <span style='color:#8b98a9'>{conf_label}</span>",
                unsafe_allow_html=True,
            )
            if event_risk:
                st.markdown(
                    f"<span style='color:{BEAR};font-weight:700'>&#9888; EVENT RISK</span>",
                    unsafe_allow_html=True,
                )
                if event_risk_note:
                    st.caption(event_risk_note)

        # Flip stamp / since
        since = block.get("bias_since_utc")
        if since:
            st.caption(f"Stance since: {since}")

        # Bull/bear bar
        st.plotly_chart(bar_figure(bull_pct, bear_pct), use_container_width=True)

        # Bull/bear percentage boxes
        b1, b2 = st.columns(2)
        with b1:
            st.metric("BULLISH", f"{bull_pct:.0f}%", delta=None)
        with b2:
            st.metric("BEARISH", f"{bear_pct:.0f}%", delta=None)

        # Drivers
        if bull_drivers or bear_drivers:
            d1, d2 = st.columns(2)
            with d1:
                if bull_drivers:
                    st.markdown("**Bullish drivers**")
                    for dr in bull_drivers[:3]:
                        st.caption(f"+{float(dr.get('contribution',0)):.3f} {dr.get('title','')}")
            with d2:
                if bear_drivers:
                    st.markdown("**Bearish drivers**")
                    for dr in bear_drivers[:3]:
                        st.caption(f"{float(dr.get('contribution',0)):+.3f} {dr.get('title','')}")

        # Conflict
        if conflict:
            st.markdown(
                f"<span style='color:#e0a33e;font-weight:700'>&#9888; "
                f"Conflicting: {conflict.get('title','')}</span>",
                unsafe_allow_html=True,
            )

        # Footer: delta, mode, signal count, risk
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            st.metric("60min Δ", f"{delta:+.1f}")
        with f2:
            st.caption(f"Mode: {mode}")
        with f3:
            st.caption(f"Signals: {signal_count}")
        with f4:
            st.caption(f"Articles: {contributing}")

        if note:
            st.caption(note)

        if is_weak:
            st.caption("Weak conviction &mdash; watch only.", unsafe_allow_html=True)


def render_equity_card(equity: dict | None) -> None:
    """Render the session-aware global equity card."""
    if not equity:
        st.info("No equity card data.")
        return
    title = equity.get("title", "EQUITY")
    symbol = equity.get("symbol", "")
    region = equity.get("region", "")
    disp = display_of(equity)
    label_txt = disp.get("label", "NO_BIAS")
    pct = disp.get("pct", 50)
    bull_pct = disp.get("bull_pct", 50)
    bear_pct = disp.get("bear_pct", 50)
    confidence = float(equity.get("confidence", 0) or 0)
    stale = bool(equity.get("stale"))
    no_data = bool(equity.get("no_data"))
    contradiction = bool(equity.get("contradiction_detected"))
    drivers = equity.get("drivers") or []
    contributing = int(equity.get("contributing_articles", 0) or 0)
    note = equity.get("note") or ""

    accent = _asset_colour("EQUITY")
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 1, 2])
        with c1:
            st.markdown(f"**{title}**")
            st.caption(f"Region: {region} &middot; {symbol}")
        with c2:
            st.markdown(f"**{label_txt} {pct}%**")
        with c3:
            conf_label = equity.get("confidence_label", "LOW")
            conv = conviction_label(float(equity.get("score", 0) or 0), confidence, contradiction)
            conv_color = {"STRONG": BULL, "MODERATE": GOLD, "WEAK": NEU}.get(conv, NEU)
            st.markdown(
                f"<span style='color:{conv_color};font-weight:700'>{conv}</span>"
                f" &middot; <span style='color:#8b98a9'>{conf_label}</span>",
                unsafe_allow_html=True,
            )
            if no_data:
                st.markdown(
                    "<span style='color:#8b98a9'>no regional news &mdash; carried forward</span>",
                    unsafe_allow_html=True,
                )
            if stale:
                st.markdown("<span style='color:#f85149'>stale</span>", unsafe_allow_html=True)

        st.plotly_chart(bar_figure(bull_pct, bear_pct), use_container_width=True)
        b1, b2 = st.columns(2)
        with b1:
            st.metric("BULLISH", f"{bull_pct:.0f}%")
        with b2:
            st.metric("BEARISH", f"{bear_pct:.0f}%")

        if drivers:
            st.markdown("**Top drivers**")
            for dr in drivers[:3]:
                st.caption(f"{float(dr.get('contribution',0)):+.3f} {dr.get('title','')}")

        if note:
            st.caption(note)


def convergence_banner(convergence: dict | None) -> None:
    """Render the cross-asset convergence/divergence banner."""
    if not convergence:
        return
    alignment = convergence.get("alignment", "NO_CONSENSUS")
    note = convergence.get("note", "")
    bull_count = convergence.get("bull_count", 0)
    bear_count = convergence.get("bear_count", 0)

    if alignment == "UNIFIED_BULLISH":
        color, bg = BULL, f"rgba(46,160,67,.10)"
        icon = "&#128077;"
    elif alignment == "UNIFIED_BEARISH":
        color, bg = BEAR, f"rgba(248,81,73,.10)"
        icon = "&#128073;"
    elif alignment == "DIVERGENT":
        color, bg = GOLD, f"rgba(240,180,41,.10)"
        icon = "&#9888;"
    else:
        color, bg = NEU, f"rgba(110,118,129,.10)"
        icon = "&#128169;"

    assets = convergence.get("assets", {})
    asset_summaries = []
    for sym in ("XAUUSD", "XAGUSD", "BTCUSD"):
        a = assets.get(sym, {})
        asset_summaries.append(f"{sym}: {a.get('bias','-')} {a.get('score',0):+.0f}")

    st.markdown(
        f"<div style='background:{bg};border:1px solid {color};border-radius:8px;"
        f"padding:8px 12px;margin-bottom:12px;color:#e6edf3'>"
        f"<b>{icon} Convergence: {alignment}</b> &middot; {note}<br>"
        f"{' &middot; '.join(asset_summaries)}"
        f"</div>",
        unsafe_allow_html=True,
    )


def session_heatmap(assets: list[dict]) -> None:
    """Color-coded grid: Asia / Europe / US x assets showing bias and score."""
    st.markdown("### Session Heatmap &mdash; who moved where today")
    sessions = ["asia", "europe", "us"]
    session_labels = {"asia": "ASIA", "europe": "EUROPE", "us": "US"}

    header_cols = st.columns([2] + [1] * len(sessions))
    header_cols[0].markdown("**Asset**")
    for i, s in enumerate(sessions):
        header_cols[i + 1].markdown(f"**{session_labels[s]}**")

    for a in assets:
        symbol = a.get("symbol", a.get("asset", ""))
        accent = _asset_colour(symbol)
        row = st.columns([2] + [1] * len(sessions))
        row[0].markdown(f"<span style='color:{accent};font-weight:700'>{symbol}</span>",
                        unsafe_allow_html=True)
        for i, s in enumerate(sessions):
            block = a.get(s) or {}
            disp = display_of(block)
            label_txt = disp.get("label", "NO_BIAS")
            pct = disp.get("pct", 50)
            score = float(block.get("score", 0) or 0)
            stale = bool(block.get("stale"))
            color = BULL if label_txt == "BULLISH" else BEAR if label_txt == "BEARISH" else NEU
            bg = f"rgba(0,0,0,0.15)" if stale else "rgba(0,0,0,0.05)"
            cell = (
                f"<div style='background:{bg};border:1px solid {LINE};border-radius:6px;"
                f"padding:6px 8px;color:#e6edf3'>"
                f"<span style='color:{color};font-weight:700'>{label_txt} {pct:.0f}%</span><br>"
                f"<span style='font-size:10px;color:#8b98a9'>{score:+.0f}</span>"
                f"{' &middot; stale' if stale else ''}"
                f"</div>"
            )
            row[i + 1].markdown(cell, unsafe_allow_html=True)


def price_overlay(assets: list[dict]) -> None:
    """Render a price overlay strip: current price / VWAP / opening range.

    Price-action confirmation is disabled by default (no keyless spot feed for
    XAUUSD/XAGUSD). When PRICE_DATA_ENABLED is true the blocks carry price
    confirmation fields; otherwise we render an unavailable notice.
    """
    if not settings.price_data_enabled:
        st.info("Price-action confirmation unavailable. Set PRICE_DATA_ENABLED=true "
                "to attach intraday price data (BTC via Coinbase; GOLD/SILVER "
                "have no keyless spot feed).")
        return

    st.markdown("### Price overlay")
    cols = st.columns(len(assets))
    for i, a in enumerate(assets):
        symbol = a.get("symbol", a.get("asset", ""))
        current = a.get("current") or {}
        price_note = current.get("price_note", "Price-action confirmation unavailable.")
        with cols[i]:
            st.metric(symbol, price_note, delta=None)
            st.caption("VWAP / ORH / ORL not available without price feed")


# ---------------------------------------------------------------------------
# Trade blotter
# ---------------------------------------------------------------------------
BLOTTER_PATH = Path("data/blotter.json")


def _load_blotter() -> list[dict]:
    if not BLOTTER_PATH.exists():
        return []
    try:
        import json
        return json.loads(BLOTTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_blotter(items: list[dict]) -> None:
    BLOTTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    import json
    BLOTTER_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def blotter_ui() -> None:
    """Trade blotter widget: open signals, P&L, stop/target status."""
    st.markdown("### Trade Blotter &mdash; open signals & P&L")

    positions = _load_blotter()
    open_positions = [p for p in positions if p.get("status") == "OPEN"]
    closed = [p for p in positions if p.get("status") == "CLOSED"]

    # Add new position form
    with st.expander("Add signal", expanded=not open_positions):
        with st.form("add_signal"):
            b1, b2, b3 = st.columns(3)
            with b1:
                asset = st.selectbox("Asset", ["XAUUSD", "XAGUSD", "BTCUSD"])
                side = st.selectbox("Side", ["LONG", "SHORT"])
            with b2:
                entry = st.number_input("Entry price", min_value=0.0, value=0.0, step=0.01)
                stop = st.number_input("Stop loss", min_value=0.0, value=0.0, step=0.01)
            with b3:
                target = st.number_input("Target", min_value=0.0, value=0.0, step=0.01)
                size = st.number_input("Size", min_value=0.0, value=0.0, step=0.01)
            note = st.text_input("Note (optional)")
            submitted = st.form_submit_button("Add")
            if submitted:
                if entry <= 0:
                    st.error("Entry price must be > 0")
                else:
                    pos = {
                        "id": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "asset": asset,
                        "side": side,
                        "entry": entry,
                        "stop": stop,
                        "target": target,
                        "size": size,
                        "note": note,
                        "status": "OPEN",
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "closed_at": None,
                        "pnl": None,
                    }
                    positions.append(pos)
                    _save_blotter(positions)
                    st.success("Signal added")
                    st.rerun()

    # Open positions table
    if open_positions:
        st.markdown("#### Open positions")
        rows = []
        for p in open_positions:
            entry = float(p.get("entry", 0) or 0)
            stop = float(p.get("stop", 0) or 0)
            target = float(p.get("target", 0) or 0)
            side = p.get("side", "")
            # Unrealized P&L placeholder: needs live price to compute real P&L.
            # Show risk/reward instead.
            risk = abs(entry - stop)
            reward = abs(target - entry)
            rr = (reward / risk) if risk else 0.0
            rows.append({
                "Asset": p.get("asset", ""),
                "Side": side,
                "Entry": f"{entry:,.2f}",
                "Stop": f"{stop:,.2f}",
                "Target": f"{target:,.2f}",
                "R:R": f"{rr:.2f}:1",
                "Note": p.get("note", ""),
                "Opened": p.get("opened_at", "")[:16],
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)

        # Close position
        st.markdown("#### Close position")
        with st.form("close_signal"):
            c1, c2, c3 = st.columns(3)
            with c1:
                close_id = st.selectbox(
                    "Position",
                    options=[str(p["id"]) for p in open_positions],
                    format_func=lambda x: next(
                        f"{p['asset']} {p['side']} @ {p['entry']}" for p in open_positions
                        if str(p["id"]) == x
                    ),
                )
            with c2:
                close_price = st.number_input("Close price", min_value=0.0, value=0.0, step=0.01)
            with c3:
                submitted = st.form_submit_button("Close position")
            if submitted:
                if close_price <= 0:
                    st.error("Close price must be > 0")
                else:
                    for p in positions:
                        if str(p["id"]) == close_id:
                            entry = float(p.get("entry", 0) or 0)
                            side = p.get("side", "")
                            sz = float(p.get("size", 1) or 1)
                            if side == "LONG":
                                pnl = (close_price - entry) * sz
                            else:
                                pnl = (entry - close_price) * sz
                            p["status"] = "CLOSED"
                            p["closed_at"] = datetime.now(timezone.utc).isoformat()
                            p["pnl"] = round(pnl, 2)
                            p["close_price"] = close_price
                            break
                    _save_blotter(positions)
                    st.success("Position closed")
                    st.rerun()

    # Closed history
    if closed:
        st.markdown("#### Recent closed")
        rows = []
        for p in closed[:10]:
            rows.append({
                "Asset": p.get("asset", ""),
                "Side": p.get("side", ""),
                "Entry": f"{float(p.get('entry',0) or 0):,.2f}",
                "Close": f"{float(p.get('close_price',0) or 0):,.2f}",
                "P&L": f"{float(p.get('pnl',0) or 0):+,.2f}",
                "Closed": p.get("closed_at", "")[:16],
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        total_pnl = sum(float(p.get("pnl", 0) or 0) for p in closed)
        st.metric("Realized P&L", f"{total_pnl:+,.2f}")

    if not positions:
        st.info("No positions recorded yet. Add a signal above.")


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def main() -> None:
    st.title("Intraday News Sentiment & Market Impact Engine")
    st.caption("News-based intraday sentiment for XAUUSD, XAGUSD, BTCUSD "
               "&mdash; not a price prediction, not financial advice.")

    data = _fetch_dashboard()
    if data is None:
        err = st.session_state.get("_last_fetch_error", "unknown error")
        st.error(f"Could not reach the API at {API_BASE}/api/dashboard ({err}). "
                 "Start the engine with `python run.py --api` first.")
        return

    status = data.get("status", {})
    st.divider()

    # Top health strip
    h1, h2, h3, h4, h5 = st.columns(5)
    with h1:
        st.metric("Pipeline", "RUNNING" if status.get("running") else "STOPPED")
    with h2:
        st.metric("Stale", "YES" if status.get("stale") else "NO")
    with h3:
        st.metric("Articles today", status.get("articles_today", 0))
    with h4:
        st.metric("Events today", status.get("events_today", 0))
    with h5:
        st.metric("Mode", "LLM" if status.get("llm_enabled") else "Heuristic")

    if status.get("stale_reason"):
        st.warning(f"Pipeline stale: {status['stale_reason']}")

    st.divider()

    # Convergence banner
    convergence_banner(data.get("convergence"))

    # Session heatmap
    session_heatmap(data.get("assets", []))

    st.divider()

    # Per-asset sentiment cards (top strip: current scope)
    st.markdown("### Current intraday sentiment")
    asset_cols = st.columns(len(data.get("assets", [])))
    for i, a in enumerate(data.get("assets", [])):
        symbol = a.get("symbol", a.get("asset", ""))
        accent = _asset_colour(symbol)
        with asset_cols[i]:
            render_block(a.get("current"), a.get("label", symbol), accent,
                         show_price=settings.price_data_enabled)

    st.divider()

    # Equity card
    st.markdown("### Session equity card")
    render_equity_card(data.get("equity"))

    st.divider()

    # Price overlay
    price_overlay(data.get("assets", []))

    st.divider()

    # Trade blotter
    blotter_ui()

    st.divider()
    st.caption(data.get("disclaimer", ""))


if __name__ == "__main__":
    main()
