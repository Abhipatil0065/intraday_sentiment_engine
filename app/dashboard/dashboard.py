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
    fig = go.Figure()
    for value, name, colour in (
        (b["bull_pct"], "Bullish", "#2ea043"),
        (b["bear_pct"], "Bearish", "#f85149"),
    ):
        fig.add_trace(go.Bar(
            y=["Sentiment"], x=[value], name=name,
            orientation="h", marker_color=colour))
    fig.update_layout(
        barmode="stack", height=90, showlegend=False,
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(range=[0, 100], visible=False),
        yaxis=dict(visible=False),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def render_block(b: dict) -> None:
    c1, c2 = st.columns([3, 2])
    with c1:
        st.plotly_chart(bar_figure(b), use_container_width=True,
                        config={"displayModeBar": False})
    with c2:
        st.markdown(
            f"**Bullish {b['bull_pct']:.1f}%** · "
            f"**Bearish {b['bear_pct']:.1f}%**"
        )
        st.markdown(
            f"**Sentiment:** `{b['label']}` &nbsp;·&nbsp; "
            f"Confidence {b['confidence'] * 100:.0f}% &nbsp;·&nbsp; "
            f"n={b['contributing_articles']}"
        )
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

# ---------------- Alerts ----------------
if data["alerts"]:
    st.subheader("🚨 High-Impact Alerts")
    for a in data["alerts"]:
        st.error(f"**{a['asset']}** · {a['direction']} · "
                 f"est. delta {a['sentiment_delta']:+.1f} — {a['headline']}")

# ---------------- Section B ----------------
st.subheader("Section B — Today's News Impact Feed")
if not data["news_feed"]:
    st.info("No qualifying news for today yet.")

for item in data["news_feed"]:
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

    for d in (b.get("drivers") or [])[:3]:
        st.markdown(f"🟢 {d}")
    for d in (b.get("risks") or [])[:3]:
        st.markdown(f"🔴 {d}")
    for c in (b.get("conflicts") or [])[:1]:
        st.warning(f"Conflicting signals — {c['resolution']}")
    st.caption(b.get("price_note", "Price-action confirmation unavailable."))