"""
Streamlit dashboard (section 27).

Renders the SAME zero-dependency HTML dashboard the FastAPI serves at "/".
It reads the HTML from disk, rewrites the relative API / WebSocket URLs to
point at the running engine, and embeds the whole page inline so the
Streamlit tab looks identical to the standalone dashboard.

Run both:
    python run.py --api          (terminal 1)
    python run.py --streamlit    (terminal 2)
"""

from __future__ import annotations

import re
from pathlib import Path

import streamlit as st

API = "http://127.0.0.1:8000"

st.set_page_config(
    page_title="Intraday News Sentiment & Market Impact Engine",
    page_icon="📈",
    layout="wide",
)

# The HTML dashboard is built for the FastAPI origin, where /api/* and /ws are
# relative. Inside the Streamlit iframe those would resolve against 127.0.0.1:8501
# and come back empty, so the URLs are rewritten to absolute API endpoints before
# the page is embedded. The WebSocket keep-alive is pointed at the engine too.
_WEB_HTML = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
_WEB_HTML = _WEB_HTML.replace("fetch('/api/", f"fetch('{API}/api/")
_WEB_HTML = re.sub(
    r"location\.host\+'\/ws'",
    f"'{API.replace('http://', '')}/ws'",
    _WEB_HTML,
)

# The page is long (prep strip + regime + per-asset cards + news feed), so it is
# rendered as a tall embedded document with its own scrollbar rather than a
# fixed-height frame that clips the bottom of the feed.
st.components.v1.html(_WEB_HTML, height=1700, scrolling=True)