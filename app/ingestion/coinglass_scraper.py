"""
COINGLASS PUBLIC CHART SCRAPER (no API key required).

WHAT THIS IS
------------
CoinGlass publishes liquidation-heatmap, CVD (Cumulative Volume Delta) and
footprint charts on public "legend" pages. There is no free API key, but the
data is free to VIEW, so the charts are rendered in a headless browser and the
numbers are read back out of the page.

    Gold (XAU):   https://legend.coinglass.com/chart/ed5d16e5697a494fa6e4ab0f0586eaca
    Silver (XAG): https://legend.coinglass.com/chart/ed5d16e5697a494fa6e4ab0f0586eaca
    Bitcoin (BTC):https://legend.coinglass.com/chart/ed5d16e5697a494fa6e4ab0f0586eaca

WHAT IT PRODUCES
----------------
For every asset, one ``CoinGlassSnapshot`` holding:

    liquidation_heatmap  - nearest large liquidation cluster BELOW and ABOVE
                           spot, in the documented output shape:
                               {"price_level": 4230.50,
                                "intensity": "high",
                                "side": "long"}
    cvd                  - the CVD time series plus the DIVERGENCE verdict
                           computed locally against the price series
                           (CVD_BULLISH / CVD_BEARISH / CONFIRMED / NONE).
    footprint            - aggressive buy/sell imbalance per price bucket.

Every field is optional: a chart that does not render is simply absent, and the
caller degrades to news + price confirmation instead of being handed a guess.

HARD RULES
----------
1.  RATE LIMIT. The site is hit at most once every
    ``coinglass_cache_ttl_seconds`` (default 300s = 5 minutes) per asset, and
    the cache is persisted to disk so a restart never costs a fresh fetch.
2.  NEVER BLOCK THE TRADING LOOP. Every public function returns ``None`` on
    failure. There is no exception path into the pipeline.
3.  ANTI-BOT. A Cloudflare challenge / block page is a FAILED poll: logged
    once, backed off, never re-tried rapidly and never solved. On repeated
    failure the poll interval is doubled (up to a ceiling) instead.
4.  NO CHROME-EVASION ARGS. Plain Chromium, same as the calendar provider.
    CoinGlass's public chart pages render without them.

INTEGRATION
-----------
``coinglass_signals_to_weighted()`` converts a snapshot into the same
``WeightedSignal`` objects the news feed produces, so a CVD_BULLISH read moves
the dashboard's bullish/bearish badge through exactly the same scoring path as
a headline. See pipeline._recompute_sentiment.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.ingestion.chromium_lock import chromium_slot
from app.logging_conf import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Anti-bot / backoff state
# ---------------------------------------------------------------------------
#: Markers that mean we were served a challenge or block page rather than the
#: chart app. Any of these is a FAILED poll - never a solve attempt.
CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
    "attention required",
    "access denied",
    "cf-challenge",
    "cloudflare",
    "rate limit",
    "too many requests",
)

#: Hard ceiling on the adaptive poll interval (doubled per failure).
MAX_BACKOFF_SECONDS = 3600
#: Minimum spacing between two REAL fetches of the same asset, regardless of
#: what the cache says. Enforced in-process so two callers racing in the same
#: tick cannot both open a browser.
MIN_FETCH_SPACING_SECONDS = 60

# ---------------------------------------------------------------------------
# Chart classification keywords
# ---------------------------------------------------------------------------
#: Series-name fragments, lowercased, used to decide which rendered chart is
#: which. Checked in order, so more specific fragments come first.
_HEATMAP_HINTS = ("liquidat", "heatmap", "heat map", "liq.")
_CVD_HINTS = ("cvd", "cumulative volume delta", "cumulative delta", "delta")
_FOOTPRINT_HINTS = ("footprint", "foot print", "cluster chart", "bid/ask")
_PRICE_HINTS = ("close", "price", "spot", "last", "mark", "index")

#: A liquidation cluster has to be at least this "hot" (0..1 normalised
#: colour value) to count as a LARGE cluster rather than a stray warm pixel.
MIN_CLUSTER_INTENSITY = 0.35

#: A liquidation cluster within this fraction of spot is a caution flag.
LIQ_CAUTION_PCT = 0.01

#: Pixel gap within which two hot canvas rows count as one cluster. The
#: extractor samples every 2 pixel rows, so 1.5 sample-pitches (3px) keeps
#: genuinely adjacent rows together while splitting a band that has a cold
#: row between two hot ones.
_ROW_MERGE_PIXELS = 3.0

#: Plausible price range per asset, used to confirm the page actually switched
#: symbols before its data is trusted.
#:
#: WHY THIS EXISTS. The public heatmap page opens on BTC and its symbol picker
#: is a UI interaction, not a URL parameter - so a Gold scrape can silently
#: return Bitcoin's chart. Without this guard a BTC heatmap would be stored,
#: scored and displayed as GOLD, which is worse than no data at all. The page's
#: own price axis settles the question: BTC trades in the tens of thousands,
#: XAU in the low thousands, XAG in the tens. If the requested asset's numbers
#: cannot be the ones on the axis, the poll fails instead of lying.
#:
#: These ranges are deliberately narrow to prevent cross-contamination. They
#: cover the plausible trading range for each asset in the current market
#: cycle and must be adjusted if the asset's price moves outside them.
_ASSET_PRICE_RANGES: dict[str, tuple[float, float]] = {
    "BTCUSD": (10_000.0, 200_000.0),
    "XAUUSD": (500.0, 10_000.0),
    "XAGUSD": (1.0, 100.0),
}


def _axis_range(axis: list[tuple[float, float]]) -> tuple[float, float] | None:
    """(min, max) price covered by a calibrated axis."""
    if len(axis) < 2:
        return None
    prices = [p for _, p in axis]
    return (min(prices), max(prices))


def _plausible_for_asset(asset: str,
                         price_range: tuple[float, float] | None) -> bool:
    """
    True when the axis range could belong to ``asset``.

    A generous but non-trivial band per asset: BTC from $1k to $10M, gold from
    $100 to $100k, silver from $1 to $10k. Any asset's axis must sit inside its
    own band, so a gold request against a BTC chart (~80k) is caught while a
    legitimate gold chart (~4.2k) passes.
    """
    if price_range is None:
        # No calibration means the poll already failed for want of data.
        return False
    low, high = _ASSET_PRICE_RANGES.get(asset_symbol(asset), (0.0, float("inf")))
    return low <= price_range[0] and price_range[1] <= high


def _looks_like_challenge(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass
class HeatmapCluster:
    """One liquidation cluster, in the documented output shape."""

    price_level: float
    intensity: str          # "low" | "medium" | "high"
    side: str               # "long" | "short" (which side gets liquidated)
    intensity_score: float = 0.0   # raw 0..1 normalised value

    def to_dict(self) -> dict:
        return {
            "price_level": self.price_level,
            "intensity": self.intensity,
            "side": self.side,
        }


@dataclass
class CVDSeries:
    """The CVD time series plus its locally computed divergence verdict."""

    points: list[tuple[float, float]] = field(default_factory=list)  # (ts, cvd)
    divergence: str = "NONE"        # CVD_BULLISH | CVD_BEARISH | CONFIRMED | NONE
    divergence_note: str = ""
    slope: float = 0.0              # normalised CVD change over the window


@dataclass
class FootprintData:
    """Aggressive buy vs sell imbalance per price bucket."""

    rows: list[dict] = field(default_factory=list)
    imbalance: str = "NONE"         # BUY_IMBALANCE | SELL_IMBALANCE | NONE
    imbalance_pct: float = 0.0


@dataclass
class CoinGlassSnapshot:
    """Everything one asset yielded, plus provenance."""

    asset: str                       # "XAUUSD" | "XAGUSD" | "BTCUSD"
    spot_price: float | None = None
    fetched_at: str = ""             # ISO-8601 UTC
    heatmap_clusters: list[HeatmapCluster] = field(default_factory=list)
    cluster_below: HeatmapCluster | None = None
    cluster_above: HeatmapCluster | None = None
    caution_below: bool = False      # cluster_below within 1% of spot
    caution_above: bool = False
    cvd: CVDSeries | None = None
    footprint: FootprintData | None = None
    source_url: str = ""
    degraded: bool = True            # True until at least one chart was read

    def caution_note(self) -> str:
        """Human-readable caution flag for the dashboard / event-risk note."""
        notes = []
        if self.caution_below and self.cluster_below is not None:
            notes.append(
                f"{self.asset} long-liquidity cluster at "
                f"{self.cluster_below.price_level:.2f} sits within "
                f"{LIQ_CAUTION_PCT * 100:.1f}% of spot ("
                f"{self.cluster_below.intensity} intensity).")
        if self.caution_above and self.cluster_above is not None:
            notes.append(
                f"{self.asset} short-liquidity cluster at "
                f"{self.cluster_above.price_level:.2f} sits within "
                f"{LIQ_CAUTION_PCT * 100:.1f}% of spot ("
                f"{self.cluster_above.intensity} intensity).")
        return " ".join(notes)

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "spot_price": self.spot_price,
            "fetched_at": self.fetched_at,
            "degraded": self.degraded,
            "cluster_below": (self.cluster_below.to_dict()
                              if self.cluster_below else None),
            "cluster_above": (self.cluster_above.to_dict()
                              if self.cluster_above else None),
            "caution_below": self.caution_below,
            "caution_above": self.caution_above,
            "caution_note": self.caution_note(),
            "cvd_divergence": (self.cvd.divergence if self.cvd else None),
            "cvd_note": (self.cvd.divergence_note if self.cvd else ""),
            "cvd_points": len(self.cvd.points) if self.cvd else 0,
            "footprint_imbalance": (self.footprint.imbalance
                                    if self.footprint else None),
            "footprint_imbalance_pct": (self.footprint.imbalance_pct
                                        if self.footprint else 0.0),
            "source_url": self.source_url,
        }


# ---------------------------------------------------------------------------
# Cache (memory + disk)
# ---------------------------------------------------------------------------
#: Version of the on-disk cache payload. Bumped whenever the serialised shape
#: changes: an entry written by an older build must be DISCARDED, not parsed by
#: a newer one, or a changed field silently produces a wrong reading (an empty
#: cluster list next to a populated price, for instance).
CACHE_VERSION = 2


class _Cache:
    """TTL cache for one asset, mirrored to disk.

    The disk copy is what keeps a process restart from costing another fetch:
    it is replayed on load as long as it is still inside its TTL.
    """

    def __init__(self, path: Path, ttl_seconds: int):
        self.path = path
        self.ttl = int(ttl_seconds)
        self._entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            # A payload from an older shape is dropped wholesale: parsing it
            # under new assumptions is how a stale entry turns into bad data.
            if raw.get("__version__") != CACHE_VERSION:
                log.info("coinglass cache format changed (was %s, now %s) - "
                         "discarding old entries",
                         raw.get("__version__"), CACHE_VERSION)
                return
            self._entries = raw.get("entries") or {}
        except Exception as exc:  # noqa: BLE001 - a corrupt cache is never fatal
            log.debug("coinglass cache unreadable (%s): %s", self.path, exc)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "__version__": CACHE_VERSION,
                "entries": self._entries,
            }), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            log.debug("coinglass cache not written: %s", exc)

    def get(self, asset: str) -> dict | None:
        entry = self._entries.get(asset)
        if not entry:
            return None
        if (time.time() - float(entry.get("stored_at", 0))) >= self.ttl:
            return None
        return entry.get("payload")

    def put(self, asset: str, payload: dict) -> None:
        self._entries[asset] = {
            "stored_at": time.time(),
            "payload": payload,
        }
        self._save()

    def age_seconds(self, asset: str) -> float | None:
        entry = self._entries.get(asset)
        if not entry:
            return None
        return round(time.time() - float(entry.get("stored_at", 0)), 1)


_cache: _Cache | None = None
_cache_path: Path | None = None
_last_fetch_attempt: dict[str, float] = {}
_failure_streak: dict[str, int] = {}
_failure_logged: set[str] = set()


def _get_cache() -> _Cache:
    global _cache, _cache_path
    if _cache is None:
        path = Path(settings.database_url.replace("sqlite:///", "").lstrip("./"))
        path = path.parent if path.suffix else path
        _cache_path = path / "coinglass_cache.json"
        _cache = _Cache(
            _cache_path,
            settings.coinglass_cache_ttl_seconds)
    return _cache


def _backoff_active(asset: str) -> bool:
    """True when repeated failures have parked this asset for a while."""
    streak = _failure_streak.get(asset, 0)
    if streak <= 0:
        return False
    wait = min(settings.coinglass_poll_seconds * (2 ** streak),
               MAX_BACKOFF_SECONDS)
    last = _last_fetch_attempt.get(asset, 0.0)
    return (time.time() - last) < wait


def _record_failure(asset: str, reason: str) -> None:
    _failure_streak[asset] = _failure_streak.get(asset, 0) + 1
    _last_fetch_attempt[asset] = time.time()
    # Log the transition once, not on every poll: a Cloudflare block that lasts
    # an hour should produce one warning, not twenty.
    if asset not in _failure_logged:
        _failure_logged.add(asset)
        log.warning("CoinGlass scrape failed for %s: %s "
                    "(backing off; trading unaffected)", asset, reason)


def _record_success(asset: str) -> None:
    _failure_streak[asset] = 0
    _last_fetch_attempt[asset] = time.time()
    _failure_logged.discard(asset)


def reset_state_for_tests() -> None:
    """Clear backoff/health so tests never leak into one another.

    The on-disk cache is removed as well as dropped from memory: the cache
    file lives beside the database, so a payload written by one test would
    otherwise still be there, ready to be replayed as a "fresh" hit by the
    next one.
    """
    global _cache, _cache_path
    if _cache_path is not None:
        try:
            _cache_path.unlink()
        except OSError:
            pass
    _cache = None
    _cache_path = None
    _last_fetch_attempt.clear()
    _failure_streak.clear()
    _failure_logged.clear()


# ---------------------------------------------------------------------------
# In-page extraction (JavaScript)
# ---------------------------------------------------------------------------
# CoinGlass's public chart pages render to a CANVAS, so the data is not in the
# DOM. Two strategies are used, in order:
#
#   1. Highcharts. Some pages still render through Highcharts, which keeps
#      every series object in JS memory with its data attached. Reading that is
#      exact, so it is always tried first.
#
#   2. Canvas instrumentation. A canvas chart passes everything it draws
#      through CanvasRenderingContext2D - including the PRICE AXIS LABELS,
#      which arrive as real strings in ``fillText("84000", x, y)``. Patching
#      the context methods (before the page's code runs, via add_init_script)
#      therefore yields the exact price axis, and reading the canvas pixels
#      back yields the exact heat cells. The page's own colour LEGEND is
#      sampled to calibrate colour -> intensity, so no palette is hard-coded.
#
# The SVG path is a last-resort fallback for pages that keep charts in the DOM.

#: Installed before any page script runs, so it wraps the chart's own calls.
#: ``getTransform()`` is essential: the chart library draws inside a transform
#: and passes LOCAL coordinates, so the current matrix is what turns them into
#: canvas pixels.
_INSTRUMENT_JS = r"""
(() => {
  const log = { texts: [] };
  window.__cgDrawLog = log;
  const proto = window.CanvasRenderingContext2D &&
                window.CanvasRenderingContext2D.prototype;
  if (!proto) return;

  const origFillText = proto.fillText;
  proto.fillText = function (text, x, y, ...rest) {
    try {
      let sx = x, sy = y;
      try {
        const m = this.getTransform();
        sx = m.a * x + m.c * y + m.e;
        sy = m.b * x + m.d * y + m.f;
      } catch (e) {}
      log.texts.push({
        s: String(text),
        x: Math.round(sx), y: Math.round(sy),
        fill: String(this.fillStyle),
        font: String(this.font || "")
      });
      if (log.texts.length > 4000) log.texts.shift();
    } catch (e) {}
    return origFillText.call(this, text, x, y, ...rest);
  };
})();
"""

#: Reads back the draw log AND the canvas pixels, performing the legend
#: calibration in-page so only a compact profile crosses the boundary.
#: ``rows`` carries, for every sampled pixel row of the plot area, the hottest
#: colour found and where it is - which is exactly what cluster detection needs.
_READ_CANVAS_JS = r"""
() => {
  const drawTexts = (window.__cgDrawLog && window.__cgDrawLog.texts) || [];
  const canvases = [...document.querySelectorAll('canvas')];

  // Return one analysis per canvas. A CoinGlass "Legend" chart holds SEVERAL
  // instruments as separate stacked panels, each with its own axis - which is
  // why the same chart id served both gold and silver. Reading every canvas
  // and letting the caller match by price magnitude handles that layout, and
  // still returns exactly one entry for a single-instrument page.
  const out = { texts: drawTexts, canvas_list: [], results: [] };
  if (!canvases.length) return out;

  canvases.forEach((cv, cvIndex) => {
    const ctx = cv.getContext('2d');
    if (!ctx) return;
    let img;
    try { img = ctx.getImageData(0, 0, cv.width, cv.height).data; }
    catch (e) { return; }

    const W = cv.width, H = cv.height;
    // Which draw-texts belong to THIS canvas. fillText carries the canvas the
    // library was drawing into, so texts are attributed per canvas when the
    // library tags them; otherwise all texts are treated as shared, which is
    // correct for a single-panel page.
    const owns = (t) => t.canvas_index === undefined || t.canvas_index === cvIndex;
    const texts = drawTexts.filter(owns);

    const hexAt = (x, y) => {
      const i = (y * W + x) * 4;
      if (img[i + 3] < 200) return null;
      return "#" + [img[i], img[i+1], img[i+2]]
        .map(v => v.toString(16).padStart(2, "0")).join("");
    };
    const isTextPixel = (i) => img[i] + img[i+1] + img[i+2] <= 30;

    out.canvas_list.push({
      index: cvIndex, w: W, h: H,
      x: Math.round(cv.getBoundingClientRect().x),
      y: Math.round(cv.getBoundingClientRect().y),
      owned_texts: texts.length,
      total_texts: drawTexts.length
    });

    // ---- plot area bounds: right edge is the numeric axis column ----
    let axisLabelX = W;
    const numericX = {};
    texts.forEach(t => {
      const v = t.s.replace(/,/g, "");
      if (/^\d+(\.\d+)?$/.test(v) && v.length >= 4) {
        numericX[t.x] = (numericX[t.x] || 0) + 1;
      }
    });
    const numericXs = Object.keys(numericX).map(Number);
    if (numericXs.length) axisLabelX = Math.max(...numericXs) - 4;

    // ---- this canvas's own dominant colours ----
    const plotHist = new Map();
    const railEnd = Math.min(140, Math.max(20, axisLabelX - 40));
    for (let y = 0; y < H; y += 2) {
      for (let x = railEnd; x < axisLabelX; x += 2) {
        const h = hexAt(x, y);
        if (!h) continue;
        plotHist.set(h, (plotHist.get(h) || 0) + 1);
      }
    }
    const plotTop = [...plotHist.entries()]
      .sort((a, b) => b[1] - a[1]).slice(0, 30).map(e => e[0]);
    if (!plotTop.length) return;
    const plotSet = new Set(plotTop);
    const bgColor = plotTop[0];

    // ---- match this canvas's legend to its own colours ----
    const columnColors = (x) => {
      const set = new Set();
      for (let y = 0; y < H; y++) {
        const i = (y * W + x) * 4;
        if (isTextPixel(i)) continue;
        const h = hexAt(x, y);
        if (h) set.add(h);
      }
      return set;
    };
    let legendX = -1, legendRight = 0, bestScore = 0;
    for (let x = 2; x < 200; x += 2) {
      const colors = columnColors(x);
      if (colors.size < 12) continue;
      let score = 0;
      colors.forEach(c => { if (plotSet.has(c)) score++; });
      if (score > bestScore) {
        bestScore = score;
        legendX = x;
        legendRight = x;
        while (legendRight < x + 24 && columnColors(legendRight + 2).size >= 12) {
          legendRight += 2;
        }
      }
    }

    const intensityOf = new Map();
    if (legendX >= 0 && bestScore >= 3) {
      // ---- the bar's TRUE vertical extent ----
      let barTop = -1, barBottom = -1, bestRun = 0, runStart = -1;
      for (let y = 0; y <= H; y++) {
        const i = (y < H) ? (y * W + legendX) * 4 : -1;
        const h = (i >= 0) ? hexAt(legendX, y) : null;
        const onBar = h && h !== bgColor && plotSet.has(h) && !isTextPixel(i);
        if (onBar && runStart < 0) runStart = y;
        if (!onBar && runStart >= 0) {
          if (y - runStart > bestRun) { bestRun = y - runStart;
                                        barTop = runStart; barBottom = y - 1; }
          runStart = -1;
        }
      }
      if (barTop < 0 || barBottom < 0 || barBottom - barTop < 8) {
        const ys = [];
        for (let y = 0; y < H; y++) {
          const i = (y * W + legendX) * 4;
          if (!isTextPixel(i) && hexAt(legendX, y) !== bgColor) ys.push(y);
        }
        if (!ys.length) return;
        barTop = ys[0]; barBottom = ys[ys.length - 1];
      }

      // ---- which end of the bar is HOT, read from its own labels ----
      const nearLegend = texts.filter(t => {
        const dx = Math.abs(t.x - legendX);
        return dx < 90 && t.y >= barTop - 60 && t.y <= barBottom + 60;
      });
      const parseAmount = (s) => {
        const m = String(s).replace(/,/g, "").match(/^([\d.]+)\s*([KMB])?$/i);
        if (!m) return null;
        let v = parseFloat(m[1]);
        const unit = (m[2] || "").toUpperCase();
        if (unit === "K") v *= 1e3;
        else if (unit === "M") v *= 1e6;
        else if (unit === "B") v *= 1e9;
        return v;
      };
      const mid = (barTop + barBottom) / 2;
      const topAmount = Math.max(...nearLegend
        .map(t => t.y < mid ? parseAmount(t.s) : null)
        .filter(v => v !== null), -Infinity);
      const bottomAmount = Math.max(...nearLegend
        .map(t => t.y >= mid ? parseAmount(t.s) : null)
        .filter(v => v !== null), -Infinity);
      const hotAtTop = !(bottomAmount > topAmount);

      const span = Math.max(1, barBottom - barTop);
      for (let y = 0; y < H; y++) {
        const i = (y * W + legendX) * 4;
        if (isTextPixel(i)) continue;
        const h = hexAt(legendX, y);
        if (!h || h === bgColor) continue;
        if (y < barTop - 1 || y > barBottom + 1) continue;
        const along = Math.min(1, Math.max(0, (y - barTop) / span));
        const value = hotAtTop ? (1 - along) : along;
        const prev = intensityOf.get(h);
        if (prev === undefined || value > prev) intensityOf.set(h, value);
      }
    }
    if (!intensityOf.size) {
      plotTop.forEach((h, i) => {
        intensityOf.set(h, i / Math.max(1, plotTop.length - 1));
      });
    }

    // ---- per-row heat profile ----
    const plotStart = Math.max(legendRight + 8, railEnd);
    if (plotStart >= axisLabelX) return;
    const rows = [];
    for (let y = 0; y < H; y += 2) {
      let best = null;
      for (let x = plotStart; x < axisLabelX; x += 2) {
        const h = hexAt(x, y);
        if (!h) continue;
        const value = intensityOf.get(h);
        if (value === undefined) continue;
        if (!best || value > best.intensity) {
          best = { intensity: value, x, hex: h };
        }
      }
      if (best) rows.push({ y, intensity: best.intensity,
                            x: best.x, hex: best.hex });
    }
    if (!rows.length) return;

    out.results.push({
      canvas_index: cvIndex,
      canvas: { w: W, h: H },
      texts: texts,
      rows: rows,
      legend: [...intensityOf.entries()].map(([hex, intensity]) =>
        ({ hex, intensity })),
      legend_x: legendX
    });
  });

  return out;
}
"""

_EXTRACT_JS = r"""
() => {
  const out = {
    title: document.title || "",
    heading: "",
    text: "",
    price_texts: [],
    charts: [],
    svgs: [],
  };

  try { out.heading = (document.querySelector("h1, h2") || {}).textContent || ""; } catch (e) {}
  // A cheap fingerprint of the whole page for challenge detection.
  try { out.text = (document.body ? document.body.innerText : "").slice(0, 4000); } catch (e) {}

  const classify = (name) => {
    const n = (name || "").toLowerCase();
    if (!n) return "other";
    if (/(liquidat|heatmap|heat map|liq\.?)/.test(n)) return "heatmap";
    if (/(cvd|cumulative volume delta|cumulative delta|delta)/.test(n)) return "cvd";
    if (/(footprint|foot print|cluster|bid\/ask|imbalance)/.test(n)) return "footprint";
    if (/(close|price|spot|last|mark|index|open|high|low|volume)/.test(n)) return "price";
    return "other";
  };

  const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);

  // Spot price candidates. CoinGlass repeats the current price in several
  // places (header, tooltip, axis); the MODE of those readings is a better
  // estimate than any single one, so all price-shaped strings are collected
  // and the decision is made in Python.
  try {
    const text = document.body ? document.body.innerText : "";
    const re = /\\b\\d{1,3}(?:,\\d{3})+(?:\\.\\d+)?\\b|\\b\\d+\\.\\d{2,}\\b/g;
    let m;
    while ((m = re.exec(text)) !== null) out.price_texts.push(m[0]);
  } catch (e) {}

  const pushSeries = (chart, s, idx) => {
    const name = s.name || (s.options && s.options.name) || ("series_" + idx);
    const data = (s.options && s.options.data) || s.data || [];
    const pts = [];
    for (let i = 0; i < data.length; i++) {
      const d = data[i];
      const o = (d && typeof d === "object" && !Array.isArray(d)) ? d : null;
      const x = o ? num(o.x) : (Array.isArray(d) ? num(d[0]) : num(d));
      const y = o ? num(o.y) : (Array.isArray(d) ? num(d[1]) : null);
      const z = o ? (num(o.value) !== null ? num(o.value) : num(o.z)) : null;
      const color = o ? (o.color || (o.dataLabels && o.dataLabels.color) || "") : "";
      if (x !== null) pts.push({ x, y, value: z, color });
    }
    const axisVals = [];
    try {
      (s.yAxis && s.yAxis.ticks ? Object.values(s.yAxis.ticks) : []).forEach(t => {
        if (t && t.label) axisVals.push(t.label.textStr || "");
      });
    } catch (e) {}
    out.charts.push({
      chart_index: out.charts.length,
      series_name: name,
      kind: classify(name),
      series_index: idx,
      type: (s.type || (s.options && s.options.type) || ""),
      points: pts,
      axis_labels: axisVals,
      x_min: s.xAxis && s.xAxis.min ? s.xAxis.min : null,
      x_max: s.xAxis && s.xAxis.max ? s.xAxis.max : null,
    });
  };

  // --- Highcharts: the primary, richest source ---
  try {
    const HC = window.Highcharts;
    const charts = (HC && (HC.charts || [])) || [];
    charts.forEach(ch => {
      if (!ch || !ch.series) return;
      ch.series.forEach((s, i) => { try { pushSeries(ch, s, i); } catch (e) {} });
    });
  } catch (e) {}

  // --- SVG fallback: axis text + element colours ---
  try {
    document.querySelectorAll("svg").forEach(svg => {
      const texts = [];
      svg.querySelectorAll("text").forEach(t => {
        if (t.textContent && t.textContent.trim()) texts.push(t.textContent.trim());
      });
      const fills = [];
      svg.querySelectorAll("path, rect, circle").forEach(el => {
        const f = el.getAttribute && (el.getAttribute("fill") || el.style && el.style.fill);
        if (f && f !== "none" && f !== "transparent") fills.push(f);
      });
      if (texts.length) out.svgs.push({ texts, fills });
    });
  } catch (e) {}

  return out;
}
"""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _intensity_bucket(value: float) -> str:
    """Map a 0..1 normalised colour/cluster value to a named intensity."""
    if value >= 0.75:
        return "high"
    if value >= 0.45:
        return "medium"
    return "low"


def _median_step(values: list[tuple[float, float]]) -> float:
    """Median gap between consecutive price rows - the local bin width."""
    if len(values) < 2:
        return 0.0
    gaps = sorted(b[0] - a[0] for a, b in zip(values, values[1:]))
    mid = len(gaps) // 2
    if len(gaps) % 2:
        return gaps[mid]
    return (gaps[mid - 1] + gaps[mid]) / 2.0


def _parse_axis_number(text: str) -> float | None:
    """
    Read a price out of an axis label.

    Highcharts labels arrive as "4,230.5", "4230.5", "4.2K", "4,230/oz".
    Strips thousands separators and currency/metal decorations before
    parsing; anything unparseable returns None.
    """
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"(?i)\b(usd|us\s*\$|\$|€|eur|/oz|oz|btc|xbt|xau|xag|usd\s*per\s*oz)\b",
                     "", cleaned)
    cleaned = cleaned.replace(",", "").replace(" ", "").replace("\u00a0", "")
    multiplier = 1.0
    match = re.search(r"([0-9]*\.?[0-9]+)\s*([kKmM])$", cleaned)
    if match:
        cleaned = match.group(1)
        if match.group(2).lower() == "k":
            multiplier = 1_000.0
        else:
            multiplier = 1_000_000.0
    else:
        match = re.search(r"([0-9]*\.?[0-9]+)", cleaned)
        if match:
            cleaned = match.group(1)
        else:
            return None
    value = _to_float(cleaned)
    return None if value is None else value * multiplier


def _parse_heatmap(chart: dict, spot: float | None) -> tuple[list[HeatmapCluster], float | None]:
    """
    Turn one rendered chart into a list of liquidation clusters.

    CoinGlass's heatmap is a tile/bubble chart whose colour encodes how much
    leverage sits at that price. The extractor:

      1. normalises every point's colour value against the hottest point, so
         intensity is comparable across instruments with different scales;
      2. keeps only the rows that clear ``MIN_CLUSTER_INTENSITY`` - a stray
         warm pixel is not a LARGE cluster;
      3. merges rows that are adjacent on the heatmap's own price GRID (the
         bin size is the smallest gap between any two plotted rows), so one
         dense band reads as one cluster instead of N;
      4. returns each band as one cluster at its mean price, carrying the
         strongest row's intensity.

    ``side`` is resolved by the caller against spot: a cluster BELOW spot
    liquidates longs on the way down, a cluster ABOVE spot liquidates shorts
    on the way up. Determining it here from the plot's own minimum would call
    every cluster a "long", which is wrong whenever spot is mid-range.
    """
    points = chart.get("points") or []
    if not points:
        return [], spot

    peak = None
    plotted: list[float] = []
    for p in points:
        value = _to_float(p.get("value"))
        if value is None:
            value = _to_float(p.get("y"))
        if value is None:
            continue
        p["value"] = value
        peak = value if peak is None else max(peak, value)
        price = _to_float(p.get("y"))
        if price is not None:
            plotted.append(price)

    if peak is None or peak <= 0:
        return [], spot

    scored: list[tuple[float, float]] = []   # (price, normalised 0..1)
    for p in points:
        price = _to_float(p.get("y"))
        value = _to_float(p.get("value"))
        if price is None or value is None:
            continue
        norm = value / peak
        if norm >= MIN_CLUSTER_INTENSITY:
            scored.append((price, norm))

    if not scored:
        return [], spot

    scored.sort(key=lambda item: item[0])
    bands: list[list[tuple[float, float]]] = [[scored[0]]]
    for price, norm in scored[1:]:
        last_price = bands[-1][-1][0]
        if (price - last_price) <= _merge_threshold(plotted):
            bands[-1].append((price, norm))
        else:
            bands.append([(price, norm)])

    # Side is left undecided here; _parse_extraction resolves it against spot.
    clusters = [
        HeatmapCluster(
            price_level=round(sum(p for p, _ in band) / len(band), 2),
            intensity=_intensity_bucket(max(n for _, n in band)),
            side="",
            intensity_score=round(max(n for _, n in band), 4),
        )
        for band in bands
    ]
    return clusters, spot


def _merge_threshold(plotted: list[float]) -> float:
    """
    Gap below which two hot rows count as ONE cluster.

    The bin size is the SMALLEST gap between any two plotted rows - the
    heatmap's own grid resolution. Two hot rows more than 1.5 bins apart are
    separated by a cold region, so they are different clusters. Using the
    smallest gap rather than the median matters: the median gap between rows
    that happened to be hot is the distance BETWEEN clusters, not the width of
    one, and merging on it collapses distinct clusters into a single band.
    """
    if len(plotted) < 2:
        return 0.0
    gaps = sorted(b - a for a, b in zip(plotted, plotted[1:]) if b > a)
    if not gaps:
        return 0.0
    return gaps[0] * 1.5


def _parse_cvd(chart: dict) -> CVDSeries | None:
    """Extract the CVD series, de-duplicated and time-ordered."""
    points = chart.get("points") or []
    if not points:
        return None
    series: list[tuple[float, float]] = []
    for p in points:
        x = _to_float(p.get("x"))
        y = _to_float(p.get("y"))
        if x is not None and y is not None:
            series.append((x, y))
    if len(series) < 3:
        return None
    series.sort(key=lambda item: item[0])
    return CVDSeries(points=series)


def _parse_price_series(charts: list[dict]) -> list[tuple[float, float]]:
    """Find the closest-to-a-price series among the rendered charts."""
    best: list[tuple[float, float]] = []
    best_score = -1
    for chart in charts:
        if chart.get("kind") != "price":
            continue
        points = chart.get("points") or []
        series = [(_to_float(p.get("x")), _to_float(p.get("y")))
                  for p in points]
        series = [(x, y) for x, y in series if x is not None and y is not None]
        if len(series) > len(best):
            best, best_score = series, len(series)
    best.sort(key=lambda item: item[0])
    return best


def _nearest(series: list[tuple[float, float]], x: float) -> float | None:
    """Value of ``series`` at (or nearest to) ``x`` by linear interpolation."""
    if not series:
        return None
    best = min(series, key=lambda item: abs(item[0] - x))
    if len(series) < 2:
        return best[1]
    # linear interpolation between the two bracketing points
    for (x0, y0), (x1, y1) in zip(series, series[1:]):
        if min(x0, x1) <= x <= max(x0, x1) and x1 != x0:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return best[1]


def detect_divergence(cvd: CVDSeries | None,
                      price: list[tuple[float, float]]) -> tuple[str, str, float]:
    """
    Price vs CVD trend, computed locally from the scraped series.

    Both series are split into a first and second half. When price and CVD
    move in OPPOSITE directions the reading is a divergence (CVD_BEARISH when
    price rose into falling CVD, CVD_BULLISH when price fell into rising CVD);
    when they agree it is CONFIRMED. A window shorter than four points per
    half cannot support a direction, so it reports NONE.

    Returns ``(divergence, note, slope)`` where ``slope`` is the normalised
    CVD change over the whole window.
    """
    if cvd is None or len(cvd.points) < 4 or len(price) < 4:
        return ("NONE",
                "insufficient series for divergence", 0.0)

    half = len(cvd.points) // 2
    cvd_a, cvd_b = cvd.points[0][1], cvd.points[half][1]
    cvd_change = cvd_b - cvd_a
    span = cvd.points[-1][0] - cvd.points[0][0]
    slope = (cvd_change / span) if span else 0.0
    # Normalise by the series magnitude so the slope is comparable across
    # instruments with very different CVD scales.
    mag = max(abs(v) for _, v in cvd.points) or 1.0
    rel = cvd_change / mag

    price_first = _nearest(price, cvd.points[0][0])
    price_second = _nearest(price, cvd.points[half][0])
    if price_first is None or price_second is None:
        return ("NONE", "no price series to compare CVD against", round(slope, 6))

    price_change = price_second - price_first
    if abs(price_change) < 1e-9 and abs(rel) < 1e-9:
        return ("CONFIRMED", "price and CVD both flat", 0.0)

    if price_change > 0 and rel < 0:
        return ("CVD_BEARISH",
                f"price +{price_change:.4f} while CVD fell "
                f"{rel * 100:.1f}% - bearish divergence", round(slope, 6))
    if price_change < 0 and rel > 0:
        return ("CVD_BULLISH",
                f"price {price_change:.4f} while CVD rose "
                f"{rel * 100:.1f}% - bullish divergence", round(slope, 6))
    return ("CONFIRMED",
            f"price and CVD moved together "
            f"(price {price_change:+.4f}, CVD {rel * 100:+.1f}%)", round(slope, 6))


def _parse_footprint(chart: dict) -> FootprintData | None:
    """
    Read a footprint / cluster chart into buy-vs-sell imbalance.

    CoinGlass colour footprint bars by aggressive side. The extractor buckets
    positive and negative deltas per price so a single imbalance percentage
    can be derived, and reports BUY_IMBALANCE / SELL_IMBALANCE / NONE.
    """
    points = chart.get("points") or []
    if not points:
        return None
    buy = sell = 0.0
    rows: list[dict] = []
    for p in points:
        y = _to_float(p.get("y"))
        if y is None:
            continue
        delta = _to_float(p.get("value"))
        if delta is None:
            delta = _to_float(p.get("y"))
        if delta is None:
            continue
        if delta >= 0:
            buy += delta
        else:
            sell += -delta
        rows.append({"price": round(y, 2), "delta": round(delta, 4)})
    if not rows:
        return None
    total = buy + sell
    pct = ((buy - sell) / total * 100.0) if total else 0.0
    if abs(pct) < 10:
        imbalance = "NONE"
    elif pct > 0:
        imbalance = "BUY_IMBALANCE"
    else:
        imbalance = "SELL_IMBALANCE"
    return FootprintData(rows=rows, imbalance=imbalance,
                         imbalance_pct=round(pct, 2))


def _pick_spot(dom_prices: list[float] | None,
               charts: list[dict],
               clusters: list[HeatmapCluster]) -> float | None:
    """
    Best estimate of the current price, most trustworthy source first.

    1. the MODE of the price-shaped strings on the page - CoinGlass repeats
       the current price in several places (header, tooltip, axis), so the
       value that recurs is almost always the live one rather than a random
       figure of speech in a footnote;
    2. the last value of a rendered price/close series;
    3. the MEDIAN plotted heatmap row - CoinGlass draws these charts centred
       on the current price, so the middle of the plotted band is a far better
       estimate than its top;
    4. the highest plotted heatmap row.
    """
    if dom_prices:
        counts: dict[float, int] = {}
        for value in dom_prices:
            counts[value] = counts.get(value, 0) + 1
        # Highest count wins; ties broken toward the higher price, so a
        # two-way tie between a header price and an axis figure favours the
        # more prominent one rather than the coin flip.
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    price_series = _parse_price_series(charts)
    if price_series:
        return price_series[-1][1]

    if clusters:
        prices = sorted(c.price_level for c in clusters)
        return prices[len(prices) // 2]
    return None


def _resolve_sides(clusters: list[HeatmapCluster],
                   spot: float | None) -> list[HeatmapCluster]:
    """
    Set each cluster's ``side`` from spot: below -> longs, above -> shorts.

    With no usable spot the side is left blank rather than guessed, so a
    snapshot that cannot place itself still reports a price level and an
    intensity without inventing a direction.
    """
    if spot is None:
        return clusters
    for cluster in clusters:
        cluster.side = "long" if cluster.price_level < spot else "short"
    return clusters


def _calibrate_price_axis(texts: list[dict]) -> list[tuple[float, float]]:
    """
    Build (pixel_y, price) calibration points from the price-axis labels.

    The price axis is identifiable because its labels are numeric, all share
    one x column (the right-hand scale), and each has a DISTINCT y. The most
    common x among numeric labels is that column; noise elsewhere (the legend's
    "0" / "48.88M", or stray numbers) is dropped because it shares neither.

    Every axis label is kept - including the non-round min/max labels at the
    chart's edges, which are the actual data-range endpoints rather than
    gridlines, and which extend the calibration to the full plot.
    """
    by_x: dict[int, list[tuple[float, float]]] = {}
    for t in texts:
        value = _parse_axis_number(t.get("s", ""))
        if value is None or value <= 0:
            continue
        x = int(t.get("x", -1))
        by_x.setdefault(x, []).append((float(t.get("y", 0)), value))

    if not by_x:
        return []

    # The column carrying the most numeric labels is the price axis.
    axis_x = max(by_x.items(), key=lambda kv: len(kv[1]))[0]
    column = by_x[axis_x]

    # De-duplicate: the same label is drawn several times per frame.
    unique: dict[float, list[float]] = {}
    for y, value in column:
        unique.setdefault(round(y, 1), []).append(value)
    points = [(y, round(sum(v) / len(v), 4)) for y, v in unique.items()]
    points.sort(key=lambda item: item[0])
    return points


def _price_at(axis: list[tuple[float, float]], pixel_y: float) -> float | None:
    """Linear interpolation of pixel_y against the calibrated price axis."""
    if len(axis) < 2:
        return None
    for (y0, p0), (y1, p1) in zip(axis, axis[1:]):
        if y0 <= pixel_y <= y1 or y1 <= pixel_y <= y0:
            if y1 == y0:
                return p0
            return p0 + (p1 - p0) * (pixel_y - y0) / (y1 - y0)
    # Outside the calibrated span: extrapolate on the end segment's slope so a
    # hot row just past the top label still resolves to a price.
    (y0, p0), (y1, p1) = (axis[0], axis[1]) if axis[0][0] < axis[-1][0] \
        else (axis[-1], axis[-2])
    slope = (p1 - p0) / (y1 - y0) if y1 != y0 else 0.0
    return p0 + slope * (pixel_y - y0)


def _fetch_spot_price(asset: str) -> float | None:
    """
    Current price for one symbol, from the project's existing price feeds.

    The heatmap page does not publish the spot in the DOM, and its axis labels
    are the data RANGE (79172..87572 for BTC), not the current price. Rather
    than invent one, the scraper borrows the price the project already fetches
    for confirmation: TradingView first (covers XAUUSD/XAGUSD/BTCUSD), Coinbase
    as the BTC fallback. Returns None when neither is reachable, and the
    clusters are then reported without a below/above resolution.
    """
    symbol = asset_symbol(asset)
    # TradingView pair + exchange. The metals quote on OANDA; BTC's liquid
    # pair is Binance's BTCUSDT, and the broker-side symbol differs from the
    # engine's BTCUSD, so the mapping must be explicit rather than derived.
    tv_pairs = {
        "BTCUSD": ("BINANCE", "BTCUSDT"),
        "XAUUSD": ("OANDA", "XAUUSD"),
        "XAGUSD": ("OANDA", "XAGUSD"),
    }
    exchange, tv_symbol = tv_pairs.get(symbol, ("OANDA", symbol))
    try:
        import logging as _logging

        # tvDatafeed logs its own connection errors at ERROR level, which is
        # noise here: a missing spot only costs the below/above resolution.
        _tv_logger = _logging.getLogger("tvDatafeed")
        _previous = _tv_logger.level
        _tv_logger.setLevel(_logging.CRITICAL)
        try:
            from tvDatafeed import Interval, TvDatafeed

            tv = TvDatafeed()
            df = tv.get_hist(symbol=tv_symbol, exchange=exchange,
                             interval=Interval.in_1_minute, n_bars=2)
            if df is not None and len(df):
                return float(df["close"].iloc[-1])
        finally:
            _tv_logger.setLevel(_previous)
    except Exception as exc:  # noqa: BLE001 - price is optional context
        log.debug("coinglass spot via tvDatafeed failed for %s: %s", symbol, exc)

    try:
        import httpx
        from app.models.enums import Asset

        product = {Asset.BITCOIN: "BTC-USD"}.get(Asset(symbol))
        if product:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    f"https://api.coinbase.com/v2/prices/{product}/spot")
                if resp.status_code == 200:
                    return float(resp.json()["data"]["amount"])
    except Exception as exc:  # noqa: BLE001
        log.debug("coinglass spot via coinbase failed for %s: %s", symbol, exc)

    return None


def _parse_extraction(asset: str, data: dict, source_url: str) -> CoinGlassSnapshot:
    """
    Turn one raw page extraction into a ``CoinGlassSnapshot``.

    Dispatches on which strategy produced data: a canvas-rendered page returns
    ``results`` (one entry per canvas), a Highcharts page returns ``charts``.
    The two are never mixed, because their coordinates mean different things.
    """
    results = data.get("results") or []
    if results:
        return _parse_multi_canvas(asset, results, source_url)
    return _parse_dom_extraction(asset, data, source_url)


def _parse_multi_canvas(asset: str, results: list[dict],
                        source_url: str) -> CoinGlassSnapshot:
    """
    Parse a page whose chart is drawn across several canvases.

    A CoinGlass "Legend" chart can be laid out as several instruments in a split
    screen, each pane its own canvas, so one shared link can carry gold, silver
    and BTC at once. Each pane is parsed independently and the best match wins.

    TWO filters are applied, in order:

      1. PLAUSIBILITY - the pane's calibrated price axis must be able to belong
         to the requested asset. Gold's ~4.2k axis is not silver's ~50 axis, so
         this alone separates the instruments reliably.
      2. IS-IT-A-HEATMAP - among the plausible panes, prefer the one actually
         drawing a heat field. The same instrument can appear twice (a price
         chart and a liquidation heatmap), and both share an axis, so the axis
         filter cannot tell them apart. A heatmap pane has many palette rows; a
         line chart has few or none.

    Filter 2 matters: without it a silver heatmap request could be answered by a
    silver CVD pane, which has no liquidation clusters at all.
    """
    if len(results) == 1:
        return _parse_canvas_extraction(asset, results[0], source_url)

    scored: list[tuple[int, bool, int, int, CoinGlassSnapshot]] = []
    for index, result in enumerate(results):
        axis = _calibrate_price_axis(result.get("texts") or [])
        plausible = _plausible_for_asset(asset, _axis_range(axis))
        # Palette rows = an actual heat field was drawn in this pane.
        heat_rows = len([r for r in (result.get("rows") or [])
                         if float(r.get("intensity", 0)) > 0])
        snapshot = _parse_canvas_extraction(asset, result, source_url)
        scored.append((index, plausible, heat_rows, len(axis), snapshot))

    # Plausible panes only, then the most heat, then the best calibration.
    candidates = [c for c in scored if c[1]]
    if not candidates:
        log.warning(
            "CoinGlass %s: no pane's price axis is plausible for this asset "
            "(saw %s) - treating the poll as FAILED rather than reading the "
            "wrong instrument's chart",
            asset, [c[3] and _axis_range(
                _calibrate_price_axis(r.get("texts") or []))
                for c, r in zip(scored, results)])
        return CoinGlassSnapshot(
            asset=asset, source_url=source_url,
            spot_price=scored[0][4].spot_price if scored else None,
            fetched_at=datetime.now(timezone.utc).isoformat(), degraded=True)

    best = max(candidates, key=lambda c: (c[2], c[3]))
    log.info("CoinGlass %s: matched canvas #%d of %d panes "
             "(axis points=%d, heat rows=%d)",
             asset, best[0], len(results), best[3], best[2])
    return best[4]


def _parse_canvas_extraction(asset: str, data: dict,
                             source_url: str) -> CoinGlassSnapshot:
    """
    Parser for canvas-rendered pages (CoinGlass's public layout).

    Two inputs, both produced by the instrumented render:

      * ``texts``  - every ``fillText`` the chart made, with canvas
        coordinates. The numeric ones sharing a column ARE the price axis, so
        the calibration is read off them exactly rather than guessed.
      * ``rows``   - per pixel row of the plot area, the hottest palette colour
        found and where. The palette itself came from the page's own colour
        legend, so intensity needs no hard-coded colour table.

    Hot rows are grouped into clusters with the same banding logic the Highcharts
    path uses, and the price for each row comes from linear interpolation along
    the calibrated axis.
    """
    rows = data.get("rows") or []
    axis = _calibrate_price_axis(data.get("texts") or [])
    spot = _fetch_spot_price(asset)

    # The page's own price axis must be able to belong to the requested asset.
    # Without this a BTC page scraped for GOLD would be stored, scored and
    # displayed as gold data, which is worse than having none.
    axis_range = _axis_range(axis)
    if not _plausible_for_asset(asset, axis_range):
        log.warning(
            "CoinGlass %s: price axis %s is not plausible for this asset "
            "(range %s) - the page did not switch symbols; treating the poll "
            "as FAILED rather than storing another instrument's chart",
            asset, axis_range,
            _ASSET_PRICE_RANGES.get(asset_symbol(asset)))
        return CoinGlassSnapshot(
            asset=asset, source_url=source_url, spot_price=spot,
            fetched_at=datetime.now(timezone.utc).isoformat(), degraded=True)

    if not rows or len(axis) < 2:
        return CoinGlassSnapshot(
            asset=asset, source_url=source_url, spot_price=spot,
            fetched_at=datetime.now(timezone.utc).isoformat(), degraded=True)

    # The legend already gives an ABSOLUTE 0..1 intensity, so the "is this a
    # LARGE cluster" floor is applied to that raw value. Normalising first
    # would make an all-cold chart look fully hot, because the coldest row
    # would become the peak.
    hot = [row for row in rows
           if float(row.get("intensity", 0)) >= MIN_CLUSTER_INTENSITY]
    peak = max((float(r.get("intensity", 0)) for r in hot), default=0.0)

    # Group by pixel gap using the original per-row y, kept alongside price.
    bands: list[list[tuple[float, float]]] = []
    pending: list[tuple[float, float]] = []
    last_y: float | None = None
    for row in sorted(hot, key=lambda r: float(r["y"])):
        y = float(row["y"])
        price = _price_at(axis, y)
        if price is None:
            continue
        norm = float(row["intensity"]) / peak if peak > 0 else 0.0
        if last_y is None or (y - last_y) <= _ROW_MERGE_PIXELS:
            pending.append((price, norm))
        else:
            bands.append(pending)
            pending = [(price, norm)]
        last_y = y
    if pending:
        bands.append(pending)

    clusters = [
        HeatmapCluster(
            price_level=round(sum(p for p, _ in band) / len(band), 2),
            intensity=_intensity_bucket(max(n for _, n in band)),
            side="",
            intensity_score=round(max(n for _, n in band), 4),
        )
        for band in bands
    ]
    clusters = _resolve_sides(clusters, spot)

    below = above = None
    if spot is not None and clusters:
        lower = [c for c in clusters if c.price_level < spot]
        upper = [c for c in clusters if c.price_level >= spot]
        below = max(lower, key=lambda c: c.price_level) if lower else None
        above = min(upper, key=lambda c: c.price_level) if upper else None

    return CoinGlassSnapshot(
        asset=asset,
        spot_price=(round(spot, 2) if spot is not None else None),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        heatmap_clusters=clusters,
        cluster_below=below,
        cluster_above=above,
        caution_below=bool(below and spot
                           and (spot - below.price_level) / spot <= LIQ_CAUTION_PCT),
        caution_above=bool(above and spot
                           and (above.price_level - spot) / spot <= LIQ_CAUTION_PCT),
        source_url=source_url,
        degraded=not clusters,
    )


def _parse_dom_extraction(asset: str, data: dict,
                          source_url: str) -> CoinGlassSnapshot:
    """Parser for Highcharts / SVG rendered pages."""
    charts = data.get("charts") or []
    # A chart may carry several series; keep the most populated series per
    # kind so a legend row and its data series do not both count.
    by_kind: dict[str, dict] = {}
    for chart in charts:
        kind = chart.get("kind") or "other"
        current = by_kind.get(kind)
        if current is None or len(chart.get("points") or []) > len(
                current.get("points") or []):
            by_kind[kind] = chart

    heatmap_clusters, _ = ([], None)
    if "heatmap" in by_kind:
        heatmap_clusters, _ = _parse_heatmap(by_kind["heatmap"], None)

    cvd = _parse_cvd(by_kind["cvd"]) if "cvd" in by_kind else None
    footprint = (_parse_footprint(by_kind["footprint"])
                 if "footprint" in by_kind else None)

    dom_prices = [p for p in (_parse_axis_number(t)
                              for t in (data.get("price_texts") or []))
                  if p is not None and p > 0]
    spot = _pick_spot(dom_prices, charts, heatmap_clusters)
    heatmap_clusters = _resolve_sides(heatmap_clusters, spot)

    below = above = None
    if spot is not None and heatmap_clusters:
        lower = [c for c in heatmap_clusters if c.price_level < spot]
        upper = [c for c in heatmap_clusters if c.price_level >= spot]
        below = max(lower, key=lambda c: c.price_level) if lower else None
        above = min(upper, key=lambda c: c.price_level) if upper else None

    caution_below = bool(
        below and spot and (spot - below.price_level) / spot <= LIQ_CAUTION_PCT)
    caution_above = bool(
        above and spot and (above.price_level - spot) / spot <= LIQ_CAUTION_PCT)

    divergence, note, slope = detect_divergence(cvd, _parse_price_series(charts))
    if cvd is not None:
        cvd.divergence = divergence
        cvd.divergence_note = note
        cvd.slope = slope

    snapshot = CoinGlassSnapshot(
        asset=asset,
        spot_price=(round(spot, 2) if spot is not None else None),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        heatmap_clusters=heatmap_clusters,
        cluster_below=below,
        cluster_above=above,
        caution_below=caution_below,
        caution_above=caution_above,
        cvd=cvd,
        footprint=footprint,
        source_url=source_url,
        degraded=not (heatmap_clusters or cvd or footprint),
    )
    return snapshot


# ---------------------------------------------------------------------------
# Symbol selection helper (best-effort, never fatal)
# ---------------------------------------------------------------------------
async def _select_symbol(page, asset: str) -> None:
    """
    Best-effort symbol selection on CoinGlass's public heatmap page.

    STATUS: sets the symbol but does NOT reliably redraw the chart.

    Verified against the live page (2026-10): the public heatmap page opens on
    BTC with no URL parameter that changes the instrument, and its API payload
    is per-symbol encrypted, so the request cannot be replayed either. Driving
    the MUI Autocomplete combobox (focus -> clear -> type "XAU" -> click
    "Binance XAU/USDT Perpetual") DOES update the combobox value and the page
    title, and it DOES trigger a fresh API call for the new symbol - but the
    chart component never re-renders, so the canvas still draws BTC.

    The call is kept because it is cheap, harmless, and would start working the
    day CoinGlass fixes the redraw. It is NOT relied upon: the plausibility
    guard in ``_parse_canvas_extraction`` is what actually protects the caller,
    by rejecting any reading whose price axis cannot belong to the requested
    asset. A wrong-instrument chart therefore fails the poll rather than being
    scored as another asset's data.
    """
    search_map = {
        "BTCUSD": "BTC",
        "XAUUSD": "XAU",
        "XAGUSD": "XAG",
    }
    target = search_map.get(asset)
    if not target:
        return  # unknown asset, skip

    try:
        combobox = page.locator('input[role="combobox"]').first
        if await combobox.count() == 0:
            return

        await combobox.click(timeout=5000)
        await combobox.fill("")
        await combobox.type(target, delay=100)
        await page.wait_for_timeout(1500)

        options = page.locator('.MuiAutocomplete-listbox [role="option"]')
        if await options.count() == 0:
            return
        # Pick the first option naming the target (e.g. Binance XAU/USDT).
        for i in range(min(await options.count(), 30)):
            opt = options.nth(i)
            try:
                text = await opt.text_content()
            except Exception:
                continue
            if text and target.upper() in text.upper():
                await opt.click(timeout=5000)
                break

        # Do NOT wait for a redraw: none comes, and a long sleep only delays
        # the poll. A short settle is enough for the API call to be issued.
        await page.wait_for_timeout(1500)
    except Exception as exc:  # noqa: BLE001 - never fatal
        log.debug("coinglass symbol select failed for %s: %s", target, exc)


# ---------------------------------------------------------------------------
# The render
# ---------------------------------------------------------------------------
async def _render_chart(url: str, asset: str) -> dict:
    """
    One PLAIN-Chromium render of a CoinGlass chart page.

    Serialised against every other browser render in this process through the
    shared Chromium slot, so the scraper shares the same rendering budget as
    the news and calendar providers instead of competing with it.

    No evasion args: the public chart pages render without them, and this
    module refuses to solve a challenge. A block page simply comes back and is
    treated as a failed poll by the caller.

    When ``coinglass_profile_dir`` is set, the render reuses a PERSISTENT
    browser profile instead of a throwaway context, so a session saved by the
    one-time manual login (tools/coinglass_login.py) is sent along with the
    request. That is the only way to reach the "Legend" charts, which redirect
    anonymous visitors to a login page.
    """
    from playwright.async_api import async_playwright

    async with chromium_slot(wait_seconds=settings.chromium_lock_wait_seconds) as ok:
        if not ok:
            raise BrowserBusy("chromium busy; coinglass render deferred")

        async with async_playwright() as pw:
            profile_dir = (settings.coinglass_profile_dir or "").strip()
            ctx = None
            browser = None
            try:
                if profile_dir:
                    # Authenticated path: a real profile carries the session.
                    # user_agent MUST be passed explicitly here: a persistent
                    # context otherwise sends Playwright's default
                    # "HeadlessChrome" UA, which nginx answers with a bare 404
                    # (verified live - the same request with a normal Chrome UA
                    # returns the real page). This is why the metals failed
                    # while BTC, whose path does set the UA, worked.
                    ctx = await pw.chromium.launch_persistent_context(
                        profile_dir,
                        headless=settings.coinglass_headless,
                        user_agent=settings.coinglass_user_agent,
                        viewport={"width": 1440, "height": 1600},
                        locale="en-US",
                        timezone_id="UTC",
                    )
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                else:
                    # Anonymous path: a throwaway context, nothing persisted.
                    browser = await pw.chromium.launch(
                        headless=settings.coinglass_headless)
                    ctx = await browser.new_context(
                        user_agent=settings.coinglass_user_agent,
                        viewport={"width": 1440, "height": 1600},
                        locale="en-US",
                        timezone_id="UTC",
                    )
                    page = await ctx.new_page()

                # Installed BEFORE any page script runs, so the wrappers are
                # already in place by the time the chart library draws. This is
                # what makes a canvas chart readable: the axis labels arrive as
                # real strings in fillText, and the heat cells as real colours.
                try:
                    await page.add_init_script(_INSTRUMENT_JS)
                except Exception as exc:  # noqa: BLE001 - never fatal
                    log.debug("coinglass init script not installed: %s", exc)

                async def _block_heavy(route) -> None:
                    """
                    Drop the page's fonts and video - pure weight on a chart.

                    An ASYNC handler that swallows its own failures: the route
                    callbacks can fire during teardown, when the context they
                    belong to is already closing. Awaiting inside the handler
                    and catching there keeps those teardown errors from
                    surfacing as an unretrieved asyncio exception, which is
                    exactly what a fire-and-forget ensure_future() would do.
                    """
                    try:
                        if route.request.resource_type in ("media", "font"):
                            await route.abort()
                        else:
                            await route.continue_()
                    except Exception:  # noqa: BLE001 - teardown races are noise
                        pass

                try:
                    await page.route("**/*", _block_heavy)
                except Exception:  # noqa: BLE001 - routing is best-effort
                    pass

                await page.goto(url, timeout=45000,
                                wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:  # noqa: BLE001 - a busy page may never idle
                    pass

                # Best-effort symbol switching: if the URL matches the public
                # heatmap page and we have a specific symbol for this asset,
                # try to select it. Fails silently - the plausibility guard
                # will catch any mismatch between requested asset and rendered data.
                try:
                    await _select_symbol(page, asset)
                except Exception as exc:  # noqa: BLE001 - never fatal
                    log.debug("coinglass symbol select failed: %s", exc)

                # Wait on a CONDITION, not a fixed sleep: the chart exists only
                # once the app has hydrated and the canvas has actually drawn.
                try:
                    await page.wait_for_function(
                        """() => {
                            const HC = window.Highcharts;
                            const drawn = (HC && HC.charts ? HC.charts : [])
                                .filter(c => c && c.series && c.series.length)
                                .length;
                            const texts = window.__cgDrawLog
                                ? window.__cgDrawLog.texts.length : 0;
                            return drawn > 0 || texts > 0;
                        }""",
                        timeout=25000)
                except Exception:  # noqa: BLE001 - fall back to DOM text
                    pass

                # Small settle for the animation the chart runs on first draw.
                # A chart that never animates still yields data.
                await page.wait_for_timeout(2500)

                # Canvas first: it is what CoinGlass's public pages render.
                # A "Legend" chart holds several instruments as separate
                # canvases, so every canvas is analysed and the parser matches
                # the one whose price axis belongs to the requested asset.
                try:
                    canvas_data = await page.evaluate(_READ_CANVAS_JS)
                except Exception as exc:  # noqa: BLE001
                    log.debug("coinglass canvas read failed: %s", exc)
                    canvas_data = {}

                if canvas_data and (canvas_data.get("results")
                                    or canvas_data.get("texts")):
                    try:
                        canvas_data["title"] = await page.title()
                    except Exception:  # noqa: BLE001
                        canvas_data["title"] = ""
                    try:
                        canvas_data["text"] = await page.evaluate(
                            "() => document.body ? "
                            "document.body.innerText.slice(0, 2000) : ''")
                    except Exception:  # noqa: BLE001
                        canvas_data["text"] = ""
                    return canvas_data

                # No canvas content: fall back to the Highcharts/SVG path.
                return await page.evaluate(_EXTRACT_JS)
            finally:
                # For a persistent context, closing the context IS what
                # flushes the session back to the profile directory, so it must
                # not be skipped. For an anonymous context it is just cleanup.
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass


class BrowserBusy(RuntimeError):
    """
    Raised when the shared Chromium slot could not be taken in time.

    A DEFERRED poll, not a failure: the caller returns and the next poll
    renders normally.
    """


def _snapshot_to_dict(snapshot: CoinGlassSnapshot) -> dict:
    """Serialise a snapshot for the disk cache (JSON-safe)."""
    return {
        "asset": snapshot.asset,
        "spot_price": snapshot.spot_price,
        "fetched_at": snapshot.fetched_at,
        "degraded": snapshot.degraded,
        "cluster_below": (snapshot.cluster_below.to_dict()
                          if snapshot.cluster_below else None),
        "cluster_above": (snapshot.cluster_above.to_dict()
                          if snapshot.cluster_above else None),
        "caution_below": snapshot.caution_below,
        "caution_above": snapshot.caution_above,
        "clusters": [c.to_dict() for c in snapshot.heatmap_clusters],
        "cvd_divergence": snapshot.cvd.divergence if snapshot.cvd else None,
        "cvd_note": snapshot.cvd.divergence_note if snapshot.cvd else "",
        "cvd_points": len(snapshot.cvd.points) if snapshot.cvd else 0,
        "footprint_imbalance": (snapshot.footprint.imbalance
                                if snapshot.footprint else None),
        "footprint_imbalance_pct": (snapshot.footprint.imbalance_pct
                                    if snapshot.footprint else 0.0),
        "source_url": snapshot.source_url,
    }


def _snapshot_from_dict(payload: dict) -> CoinGlassSnapshot:
    """Rebuild a ``CoinGlassSnapshot`` from the disk cache.

    The cache intentionally stores the DECISION fields (cluster, divergence,
    imbalance), not the full point series: those are what the scorer and the
    dashboard consume, they are what keeps the payload small, and a cached
    reading is a snapshot of a decision that was already made rather than a
    re-computation of it.
    """
    below = payload.get("cluster_below")
    above = payload.get("cluster_above")
    cvd_points = int(payload.get("cvd_points") or 0)
    cvd = None
    if payload.get("cvd_divergence"):
        cvd = CVDSeries(points=[], divergence=payload["cvd_divergence"],
                        divergence_note=payload.get("cvd_note", ""))
        # Preserve the count so a cached read reports the same breadth.
        cvd.points = [(0.0, 0.0)] * min(cvd_points, 10_000)
    footprint = None
    if payload.get("footprint_imbalance"):
        footprint = FootprintData(rows=[], imbalance=payload["footprint_imbalance"],
                                  imbalance_pct=payload.get(
                                      "footprint_imbalance_pct", 0.0))
    return CoinGlassSnapshot(
        asset=payload.get("asset", ""),
        spot_price=payload.get("spot_price"),
        fetched_at=payload.get("fetched_at", ""),
        heatmap_clusters=[],
        cluster_below=(HeatmapCluster(**below) if below else None),
        cluster_above=(HeatmapCluster(**above) if above else None),
        caution_below=bool(payload.get("caution_below")),
        caution_above=bool(payload.get("caution_above")),
        cvd=cvd,
        footprint=footprint,
        source_url=payload.get("source_url", ""),
        degraded=bool(payload.get("degraded", True)),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def asset_url(asset: str) -> str:
    """Configured CoinGlass chart URL for one asset symbol."""
    key = (asset or "").upper()
    if key.startswith("XAU") or key == "GOLD":
        return settings.coinglass_gold_url
    if key.startswith("XAG") or key == "SILVER":
        return settings.coinglass_silver_url
    return settings.coinglass_btc_url


def asset_symbol(asset) -> str:
    """Normalise an ``Asset`` enum / string to the config key form."""
    value = getattr(asset, "value", asset)
    return str(value or "").upper()


async def scrape_asset_async(asset: str) -> CoinGlassSnapshot | None:
    """
    Scrape one asset, honouring the cache and the backoff. Never raises.

    Returns ``None`` when the poll is skipped (disabled, rate-limited,
    backed off) or failed - the caller then degrades to news + price only.
    """
    asset = asset_symbol(asset)
    if not settings.coinglass_enabled:
        return None

    # 1. Fresh cache wins: no browser is opened at all.
    cached = _get_cache().get(asset)
    if cached is not None:
        return _snapshot_from_dict(cached)

    # 2. Backoff after repeated failures / blocks.
    if _backoff_active(asset):
        log.debug("CoinGlass %s: backed off after repeated failures", asset)
        return None

    # 3. In-process spacing: two callers in the same tick cannot both render.
    last = _last_fetch_attempt.get(asset, 0.0)
    if last and (time.time() - last) < MIN_FETCH_SPACING_SECONDS:
        log.debug("CoinGlass %s: last fetch %.0fs ago - skipping", asset,
                  time.time() - last)
        return None

    url = asset_url(asset)
    started = time.time()
    try:
        data = await asyncio.wait_for(
            _render_chart(url, asset),
            timeout=int(settings.coinglass_browser_timeout_seconds))
    except BrowserBusy as exc:
        # Deferred, not failed: another render holds the slot. Deliberately
        # NOT counted as a failure, because nothing is wrong with the source.
        log.info("CoinGlass %s: %s", asset, exc)
        return None
    except asyncio.TimeoutError:
        _record_failure(asset, f"timeout after "
                               f"{settings.coinglass_browser_timeout_seconds}s")
        return None
    except Exception as exc:  # noqa: BLE001 - a provider never breaks a poll
        _record_failure(asset, f"{type(exc).__name__}: {exc}")
        return None

    if _looks_like_challenge(f"{data.get('title', '')} {data.get('text', '')} "
                             f"{data.get('heading', '')}"):
        _record_failure(asset, "challenge/block page detected")
        return None

    snapshot = _parse_extraction(asset, data, url)
    elapsed = time.time() - started

    if snapshot.degraded:
        # The page rendered but nothing we recognise was drawn. That is a
        # failed poll, not an empty market: cache nothing and let the next
        # poll try again.
        _record_failure(asset, "page rendered but no chart data extracted")
        return None

    _record_success(asset)
    _get_cache().put(asset, _snapshot_to_dict(snapshot))
    log.info("CoinGlass %s: %s spot=%s clusters(below/above)=%s/%s cvd=%s "
             "footprint=%s in %.1fs",
             asset, "extracted", snapshot.spot_price,
             (snapshot.cluster_below.price_level
              if snapshot.cluster_below else "-"),
             (snapshot.cluster_above.price_level
              if snapshot.cluster_above else "-"),
             (snapshot.cvd.divergence if snapshot.cvd else "-"),
             (snapshot.footprint.imbalance if snapshot.footprint else "-"),
             elapsed)
    return snapshot


def scrape_asset(asset: str) -> CoinGlassSnapshot | None:
    """Blocking wrapper around :func:`scrape_asset_async` for sync callers."""
    try:
        return asyncio.run(scrape_asset_async(asset))
    except Exception as exc:  # noqa: BLE001 - never let a scrape break a tick
        log.warning("CoinGlass scrape failed for %s: %s", asset, exc)
        return None


def configured_assets() -> list[str]:
    """
    The assets CoinGlass is actually scraped for, from ``coinglass_assets``.

    Only BTC is readable (the public heatmap page needs no key or login), so
    that is the default. An asset that cannot be read must NOT be listed: it
    would cost a wasted browser render every poll, and repeated failed renders
    against a login wall look like bot traffic. Metals therefore fall back to
    news + price confirmation instead.
    """
    raw = (settings.coinglass_assets or "").strip()
    names = [asset_symbol(n.strip()) for n in raw.split(",") if n.strip()]
    return names or ["BTCUSD"]


async def scrape_all_async(assets=None) -> dict[str, CoinGlassSnapshot]:
    """Scrape the configured assets, returning only the successful ones."""
    names = [asset_symbol(a) for a in (assets or configured_assets())]
    out: dict[str, CoinGlassSnapshot] = {}
    for name in names:
        snapshot = await scrape_asset_async(name)
        if snapshot is not None and not snapshot.degraded:
            out[name] = snapshot
    return out


def scrape_all(assets=None) -> dict[str, CoinGlassSnapshot]:
    """Blocking wrapper around :func:`scrape_all_async`."""
    try:
        return asyncio.run(scrape_all_async(assets))
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGlass scrape_all failed: %s", exc)
        return {}


def cached_snapshot(asset: str) -> CoinGlassSnapshot | None:
    """Last good snapshot for ``asset`` from cache, ignoring TTL freshness.

    Used by the dashboard/API so a stale read is still a read, while
    ``scrape_asset`` decides when to spend a fresh fetch on it.
    """
    entry = _get_cache()._entries.get(asset_symbol(asset))
    if not entry:
        return None
    return _snapshot_from_dict(entry.get("payload") or {})


def coinglass_signals_to_weighted(
        snapshots: dict[str, CoinGlassSnapshot]) -> list:
    """
    Convert scraped snapshots into ``WeightedSignal`` objects.

    This is what carries a scraped reading into the bullish/bearish badge: the
    same signal shape the news feed produces, scored by the same
    ``sentiment_engine.compute`` weight path, so there is exactly one place
    where a direction is decided.

    Mapping:
        CVD_BEARISH     -> BEARISH signal on the asset, relevance 0.85
        CVD_BULLISH     -> BULLISH  signal on the asset, relevance 0.85
        footprint SELL  -> BEARISH signal (aggressive sellers in control)
        footprint BUY   -> BULLISH  signal
    A liquidation cluster is NOT a directional signal here: it is a caution
    flag, surfaced through ``caution_note()`` and the event-risk note, because
    a cluster above spot does not mean "bearish" - it only says a short
    squeeze is nearby.
    """
    from app.analysis.sentiment_engine import WeightedSignal
    from app.models.enums import (
        Asset,
        Direction,
        EconomicImportance,
        ImpactStrength,
        NewsCategory,
        ReportKind,
    )

    now = datetime.now(timezone.utc)
    out: list[WeightedSignal] = []

    def _asset_enum(symbol: str) -> Asset | None:
        try:
            return Asset(symbol)
        except ValueError:
            return None

    for symbol, snapshot in snapshots.items():
        asset = _asset_enum(symbol)
        if asset is None:
            continue

        if snapshot.cvd is not None and snapshot.cvd.divergence in (
                "CVD_BULLISH", "CVD_BEARISH"):
            direction = (Direction.BULLISH
                         if snapshot.cvd.divergence == "CVD_BULLISH"
                         else Direction.BEARISH)
            out.append(WeightedSignal(
                asset=asset,
                title=(f"CoinGlass CVD {snapshot.cvd.divergence} "
                       f"({symbol})"),
                direction=direction,
                # CVD is a first-class intraday flow signal: weight 0.7 is the
                # documented floor, expressed here as strength+relevance that
                # clears it through the normal weight path.
                strength=ImpactStrength.HIGH,
                importance=EconomicImportance.HIGH,
                published_at=now,
                relevance=0.85,
                persistence=0.70,
                source_quality="TIER_3_CRYPTO",
                cluster_id=f"coinglass_cvd_{symbol}",
                corroboration=1.0,
                category=NewsCategory.CVD,
                report_kind=ReportKind.ACTUAL,
                mode="coinglass",
                reason=snapshot.cvd.divergence_note or "CVD divergence read "
                                                     "from CoinGlass",
                transmission="CVD",
            ))

        if snapshot.footprint is not None and snapshot.footprint.imbalance in (
                "BUY_IMBALANCE", "SELL_IMBALANCE"):
            direction = (Direction.BULLISH
                         if snapshot.footprint.imbalance == "BUY_IMBALANCE"
                         else Direction.BEARISH)
            out.append(WeightedSignal(
                asset=asset,
                title=(f"CoinGlass Footprint {snapshot.footprint.imbalance} "
                       f"({symbol})"),
                direction=direction,
                strength=ImpactStrength.MEDIUM,
                importance=EconomicImportance.MEDIUM,
                published_at=now,
                relevance=0.65,
                persistence=0.50,
                source_quality="TIER_3_CRYPTO",
                cluster_id=f"coinglass_footprint_{symbol}",
                corroboration=1.0,
                category=NewsCategory.FOOTPRINT,
                report_kind=ReportKind.ACTUAL,
                mode="coinglass",
                reason=(f"footprint delta imbalance "
                        f"{snapshot.footprint.imbalance_pct:+.1f}%"),
                transmission="FOOTPRINT",
            ))

    return out


def caution_flags(snapshots: dict[str, CoinGlassSnapshot]) -> list[tuple[str, str]]:
    """(asset, note) pairs for every liquidation cluster sitting near spot."""
    out = []
    for symbol, snapshot in snapshots.items():
        note = snapshot.caution_note()
        if note:
            out.append((symbol, note))
    return out
