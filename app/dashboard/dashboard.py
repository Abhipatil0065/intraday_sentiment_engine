"""
Streamlit dashboard (section 27).

Reads the FastAPI service over HTTP. Run both:
    python run.py --api          (terminal 1)
    python run.py --streamlit    (terminal 2)
"""

from __future__ import annotations

import plotly.graph_objects as go
import requests
import streamlit as st

from app.analysis import display

API = "http://127.0.0.1:8000"

st.set_page_config(
    page_title="Intraday News Sentiment & Market Impact Engine",
    page_icon="📈",
    layout="wide",
)


@st.cache_data(ttl=10)
def fetch(path: str = "/api/dashboard"):
    try:
        r = requests.get(f"{API}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        return {"_error": str(exc)}


def bar_figure(b: dict) -> go.Figure:
    """
    Bull/bear split bar.

    Widths come from the same `display` block the headline percentage uses, so
    the bar and the number are two views of one value and cannot disagree. The
    label names whichever side holds more than half the split, so an even split
    is the only case that renders as an even grey bar.
    """
    d = display.to_display(b.get("score", 0.0), b.get("bias", "NO_BIAS"))
    colour = {"BULLISH": "#2ea043", "BEARISH": "#f85149"}.get(d["label"], "#6e7681")
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=[d["bull_pct"], d["bear_pct"]], y=["Split"], orientation="h",
        marker_color=[colour, colour]))
    fig.update_layout(
        height=90, showlegend=False, barmode="stack",
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(range=[0, 100], visible=False),
        yaxis=dict(visible=False),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def render_block(b: dict, *, key: str = "") -> None:
    c1, c2 = st.columns([3, 2])
    with c1:
        st.plotly_chart(bar_figure(b), use_container_width=True,
                        key=key or None,
                        config={"displayModeBar": False})
    with c2:
        # Label names the side holding more than half the split, so the chip
        # cannot contradict the bar; NEUTRAL survives only on a 50/50.
        d = display.to_display(b.get("score", 0.0), b.get("bias", "NO_BIAS"))
        st.markdown(f"**Verdict:** `{d['label']} {d['pct']}%`")
        # Both sides as boxed figures under the bar - the old-dashboard read:
        # a green box for the bullish figure, a red box for the bearish one.
        #
        # Colour is keyed to the SIDE, never to the verdict, and is PERMANENT:
        # a green box for the bull figure, a red box for the bear one,
        # including on a NEUTRAL card. Greying them for NO_BIAS was tried and
        # reverted - a borderline asset flickered between coloured and grey as
        # its confidence crossed the floor, which read as a broken card. The
        # withheld call is still stated by the verdict chip above.
        bull_col, bull_border = "#2ea043", "rgba(46,160,67,.55)"
        bear_col, bear_border = "#f85149", "rgba(248,81,73,.55)"
        bull_bg, bear_bg = "rgba(46,160,67,.14)", "rgba(248,81,73,.14)"
        left, right = st.columns(2)
        with left:
            st.markdown(
                f"<div style='background:{bull_bg};"
                f"border:1px solid {bull_border};"
                f"border-radius:8px;padding:8px;text-align:center'>"
                f"<div style='font-size:10px;letter-spacing:1.4px;"
                f"color:#8b98a9'>BULLISH</div>"
                f"<div style='font-size:22px;font-weight:700;color:{bull_col}'>"
                f"{d['bull_pct']}%</div></div>",
                unsafe_allow_html=True)
        with right:
            st.markdown(
                f"<div style='background:{bear_bg};"
                f"border:1px solid {bear_border};"
                f"border-radius:8px;padding:8px;text-align:center'>"
                f"<div style='font-size:10px;letter-spacing:1.4px;"
                f"color:#8b98a9'>BEARISH</div>"
                f"<div style='font-size:22px;font-weight:700;color:{bear_col}'>"
                f"{d['bear_pct']}%</div></div>",
                unsafe_allow_html=True)
        st.markdown(
            f"**Bias:** `{b.get('bias', 'NO_BIAS')}` &nbsp;·&nbsp; "
            f"mode `{b.get('mode', 'heuristic')}`"
        )
        delta = float(b.get("delta_60m", 0.0))
        if delta:
            st.markdown(f"**Changed in last 60 min:** `{delta:+.1f}`")
        if b.get("event_risk"):
            st.warning(b.get("event_risk_note", "Event risk imminent."))
        for d_ in (b.get("drivers") or [])[:3]:
            st.markdown(f"- `{float(d_.get('contribution', 0.0)):+.3f}` "
                        f"{d_.get('title', '')}")
        if b.get("conflict"):
            st.markdown(f"⚠️ Conflict: {b['conflict'].get('title', '')}")


data = fetch()

if "_error" in data:
    st.error(
        f"Cannot reach the engine API at {API}.\n\n"
        f"Start it with `python run.py --api`.\n\n`{data['_error']}`"
    )
    st.stop()

st.title("Intraday News Sentiment & Market Impact Engine")
st.caption("NEWS-BASED SENTIMENT — not a price prediction, not financial advice.")

status = data["status"]
if status.get("stale"):
    st.error(
        f"STALE: no successful pipeline tick in the last "
        f"{status.get('stale_after_minutes', 15)} minutes. "
        "Showing carried-forward data.",
        icon="⚠️",
    )
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Date", data["date"])
m2.metric("Session", data["current_session"])
m3.metric("Prev / Next", f"{data['previous_session']} / {data['next_session']}")
m4.metric("Last Updated", status["last_updated"] or "n/a")
m5.metric("New News", status["new_news_since_last_update"])

st.caption(
    f"Mode: **{status['analysis_mode']}** · "
    f"Sources: {status['sources_ok']}/{status['sources_polled']} · "
    f"Articles today: {status['articles_today']} · "
    f"Poll every {status['poll_interval_seconds']}s · Regime: {data['regime']}"
)

# ---------------- Market Regime ----------------
st.subheader("Market Regime")
with st.container(border=True):
    regime = data.get("regime", "MIXED")
    conf = float(data.get("regime_confidence", 0.0)) * 100
    st.markdown(
        f"**Regime:** `{regime}` · confidence {conf:.0f}% · "
        f"market TZ `{data.get('market_timezone', 'N/A')}` · "
        f"display TZ `{data.get('display_timezone', 'N/A')}`"
    )
    drivers = data.get("regime_drivers") or []
    if drivers:
        for d in drivers:
            st.markdown(f"- {d}")
    else:
        st.caption("No regime drivers flagged.")

# ---------------- Asset Sentiment Blocks ----------------
st.subheader("Asset Sentiment")

equity = data.get("equity", {})

for asset in data["assets"]:
    asset_label = asset["label"]
    asset_symbol = asset["symbol"]

    with st.container(border=True):
        st.markdown(f"**{asset_label} ({asset_symbol})**")

        for scope_key, scope_label in [
            ("current", "Current"),
            ("market_prep", "Market Prep"),
            ("market_wrap", "Market Wrap"),
        ]:
            block = asset[scope_key]
            with st.expander(f"{scope_label}", expanded=(scope_key == "current")):
                render_block(block, key=f"{asset_symbol}_{scope_key}")

# ---------------- Equity Card ----------------
st.subheader("Global Equity Card")

equity_block = {
    "score": float(equity.get("score", 0.0)),
    "bias": equity.get("bias", "NO_BIAS"),
    "mode": equity.get("mode", "heuristic"),
    "drivers": equity.get("drivers", []),
    "confidence": float(equity.get("confidence", 0.0)),
    "delta_60m": 0.0,
    "event_risk": False,
    "event_risk_note": "",
    "conflict": None,
    "contributing_articles": equity.get("contributing_articles", 0),
    "stale": equity.get("stale", False),
}

with st.container(border=True):
    st.markdown(f"**{equity.get('title', 'Global Equity')}**")
    st.caption(f"Region: {equity.get('region', 'N/A')} | Symbol: {equity.get('symbol', 'N/A')}")
    render_block(equity_block, key="equity_global")
    if equity.get("note"):
        st.info(equity["note"])

# ---------------- FinancialJuice Session Analysis ----------------
fj = fetch("/api/financialjuice/sessions")
if fj and "boxes" in fj:
    st.subheader("FinancialJuice Session Analysis")
    st.caption(
        f"Source: {fj.get('source', 'FinancialJuice')} · "
        f"Indian Date: {fj.get('india_date_display', fj.get('india_date', 'n/a'))} · "
        f"TZ: {fj.get('timezone', 'Asia/Kolkata')} · "
        "Bullish + Bearish = 100%"
    )
    ctx = fj.get("context") or {}
    if ctx.get("news_items") or ctx.get("calendar_items"):
        st.caption(
            f"Blended with high-impact Section B news ({ctx.get('news_items', 0)}) "
            f"and economic-calendar signals ({ctx.get('calendar_items', 0)})."
        )
    for i, box in enumerate(fj["boxes"], 1):
        with st.container(border=True):
            st.markdown(
                f"**{i}. {box.get('title', '')}**"
            )
            badge = box.get("age_label", "")
            if box.get("found"):
                if box.get("is_today"):
                    badge = "TODAY'S REPORT"
                else:
                    badge = "YESTERDAY'S REPORT"
            else:
                badge = "NO REPORT"
            st.caption(
                f"{badge} · Published: {box.get('published_ist', 'Not yet')}"
                + (f" · Report date: {box.get('report_date_display', '')}"
                   if box.get("report_date_display") else "")
            )
            if box.get("report_title"):
                st.markdown(f"_{box['report_title']}_")
            for a in box.get("assets", []):
                bull = float(a.get("bull_pct", 50.0))
                bear = float(a.get("bear_pct", 50.0))
                col = st.columns([1, 3])
                with col[0]:
                    st.markdown(
                        f"<div style='font-size:22px;font-weight:700;"
                        f"color:{'#2ea043' if bull >= bear else '#f85149'}'>"
                        f"{bull if bull >= bear else bear:.1f}%</div>",
                        unsafe_allow_html=True,
                    )
                with col[1]:
                    st.markdown(
                        f"<div style='font-size:11px;letter-spacing:1.4px;"
                        f"color:#8b98a9'>{a.get('label', '').upper()}</div>"
                        f"<div style='font-size:10.5px;color:#8b98a9'>"
                        f"🟢 Bullish {bull:.1f}% · 🔴 Bearish {bear:.1f}%</div>",
                        unsafe_allow_html=True,
                    )
                    drivers = a.get("drivers") or []
                    if drivers:
                        for d in drivers[:2]:
                            st.markdown(f"- {d}")
            drivers = box.get("drivers") or []
            risks = box.get("risks") or []
            if drivers or risks:
                dcol, rcol = st.columns(2)
                with dcol:
                    st.markdown("**Top Drivers**")
                    for d in drivers[:3]:
                        st.markdown(f"- {d}")
                with rcol:
                    st.markdown("**Key Risks**")
                    for r in risks[:3]:
                        st.markdown(f"- {r}")
elif fj and "_error" in fj:
    pass  # FJ section is optional; don't break the page on it.

# ---------------- Alerts ----------------
if data["alerts"]:
    st.subheader("🚨 High-Impact Alerts")
    for a in data["alerts"]:
        st.error(f"**{a['asset']}** · {a['direction']} · "
                 f"est. delta {a['sentiment_delta']:+.1f} — {a['headline']}")

# ---------------- Section A — Economic Calendar ----------------
st.subheader("Section A — Economic Calendar")
cal_rows = []
for asset in data["assets"]:
    for r in (asset.get("economic_calendar") or []):
        cal_rows.append((asset["label"], asset["symbol"], r))
if cal_rows:
    for label, symbol, r in cal_rows:
        released = (r.get("mode") == "RELEASED")
        vals = []
        if r.get("actual") is not None:
            vals.append(f"actual **{r['actual']}**")
        if r.get("forecast") is not None:
            vals.append(f"forecast **{r['forecast']}**")
        if r.get("previous") is not None:
            vals.append(f"prev {r['previous']}")
        if r.get("surprise") is not None:
            s = r["surprise"]
            vals.append(f"surprise {'+' if s > 0 else ''}{s}")
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.markdown(
                    f"**{r.get('title', '')}** · `{label}` ({symbol})"
                )
                st.caption(
                    f"{r.get('country', '')} · "
                    f"{r.get('when_utc', '')}"
                    + ("" if released else " (expected)") + f" · impact `{r.get('impact', '')}`"
                )
                st.markdown(f"**Read:** {r.get('reason', '')}")
                st.markdown(f"**Transmission:** {r.get('transmission', '')}")
            with c2:
                st.markdown(
                    f"<div style='font-size:11px;letter-spacing:1.4px;"
                    f"color:#8b98a9'>DIRECTION</div>"
                    f"<div style='font-size:22px;font-weight:700;"
                    f"color:{'#2ea043' if r.get('direction') == 'BULLISH' else '#f85149'}'>"
                    f"{r.get('direction', '')}</div>",
                    unsafe_allow_html=True,
                )
                if vals:
                    st.markdown(" · ".join(vals))
else:
    st.info("No qualifying macro releases inside the forward-looking window.")

# ---------------- Section B ----------------
# The API ships one page of the day; streamlit pages through /api/news so the
# reader can reach every article without the main payload carrying all of them.
PAGE = st.number_input(
    "Page", min_value=1, max_value=500, value=1, step=1,
    help="Articles are ordered newest first.",
)
feed_total = int(data.get("news_feed_total", 0) or 0)
feed_page = fetch(f"/api/news?limit=60&offset={(int(PAGE) - 1) * 60}")
feed_items = feed_page.get("items", [])

st.subheader("Section B — Today's News Impact Feed")
if feed_total:
    shown = len(feed_items)
    st.caption(
        f"Showing {shown} of {feed_total} articles today "
        f"(page {int(PAGE)} of {max(1, -(-feed_total // 60))})."
    )
if not feed_items:
    st.info("No qualifying news for today yet.")

for item in feed_items:
    with st.expander(f"[{item['display_time']}] {item['title']}"):
        st.markdown(
            f"**Source:** {item['source']} · **Category:** {item['category']} · "
            f"**Importance:** {item['importance']} · "
            f"**Report:** {item['report_kind']} · "
            f"**Verification:** {item['verification']}"
        )
        if item.get("event_article_count", 1) > 1:
            st.caption(
                f"{item['event_article_count']} headlines merged into 1 event "
                "(duplicate suppression active)."
            )
        for i in item["impacts"]:
            st.markdown(f"**{i['asset']}** · `{i['direction']}` · {i['strength']}")
            st.markdown(f"- **Reason:** {i['reason']}")
            st.markdown(f"- **Transmission:** {i['transmission']}")
            if i.get("metal_character"):
                st.caption(f"Silver demand character: {i['metal_character']}")
            st.caption(
                f"Recency {i['recency_weight']} · Economic importance "
                f"{i['importance_weight']} · Asset relevance "
                f"{i['relevance']:.2f} · Session {i['session']} · "
                f"Price confirmation {i['price_confirmation']}"
            )
        st.caption(item["analysis_note"])

st.divider()
st.caption(data["disclaimer"])

if st.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()
